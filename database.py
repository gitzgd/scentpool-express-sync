from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from xlsx_importer import XlsxImportError, read_products


DEFAULT_PRODUCT_FILE = "/Users/zgd/Downloads/万物香铺 商品资料 .xlsx"
STATUSES = ("待处理", "已发货", "异常", "已取消")
EXPRESS_COMPANIES = ("圆通", "京东", "顺丰")
DEFAULT_EXPRESS_COMPANY = "圆通"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "wanwu2026"
DEFAULT_STORE_USERNAME = "store01"
DEFAULT_STORE_PASSWORD = "wanwu2026"


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt).split("$", 1)[1], digest)


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(
        self,
        product_file: str = DEFAULT_PRODUCT_FILE,
        *,
        production: bool = False,
        admin_password: str = "",
    ) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
                    store_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (store_id) REFERENCES stores(id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS products (
                    barcode TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    spec TEXT NOT NULL DEFAULT '',
                    price TEXT NOT NULL DEFAULT '0.00',
                    status TEXT NOT NULL DEFAULT '启用',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_id INTEGER NOT NULL,
                    store_name_snapshot TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    recipient_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    address TEXT NOT NULL,
                    store_order_no TEXT NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待处理',
                    express_company TEXT NOT NULL DEFAULT '',
                    tracking_no TEXT NOT NULL DEFAULT '',
                    shipping_note TEXT NOT NULL DEFAULT '',
                    shipped_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (store_id, store_order_no),
                    FOREIGN KEY (store_id) REFERENCES stores(id),
                    FOREIGN KEY (created_by) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS shipment_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER NOT NULL,
                    product_barcode TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    product_category TEXT NOT NULL,
                    unit_price TEXT NOT NULL DEFAULT '0.00',
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_store ON shipments(store_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_created ON shipments(created_at)")
            self._seed_defaults(conn, production=production, admin_password=admin_password)

        if self.count_products() == 0 and os.path.exists(product_file):
            self.import_products(product_file)

    def _seed_defaults(self, conn: sqlite3.Connection, *, production: bool, admin_password: str) -> None:
        now = now_text()
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        store_count = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]

        if production:
            if user_count == 0:
                if not admin_password or len(admin_password) < 8:
                    raise AppError("生产环境首次启动必须设置至少 8 位的 WANWU_ADMIN_PASSWORD，或迁移已重置密码的数据库。", 500)
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, store_id, active, created_at, updated_at)
                    VALUES (?, ?, 'admin', NULL, 1, ?, ?)
                    """,
                    (DEFAULT_ADMIN_USERNAME, hash_password(admin_password), now, now),
                )
            return

        store_id = None
        if store_count == 0:
            cursor = conn.execute(
                "INSERT INTO stores (name, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
                ("示例门店", now, now),
            )
            store_id = cursor.lastrowid
        else:
            row = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()
            store_id = row["id"] if row else None

        if user_count == 0:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, store_id, active, created_at, updated_at)
                VALUES (?, ?, 'admin', NULL, 1, ?, ?)
                """,
                (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD), now, now),
            )
            if store_id:
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, store_id, active, created_at, updated_at)
                    VALUES (?, ?, 'staff', ?, 1, ?, ?)
                    """,
                    (DEFAULT_STORE_USERNAME, hash_password(DEFAULT_STORE_PASSWORD), store_id, now, now),
                )

    def database_summary(self) -> Dict[str, int]:
        with self.connect() as conn:
            return {
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "stores": conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0],
                "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
            }

    def default_credentials_active(self) -> bool:
        return bool(
            self.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
            or self.authenticate(DEFAULT_STORE_USERNAME, DEFAULT_STORE_PASSWORD)
        )

    def set_user_password(self, username: str, password: str) -> None:
        username = username.strip()
        if not username:
            raise AppError("请输入账号。")
        if len(password) < 8:
            raise AppError("密码至少需要 8 位。")
        now = now_text()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (hash_password(password), now, username),
            )
            if cursor.rowcount == 0:
                raise AppError(f"账号不存在：{username}", 404)

    def backup_bytes(self) -> bytes:
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        return Path(self.path).read_bytes()

    def count_products(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def import_products(self, path: str) -> Dict[str, Any]:
        try:
            products, meta = read_products(path)
        except XlsxImportError as exc:
            raise AppError(str(exc), 400) from exc

        now = now_text()
        with self.connect() as conn:
            for product in products:
                conn.execute(
                    """
                    INSERT INTO products (barcode, name, category, spec, price, status, updated_at)
                    VALUES (:barcode, :name, :category, :spec, :price, :status, :updated_at)
                    ON CONFLICT(barcode) DO UPDATE SET
                        name = excluded.name,
                        category = excluded.category,
                        spec = excluded.spec,
                        price = excluded.price,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    {**product, "updated_at": now},
                )
        return {**meta, "imported": len(products), "path": path}

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.*, stores.name AS store_name
                FROM users
                LEFT JOIN stores ON stores.id = users.store_id
                WHERE username = ? AND users.active = 1
                """,
                (username.strip(),),
            ).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                return None
            return self._public_user(row)

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = now_text()
        expires = (datetime.now().astimezone() + timedelta(days=14)).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, expires),
            )
        return token

    def delete_session(self, token: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def user_for_session(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.*, stores.name AS store_name, sessions.expires_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                LEFT JOIN stores ON stores.id = users.store_id
                WHERE sessions.token = ? AND users.active = 1
                """,
                (token,),
            ).fetchone()
            if not row:
                return None
            try:
                if datetime.fromisoformat(row["expires_at"]) < datetime.now().astimezone():
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                    return None
            except ValueError:
                return None
            return self._public_user(row)

    def _public_user(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "store_id": row["store_id"],
            "store_name": row["store_name"],
        }

    def list_stores(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        sql = """
            SELECT stores.*,
                   GROUP_CONCAT(users.username, ', ') AS usernames
            FROM stores
            LEFT JOIN users ON users.store_id = stores.id AND users.role = 'staff'
        """
        params: List[Any] = []
        if not include_inactive:
            sql += " WHERE stores.active = 1"
        sql += " GROUP BY stores.id ORDER BY stores.active DESC, stores.name"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def create_store(self, name: str, username: str, password: str) -> Dict[str, Any]:
        name = name.strip()
        username = username.strip()
        if not name:
            raise AppError("请输入门店名称。")
        if not username:
            raise AppError("请输入店员账号。")
        if len(password) < 6:
            raise AppError("密码至少需要 6 位。")
        now = now_text()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO stores (name, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
                    (name, now, now),
                )
                store_id = cursor.lastrowid
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, store_id, active, created_at, updated_at)
                    VALUES (?, ?, 'staff', ?, 1, ?, ?)
                    """,
                    (username, hash_password(password), store_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise AppError("门店名称或账号已存在。", 409) from exc
        return {"id": store_id, "name": name, "username": username}

    def update_store(self, store_id: int, active: bool) -> Dict[str, Any]:
        now = now_text()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE stores SET active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, now, store_id),
            )
            if cursor.rowcount == 0:
                raise AppError("门店不存在。", 404)
            conn.execute(
                "UPDATE users SET active = ?, updated_at = ? WHERE role = 'staff' AND store_id = ?",
                (1 if active else 0, now, store_id),
            )
        return {"id": store_id, "active": active}

    def list_products(self, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM products"
        params: List[Any] = []
        if active_only:
            sql += " WHERE status = ?"
            params.append("启用")
        sql += " ORDER BY category, name, barcode"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def grouped_products(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for product in self.list_products(active_only=True):
            grouped.setdefault(product["category"], []).append(product)
        return grouped

    def create_shipment(self, user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        store_id = user.get("store_id") if user.get("role") == "staff" else payload.get("store_id")
        try:
            store_id = int(store_id)
        except (TypeError, ValueError):
            raise AppError("请选择门店。")

        recipient_name = str(payload.get("recipient_name", "")).strip()
        phone = str(payload.get("phone", "")).strip()
        address = str(payload.get("address", "")).strip()
        store_order_no = str(payload.get("store_order_no", "")).strip()
        remark = str(payload.get("remark", "")).strip()
        raw_items = payload.get("items") or []

        if not recipient_name:
            raise AppError("请输入姓名。")
        if not phone or not re_phone_ok(phone):
            raise AppError("请输入有效联系电话。")
        if not address:
            raise AppError("请输入快递地址。")
        if not store_order_no:
            raise AppError("请输入门店订单号。")
        if not isinstance(raw_items, list) or not raw_items:
            raise AppError("请至少选择一个货品。")

        now = now_text()
        with self.connect() as conn:
            store = conn.execute(
                "SELECT * FROM stores WHERE id = ? AND active = 1", (store_id,)
            ).fetchone()
            if not store:
                raise AppError("门店不存在或已停用。")

            items = self._resolve_items(conn, raw_items)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO shipments (
                        store_id, store_name_snapshot, created_by, recipient_name, phone, address,
                        store_order_no, remark, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '待处理', ?, ?)
                    """,
                    (
                        store_id,
                        store["name"],
                        user["id"],
                        recipient_name,
                        phone,
                        address,
                        store_order_no,
                        remark,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AppError("这个门店订单号已经提交过。", 409) from exc

            shipment_id = cursor.lastrowid
            for item in items:
                conn.execute(
                    """
                    INSERT INTO shipment_items (
                        shipment_id, product_barcode, product_name, product_category, unit_price, quantity
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shipment_id,
                        item["barcode"],
                        item["name"],
                        item["category"],
                        item["price"],
                        item["quantity"],
                    ),
                )
        return self.get_shipment(shipment_id, user)

    def _resolve_items(self, conn: sqlite3.Connection, raw_items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for raw in raw_items:
            barcode = str(raw.get("barcode", "")).strip()
            try:
                quantity = int(raw.get("quantity", 0))
            except (TypeError, ValueError):
                quantity = 0
            if not barcode or quantity <= 0:
                raise AppError("货品和数量不能为空。")
            product = conn.execute(
                "SELECT * FROM products WHERE barcode = ? AND status = '启用'", (barcode,)
            ).fetchone()
            if not product:
                raise AppError(f"货品不存在或未启用：{barcode}")
            items.append({**dict(product), "quantity": quantity})
        return items

    def get_shipment(self, shipment_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        shipments = self.list_shipments(user, {"id": shipment_id})
        if not shipments:
            raise AppError("发货单不存在。", 404)
        return shipments[0]

    def list_shipments(self, user: Dict[str, Any], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []

        if filters.get("id"):
            where.append("shipments.id = ?")
            params.append(filters["id"])

        if user.get("role") == "staff":
            where.append("shipments.store_id = ?")
            params.append(user["store_id"])
        elif filters.get("store_id"):
            where.append("shipments.store_id = ?")
            params.append(filters["store_id"])

        if filters.get("status"):
            where.append("shipments.status = ?")
            params.append(filters["status"])
        if filters.get("date_from"):
            where.append("date(shipments.created_at) >= date(?)")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            where.append("date(shipments.created_at) <= date(?)")
            params.append(filters["date_to"])
        if filters.get("q"):
            q = f"%{filters['q']}%"
            where.append(
                """
                (
                    shipments.store_order_no LIKE ? OR shipments.recipient_name LIKE ? OR
                    shipments.phone LIKE ? OR shipments.tracking_no LIKE ? OR
                    shipments.address LIKE ?
                )
                """
            )
            params.extend([q, q, q, q, q])

        sql = "SELECT shipments.* FROM shipments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY shipments.created_at DESC, shipments.id DESC"

        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            item_rows = conn.execute(
                f"SELECT * FROM shipment_items WHERE shipment_id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()

        items_by_shipment: Dict[int, List[Dict[str, Any]]] = {}
        for item in item_rows:
            items_by_shipment.setdefault(item["shipment_id"], []).append(dict(item))

        for row in rows:
            row["items"] = items_by_shipment.get(row["id"], [])
            row["item_summary"] = "；".join(
                f"{item['product_category']} / {item['product_name']} x{item['quantity']}"
                for item in row["items"]
            )
        return rows

    def update_shipment(self, shipment_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = str(payload.get("status", "")).strip()
        if status not in STATUSES:
            raise AppError("请选择有效状态。")
        express_company = str(payload.get("express_company", "")).strip() or DEFAULT_EXPRESS_COMPANY
        if express_company not in EXPRESS_COMPANIES:
            raise AppError("请选择有效快递公司。")
        tracking_no = str(payload.get("tracking_no", "")).strip()
        shipping_note = str(payload.get("shipping_note", "")).strip()
        shipped_at = str(payload.get("shipped_at", "")).strip()
        if status == "已发货" and not shipped_at:
            shipped_at = now_text()
        if status != "已发货":
            shipped_at = ""

        now = now_text()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE shipments
                SET status = ?, express_company = ?, tracking_no = ?, shipping_note = ?,
                    shipped_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, express_company, tracking_no, shipping_note, shipped_at, now, shipment_id),
            )
            if cursor.rowcount == 0:
                raise AppError("发货单不存在。", 404)
        return {"id": shipment_id}


def re_phone_ok(phone: str) -> bool:
    compact = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    digits = "".join(ch for ch in compact if ch.isdigit())
    return 7 <= len(digits) <= 20 and compact.replace("+", "", 1).isdigit()
