from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from xlsx_importer import XlsxImportError, read_products


DEFAULT_PRODUCT_FILE = "/Users/zgd/Downloads/万物香铺 商品资料 .xlsx"
STATUSES = ("待处理", "已发货", "已签收", "异常", "已取消")
RETURN_STATUSES = ("待查询", "运输中", "已签收", "异常", "已取消")
EXPRESS_COMPANIES = ("圆通", "京东", "顺丰")
DEFAULT_EXPRESS_COMPANY = "圆通"
DEFAULT_CAINIAO_TEMPLATE_URLS = {
    "圆通": "https://cloudprint.cainiao.com/template/standard/850338",
}
DEFAULT_CAINIAO_CUSTOM_TEMPLATE_URLS = {
    "圆通": "https://cloudprint.cainiao.com/template/customArea/77205369",
}
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "scentpool2026"
DEFAULT_STORE_USERNAME = "store01"
DEFAULT_STORE_PASSWORD = "scentpool2026"
APP_TZ = ZoneInfo("Asia/Shanghai")
BOOKING_EDITABLE_STATUSES = ("未下单", "下单失败", "已取消")
BOOKING_ACTIVE_STATUSES = ("排队中", "提交中")
SHIPPING_STALE_MINUTES = 10
SHIPPING_STALE_MAX_ATTEMPTS = 3
AUDIT_HISTORY_SCHEMA_VERSION = 1
AUDIT_FAILURE_ACTION_HINTS = {
    "provider_or_api_rejected": "请先核对承运范围或供应商规则；确认可继续后再重试，避免重复提交。",
    "system_error_or_timeout": "请先等待系统自动恢复；持续失败时联系管理员检查服务状态，不要连续重复提交。",
    "data_or_configuration": "请检查地址、联系方式、承运商、网点、余额及相关配置后再重试。",
    "insufficient_historical_evidence": "现有脱敏证据不足以判断原因，请由管理员在受控后台核对后处理。",
}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}


def now_text() -> str:
    return datetime.now(APP_TZ).isoformat(timespec="seconds")


def classify_audit_failure(message: str, *, evidence: str = "observed") -> str:
    """Map an internal failure message to a fixed privacy-safe category."""
    if evidence != "observed":
        return "insufficient_historical_evidence"
    text = str(message or "").strip().lower()
    if not text:
        return "insufficient_historical_evidence"
    data_markers = (
        "地址", "行政区", "手机号", "电话", "配置", "授权", "网点", "余额", "参数",
        "单号", "快递公司", "格式", "缺少", "不能为空", "不匹配",
    )
    provider_markers = (
        "明确拒绝", "拒绝", "停发", "超出范围", "超区", "http error 4", "http 4",
        "供应商返回", "api 返回", "api拒绝", "api 拒绝",
    )
    system_markers = (
        "timeout", "timed out", "超时", "网络", "连接", "http error 5", "http 5",
        "系统异常", "内部错误", "服务异常", "暂时无法", "unexpected", "traceback",
    )
    if any(marker in text for marker in data_markers):
        return "data_or_configuration"
    if any(marker in text for marker in provider_markers):
        return "provider_or_api_rejected"
    if any(marker in text for marker in system_markers):
        return "system_error_or_timeout"
    return "insufficient_historical_evidence"


def local_day_start(date_text: str) -> str:
    return datetime.fromisoformat(f"{date_text}T00:00:00").replace(tzinfo=APP_TZ).isoformat(timespec="seconds")


def local_day_end(date_text: str) -> str:
    return datetime.fromisoformat(f"{date_text}T23:59:59").replace(tzinfo=APP_TZ).isoformat(timespec="seconds")


def shipment_business_id(order_date: str, store_id: int, store_order_no: str) -> str:
    return f"{order_date.replace('-', '')}-S{int(store_id):02d}-{store_order_no}"


def new_booking_request_id(order_date: str, store_id: int, shipment_id: int) -> str:
    seed = f"{order_date}:{store_id}:{shipment_id}:{secrets.token_hex(12)}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()
    return f"SP{order_date.replace('-', '')}{digest}"


def business_search_parts(query: str) -> Optional[tuple[str, Optional[int], str]]:
    text = str(query or "").strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})[-_\s]+[sS](\d+)[-_\s]+(.+)$", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}", int(match.group(4)), match.group(5).strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})[-_\s]+(.+)$", text)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}", None, match.group(4).strip()


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
        self._shipment_columns: Optional[List[str]] = None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA cache_size = -2048")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        return conn

    def connect_readonly(self) -> sqlite3.Connection:
        """Open a reporting connection that cannot initialize, migrate, or write the database."""
        database_uri = Path(self.path).expanduser().resolve().as_uri()
        conn = sqlite3.connect(f"{database_uri}?mode=ro", uri=True, timeout=15)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 15000")
            conn.execute("PRAGMA cache_size = -2048")
            conn.execute("PRAGMA temp_store = FILE")
            conn.execute("PRAGMA query_only = ON")
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise sqlite3.OperationalError("read-only reporting connection is unavailable")
            return conn
        except Exception:
            conn.close()
            raise

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
            conn.execute("PRAGMA journal_mode = WAL")
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
                    order_date TEXT NOT NULL,
                    business_id TEXT NOT NULL UNIQUE,
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
                    tracking_provider TEXT NOT NULL DEFAULT '',
                    tracking_status TEXT NOT NULL DEFAULT '',
                    tracking_state_code TEXT NOT NULL DEFAULT '',
                    tracking_last_event TEXT NOT NULL DEFAULT '',
                    tracking_last_checked_at TEXT NOT NULL DEFAULT '',
                    tracking_signed_at TEXT NOT NULL DEFAULT '',
                    tracking_error TEXT NOT NULL DEFAULT '',
                    tracking_raw TEXT NOT NULL DEFAULT '',
                    shipping_note TEXT NOT NULL DEFAULT '',
                    shipped_at TEXT NOT NULL DEFAULT '',
                    booking_status TEXT NOT NULL DEFAULT '未下单',
                    booking_task_id TEXT NOT NULL DEFAULT '',
                    booking_order_id TEXT NOT NULL DEFAULT '',
                    booking_request_id TEXT NOT NULL DEFAULT '',
                    booking_poll_token TEXT NOT NULL DEFAULT '',
                    booking_salt TEXT NOT NULL DEFAULT '',
                    booking_error TEXT NOT NULL DEFAULT '',
                    booking_carrier_status TEXT NOT NULL DEFAULT '',
                    booking_raw TEXT NOT NULL DEFAULT '',
                    booking_requested_at TEXT NOT NULL DEFAULT '',
                    booking_updated_at TEXT NOT NULL DEFAULT '',
                    booking_courier_name TEXT NOT NULL DEFAULT '',
                    booking_courier_mobile TEXT NOT NULL DEFAULT '',
                    booking_weight TEXT NOT NULL DEFAULT '',
                    booking_freight TEXT NOT NULL DEFAULT '',
                    label_url TEXT NOT NULL DEFAULT '',
                    label_print_status TEXT NOT NULL DEFAULT '',
                    label_print_error TEXT NOT NULL DEFAULT '',
                    label_print_type TEXT NOT NULL DEFAULT '',
                    label_carrier_order_no TEXT NOT NULL DEFAULT '',
                    label_child_no TEXT NOT NULL DEFAULT '',
                    label_return_no TEXT NOT NULL DEFAULT '',
                    pickup_day TEXT NOT NULL DEFAULT '',
                    pickup_start_time TEXT NOT NULL DEFAULT '',
                    pickup_end_time TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (order_date, store_id, store_order_no),
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

                CREATE TABLE IF NOT EXISTS return_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_id INTEGER NOT NULL,
                    store_name_snapshot TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    express_company TEXT NOT NULL DEFAULT '',
                    express_company_code TEXT NOT NULL DEFAULT '',
                    express_company_source TEXT NOT NULL DEFAULT 'manual',
                    tracking_no TEXT NOT NULL,
                    sender_phone TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待查询',
                    tracking_provider TEXT NOT NULL DEFAULT '',
                    tracking_status TEXT NOT NULL DEFAULT '',
                    tracking_state_code TEXT NOT NULL DEFAULT '',
                    tracking_last_event TEXT NOT NULL DEFAULT '',
                    tracking_last_checked_at TEXT NOT NULL DEFAULT '',
                    tracking_signed_at TEXT NOT NULL DEFAULT '',
                    tracking_error TEXT NOT NULL DEFAULT '',
                    tracking_raw TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (store_id, tracking_no),
                    FOREIGN KEY (store_id) REFERENCES stores(id),
                    FOREIGN KEY (created_by) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS return_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    return_order_id INTEGER NOT NULL,
                    product_barcode TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    product_category TEXT NOT NULL,
                    unit_price TEXT NOT NULL DEFAULT '0.00',
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    FOREIGN KEY (return_order_id) REFERENCES return_orders(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shipping_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_mobile TEXT NOT NULL DEFAULT '',
                    sender_address TEXT NOT NULL DEFAULT '',
                    sender_company TEXT NOT NULL DEFAULT '万物香铺',
                    default_company TEXT NOT NULL DEFAULT '圆通',
                    cargo_name TEXT NOT NULL DEFAULT '香氛商品',
                    pay_type TEXT NOT NULL DEFAULT 'MONTHLY',
                    print_mode TEXT NOT NULL DEFAULT 'PDF',
                    printer_siid TEXT NOT NULL DEFAULT '',
                    template_id TEXT NOT NULL DEFAULT '',
                    paper_width TEXT NOT NULL DEFAULT '100',
                    paper_height TEXT NOT NULL DEFAULT '180',
                    need_desensitization INTEGER NOT NULL DEFAULT 0,
                    need_logo INTEGER NOT NULL DEFAULT 0,
                    partner_id TEXT NOT NULL DEFAULT '',
                    partner_key TEXT NOT NULL DEFAULT '',
                    partner_secret TEXT NOT NULL DEFAULT '',
                    partner_name TEXT NOT NULL DEFAULT '',
                    partner_net TEXT NOT NULL DEFAULT '',
                    partner_code TEXT NOT NULL DEFAULT '',
                    partner_check_man TEXT NOT NULL DEFAULT '',
                    carrier_settings_json TEXT NOT NULL DEFAULT '{}',
                    branch_options_json TEXT NOT NULL DEFAULT '[]',
                    authorized_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shipping_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    pickup_day TEXT NOT NULL,
                    pickup_start_time TEXT NOT NULL,
                    pickup_end_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '排队中',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (created_by) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS shipping_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    shipment_id INTEGER NOT NULL,
                    express_company TEXT NOT NULL DEFAULT '圆通',
                    request_id TEXT NOT NULL DEFAULT '',
                    callback_salt TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '排队中',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    response_raw TEXT NOT NULL DEFAULT '',
                    cancel_param_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (batch_id, shipment_id),
                    FOREIGN KEY (batch_id) REFERENCES shipping_batches(id) ON DELETE CASCADE,
                    FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shipping_callback_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    carrier_status TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    param_raw TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS label_auth_sessions (
                    state TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    used_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_alert_acknowledgements (
                    alert_key TEXT PRIMARY KEY,
                    alert_type TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    resolved_by INTEGER NOT NULL,
                    resolved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operational_incidents (
                    incident_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    incident_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    affected_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

        self._migrate_shipments_schema()

        with self.connect() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_store ON shipments(store_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_created ON shipments(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_order_date ON shipments(order_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_booking_status ON shipments(booking_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipping_batch_items_status ON shipping_batch_items(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_return_orders_status ON return_orders(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_return_orders_store ON return_orders(store_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_return_orders_created ON return_orders(created_at)")
            self._ensure_shipment_tracking_columns(conn)
            self._ensure_return_tracking_columns(conn)
            self._ensure_shipping_batch_columns(conn)
            self._ensure_label_columns(conn)
            self._normalize_shipments_with_tracking(conn)
            self._ensure_audit_history(conn)
            conn.execute(
                """
                INSERT INTO shipping_settings (id, default_company, cargo_name, updated_at)
                VALUES (1, ?, '香氛商品', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (DEFAULT_EXPRESS_COMPANY, now_text()),
            )
            self._seed_defaults(conn, production=production, admin_password=admin_password)

        if self.count_products() == 0 and os.path.exists(product_file):
            self.import_products(product_file)

    def _ensure_audit_history(self, conn: sqlite3.Connection) -> None:
        """Create privacy-safe append-only state evidence and seed one legacy snapshot."""
        started_at = now_text()
        started_dt = datetime.fromisoformat(started_at).astimezone(APP_TZ)
        full_day_coverage_from = (started_dt + timedelta(days=1)).date().isoformat()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_event_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at TEXT NOT NULL,
                full_day_coverage_from TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL CHECK (
                    domain IN ('shipment_state', 'label', 'tracking', 'printing')
                ),
                entity_kind TEXT NOT NULL CHECK (entity_kind IN ('shipment', 'return')),
                entity_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('created', 'changed', 'deleted', 'snapshot')
                ),
                state TEXT NOT NULL,
                reason_category TEXT NOT NULL DEFAULT '',
                entity_created_at TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                evidence TEXT NOT NULL CHECK (evidence IN ('observed', 'legacy_snapshot'))
            );

            CREATE INDEX IF NOT EXISTS idx_audit_state_events_domain_time
                ON audit_state_events(domain, julianday(occurred_at), id);
            CREATE INDEX IF NOT EXISTS idx_audit_state_events_entity
                ON audit_state_events(
                    domain, entity_kind, entity_id, julianday(occurred_at), id
                );

            CREATE TRIGGER IF NOT EXISTS audit_state_events_no_update
            BEFORE UPDATE ON audit_state_events
            BEGIN
                SELECT RAISE(ABORT, 'audit state events are append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS audit_state_events_no_delete
            BEFORE DELETE ON audit_state_events
            BEGIN
                SELECT RAISE(ABORT, 'audit state events are append-only');
            END;
            """
        )
        inserted_meta = conn.execute(
            """
            INSERT INTO audit_event_meta (id, started_at, full_day_coverage_from, schema_version)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (started_at, full_day_coverage_from, AUDIT_HISTORY_SCHEMA_VERSION),
        ).rowcount
        if inserted_meta:
            conn.execute(
                """
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                )
                SELECT 'shipment_state', 'shipment', id, 'snapshot',
                    CASE
                        WHEN tracking_signed_at <> '' OR status = '已签收' THEN 'signed'
                        WHEN shipped_at <> '' OR status = '已发货' THEN 'shipped'
                        WHEN status = '待处理' THEN 'pending'
                        WHEN status = '异常' THEN 'exception'
                        WHEN status = '已取消' THEN 'cancelled'
                        ELSE 'unknown'
                    END,
                    '', created_at, ?, 'legacy_snapshot'
                FROM shipments
                """,
                (started_at,),
            )
            conn.execute(
                """
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                )
                SELECT 'label', 'shipment', id, 'snapshot',
                    CASE booking_status
                        WHEN '排队中' THEN 'queued'
                        WHEN '提交中' THEN 'submitting'
                        WHEN '下单失败' THEN 'failed'
                        WHEN '已出单' THEN 'completed'
                        WHEN '已取消' THEN 'cancelled'
                        ELSE 'idle'
                    END,
                    CASE WHEN booking_status = '下单失败'
                        THEN 'insufficient_historical_evidence' ELSE '' END,
                    created_at, ?, 'legacy_snapshot'
                FROM shipments
                """,
                (started_at,),
            )
            conn.execute(
                """
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                )
                SELECT 'tracking', 'shipment', id, 'snapshot',
                    CASE WHEN tracking_status = '查询失败' THEN 'failed'
                        WHEN tracking_status = '已签收' THEN 'signed'
                        WHEN tracking_status = '运输中' THEN 'in_transit'
                        WHEN tracking_status = '等待揽收' THEN 'waiting_pickup'
                        ELSE 'idle' END,
                    CASE WHEN tracking_status = '查询失败'
                        THEN 'insufficient_historical_evidence' ELSE '' END,
                    created_at, ?, 'legacy_snapshot'
                FROM shipments
                """,
                (started_at,),
            )
            conn.execute(
                """
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                )
                SELECT 'tracking', 'return', id, 'snapshot',
                    CASE WHEN tracking_status = '查询失败' THEN 'failed'
                        WHEN tracking_status = '已签收' THEN 'signed'
                        WHEN tracking_status = '运输中' THEN 'in_transit'
                        WHEN tracking_status = '等待揽收' THEN 'waiting_pickup'
                        ELSE 'idle' END,
                    CASE WHEN tracking_status = '查询失败'
                        THEN 'insufficient_historical_evidence' ELSE '' END,
                    created_at, ?, 'legacy_snapshot'
                FROM return_orders
                """,
                (started_at,),
            )
            conn.execute(
                """
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                )
                SELECT 'printing', 'shipment', id, 'snapshot',
                    CASE label_print_status
                        WHEN '待打印' THEN 'waiting'
                        WHEN '打印中' THEN 'processing'
                        WHEN '打印成功' THEN 'succeeded'
                        WHEN '打印失败' THEN 'failed'
                        ELSE 'idle'
                    END,
                    CASE WHEN label_print_status = '打印失败'
                        THEN 'insufficient_historical_evidence' ELSE '' END,
                    created_at, ?, 'legacy_snapshot'
                FROM shipments
                """,
                (started_at,),
            )

        error_category_sql = """
            CASE
                WHEN TRIM(COALESCE({error}, '')) = '' THEN 'insufficient_historical_evidence'
                WHEN LOWER(COALESCE({error}, '')) LIKE '%地址%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%行政区%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%手机号%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%电话%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%配置%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%授权%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%网点%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%余额%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%参数%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%单号%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%快递公司%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%格式%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%缺少%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%不能为空%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%不匹配%'
                    THEN 'data_or_configuration'
                WHEN LOWER(COALESCE({error}, '')) LIKE '%明确拒绝%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%拒绝%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%停发%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%超出范围%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%超区%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%http error 4%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%http 4%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%供应商返回%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%api 返回%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%api拒绝%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%api 拒绝%'
                    THEN 'provider_or_api_rejected'
                WHEN LOWER(COALESCE({error}, '')) LIKE '%timeout%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%timed out%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%超时%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%网络%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%连接%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%http error 5%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%http 5%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%系统异常%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%内部错误%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%服务异常%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%暂时无法%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%unexpected%'
                    OR LOWER(COALESCE({error}, '')) LIKE '%traceback%'
                    THEN 'system_error_or_timeout'
                ELSE 'insufficient_historical_evidence'
            END
        """
        booking_category = error_category_sql.format(error="NEW.booking_error")
        tracking_category = error_category_sql.format(error="NEW.tracking_error")
        printing_category = error_category_sql.format(error="NEW.label_print_error")
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS audit_shipments_insert
            AFTER INSERT ON shipments
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'shipment_state', 'shipment', NEW.id, 'created',
                    CASE WHEN NEW.tracking_signed_at <> '' OR NEW.status = '已签收' THEN 'signed'
                        WHEN NEW.shipped_at <> '' OR NEW.status = '已发货' THEN 'shipped'
                        WHEN NEW.status = '待处理' THEN 'pending'
                        WHEN NEW.status = '异常' THEN 'exception'
                        WHEN NEW.status = '已取消' THEN 'cancelled' ELSE 'unknown' END,
                    '', NEW.created_at, NEW.created_at, 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'label', 'shipment', NEW.id, 'created',
                    CASE NEW.booking_status WHEN '排队中' THEN 'queued' WHEN '提交中' THEN 'submitting'
                        WHEN '下单失败' THEN 'failed' WHEN '已出单' THEN 'completed'
                        WHEN '已取消' THEN 'cancelled' ELSE 'idle' END,
                    CASE WHEN NEW.booking_status = '下单失败' THEN {booking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.booking_updated_at, ''), NEW.created_at), 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'shipment', NEW.id, 'created',
                    CASE WHEN NEW.tracking_status = '查询失败' THEN 'failed'
                        WHEN NEW.tracking_status = '已签收' THEN 'signed'
                        WHEN NEW.tracking_status = '运输中' THEN 'in_transit'
                        WHEN NEW.tracking_status = '等待揽收' THEN 'waiting_pickup' ELSE 'idle' END,
                    CASE WHEN NEW.tracking_status = '查询失败' THEN {tracking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.tracking_last_checked_at, ''), NEW.created_at), 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'printing', 'shipment', NEW.id, 'created',
                    CASE NEW.label_print_status WHEN '待打印' THEN 'waiting'
                        WHEN '打印中' THEN 'processing' WHEN '打印成功' THEN 'succeeded'
                        WHEN '打印失败' THEN 'failed' ELSE 'idle' END,
                    CASE WHEN NEW.label_print_status = '打印失败' THEN {printing_category} ELSE '' END,
                    NEW.created_at, NEW.updated_at, 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_shipments_state_update
            AFTER UPDATE OF status, shipped_at, tracking_signed_at ON shipments
            WHEN OLD.status IS NOT NEW.status
                OR OLD.shipped_at IS NOT NEW.shipped_at
                OR OLD.tracking_signed_at IS NOT NEW.tracking_signed_at
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'shipment_state', 'shipment', NEW.id, 'changed',
                    CASE WHEN NEW.tracking_signed_at <> '' OR NEW.status = '已签收' THEN 'signed'
                        WHEN NEW.shipped_at <> '' OR NEW.status = '已发货' THEN 'shipped'
                        WHEN NEW.status = '待处理' THEN 'pending'
                        WHEN NEW.status = '异常' THEN 'exception'
                        WHEN NEW.status = '已取消' THEN 'cancelled' ELSE 'unknown' END,
                    '', NEW.created_at, NEW.updated_at, 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_shipments_label_update
            AFTER UPDATE OF booking_status, booking_error ON shipments
            WHEN OLD.booking_status IS NOT NEW.booking_status
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'label', 'shipment', NEW.id, 'changed',
                    CASE NEW.booking_status WHEN '排队中' THEN 'queued' WHEN '提交中' THEN 'submitting'
                        WHEN '下单失败' THEN 'failed' WHEN '已出单' THEN 'completed'
                        WHEN '已取消' THEN 'cancelled' ELSE 'idle' END,
                    CASE WHEN NEW.booking_status = '下单失败' THEN {booking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.booking_updated_at, ''), NEW.updated_at), 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_shipments_tracking_update
            AFTER UPDATE OF tracking_status, tracking_error ON shipments
            WHEN OLD.tracking_status IS NOT NEW.tracking_status
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'shipment', NEW.id, 'changed',
                    CASE WHEN NEW.tracking_status = '查询失败' THEN 'failed'
                        WHEN NEW.tracking_status = '已签收' THEN 'signed'
                        WHEN NEW.tracking_status = '运输中' THEN 'in_transit'
                        WHEN NEW.tracking_status = '等待揽收' THEN 'waiting_pickup' ELSE 'idle' END,
                    CASE WHEN NEW.tracking_status = '查询失败' THEN {tracking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.tracking_last_checked_at, ''), NEW.updated_at), 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_shipments_printing_update
            AFTER UPDATE OF label_print_status, label_print_error ON shipments
            WHEN OLD.label_print_status IS NOT NEW.label_print_status
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'printing', 'shipment', NEW.id, 'changed',
                    CASE NEW.label_print_status WHEN '待打印' THEN 'waiting'
                        WHEN '打印中' THEN 'processing' WHEN '打印成功' THEN 'succeeded'
                        WHEN '打印失败' THEN 'failed' ELSE 'idle' END,
                    CASE WHEN NEW.label_print_status = '打印失败' THEN {printing_category} ELSE '' END,
                    NEW.created_at, NEW.updated_at, 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_shipments_delete
            BEFORE DELETE ON shipments
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'shipment_state', 'shipment', OLD.id, 'deleted', 'deleted', '',
                    OLD.created_at, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'label', 'shipment', OLD.id, 'deleted', 'deleted', '',
                    OLD.created_at, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'shipment', OLD.id, 'deleted', 'deleted', '',
                    OLD.created_at, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), 'observed'
                );
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'printing', 'shipment', OLD.id, 'deleted', 'deleted', '',
                    OLD.created_at, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_returns_insert
            AFTER INSERT ON return_orders
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'return', NEW.id, 'created',
                    CASE WHEN NEW.tracking_status = '查询失败' THEN 'failed'
                        WHEN NEW.tracking_status = '已签收' THEN 'signed'
                        WHEN NEW.tracking_status = '运输中' THEN 'in_transit'
                        WHEN NEW.tracking_status = '等待揽收' THEN 'waiting_pickup' ELSE 'idle' END,
                    CASE WHEN NEW.tracking_status = '查询失败' THEN {tracking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.tracking_last_checked_at, ''), NEW.created_at), 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_returns_tracking_update
            AFTER UPDATE OF tracking_status, tracking_error ON return_orders
            WHEN OLD.tracking_status IS NOT NEW.tracking_status
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'return', NEW.id, 'changed',
                    CASE WHEN NEW.tracking_status = '查询失败' THEN 'failed'
                        WHEN NEW.tracking_status = '已签收' THEN 'signed'
                        WHEN NEW.tracking_status = '运输中' THEN 'in_transit'
                        WHEN NEW.tracking_status = '等待揽收' THEN 'waiting_pickup' ELSE 'idle' END,
                    CASE WHEN NEW.tracking_status = '查询失败' THEN {tracking_category} ELSE '' END,
                    NEW.created_at, COALESCE(NULLIF(NEW.tracking_last_checked_at, ''), NEW.updated_at), 'observed'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS audit_returns_delete
            BEFORE DELETE ON return_orders
            BEGIN
                INSERT INTO audit_state_events (
                    domain, entity_kind, entity_id, event_type, state, reason_category,
                    entity_created_at, occurred_at, evidence
                ) VALUES (
                    'tracking', 'return', OLD.id, 'deleted', 'deleted', '',
                    OLD.created_at, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), 'observed'
                );
            END;
            """
        )

    def _migrate_shipments_schema(self) -> None:
        with sqlite3.connect(self.path) as check:
            check.row_factory = sqlite3.Row
            row = check.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'shipments'").fetchone()
            if not row:
                return
            columns = {item[1] for item in check.execute("PRAGMA table_info(shipments)").fetchall()}
            normalized_sql = re.sub(r"\s+", " ", str(row["sql"] or "").lower())
            needs_rebuild = (
                "order_date" not in columns
                or "business_id" not in columns
                or "booking_status" not in columns
                or "unique (store_id, store_order_no)" in normalized_sql
            )
        if not needs_rebuild:
            return

        self._backup_before_migration()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            existing = {item[1] for item in conn.execute("PRAGMA table_info(shipments)").fetchall()}

            def old(name: str, fallback: str) -> str:
                return name if name in existing else fallback

            conn.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS shipments_new;
                CREATE TABLE shipments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_date TEXT NOT NULL,
                    business_id TEXT NOT NULL UNIQUE,
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
                    tracking_provider TEXT NOT NULL DEFAULT '',
                    tracking_status TEXT NOT NULL DEFAULT '',
                    tracking_state_code TEXT NOT NULL DEFAULT '',
                    tracking_last_event TEXT NOT NULL DEFAULT '',
                    tracking_last_checked_at TEXT NOT NULL DEFAULT '',
                    tracking_signed_at TEXT NOT NULL DEFAULT '',
                    tracking_error TEXT NOT NULL DEFAULT '',
                    tracking_raw TEXT NOT NULL DEFAULT '',
                    shipping_note TEXT NOT NULL DEFAULT '',
                    shipped_at TEXT NOT NULL DEFAULT '',
                    booking_status TEXT NOT NULL DEFAULT '未下单',
                    booking_task_id TEXT NOT NULL DEFAULT '',
                    booking_order_id TEXT NOT NULL DEFAULT '',
                    booking_request_id TEXT NOT NULL DEFAULT '',
                    booking_poll_token TEXT NOT NULL DEFAULT '',
                    booking_salt TEXT NOT NULL DEFAULT '',
                    booking_error TEXT NOT NULL DEFAULT '',
                    booking_carrier_status TEXT NOT NULL DEFAULT '',
                    booking_raw TEXT NOT NULL DEFAULT '',
                    booking_requested_at TEXT NOT NULL DEFAULT '',
                    booking_updated_at TEXT NOT NULL DEFAULT '',
                    booking_courier_name TEXT NOT NULL DEFAULT '',
                    booking_courier_mobile TEXT NOT NULL DEFAULT '',
                    booking_weight TEXT NOT NULL DEFAULT '',
                    booking_freight TEXT NOT NULL DEFAULT '',
                    pickup_day TEXT NOT NULL DEFAULT '',
                    pickup_start_time TEXT NOT NULL DEFAULT '',
                    pickup_end_time TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (order_date, store_id, store_order_no),
                    FOREIGN KEY (store_id) REFERENCES stores(id),
                    FOREIGN KEY (created_by) REFERENCES users(id)
                );
                """
            )
            order_date_expr = old("order_date", "substr(created_at, 1, 10)")
            business_expr = old(
                "business_id",
                "replace(substr(created_at, 1, 10), '-', '') || '-S' || printf('%02d', store_id) || '-' || store_order_no",
            )
            names = [
                "id", "order_date", "business_id", "store_id", "store_name_snapshot", "created_by",
                "recipient_name", "phone", "address", "store_order_no", "remark", "status",
                "express_company", "tracking_no", "tracking_provider", "tracking_status",
                "tracking_state_code", "tracking_last_event", "tracking_last_checked_at", "tracking_signed_at",
                "tracking_error", "tracking_raw", "shipping_note", "shipped_at", "booking_status",
                "booking_task_id", "booking_order_id", "booking_request_id", "booking_poll_token",
                "booking_salt", "booking_error", "booking_carrier_status", "booking_raw",
                "booking_requested_at", "booking_updated_at", "booking_courier_name",
                "booking_courier_mobile", "booking_weight", "booking_freight", "pickup_day",
                "pickup_start_time", "pickup_end_time", "created_at", "updated_at",
            ]
            defaults = {
                "order_date": order_date_expr,
                "business_id": business_expr,
                "tracking_provider": "''", "tracking_status": "''", "tracking_state_code": "''",
                "tracking_last_event": "''", "tracking_last_checked_at": "''", "tracking_signed_at": "''",
                "tracking_error": "''", "tracking_raw": "''", "booking_status": "'未下单'",
                "booking_task_id": "''", "booking_order_id": "''", "booking_request_id": "''",
                "booking_poll_token": "''", "booking_salt": "''", "booking_error": "''",
                "booking_carrier_status": "''", "booking_raw": "''", "booking_requested_at": "''",
                "booking_updated_at": "''", "booking_courier_name": "''", "booking_courier_mobile": "''",
                "booking_weight": "''", "booking_freight": "''", "pickup_day": "''",
                "pickup_start_time": "''", "pickup_end_time": "''",
            }
            select_parts = [defaults.get(name, old(name, "''")) for name in names]
            conn.execute(
                f"INSERT INTO shipments_new ({', '.join(names)}) SELECT {', '.join(select_parts)} FROM shipments"
            )
            conn.execute("DROP TABLE shipments")
            conn.execute("ALTER TABLE shipments_new RENAME TO shipments")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if violations or integrity != "ok":
                raise RuntimeError("发货表迁移完整性检查失败。")
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _backup_before_migration(self) -> None:
        source_path = Path(self.path)
        if not source_path.exists() or source_path.stat().st_size == 0:
            return
        backup_dir = source_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(APP_TZ).strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"{source_path.stem}-before-order-date-{timestamp}.db"
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    def _seed_defaults(self, conn: sqlite3.Connection, *, production: bool, admin_password: str) -> None:
        now = now_text()
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        store_count = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]

        if production:
            if user_count == 0:
                if not admin_password or len(admin_password) < 8:
                    raise AppError("生产环境首次启动必须设置至少 8 位的 SCENTPOOL_ADMIN_PASSWORD，或迁移已重置密码的数据库。", 500)
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

    def _ensure_shipment_tracking_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(shipments)").fetchall()}
        columns = {
            "tracking_provider": "TEXT NOT NULL DEFAULT ''",
            "tracking_status": "TEXT NOT NULL DEFAULT ''",
            "tracking_state_code": "TEXT NOT NULL DEFAULT ''",
            "tracking_last_event": "TEXT NOT NULL DEFAULT ''",
            "tracking_last_checked_at": "TEXT NOT NULL DEFAULT ''",
            "tracking_signed_at": "TEXT NOT NULL DEFAULT ''",
            "tracking_error": "TEXT NOT NULL DEFAULT ''",
            "tracking_raw": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE shipments ADD COLUMN {name} {definition}")

    def _ensure_return_tracking_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(return_orders)").fetchall()}
        columns = {
            "express_company_code": "TEXT NOT NULL DEFAULT ''",
            "express_company_source": "TEXT NOT NULL DEFAULT 'manual'",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE return_orders ADD COLUMN {name} {definition}")
        conn.execute(
            """
            UPDATE return_orders
            SET express_company_code = CASE express_company
                WHEN '圆通' THEN 'yuantong'
                WHEN '顺丰' THEN 'shunfeng'
                WHEN '京东' THEN 'jd'
                ELSE express_company_code
            END
            WHERE express_company_code = '' AND express_company IN ('圆通', '顺丰', '京东')
            """
        )

    def _normalize_shipments_with_tracking(self, conn: sqlite3.Connection) -> None:
        now = now_text()
        conn.execute(
            """
            UPDATE shipments
            SET status = '已发货',
                express_company = CASE WHEN express_company = '' THEN ? ELSE express_company END,
                shipped_at = CASE WHEN shipped_at = '' THEN updated_at ELSE shipped_at END,
                tracking_status = CASE WHEN tracking_status = '' THEN '待查询' ELSE tracking_status END,
                updated_at = ?
            WHERE status = '待处理' AND tracking_no <> ''
            """,
            (DEFAULT_EXPRESS_COMPANY, now),
        )
        conn.execute(
            """
            UPDATE shipments
            SET tracking_status = '等待揽收', tracking_error = ''
            WHERE tracking_status = '查询失败'
              AND (
                tracking_error LIKE '%查询无结果%' OR tracking_error LIKE '%暂无轨迹%'
                OR tracking_error LIKE '%暂无物流%' OR tracking_error LIKE '%未查询到物流%'
                OR tracking_error LIKE '%没有物流信息%'
              )
            """
        )

    def _ensure_shipping_batch_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(shipping_batch_items)").fetchall()}
        columns = {
            "request_id": "TEXT NOT NULL DEFAULT ''",
            "callback_salt": "TEXT NOT NULL DEFAULT ''",
            "task_id": "TEXT NOT NULL DEFAULT ''",
            "order_id": "TEXT NOT NULL DEFAULT ''",
            "cancel_param_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE shipping_batch_items ADD COLUMN {name} {definition}")

    def _ensure_label_columns(self, conn: sqlite3.Connection) -> None:
        shipment_existing = {row["name"] for row in conn.execute("PRAGMA table_info(shipments)").fetchall()}
        shipment_columns = {
            "label_url": "TEXT NOT NULL DEFAULT ''",
            "label_print_status": "TEXT NOT NULL DEFAULT ''",
            "label_print_error": "TEXT NOT NULL DEFAULT ''",
            "label_print_type": "TEXT NOT NULL DEFAULT ''",
            "label_carrier_order_no": "TEXT NOT NULL DEFAULT ''",
            "label_child_no": "TEXT NOT NULL DEFAULT ''",
            "label_return_no": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in shipment_columns.items():
            if name not in shipment_existing:
                conn.execute(f"ALTER TABLE shipments ADD COLUMN {name} {definition}")

        settings_existing = {row["name"] for row in conn.execute("PRAGMA table_info(shipping_settings)").fetchall()}
        settings_columns = {
            "sender_company": "TEXT NOT NULL DEFAULT '万物香铺'",
            "pay_type": "TEXT NOT NULL DEFAULT 'MONTHLY'",
            "print_mode": "TEXT NOT NULL DEFAULT 'PDF'",
            "printer_siid": "TEXT NOT NULL DEFAULT ''",
            "template_id": "TEXT NOT NULL DEFAULT ''",
            "paper_width": "TEXT NOT NULL DEFAULT '100'",
            "paper_height": "TEXT NOT NULL DEFAULT '180'",
            "need_desensitization": "INTEGER NOT NULL DEFAULT 0",
            "need_logo": "INTEGER NOT NULL DEFAULT 0",
            "partner_id": "TEXT NOT NULL DEFAULT ''",
            "partner_key": "TEXT NOT NULL DEFAULT ''",
            "partner_secret": "TEXT NOT NULL DEFAULT ''",
            "partner_name": "TEXT NOT NULL DEFAULT ''",
            "partner_net": "TEXT NOT NULL DEFAULT ''",
            "partner_code": "TEXT NOT NULL DEFAULT ''",
            "partner_check_man": "TEXT NOT NULL DEFAULT ''",
            "carrier_settings_json": "TEXT NOT NULL DEFAULT '{}'",
            "branch_options_json": "TEXT NOT NULL DEFAULT '[]'",
            "authorized_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in settings_columns.items():
            if name not in settings_existing:
                conn.execute(f"ALTER TABLE shipping_settings ADD COLUMN {name} {definition}")

    def database_summary(self) -> Dict[str, int]:
        with self.connect() as conn:
            return {
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "stores": conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0],
                "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
                "returns": conn.execute("SELECT COUNT(*) FROM return_orders").fetchone()[0],
            }

    def health_check(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def storage_diagnostics(self) -> Dict[str, Any]:
        database_path = Path(self.path)
        table_names = (
            "stores",
            "users",
            "sessions",
            "products",
            "shipments",
            "shipment_items",
            "shipping_batches",
            "shipping_batch_items",
            "shipping_callback_events",
            "task_alert_acknowledgements",
            "operational_incidents",
            "audit_event_meta",
            "audit_state_events",
            "return_orders",
            "return_items",
        )
        with self.connect() as conn:
            table_counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in table_names
            }
            raw_sizes = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(LENGTH(tracking_raw)), 0) AS tracking_raw_bytes,
                        COALESCE(SUM(LENGTH(booking_raw)), 0) AS booking_raw_bytes,
                        COALESCE(MAX(LENGTH(tracking_raw)), 0) AS largest_tracking_raw_bytes,
                        COALESCE(MAX(LENGTH(booking_raw)), 0) AS largest_booking_raw_bytes
                    FROM shipments
                    """
                ).fetchone()
            )
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        return {
            "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
            "wal_bytes": Path(f"{self.path}-wal").stat().st_size if Path(f"{self.path}-wal").exists() else 0,
            "page_count": page_count,
            "page_size": page_size,
            "freelist_count": freelist_count,
            "journal_mode": journal_mode,
            "table_counts": table_counts,
            "raw_sizes": {key: int(value or 0) for key, value in raw_sizes.items()},
        }

    def record_operational_incident(
        self,
        incident_key: str,
        *,
        category: str,
        incident_type: str,
        status: str,
        message: str,
        affected_count: int = 0,
    ) -> None:
        """Persist one system-level incident without order or recipient data."""
        normalized_key = str(incident_key or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{2,95}", normalized_key):
            raise AppError("系统异常标识无效。")
        now = now_text()
        normalized_count = max(0, int(affected_count or 0))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO operational_incidents (
                    incident_key, category, incident_type, status, message, affected_count,
                    first_seen_at, last_seen_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(incident_key) DO UPDATE SET
                    category = excluded.category,
                    incident_type = excluded.incident_type,
                    status = excluded.status,
                    message = excluded.message,
                    affected_count = CASE
                        WHEN operational_incidents.resolved_at <> '' THEN excluded.affected_count
                        ELSE MAX(operational_incidents.affected_count, excluded.affected_count)
                    END,
                    first_seen_at = CASE
                        WHEN operational_incidents.resolved_at <> '' THEN excluded.first_seen_at
                        ELSE operational_incidents.first_seen_at
                    END,
                    last_seen_at = excluded.last_seen_at,
                    resolved_at = ''
                """,
                (
                    normalized_key,
                    str(category or "其他")[:40],
                    str(incident_type or "系统服务异常")[:80],
                    str(status or "系统检查中")[:40],
                    str(message or "系统服务暂时不可用。")[:500],
                    normalized_count,
                    now,
                    now,
                ),
            )

    def clear_operational_incident(
        self,
        incident_key: str,
    ) -> None:
        """Resolve only the system-level incident; business records are never bulk-mutated."""
        normalized_key = str(incident_key or "").strip().lower()
        now = now_text()
        with self.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM operational_incidents WHERE incident_key = ? AND resolved_at = ''",
                (normalized_key,),
            ).fetchone()
            if not active:
                return
            conn.execute(
                """
                UPDATE operational_incidents
                SET resolved_at = ?
                WHERE incident_key = ? AND resolved_at = ''
                """,
                (now, normalized_key),
            )

    def task_alerts(self, limit: int = 50) -> Dict[str, Any]:
        """Return current operational failures without recipient personal data."""
        stale_before = (datetime.now(APP_TZ) - timedelta(minutes=30)).isoformat(timespec="seconds")
        items: List[Dict[str, Any]] = []
        tracking_service_failures: List[Dict[str, str]] = []

        def is_tracking_service_failure(message: str) -> bool:
            normalized = str(message or "")
            return bool(
                re.search(
                    r"快递100请求失败：|快递100接口.*(?:拒绝访问|请求过多|不可用|无法连接)"
                    r"|快递100返回(?:内容|格式).*(?:异常|有效 JSON)",
                    normalized,
                )
            )

        def classify(alert_type: str, message: str) -> tuple[str, str, bool]:
            normalized = str(message or "")
            if alert_type == "面单下单失败":
                if re.search(r"固话|手机|电话|号码|格式", normalized):
                    return "电子面单", "联系方式格式", False
                if re.search(r"停发|暂停服务|超区|服务范围|不派送|无法配送|内部整顿", normalized):
                    return "电子面单", "停发或超范围", False
                if re.search(r"行政区|省市区|地址.*解析|地址.*格式", normalized):
                    return "电子面单", "地址信息", False
                if re.search(r"余额|欠费|单量|数量不足|授权|网点|账号|账户|配置", normalized):
                    return "电子面单", "授权或余额", False
                if re.search(r"网络|超时|服务暂时|繁忙|限流|自动重试", normalized):
                    return "电子面单", "网络或平台", False
                return "电子面单", "其他下单问题", False
            if alert_type == "面单等待过久":
                return "电子面单", "任务等待过久", True
            if alert_type == "面单打印失败":
                return "打印", "打印设备或文件", False
            if alert_type in {"物流查询失败", "退货物流查询失败"}:
                if re.search(r"查询无结果|暂无轨迹|暂无物流|未查询到|没有物流", normalized):
                    return "物流查询", "暂未查到物流", True
                if re.search(r"识别|单号|快递公司|不支持", normalized):
                    return "物流查询", "单号或快递公司", True
                if re.search(r"网络|超时|服务|繁忙|限流", normalized):
                    return "物流查询", "查询服务异常", True
                return "物流查询", "其他物流问题", True
            return "其他", "其他问题", False

        def add_alert(
            row: sqlite3.Row,
            alert_key: str,
            alert_type: str,
            status: str,
            message: str,
            updated_at: str,
            *,
            batch_id: Any = None,
        ) -> None:
            category, reason, auto_retry = classify(alert_type, message)
            fingerprint_source = "\x00".join((alert_key, alert_type, status, message, updated_at))
            items.append(
                {
                    "alert_key": alert_key,
                    "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
                    "type": alert_type,
                    "category": category,
                    "reason": reason,
                    "auto_retry": auto_retry,
                    "record_id": int(row["id"]),
                    "business_id": str(row["business_id"]),
                    "store_name": str(row["store_name"]),
                    "status": status,
                    "message": message,
                    "updated_at": updated_at,
                    "batch_id": int(batch_id) if batch_id else None,
                }
            )

        with self.connect() as conn:
            shipment_rows = conn.execute(
                """
                SELECT
                    shipments.id,
                    shipments.business_id,
                    shipments.store_name_snapshot AS store_name,
                    shipments.booking_status,
                    shipments.booking_error,
                    shipments.booking_updated_at,
                    shipments.tracking_status,
                    shipments.tracking_error,
                    shipments.tracking_last_checked_at,
                    shipments.label_print_status,
                    shipments.label_print_error,
                    shipments.updated_at,
                    (
                        SELECT shipping_batch_items.batch_id
                        FROM shipping_batch_items
                        WHERE shipping_batch_items.shipment_id = shipments.id
                        ORDER BY shipping_batch_items.id DESC
                        LIMIT 1
                    ) AS latest_batch_id
                FROM shipments
                WHERE
                    shipments.booking_status = '下单失败'
                    OR (
                        shipments.booking_status IN ('排队中', '提交中')
                        AND shipments.booking_updated_at <> ''
                        AND shipments.booking_updated_at < ?
                    )
                    OR shipments.tracking_status = '查询失败'
                    OR shipments.label_print_status = '打印失败'
                """,
                (stale_before,),
            ).fetchall()
            return_rows = conn.execute(
                """
                SELECT
                    return_orders.id,
                    'RETURN-' || return_orders.id AS business_id,
                    return_orders.store_name_snapshot AS store_name,
                    return_orders.tracking_status,
                    return_orders.tracking_error,
                    return_orders.tracking_last_checked_at,
                    return_orders.updated_at
                FROM return_orders
                WHERE return_orders.tracking_status = '查询失败'
                """
            ).fetchall()
            acknowledged = {
                str(row["alert_key"]): str(row["source_fingerprint"])
                for row in conn.execute(
                    "SELECT alert_key, source_fingerprint FROM task_alert_acknowledgements"
                ).fetchall()
            }
            operational_incidents = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT incident_key, category, incident_type, status, message, affected_count,
                           first_seen_at, last_seen_at
                    FROM operational_incidents
                    WHERE resolved_at = ''
                    ORDER BY last_seen_at DESC
                    """
                ).fetchall()
            ]

        for row in shipment_rows:
            if row["booking_status"] == "下单失败":
                add_alert(
                    row,
                    f"shipment:{int(row['id'])}:booking",
                    "面单下单失败",
                    "需要处理",
                    str(row["booking_error"] or "电子面单没有取得快递单号。"),
                    str(row["booking_updated_at"] or row["updated_at"]),
                    batch_id=row["latest_batch_id"],
                )
            elif (
                row["booking_status"] in BOOKING_ACTIVE_STATUSES
                and str(row["booking_updated_at"] or "")
                and str(row["booking_updated_at"]) < stale_before
            ):
                add_alert(
                    row,
                    f"shipment:{int(row['id'])}:booking_wait",
                    "面单等待过久",
                    "等待超过30分钟",
                    "电子面单任务长时间没有完成，系统会自动尝试恢复；刷新后仍未变化请联系管理员。",
                    str(row["booking_updated_at"] or row["updated_at"]),
                    batch_id=row["latest_batch_id"],
                )
            if row["tracking_status"] == "查询失败":
                tracking_message = str(row["tracking_error"] or "暂时没有取得物流信息。")
                if is_tracking_service_failure(tracking_message):
                    tracking_service_failures.append(
                        {
                            "message": tracking_message,
                            "updated_at": str(row["tracking_last_checked_at"] or row["updated_at"]),
                        }
                    )
                else:
                    add_alert(
                        row,
                        f"shipment:{int(row['id'])}:tracking",
                        "物流查询失败",
                        "稍后重试",
                        tracking_message,
                        str(row["tracking_last_checked_at"] or row["updated_at"]),
                    )
            if row["label_print_status"] == "打印失败":
                add_alert(
                    row,
                    f"shipment:{int(row['id'])}:print",
                    "面单打印失败",
                    "需要重新打印",
                    str(row["label_print_error"] or "打印机没有确认打印成功。"),
                    str(row["updated_at"]),
                )

        for row in return_rows:
            tracking_message = str(row["tracking_error"] or "暂时没有取得退货物流信息。")
            if is_tracking_service_failure(tracking_message):
                tracking_service_failures.append(
                    {
                        "message": tracking_message,
                        "updated_at": str(row["tracking_last_checked_at"] or row["updated_at"]),
                    }
                )
            else:
                add_alert(
                    row,
                    f"return:{int(row['id'])}:tracking",
                    "退货物流查询失败",
                    "稍后重试",
                    tracking_message,
                    str(row["tracking_last_checked_at"] or row["updated_at"]),
                )

        tracking_incident = next(
            (
                incident
                for incident in operational_incidents
                if str(incident.get("incident_key") or "") == "tracking:kuaidi100"
            ),
            None,
        )
        if tracking_incident or tracking_service_failures:
            legacy_count = len(tracking_service_failures)
            latest_legacy = max(
                (str(item.get("updated_at") or "") for item in tracking_service_failures),
                default="",
            )
            updated_at = str((tracking_incident or {}).get("last_seen_at") or latest_legacy or now_text())
            affected_count = max(
                legacy_count,
                int((tracking_incident or {}).get("affected_count") or 0),
            )
            message = str(
                (tracking_incident or {}).get("message")
                or "此前快递100接口层查询失败。系统已把同一故障合并显示，并会按每个单号原定节奏重新查询。"
            )
            alert_key = "system:tracking:kuaidi100"
            fingerprint_source = "\x00".join((alert_key, message, updated_at, str(affected_count)))
            items.append(
                {
                    "alert_key": alert_key,
                    "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
                    "type": "物流服务异常",
                    "category": "物流查询",
                    "reason": "快递100接口异常",
                    "auto_retry": True,
                    "system_scope": True,
                    "incident_active": bool(tracking_incident),
                    "record_id": None,
                    "business_id": "快递100批量查询",
                    "store_name": "全部门店",
                    "status": str((tracking_incident or {}).get("status") or "等待下轮验证"),
                    "message": message,
                    "updated_at": updated_at,
                    "batch_id": None,
                    "affected_count": affected_count,
                }
            )

        for incident in operational_incidents:
            if str(incident.get("incident_key") or "") == "tracking:kuaidi100":
                continue
            incident_key = str(incident.get("incident_key") or "")
            updated_at = str(incident.get("last_seen_at") or now_text())
            message = str(incident.get("message") or "系统服务暂时不可用。")
            alert_key = f"system:{incident_key}"
            fingerprint_source = "\x00".join((alert_key, message, updated_at))
            items.append(
                {
                    "alert_key": alert_key,
                    "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
                    "type": str(incident.get("incident_type") or "系统服务异常"),
                    "category": str(incident.get("category") or "其他"),
                    "reason": "系统服务异常",
                    "auto_retry": True,
                    "system_scope": True,
                    "incident_active": True,
                    "record_id": None,
                    "business_id": "系统服务",
                    "store_name": "全部门店",
                    "status": str(incident.get("status") or "系统检查中"),
                    "message": message,
                    "updated_at": updated_at,
                    "batch_id": None,
                    "affected_count": int(incident.get("affected_count") or 0),
                }
            )

        items = [
            item for item in items
            if acknowledged.get(str(item["alert_key"])) != str(item["fingerprint"])
        ]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        for item in items:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
            category = str(item["category"])
            category_counts[category] = category_counts.get(category, 0) + 1
        counts["total"] = len(items)
        visible_limit = max(1, min(int(limit or 50), 100))
        visible_items = items[:visible_limit]
        category_items = {
            category: [item for item in items if item["category"] == category][:visible_limit]
            for category in category_counts
        }
        return {
            "counts": counts,
            "category_counts": category_counts,
            "items": visible_items,
            "category_items": category_items,
            "affected_total": sum(max(1, int(item.get("affected_count") or 0)) for item in items),
            "stale_after_minutes": 30,
            "generated_at": now_text(),
        }

    def resolve_task_alert(
        self,
        alert_key: str,
        source_fingerprint: str,
        resolved_by: int,
    ) -> Dict[str, Any]:
        """Acknowledge one exact alert occurrence without mutating its business record."""
        normalized_key = str(alert_key or "").strip()
        normalized_fingerprint = str(source_fingerprint or "").strip().lower()
        if not re.fullmatch(r"(?:shipment|return):[1-9][0-9]*:[a-z_]+", normalized_key):
            raise AppError("异常提醒标识无效。")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_fingerprint):
            raise AppError("异常提醒版本无效。")

        alert_payload = self.task_alerts(limit=100)
        candidate_items = list(alert_payload["items"])
        for category_rows in alert_payload.get("category_items", {}).values():
            candidate_items.extend(category_rows)
        current = next(
            (
                item for item in candidate_items
                if item["alert_key"] == normalized_key
                and item["fingerprint"] == normalized_fingerprint
            ),
            None,
        )
        if not current:
            raise AppError("这条异常已经恢复或发生变化，请刷新后再确认。", 409)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_alert_acknowledgements (
                    alert_key, alert_type, record_id, source_fingerprint, resolved_by, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    alert_type = excluded.alert_type,
                    record_id = excluded.record_id,
                    source_fingerprint = excluded.source_fingerprint,
                    resolved_by = excluded.resolved_by,
                    resolved_at = excluded.resolved_at
                """,
                (
                    normalized_key,
                    str(current["type"]),
                    int(current["record_id"]),
                    normalized_fingerprint,
                    int(resolved_by),
                    now_text(),
                ),
            )
        return self.task_alerts()

    @staticmethod
    def _audit_metric(count: Optional[int], completeness: str, limitations: List[str]) -> Dict[str, Any]:
        return {
            "count": None if count is None else int(count),
            "completeness": completeness,
            "limitations": limitations,
        }

    def _daily_audit_history(
        self,
        conn: sqlite3.Connection,
        *,
        date_text: str,
        day_start: str,
        day_end: str,
        pending_24_cutoff: str,
        waiting_30_cutoff: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        coverage = conn.execute(
            "SELECT started_at, full_day_coverage_from, schema_version FROM audit_event_meta WHERE id = 1"
        ).fetchone()
        if not coverage:
            raise sqlite3.OperationalError("audit history metadata is unavailable")
        coverage_available = date_text >= str(coverage["full_day_coverage_from"])
        requested_day_finished = datetime.fromisoformat(day_end) <= datetime.now(APP_TZ)
        history_complete = coverage_available and requested_day_finished
        if history_complete:
            row = dict(
                conn.execute(
                    """
                    WITH latest_shipment_state AS (
                        SELECT entity_id, state, entity_created_at
                        FROM (
                            SELECT entity_id, state, entity_created_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY entity_id
                                    ORDER BY julianday(occurred_at) DESC, id DESC
                                ) AS position
                            FROM audit_state_events
                            WHERE domain = 'shipment_state'
                                AND entity_kind = 'shipment'
                                AND julianday(occurred_at) < julianday(?)
                        )
                        WHERE position = 1
                    ),
                    latest_label_state AS (
                        SELECT entity_id, state, occurred_at
                        FROM (
                            SELECT entity_id, state, occurred_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY entity_id
                                    ORDER BY julianday(occurred_at) DESC, id DESC
                                ) AS position
                            FROM audit_state_events
                            WHERE domain = 'label'
                                AND entity_kind = 'shipment'
                                AND julianday(occurred_at) < julianday(?)
                        )
                        WHERE position = 1
                    ),
                    latest_print_state AS (
                        SELECT entity_id, state, occurred_at
                        FROM (
                            SELECT entity_id, state, occurred_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY entity_id
                                    ORDER BY julianday(occurred_at) DESC, id DESC
                                ) AS position
                            FROM audit_state_events
                            WHERE domain = 'printing'
                                AND entity_kind = 'shipment'
                                AND julianday(occurred_at) < julianday(?)
                        )
                        WHERE position = 1
                    )
                    SELECT
                        (SELECT COUNT(*) FROM latest_shipment_state
                            WHERE state NOT IN ('shipped', 'signed', 'cancelled', 'deleted')
                                AND julianday(entity_created_at) >= julianday(?)
                                AND julianday(entity_created_at) < julianday(?)) AS new_unshipped,
                        (SELECT COUNT(*) FROM latest_shipment_state
                            WHERE state = 'shipped') AS shipped_not_signed,
                        (SELECT COUNT(*) FROM latest_shipment_state
                            WHERE state = 'pending'
                                AND julianday(entity_created_at) < julianday(?)) AS pending_over_24_hours,
                        (SELECT COUNT(*) FROM latest_label_state
                            WHERE state IN ('queued', 'submitting')
                                AND julianday(occurred_at) < julianday(?)) AS label_over_30_minutes,
                        (SELECT COUNT(*) FROM latest_print_state
                            WHERE state = 'processing'
                                AND julianday(occurred_at) < julianday(?)) AS printing_over_30_minutes
                    """,
                    (
                        day_end,
                        day_end,
                        day_end,
                        day_start,
                        day_end,
                        pending_24_cutoff,
                        waiting_30_cutoff,
                        waiting_30_cutoff,
                    ),
                ).fetchone()
            )
            completeness = "complete_append_only_events"
            limitations: List[str] = []
        else:
            row = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE
                            WHEN julianday(created_at) >= julianday(?)
                                AND julianday(created_at) < julianday(?)
                                AND status <> '已取消'
                                AND (shipped_at = '' OR julianday(shipped_at) >= julianday(?))
                            THEN 1 ELSE 0 END), 0) AS new_unshipped,
                        COALESCE(SUM(CASE
                            WHEN shipped_at <> ''
                                AND julianday(shipped_at) < julianday(?)
                                AND (tracking_signed_at = '' OR julianday(tracking_signed_at) >= julianday(?))
                            THEN 1 ELSE 0 END), 0) AS shipped_not_signed,
                        COALESCE(SUM(CASE
                            WHEN status = '待处理'
                                AND julianday(created_at) < julianday(?)
                                AND (shipped_at = '' OR julianday(shipped_at) >= julianday(?))
                            THEN 1 ELSE 0 END), 0) AS pending_over_24_hours,
                        COALESCE(SUM(CASE
                            WHEN booking_status IN ('排队中', '提交中')
                                AND booking_updated_at <> ''
                                AND julianday(booking_updated_at) < julianday(?)
                            THEN 1 ELSE 0 END), 0) AS label_over_30_minutes,
                        COALESCE(SUM(CASE
                            WHEN label_print_status = '打印中'
                                AND julianday(updated_at) < julianday(?)
                            THEN 1 ELSE 0 END), 0) AS printing_over_30_minutes
                    FROM shipments
                    """,
                    (
                        day_start,
                        day_end,
                        day_end,
                        day_end,
                        day_end,
                        pending_24_cutoff,
                        day_end,
                        waiting_30_cutoff,
                        waiting_30_cutoff,
                    ),
                ).fetchone()
            )
            completeness = "partial_current_fields"
            limitations = []
            if not coverage_available:
                limitations.append("state_changes_before_full_day_event_coverage_cannot_be_reconstructed")
            if not requested_day_finished:
                limitations.append("requested_day_has_not_ended")
            limitations.extend(
                [
                    "later_cancellation_or_status_overwrite_can_change_observed_counts",
                    "historical_print_start_time_was_not_stored_before_event_coverage",
                ]
            )

        report = {
            "new_shipments_unshipped_at_day_end": self._audit_metric(
                row["new_unshipped"], completeness, limitations
            ),
            "shipped_not_signed_at_day_end": self._audit_metric(
                row["shipped_not_signed"], completeness, limitations
            ),
            "pending_over_24_hours_at_day_end": self._audit_metric(
                row["pending_over_24_hours"], completeness, limitations
            ),
            "label_queued_or_submitting_over_30_minutes_at_day_end": self._audit_metric(
                row["label_over_30_minutes"], completeness, limitations
            ),
            "printing_processing_over_30_minutes_at_day_end": self._audit_metric(
                row["printing_over_30_minutes"], completeness, limitations
            ),
        }
        completeness_report = {
            "schema_version": int(coverage["schema_version"]),
            "full_day_coverage_from": str(coverage["full_day_coverage_from"]),
            "requested_day": "complete" if history_complete else "partial",
            "limitations": limitations,
        }
        return report, completeness_report

    @staticmethod
    def _failure_categories(categories: Iterable[str]) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        for value in categories:
            category = value if value in AUDIT_FAILURE_ACTION_HINTS else "insufficient_historical_evidence"
            counts[category] = counts.get(category, 0) + 1
        return [
            {
                "reason_category": category,
                "count": counts[category],
                "action_hint": AUDIT_FAILURE_ACTION_HINTS[category],
            }
            for category in AUDIT_FAILURE_ACTION_HINTS
            if counts.get(category)
        ]

    def _daily_audit_failures(
        self,
        conn: sqlite3.Connection,
        *,
        day_start: str,
        day_end: str,
        history_complete: bool,
    ) -> Dict[str, Any]:
        current_rows = [
            dict(row)
            for row in conn.execute(
                """
                WITH current_failures AS (
                    SELECT 'label' AS domain, 'shipment' AS entity_kind, id AS entity_id,
                        booking_updated_at AS failure_at, booking_error AS message
                    FROM shipments WHERE booking_status = '下单失败'
                    UNION ALL
                    SELECT 'tracking', 'shipment', id, tracking_last_checked_at, tracking_error
                    FROM shipments WHERE tracking_status = '查询失败'
                    UNION ALL
                    SELECT 'tracking', 'return', id, tracking_last_checked_at, tracking_error
                    FROM return_orders WHERE tracking_status = '查询失败'
                    UNION ALL
                    SELECT 'printing', 'shipment', id, updated_at, label_print_error
                    FROM shipments WHERE label_print_status = '打印失败'
                )
                SELECT *, CASE
                    WHEN failure_at = '' OR julianday(failure_at) IS NULL THEN 'unknown'
                    WHEN julianday(failure_at) < julianday(?) THEN 'historical'
                    WHEN julianday(failure_at) < julianday(?) THEN 'on_date'
                    ELSE 'after_date'
                END AS time_bucket
                FROM current_failures
                """,
                (day_start, day_end),
            ).fetchall()
        ]
        for row in current_rows:
            row["reason_category"] = classify_audit_failure(str(row["message"] or ""))

        if history_complete:
            new_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT CASE domain WHEN 'label' THEN 'label'
                            WHEN 'tracking' THEN 'tracking'
                            WHEN 'printing' THEN 'printing' END AS domain,
                        reason_category
                    FROM audit_state_events
                    WHERE domain IN ('label', 'tracking', 'printing')
                        AND state = 'failed'
                        AND evidence = 'observed'
                        AND julianday(occurred_at) >= julianday(?)
                        AND julianday(occurred_at) < julianday(?)
                    """,
                    (day_start, day_end),
                ).fetchall()
            ]
            new_completeness = "complete_append_only_events"
            limitations: List[str] = []
        else:
            new_rows = [row for row in current_rows if row["time_bucket"] == "on_date"]
            new_completeness = "partial_current_failures_only"
            limitations = ["failures_recovered_or_overwritten_before_event_coverage_are_not_recoverable"]

        result: Dict[str, Any] = {}
        for domain in ("label", "tracking", "printing"):
            domain_current = [row for row in current_rows if row["domain"] == domain]
            domain_new = [row for row in new_rows if row["domain"] == domain]
            historical = [row for row in domain_current if row["time_bucket"] == "historical"]
            unknown = [row for row in domain_current if row["time_bucket"] == "unknown"]
            result[domain] = {
                "new_on_date": {
                    "count": len(domain_new),
                    "completeness": new_completeness,
                    "categories": self._failure_categories(
                        str(row.get("reason_category") or "") for row in domain_new
                    ),
                    "limitations": limitations,
                },
                "unresolved_at_collection": {
                    "count": len(domain_current),
                    "categories": self._failure_categories(
                        str(row.get("reason_category") or "") for row in domain_current
                    ),
                },
                "historical_residual_before_date": {
                    "count": len(historical),
                    "categories": self._failure_categories(
                        str(row.get("reason_category") or "") for row in historical
                    ),
                    "completeness": "current_failure_timestamp_evidence",
                },
                "unresolved_with_unknown_start": {
                    "count": len(unknown),
                    "categories": self._failure_categories(
                        str(row.get("reason_category") or "") for row in unknown
                    ),
                },
            }
        return result

    def daily_audit(self, date_text: str) -> Dict[str, Any]:
        """Return aggregate-only audit metrics through an independent read-only connection."""
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(date_text or "")):
            raise AppError("日期格式不正确。")
        try:
            day_start_dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=APP_TZ)
        except ValueError as exc:
            raise AppError("日期格式不正确。") from exc

        day_end_dt = day_start_dt + timedelta(days=1)
        day_start = day_start_dt.isoformat(timespec="seconds")
        day_end = day_end_dt.isoformat(timespec="seconds")
        recent_start = (day_start_dt - timedelta(days=6)).isoformat(timespec="seconds")
        current_time = datetime.now(APP_TZ)
        stale_before = (current_time - timedelta(minutes=30)).isoformat(timespec="seconds")
        current_pending_24_cutoff = (current_time - timedelta(hours=24)).isoformat(timespec="seconds")
        pending_24_cutoff = (day_end_dt - timedelta(hours=24)).isoformat(timespec="seconds")
        waiting_30_cutoff = (day_end_dt - timedelta(minutes=30)).isoformat(timespec="seconds")

        with self.connect_readonly() as conn:
            totals = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN julianday(created_at) >= julianday(?)
                            AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS new_shipments,
                        COALESCE(SUM(CASE WHEN julianday(shipped_at) >= julianday(?)
                            AND julianday(shipped_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS shipped_shipments,
                        COALESCE(SUM(CASE WHEN julianday(tracking_signed_at) >= julianday(?)
                            AND julianday(tracking_signed_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS signed_shipments,
                        COALESCE(SUM(CASE WHEN status = '待处理'
                            AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS backlog_current_snapshot
                    FROM shipments
                    """,
                    (day_start, day_end, day_start, day_end, day_start, day_end, day_end),
                ).fetchone()
            )

            store_rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(stores.name), ''), '未知门店') AS store_name,
                    COALESCE(SUM(CASE WHEN julianday(shipments.created_at) >= julianday(?)
                        AND julianday(shipments.created_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS new_shipments,
                    COALESCE(SUM(CASE WHEN julianday(shipments.shipped_at) >= julianday(?)
                        AND julianday(shipments.shipped_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS shipped_shipments,
                    COALESCE(SUM(CASE WHEN julianday(shipments.tracking_signed_at) >= julianday(?)
                        AND julianday(shipments.tracking_signed_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS signed_shipments,
                    COALESCE(SUM(CASE WHEN shipments.status = '待处理'
                        AND julianday(shipments.created_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS backlog_current_snapshot
                FROM shipments
                LEFT JOIN stores ON stores.id = shipments.store_id
                GROUP BY shipments.store_id, store_name
                HAVING new_shipments + shipped_shipments + signed_shipments + backlog_current_snapshot > 0
                ORDER BY store_name
                """,
                (day_start, day_end, day_start, day_end, day_start, day_end, day_end),
            ).fetchall()

            recent_totals = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN julianday(created_at) >= julianday(?)
                            AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS new_shipments,
                        COALESCE(SUM(CASE WHEN julianday(shipped_at) >= julianday(?)
                            AND julianday(shipped_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS shipped_shipments,
                        COALESCE(SUM(CASE WHEN julianday(tracking_signed_at) >= julianday(?)
                            AND julianday(tracking_signed_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS signed_shipments
                    FROM shipments
                    """,
                    (recent_start, day_end, recent_start, day_end, recent_start, day_end),
                ).fetchone()
            )

            shipment_exceptions = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN booking_status = '下单失败' THEN 1 ELSE 0 END), 0) AS label_booking_failures,
                        COALESCE(SUM(CASE WHEN booking_status = '下单失败'
                            AND julianday(booking_updated_at) >= julianday(?)
                            AND julianday(booking_updated_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS label_booking_failures_on_date,
                        COALESCE(SUM(CASE WHEN tracking_status = '查询失败' THEN 1 ELSE 0 END), 0) AS shipment_tracking_failures,
                        COALESCE(SUM(CASE WHEN tracking_status = '查询失败'
                            AND julianday(tracking_last_checked_at) >= julianday(?)
                            AND julianday(tracking_last_checked_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS shipment_tracking_failures_on_date,
                        COALESCE(SUM(CASE WHEN label_print_status = '打印失败' THEN 1 ELSE 0 END), 0) AS printing_failures,
                        COALESCE(SUM(CASE WHEN label_print_status = '打印失败'
                            AND julianday(updated_at) >= julianday(?)
                            AND julianday(updated_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS printing_failures_on_date,
                        COALESCE(SUM(CASE WHEN booking_status IN ('排队中', '提交中')
                            AND booking_updated_at <> ''
                            AND julianday(booking_updated_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS long_waiting
                    FROM shipments
                    """,
                    (day_start, day_end, day_start, day_end, day_start, day_end, stale_before),
                ).fetchone()
            )
            return_exceptions = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN tracking_status = '查询失败' THEN 1 ELSE 0 END), 0) AS return_tracking_failures,
                        COALESCE(SUM(CASE WHEN tracking_status = '查询失败'
                            AND julianday(tracking_last_checked_at) >= julianday(?)
                            AND julianday(tracking_last_checked_at) < julianday(?) THEN 1 ELSE 0 END), 0) AS return_tracking_failures_on_date
                    FROM return_orders
                    """,
                    (day_start, day_end),
                ).fetchone()
            )
            current_waiting = dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN status = '待处理'
                            AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END), 0)
                            AS pending_over_24_hours,
                        COALESCE(SUM(CASE WHEN label_print_status = '打印中'
                            AND julianday(updated_at) < julianday(?) THEN 1 ELSE 0 END), 0)
                            AS printing_over_30_minutes
                    FROM shipments
                    """,
                    (current_pending_24_cutoff, stale_before),
                ).fetchone()
            )

            historical_end_of_day, completeness = self._daily_audit_history(
                conn,
                date_text=date_text,
                day_start=day_start,
                day_end=day_end,
                pending_24_cutoff=pending_24_cutoff,
                waiting_30_cutoff=waiting_30_cutoff,
            )
            failures = self._daily_audit_failures(
                conn,
                day_start=day_start,
                day_end=day_end,
                history_complete=completeness["requested_day"] == "complete",
            )

            quality_counts = {
                key: int(value or 0)
                for key, value in dict(
                    conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(CASE WHEN shipments.created_at = '' OR julianday(shipments.created_at) IS NULL THEN 1 ELSE 0 END), 0) AS invalid_created_at,
                            COALESCE(SUM(CASE WHEN shipments.shipped_at <> '' AND julianday(shipments.shipped_at) IS NULL THEN 1 ELSE 0 END), 0) AS invalid_shipped_at,
                            COALESCE(SUM(CASE WHEN shipments.tracking_signed_at <> '' AND julianday(shipments.tracking_signed_at) IS NULL THEN 1 ELSE 0 END), 0) AS invalid_tracking_signed_at,
                            COALESCE(SUM(CASE WHEN shipments.status IN ('已发货', '已签收') AND shipments.shipped_at = '' THEN 1 ELSE 0 END), 0) AS shipped_state_missing_shipped_at,
                            COALESCE(SUM(CASE WHEN shipments.status = '已签收' AND shipments.tracking_signed_at = '' THEN 1 ELSE 0 END), 0) AS signed_state_missing_tracking_signed_at,
                            COALESCE(SUM(CASE WHEN julianday(shipments.shipped_at) < julianday(shipments.created_at) THEN 1 ELSE 0 END), 0) AS shipped_before_created,
                            COALESCE(SUM(CASE WHEN julianday(shipments.tracking_signed_at) < julianday(shipments.shipped_at) THEN 1 ELSE 0 END), 0) AS signed_before_shipped,
                            COALESCE(SUM(CASE WHEN TRIM(COALESCE(stores.name, '')) = '' THEN 1 ELSE 0 END), 0) AS missing_store_name
                        FROM shipments
                        LEFT JOIN stores ON stores.id = shipments.store_id
                        """
                    ).fetchone()
                ).items()
            }

        current_tracking_failures = int(shipment_exceptions["shipment_tracking_failures"] or 0) + int(
            return_exceptions["return_tracking_failures"] or 0
        )
        dated_tracking_failures = int(shipment_exceptions["shipment_tracking_failures_on_date"] or 0) + int(
            return_exceptions["return_tracking_failures_on_date"] or 0
        )
        return {
            "date": date_text,
            "timezone": "Asia/Shanghai",
            "metrics": {key: int(value or 0) for key, value in totals.items()},
            "by_store": [
                {
                    "store_name": str(row["store_name"]),
                    "new_shipments": int(row["new_shipments"] or 0),
                    "shipped_shipments": int(row["shipped_shipments"] or 0),
                    "signed_shipments": int(row["signed_shipments"] or 0),
                    "backlog_current_snapshot": int(row["backlog_current_snapshot"] or 0),
                }
                for row in store_rows
            ],
            "exceptions": {
                "current_snapshot": {
                    "label_booking_failures": int(shipment_exceptions["label_booking_failures"] or 0),
                    "tracking_failures": current_tracking_failures,
                    "shipment_tracking_failures": int(shipment_exceptions["shipment_tracking_failures"] or 0),
                    "return_tracking_failures": int(return_exceptions["return_tracking_failures"] or 0),
                    "printing_failures": int(shipment_exceptions["printing_failures"] or 0),
                },
                "current_snapshot_updated_on_date": {
                    "label_booking_failures": int(shipment_exceptions["label_booking_failures_on_date"] or 0),
                    "tracking_failures": dated_tracking_failures,
                    "shipment_tracking_failures": int(shipment_exceptions["shipment_tracking_failures_on_date"] or 0),
                    "return_tracking_failures": int(return_exceptions["return_tracking_failures_on_date"] or 0),
                    "printing_failures": int(shipment_exceptions["printing_failures_on_date"] or 0),
                },
            },
            "historical_end_of_day": historical_end_of_day,
            "failures": failures,
            "completeness": completeness,
            "long_waiting": {
                "pending_shipments_over_24_hours_current_snapshot": int(
                    current_waiting["pending_over_24_hours"] or 0
                ),
                "label_tasks_over_30_minutes_current_snapshot": int(shipment_exceptions["long_waiting"] or 0),
                "printing_processing_over_30_minutes_current_snapshot": int(
                    current_waiting["printing_over_30_minutes"] or 0
                ),
            },
            "recent_7_day_average": {
                "calendar_days": 7,
                "new_shipments": round(int(recent_totals["new_shipments"] or 0) / 7, 2),
                "shipped_shipments": round(int(recent_totals["shipped_shipments"] or 0) / 7, 2),
                "signed_shipments": round(int(recent_totals["signed_shipments"] or 0) / 7, 2),
            },
            "data_quality": {
                "total_issues": sum(quality_counts.values()),
                **quality_counts,
            },
            "basis": {
                "event_metrics": "timestamp_events_in_shanghai_calendar_day",
                "backlog": "current_pending_snapshot_created_before_requested_day_end_not_historical",
                "exceptions": "current_failure_snapshot_not_historical_event_log",
                "historical_end_of_day": "append_only_state_events_when_complete_otherwise_labeled_observation",
                "failures": "privacy_safe_failure_categories_no_raw_messages",
                "recent_7_day_average": "seven_calendar_days_including_date_fixed_denominator_empty_is_zero",
                "data_quality": "current_snapshot_all_shipments",
                "data_quality_total": "sum_of_issue_counts_a_record_may_contribute_multiple_issues",
            },
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

    def backup_to(self, target: str | Path) -> Path:
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(str(target_path)) as destination:
            source.backup(destination)
            integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise AppError(f"数据库备份完整性校验失败：{integrity}", 500)
        return target_path

    def backup_bytes(self) -> bytes:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="scentpool-backup-") as directory:
            target = self.backup_to(Path(directory) / "scentpool.db")
            return target.read_bytes()

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
        expires = (datetime.now(APP_TZ) + timedelta(days=14)).isoformat(timespec="seconds")
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
                if datetime.fromisoformat(row["expires_at"]) < datetime.now(APP_TZ):
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

    def upsert_product(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        barcode = str(payload.get("barcode", "")).strip()
        name = str(payload.get("name", "")).strip()
        category = str(payload.get("category", "")).strip()
        spec = str(payload.get("spec", "")).strip()
        price = str(payload.get("price", "")).strip() or "0.00"
        status = str(payload.get("status", "")).strip() or "启用"

        if not barcode:
            raise AppError("请输入商品条码。")
        if not name:
            raise AppError("请输入商品名称。")
        if not category:
            raise AppError("请输入商品分类。")
        if status not in {"启用", "停用"}:
            raise AppError("请选择有效商品状态。")

        now = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO products (barcode, name, category, spec, price, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(barcode) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    spec = excluded.spec,
                    price = excluded.price,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (barcode, name, category, spec, price, status, now),
            )
            product = conn.execute("SELECT * FROM products WHERE barcode = ?", (barcode,)).fetchone()
        return dict(product)

    def delete_product(self, barcode: str) -> Dict[str, Any]:
        barcode = str(barcode or "").strip()
        if not barcode:
            raise AppError("商品条码不能为空。")
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM products WHERE barcode = ?", (barcode,))
            if cursor.rowcount == 0:
                raise AppError("商品不存在。", 404)
        return {"barcode": barcode, "deleted": True}

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
        order_date = now[:10]
        business_id = shipment_business_id(order_date, store_id, store_order_no)
        with self.connect() as conn:
            store = conn.execute(
                "SELECT * FROM stores WHERE id = ? AND active = 1", (store_id,)
            ).fetchone()
            if not store:
                raise AppError("门店不存在或已停用。")

            items = self._resolve_items(conn, raw_items)
            existing = conn.execute(
                """
                SELECT id, business_id
                FROM shipments
                WHERE order_date = ? AND store_id = ? AND store_order_no = ?
                """,
                (order_date, store_id, store_order_no),
            ).fetchone()
            if existing:
                raise AppError(
                    f"今天的门店订单号 {store_order_no} 已经提交过。",
                    409,
                    {"shipment_id": existing["id"], "business_id": existing["business_id"]},
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO shipments (
                        order_date, business_id, store_id, store_name_snapshot, created_by, recipient_name, phone, address,
                        store_order_no, remark, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '待处理', ?, ?)
                    """,
                    (
                        order_date,
                        business_id,
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
                raise AppError("同一门店同一天的订单号不能重复。", 409) from exc

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

    def update_shipment_items(self, shipment_id: int, user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list) or not raw_items:
            raise AppError("请至少选择一个货品。")

        now = now_text()
        with self.connect() as conn:
            shipment = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
            if not shipment:
                raise AppError("发货单不存在。", 404)
            if user.get("role") == "staff" and shipment["store_id"] != user.get("store_id"):
                raise AppError("无权编辑这个发货单。", 403)
            if shipment["status"] != "待处理":
                raise AppError("只有待处理订单可以编辑商品。", 409)
            if shipment["booking_status"] not in BOOKING_EDITABLE_STATUSES:
                raise AppError("快递下单处理中。如需修改，请先取消快递下单。", 409)

            items = self._resolve_items(conn, raw_items)
            conn.execute("DELETE FROM shipment_items WHERE shipment_id = ?", (shipment_id,))
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
            conn.execute("UPDATE shipments SET updated_at = ? WHERE id = ?", (now, shipment_id))
        return self.get_shipment(shipment_id, user)

    def update_shipment_remark(self, shipment_id: int, user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        remark = str(payload.get("remark") or "").strip()
        if len(remark) > 500:
            raise AppError("订单备注不能超过 500 个字符。")

        now = now_text()
        with self.connect() as conn:
            shipment = conn.execute(
                "SELECT store_id, status, booking_status FROM shipments WHERE id = ?",
                (shipment_id,),
            ).fetchone()
            if not shipment:
                raise AppError("发货单不存在。", 404)
            if user.get("role") == "staff" and shipment["store_id"] != user.get("store_id"):
                raise AppError("无权编辑这个发货单。", 403)
            if shipment["status"] != "待处理":
                raise AppError("只有尚未发货的待处理订单可以修改备注。", 409)
            if shipment["booking_status"] not in BOOKING_EDITABLE_STATUSES:
                raise AppError("快递下单处理中。如需修改，请先取消快递下单。", 409)
            conn.execute(
                "UPDATE shipments SET remark = ?, updated_at = ? WHERE id = ?",
                (remark, now, shipment_id),
            )
        return self.get_shipment(shipment_id, user)

    def _shipment_filter_sql(
        self,
        user: Dict[str, Any],
        filters: Dict[str, Any],
    ) -> tuple[List[str], List[Any]]:
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
            where.append("shipments.order_date >= ?")
            params.append(str(filters["date_from"]))
        if filters.get("date_to"):
            where.append("shipments.order_date <= ?")
            params.append(str(filters["date_to"]))
        if filters.get("q"):
            query_text = str(filters["q"]).strip()
            q = f"%{query_text}%"
            business_parts = business_search_parts(query_text)
            if business_parts:
                date_text, business_store_id, order_text = business_parts
                if business_store_id is not None:
                    where.append(
                        """
                        (
                            shipments.business_id LIKE ? OR shipments.store_order_no LIKE ? OR shipments.recipient_name LIKE ? OR
                            shipments.phone LIKE ? OR shipments.tracking_no LIKE ? OR
                            shipments.address LIKE ? OR
                            (
                                shipments.order_date = ? AND
                                shipments.store_id = ? AND
                                shipments.store_order_no LIKE ?
                            )
                        )
                        """
                    )
                    params.extend(
                        [
                            q,
                            q,
                            q,
                            q,
                            q,
                            q,
                            date_text,
                            business_store_id,
                            f"%{order_text}%",
                        ]
                    )
                else:
                    where.append(
                        """
                        (
                            shipments.business_id LIKE ? OR shipments.store_order_no LIKE ? OR shipments.recipient_name LIKE ? OR
                            shipments.phone LIKE ? OR shipments.tracking_no LIKE ? OR
                            shipments.address LIKE ? OR
                            (
                                shipments.order_date = ? AND
                                shipments.store_order_no LIKE ?
                            )
                        )
                        """
                    )
                    params.extend([q, q, q, q, q, q, date_text, f"%{order_text}%"])
            else:
                where.append(
                    """
                    (
                        shipments.business_id LIKE ? OR shipments.store_order_no LIKE ? OR shipments.recipient_name LIKE ? OR
                        shipments.phone LIKE ? OR shipments.tracking_no LIKE ? OR
                        shipments.address LIKE ?
                    )
                    """
                )
                params.extend([q, q, q, q, q, q])

        return where, params

    def shipment_status_counts(self, user: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, int]:
        where, params = self._shipment_filter_sql(user, filters)
        sql = "SELECT shipments.status, COUNT(*) AS count FROM shipments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY shipments.status"
        counts = {status: 0 for status in STATUSES}
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        counts["total"] = sum(counts.values())
        return counts

    def list_shipments(self, user: Dict[str, Any], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        where, params = self._shipment_filter_sql(user, filters)
        include_tracking_raw = (
            str(filters.get("include_tracking_raw") or "").strip() == "1"
            and bool(str(filters.get("id") or "").strip())
        )

        with self.connect() as conn:
            if self._shipment_columns is None:
                self._shipment_columns = [
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(shipments)").fetchall()
                ]
            excluded = {"booking_raw", "booking_salt", "booking_poll_token"}
            if not include_tracking_raw:
                excluded.add("tracking_raw")
            selected_columns = ", ".join(
                f"shipments.{column}"
                for column in self._shipment_columns
                if column not in excluded
            )
            sql = f"SELECT {selected_columns} FROM shipments"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY shipments.created_at DESC, shipments.id DESC"
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
            if not include_tracking_raw:
                row.pop("tracking_raw", None)
            row.pop("booking_raw", None)
            row.pop("booking_salt", None)
            row.pop("booking_poll_token", None)
        return rows

    def get_shipping_settings(self, *, public: bool = False) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM shipping_settings WHERE id = 1").fetchone()
        settings = dict(row) if row else {
            "id": 1,
            "sender_name": "",
            "sender_mobile": "",
            "sender_address": "",
            "sender_company": "万物香铺",
            "default_company": DEFAULT_EXPRESS_COMPANY,
            "cargo_name": "香氛商品",
            "pay_type": "MONTHLY",
            "print_mode": "PDF",
            "carrier_settings_json": "{}",
            "branch_options_json": "[]",
            "updated_at": "",
        }
        try:
            settings["carrier_settings"] = json.loads(settings.get("carrier_settings_json") or "{}")
        except json.JSONDecodeError:
            settings["carrier_settings"] = {}
        if not isinstance(settings["carrier_settings"], dict):
            settings["carrier_settings"] = {}
        for company in EXPRESS_COMPANIES:
            carrier = settings["carrier_settings"].get(company)
            if not isinstance(carrier, dict):
                carrier = {}
                settings["carrier_settings"][company] = carrier
            carrier.setdefault("thirdTemplateURL", DEFAULT_CAINIAO_TEMPLATE_URLS.get(company, ""))
            if not carrier.get("thirdCustomTemplateUrl"):
                carrier["thirdCustomTemplateUrl"] = DEFAULT_CAINIAO_CUSTOM_TEMPLATE_URLS.get(company, "")
        try:
            settings["branch_options"] = json.loads(settings.get("branch_options_json") or "[]")
        except json.JSONDecodeError:
            settings["branch_options"] = []
        settings["partner_authorized"] = bool(settings.get("partner_id") and settings.get("partner_key"))
        if public:
            settings["partner_id_masked"] = self._mask_value(settings.get("partner_id"))
            settings["partner_key_masked"] = self._mask_value(settings.get("partner_key"))
            for key in ("partner_id", "partner_key", "partner_secret"):
                settings.pop(key, None)
        return settings

    @staticmethod
    def _mask_value(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        if len(text) <= 6:
            return "*" * len(text)
        return f"{text[:3]}***{text[-3:]}"

    def update_shipping_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        sender_name = str(payload.get("sender_name") or "").strip()
        sender_mobile = str(payload.get("sender_mobile") or "").strip()
        sender_address = str(payload.get("sender_address") or "").strip()
        sender_company = str(payload.get("sender_company") or "万物香铺").strip()
        default_company = str(payload.get("default_company") or DEFAULT_EXPRESS_COMPANY).strip()
        cargo_name = str(payload.get("cargo_name") or "香氛商品").strip()
        pay_type = str(payload.get("pay_type") or "MONTHLY").strip()
        print_mode = str(payload.get("print_mode") or "PDF").strip().upper()
        printer_siid = str(payload.get("printer_siid") or "").strip()
        template_id = str(payload.get("template_id") or "").strip()
        paper_width = str(payload.get("paper_width") or "100").strip()
        paper_height = str(payload.get("paper_height") or "180").strip()
        carrier_settings = payload.get("carrier_settings") if isinstance(payload.get("carrier_settings"), dict) else {}
        if not sender_name:
            raise AppError("请输入总部寄件人姓名。")
        if not sender_mobile or not re_phone_ok(sender_mobile):
            raise AppError("请输入有效的总部寄件联系电话。")
        if not sender_address:
            raise AppError("请输入总部完整寄件地址。")
        if default_company not in EXPRESS_COMPANIES:
            raise AppError("请选择有效的默认快递公司。")
        if not cargo_name:
            raise AppError("请输入物品名称。")
        if pay_type not in {"SHIPPER", "MONTHLY"}:
            raise AppError("请选择寄方付或月结。")
        if print_mode not in {"PDF", "CLOUD"}:
            raise AppError("请选择 PDF 面单或快递100云打印。")
        if print_mode == "CLOUD" and not printer_siid:
            raise AppError("云打印需要填写打印设备码 siid。")
        if not paper_width.isdigit() or not paper_height.isdigit():
            raise AppError("面单纸宽度和高度必须是正整数。")
        normalized_carriers: Dict[str, Dict[str, str]] = {}
        for company in EXPRESS_COMPANIES:
            value = carrier_settings.get(company) if isinstance(carrier_settings.get(company), dict) else {}
            template_url = str(
                value.get("thirdTemplateURL") or DEFAULT_CAINIAO_TEMPLATE_URLS.get(company, "")
            ).strip()
            if template_url and not template_url.startswith("https://cloudprint.cainiao.com/template/"):
                raise AppError(f"{company}的菜鸟面单模板 URL 无效。")
            custom_template_url = str(
                value.get("thirdCustomTemplateUrl") or DEFAULT_CAINIAO_CUSTOM_TEMPLATE_URLS.get(company, "")
            ).strip()
            if custom_template_url and not custom_template_url.startswith("https://cloudprint.cainiao.com/template/"):
                raise AppError(f"{company}的菜鸟货物自定义区模板 URL 无效。")
            normalized_carriers[company] = {
                "tbNet": str(value.get("tbNet") or "").strip(),
                "expType": str(value.get("expType") or ("顺丰标快" if company == "顺丰" else "标准快递")).strip(),
                "thirdTemplateURL": template_url,
                "thirdCustomTemplateUrl": custom_template_url,
            }
        now = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shipping_settings (
                    id, sender_name, sender_mobile, sender_address, sender_company, default_company, cargo_name,
                    pay_type, print_mode, printer_siid, template_id, paper_width, paper_height,
                    need_desensitization, need_logo, carrier_settings_json, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    sender_name = excluded.sender_name,
                    sender_mobile = excluded.sender_mobile,
                    sender_address = excluded.sender_address,
                    sender_company = excluded.sender_company,
                    default_company = excluded.default_company,
                    cargo_name = excluded.cargo_name,
                    pay_type = excluded.pay_type,
                    print_mode = excluded.print_mode,
                    printer_siid = excluded.printer_siid,
                    template_id = excluded.template_id,
                    paper_width = excluded.paper_width,
                    paper_height = excluded.paper_height,
                    need_desensitization = excluded.need_desensitization,
                    need_logo = excluded.need_logo,
                    carrier_settings_json = excluded.carrier_settings_json,
                    updated_at = excluded.updated_at
                """,
                (
                    sender_name, sender_mobile, sender_address, sender_company, default_company, cargo_name,
                    pay_type, print_mode, printer_siid, template_id, paper_width, paper_height,
                    1 if payload.get("need_desensitization") else 0,
                    1 if payload.get("need_logo") else 0,
                    json.dumps(normalized_carriers, ensure_ascii=False), now,
                ),
            )
        return self.get_shipping_settings(public=True)

    def shipping_settings_for_company(self, company: str) -> Dict[str, Any]:
        settings = self.get_shipping_settings()
        carrier = settings.get("carrier_settings", {}).get(company, {})
        settings.update(
            {
                "partnerId": settings.get("partner_id", ""),
                "partnerKey": settings.get("partner_key", ""),
                "partnerSecret": settings.get("partner_secret", ""),
                "partnerName": settings.get("partner_name", ""),
                "net": settings.get("partner_net", ""),
                "code": settings.get("partner_code", ""),
                "checkMan": settings.get("partner_check_man", ""),
                "tbNet": carrier.get("tbNet", ""),
                "exp_type": carrier.get("expType", "顺丰标快" if company == "顺丰" else "标准快递"),
                "third_template_url": carrier.get("thirdTemplateURL", ""),
                "third_custom_template_url": (
                    carrier.get("thirdCustomTemplateUrl") or DEFAULT_CAINIAO_CUSTOM_TEMPLATE_URLS.get(company, "")
                ),
            }
        )
        return settings

    def create_label_auth_session(self) -> str:
        state = secrets.token_urlsafe(24)
        now = now_text()
        expires = (datetime.now(APP_TZ) + timedelta(minutes=30)).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("DELETE FROM label_auth_sessions WHERE expires_at < ? OR used_at <> ''", (now,))
            conn.execute(
                "INSERT INTO label_auth_sessions (state, expires_at, created_at) VALUES (?, ?, ?)",
                (state, expires, now),
            )
        return state

    def consume_label_auth_session(self, state: str) -> None:
        now = now_text()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM label_auth_sessions WHERE state = ? AND used_at = '' AND expires_at >= ?",
                (state, now),
            ).fetchone()
            if not row:
                raise AppError("菜鸟授权链接无效或已过期。", 403)
            conn.execute("UPDATE label_auth_sessions SET used_at = ? WHERE state = ?", (now, state))

    def save_label_authorization(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        now = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipping_settings
                SET partner_id = ?, partner_key = ?, partner_secret = ?, partner_name = ?, partner_net = ?,
                    partner_code = ?, partner_check_man = ?, authorized_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    str(credentials.get("partnerId") or ""), str(credentials.get("partnerKey") or ""),
                    str(credentials.get("partnerSecret") or ""), str(credentials.get("partnerName") or ""),
                    str(credentials.get("net") or "cainiao"), str(credentials.get("code") or ""),
                    str(credentials.get("checkMan") or ""), now, now,
                ),
            )
        return self.get_shipping_settings(public=True)

    def save_label_branches(self, branches: List[Dict[str, Any]]) -> Dict[str, Any]:
        settings = self.get_shipping_settings()
        carriers = settings.get("carrier_settings", {})
        options: List[Dict[str, Any]] = []
        code_to_company = {"yuantong": "圆通", "jd": "京东", "shunfeng": "顺丰"}
        for entry in branches:
            company = code_to_company.get(str(entry.get("kuaidicom") or ""))
            if not company:
                continue
            company_options = []
            for branch in entry.get("branchAccounts") or []:
                option = {
                    "company": company,
                    "tbNet": str(branch.get("tbNet") or ""),
                    "branchName": str(branch.get("branchName") or ""),
                    "quantity": int(branch.get("quantity") or 0),
                }
                options.append(option)
                company_options.append(option)
            current = carriers.get(company) if isinstance(carriers.get(company), dict) else {}
            if company_options and not current.get("tbNet"):
                current["tbNet"] = company_options[0]["tbNet"]
            current.setdefault("expType", "顺丰标快" if company == "顺丰" else "标准快递")
            carriers[company] = current
        with self.connect() as conn:
            conn.execute(
                "UPDATE shipping_settings SET carrier_settings_json = ?, branch_options_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(carriers, ensure_ascii=False), json.dumps(options, ensure_ascii=False), now_text()),
            )
        return self.get_shipping_settings(public=True)

    @staticmethod
    def shipment_booking_eligible(row: Dict[str, Any]) -> bool:
        return (
            row.get("status") == "待处理"
            and not str(row.get("tracking_no") or "").strip()
            and str(row.get("booking_status") or "未下单") in BOOKING_EDITABLE_STATUSES
        )

    def preview_shipping_batch(self, user: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
        shipments = self.list_shipments(user, filters)
        settings = self.get_shipping_settings()
        default_company = settings.get("default_company") or DEFAULT_EXPRESS_COMPANY
        eligible = []
        excluded = []
        company_counts = {company: 0 for company in EXPRESS_COMPANIES}
        for shipment in shipments:
            if self.shipment_booking_eligible(shipment):
                company = shipment.get("express_company") if shipment.get("express_company") in EXPRESS_COMPANIES else default_company
                row = {
                    "id": shipment["id"],
                    "business_id": shipment["business_id"],
                    "store_name_snapshot": shipment["store_name_snapshot"],
                    "store_order_no": shipment["store_order_no"],
                    "recipient_name": shipment["recipient_name"],
                    "address": shipment["address"],
                    "express_company": company,
                }
                eligible.append(row)
                company_counts[company] += 1
            else:
                reason = "状态不可下单"
                if shipment.get("tracking_no"):
                    reason = "已有快递单号"
                elif shipment.get("booking_status") not in BOOKING_EDITABLE_STATUSES:
                    reason = f"下单状态：{shipment.get('booking_status')}"
                excluded.append({"id": shipment["id"], "business_id": shipment["business_id"], "reason": reason})
        return {
            "matched": len(shipments),
            "eligible": eligible,
            "excluded": excluded,
            "company_counts": company_counts,
            "settings_ready": bool(settings.get("sender_name") and settings.get("sender_mobile") and settings.get("sender_address")),
            "label_ready": bool(settings.get("partner_id") and settings.get("partner_key")),
        }

    def create_shipping_batch(
        self,
        user: Dict[str, Any],
        shipment_choices: List[Dict[str, Any]],
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not shipment_choices:
            raise AppError("没有可下单的发货单。")

        now = now_text()
        seen: set[int] = set()
        normalized: List[tuple[int, str]] = []
        for choice in shipment_choices:
            try:
                shipment_id = int(choice.get("id"))
            except (TypeError, ValueError):
                continue
            company = str(choice.get("express_company") or DEFAULT_EXPRESS_COMPANY).strip()
            if company not in EXPRESS_COMPANIES:
                raise AppError(f"不支持这个快递公司：{company}")
            if shipment_id not in seen:
                seen.add(shipment_id)
                normalized.append((shipment_id, company))
        if not normalized:
            raise AppError("没有可下单的发货单。")

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            valid: List[tuple[sqlite3.Row, str]] = []
            for shipment_id, company in normalized:
                row = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
                if row and self.shipment_booking_eligible(dict(row)):
                    valid.append((row, company))
            if not valid:
                raise AppError("所选订单已被处理，请刷新后重试。", 409)
            cursor = conn.execute(
                """
                INSERT INTO shipping_batches (
                    created_by, filters_json, pickup_day, pickup_start_time, pickup_end_time,
                    status, total_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '排队中', ?, ?, ?)
                """,
                (user["id"], json.dumps(filters or {}, ensure_ascii=False), "", "", "", len(valid), now, now),
            )
            batch_id = int(cursor.lastrowid)
            for shipment, company in valid:
                request_id = str(
                    shipment["booking_request_id"]
                    or new_booking_request_id(
                        str(shipment["order_date"]), int(shipment["store_id"]), int(shipment["id"])
                    )
                )[:32]
                salt = str(shipment["booking_salt"] or secrets.token_hex(12))
                conn.execute(
                    """
                    UPDATE shipments
                    SET express_company = ?, booking_status = '排队中', booking_request_id = ?,
                        booking_salt = ?, booking_error = '', booking_requested_at = ?, booking_updated_at = ?,
                        pickup_day = '', pickup_start_time = '', pickup_end_time = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (company, request_id, salt, now, now, now, shipment["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO shipping_batch_items (
                        batch_id, shipment_id, express_company, request_id, callback_salt,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '排队中', ?, ?)
                    """,
                    (batch_id, shipment["id"], company, request_id, salt, now, now),
                )
        return self.get_shipping_batch(batch_id)

    def claim_next_shipping_job(self) -> Optional[Dict[str, Any]]:
        now = now_text()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                """
                SELECT shipping_batch_items.*
                FROM shipping_batch_items
                WHERE shipping_batch_items.status = '排队中'
                ORDER BY shipping_batch_items.id
                LIMIT 1
                """
            ).fetchone()
            if not job:
                return None
            conn.execute(
                """
                UPDATE shipping_batch_items
                SET status = '提交中', attempt_count = attempt_count + 1, error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, job["id"]),
            )
            conn.execute(
                "UPDATE shipments SET booking_status = '提交中', booking_updated_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job["shipment_id"]),
            )
            conn.execute(
                "UPDATE shipping_batches SET status = '处理中', updated_at = ? WHERE id = ?",
                (now, job["batch_id"]),
            )
            shipment = conn.execute("SELECT * FROM shipments WHERE id = ?", (job["shipment_id"],)).fetchone()
            items = conn.execute("SELECT * FROM shipment_items WHERE shipment_id = ? ORDER BY id", (job["shipment_id"],)).fetchall()
        payload = dict(shipment)
        payload["items"] = [dict(item) for item in items]
        payload["batch_item_id"] = job["id"]
        payload["batch_id"] = job["batch_id"]
        return payload

    def complete_shipping_job(self, item_id: int, result: Dict[str, Any]) -> Dict[str, Any]:
        now = now_text()
        success = bool(result.get("success"))
        tracking_no = str(result.get("tracking_no") or "").strip()
        item_status = "成功" if success and tracking_no else "失败"
        booking_status = "已出单" if success and tracking_no else "下单失败"
        with self.connect() as conn:
            item = conn.execute("SELECT * FROM shipping_batch_items WHERE id = ?", (item_id,)).fetchone()
            if not item:
                raise AppError("批次任务不存在。", 404)
            shipment = conn.execute("SELECT * FROM shipments WHERE id = ?", (item["shipment_id"],)).fetchone()
            if not shipment:
                raise AppError("发货单不存在。", 404)
            status = "已发货" if tracking_no else shipment["status"]
            shipped_at = shipment["shipped_at"] or (now if tracking_no else "")
            conn.execute(
                """
                UPDATE shipping_batch_items
                SET status = ?, task_id = ?, order_id = ?, error = ?, response_raw = ?,
                    cancel_param_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    item_status, str(result.get("task_id") or ""), str(result.get("carrier_order_no") or ""),
                    str(result.get("error") or ""), str(result.get("raw") or ""),
                    json.dumps(result.get("cancel_param") or {}, ensure_ascii=False), now, item_id,
                ),
            )
            conn.execute(
                """
                UPDATE shipments
                SET status = ?, tracking_no = CASE WHEN ? <> '' THEN ? ELSE tracking_no END,
                    tracking_status = CASE WHEN ? <> '' THEN '等待揽收' ELSE tracking_status END,
                    tracking_provider = CASE WHEN ? <> '' THEN 'kuaidi100' ELSE tracking_provider END,
                    shipped_at = ?, booking_status = ?, booking_task_id = ?, booking_order_id = ?,
                    booking_poll_token = '', booking_error = ?, booking_raw = ?, booking_updated_at = ?,
                    label_url = ?, label_print_status = ?, label_print_error = '', label_print_type = ?,
                    label_carrier_order_no = ?, label_child_no = ?, label_return_no = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status, tracking_no, tracking_no, tracking_no, tracking_no, shipped_at, booking_status,
                    str(result.get("task_id") or ""), str(result.get("carrier_order_no") or ""),
                    str(result.get("error") or ""), str(result.get("raw") or ""), now,
                    str(result.get("label_url") or ""), str(result.get("print_status") or ""),
                    str(result.get("print_type") or ""), str(result.get("carrier_order_no") or ""),
                    str(result.get("child_no") or ""), str(result.get("return_no") or ""), now,
                    item["shipment_id"],
                ),
            )
            self._refresh_shipping_batch_status(conn, int(item["batch_id"]), now)
        return {"shipment_id": item["shipment_id"], "tracking_no": tracking_no, "success": success}

    def _refresh_shipping_batch_status(self, conn: sqlite3.Connection, batch_id: int, now: str) -> None:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM shipping_batch_items WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        if counts.get("排队中") or counts.get("提交中"):
            status = "处理中"
            finished_at = ""
        elif counts.get("失败") and not counts.get("成功"):
            status = "失败"
            finished_at = now
        elif counts.get("失败"):
            status = "部分完成"
            finished_at = now
        else:
            status = "已完成"
            finished_at = now
        conn.execute(
            "UPDATE shipping_batches SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (status, now, finished_at, batch_id),
        )

    def get_shipping_batch(self, batch_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM shipping_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch:
                raise AppError("下单批次不存在。", 404)
            items = conn.execute(
                """
                SELECT shipping_batch_items.*, shipments.business_id, shipments.store_order_no,
                       shipments.store_name_snapshot, shipments.recipient_name, shipments.tracking_no,
                       shipments.booking_status
                FROM shipping_batch_items
                JOIN shipments ON shipments.id = shipping_batch_items.shipment_id
                WHERE shipping_batch_items.batch_id = ?
                ORDER BY shipping_batch_items.id
                """,
                (batch_id,),
            ).fetchall()
        item_list = [dict(item) for item in items]
        for item in item_list:
            item.pop("cancel_param_json", None)
        counts: Dict[str, int] = {}
        for item in item_list:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"batch": dict(batch), "items": item_list, "counts": counts}

    def retry_shipping_batch(self, batch_id: int) -> Dict[str, Any]:
        now = now_text()
        with self.connect() as conn:
            failed = conn.execute(
                "SELECT shipment_id FROM shipping_batch_items WHERE batch_id = ? AND status = '失败'",
                (batch_id,),
            ).fetchall()
            if not failed:
                raise AppError("这个批次没有可重试的失败订单。", 409)
            conn.execute(
                "UPDATE shipping_batch_items SET status = '排队中', error = '', updated_at = ? WHERE batch_id = ? AND status = '失败'",
                (now, batch_id),
            )
            for row in failed:
                conn.execute(
                    "UPDATE shipments SET booking_status = '排队中', booking_error = '', booking_updated_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, row["shipment_id"]),
                )
            conn.execute(
                "UPDATE shipping_batches SET status = '排队中', finished_at = '', updated_at = ? WHERE id = ?",
                (now, batch_id),
            )
        return self.get_shipping_batch(batch_id)

    def reset_stale_shipping_jobs(self) -> Dict[str, int]:
        now = now_text()
        stale_before = (
            datetime.now(APP_TZ) - timedelta(minutes=SHIPPING_STALE_MINUTES)
        ).isoformat(timespec="seconds")
        requeued = 0
        failed = 0
        affected_batches: set[int] = set()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, shipment_id, batch_id, attempt_count
                FROM shipping_batch_items
                WHERE status = '提交中' AND updated_at < ?
                """,
                (stale_before,),
            ).fetchall()
            for row in rows:
                affected_batches.add(int(row["batch_id"]))
                if int(row["attempt_count"] or 0) >= SHIPPING_STALE_MAX_ATTEMPTS:
                    message = (
                        f"电子面单任务连续 {SHIPPING_STALE_MAX_ATTEMPTS} 次等待超时，"
                        "系统已停止自动重试。请检查面单设置后点击“重试失败订单”。"
                    )
                    conn.execute(
                        "UPDATE shipping_batch_items SET status = '失败', error = ?, updated_at = ? WHERE id = ?",
                        (message, now, row["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE shipments
                        SET booking_status = '下单失败', booking_error = ?, booking_updated_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (message, now, now, row["shipment_id"]),
                    )
                    failed += 1
                else:
                    next_attempt = int(row["attempt_count"] or 0) + 1
                    message = (
                        f"上次提交等待超时，系统已自动重新排队，准备第 {next_attempt} 次尝试。"
                    )
                    conn.execute(
                        "UPDATE shipping_batch_items SET status = '排队中', error = ?, updated_at = ? WHERE id = ?",
                        (message, now, row["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE shipments
                        SET booking_status = '排队中', booking_error = ?, booking_updated_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (message, now, now, row["shipment_id"]),
                    )
                    requeued += 1
            for batch_id in affected_batches:
                self._refresh_shipping_batch_status(conn, batch_id, now)
        return {"requeued": requeued, "failed": failed}

    def apply_label_print_callback(self, task_id: str, param_raw: str, param: Dict[str, Any]) -> Dict[str, Any]:
        print_status_code = str(param.get("status") or "")
        event_key = hashlib.sha256(f"print|{task_id}|{print_status_code}|{param_raw}".encode("utf-8")).hexdigest()
        now = now_text()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO shipping_callback_events (task_id, carrier_status, event_key, param_raw, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, print_status_code, event_key, param_raw, now),
            )
            if cursor.rowcount == 0:
                return {"duplicate": True}
            batch_item = conn.execute(
                "SELECT * FROM shipping_batch_items WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if not batch_item:
                raise AppError("没有找到对应的电子面单任务。", 404)
            shipment = conn.execute("SELECT * FROM shipments WHERE id = ?", (batch_item["shipment_id"],)).fetchone()
            if not shipment or shipment["booking_task_id"] != task_id:
                return {"duplicate": False, "ignored": True}
            succeeded = print_status_code == "200"
            print_status = "打印成功" if succeeded else "打印失败"
            print_error = "" if succeeded else str(param.get("message") or "打印机未完成面单打印")
            conn.execute(
                """
                UPDATE shipments
                SET label_print_status = ?, label_print_error = ?, booking_raw = ?, booking_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (print_status, print_error, param_raw, now, now, shipment["id"]),
            )
        return {"duplicate": False, "shipment_id": shipment["id"], "print_status": print_status}

    def booking_salt_for_task(self, task_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT callback_salt AS booking_salt FROM shipping_batch_items WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if not row:
            raise AppError("没有找到对应的电子面单任务。", 404)
        return str(row["booking_salt"] or "")

    def booking_for_cancel(self, shipment_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
            batch_item = conn.execute(
                """
                SELECT cancel_param_json
                FROM shipping_batch_items
                WHERE shipment_id = ? AND status = '成功'
                ORDER BY id DESC
                LIMIT 1
                """,
                (shipment_id,),
            ).fetchone()
        if not row:
            raise AppError("发货单不存在。", 404)
        if not row["booking_task_id"] or not row["tracking_no"]:
            raise AppError("这个订单没有可取消的电子面单。", 409)
        if row["status"] == "已签收":
            raise AppError("已签收订单不能取消快递下单。", 409)
        booking = dict(row)
        try:
            booking["label_cancel_param"] = json.loads(
                str(batch_item["cancel_param_json"] or "{}") if batch_item else "{}"
            )
        except json.JSONDecodeError:
            booking["label_cancel_param"] = {}
        return booking

    def mark_label_reprint(self, shipment_id: int, raw: str = "") -> None:
        now = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipments
                SET label_print_status = '打印中', label_print_error = '', booking_raw = ?,
                    booking_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (raw, now, now, shipment_id),
            )

    def mark_label_printed(self, shipment_id: int) -> None:
        now = now_text()
        with self.connect() as conn:
            row = conn.execute("SELECT label_url, booking_task_id FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
            if not row:
                raise AppError("发货单不存在。", 404)
            if not row["label_url"] and not row["booking_task_id"]:
                raise AppError("这个订单还没有可打印的电子面单。", 409)
            conn.execute(
                "UPDATE shipments SET label_print_status = '打印成功', label_print_error = '', updated_at = ? WHERE id = ?",
                (now, shipment_id),
            )

    def batch_print_shipments(self, shipment_ids: Iterable[Any]) -> List[Dict[str, Any]]:
        normalized_ids: List[int] = []
        for value in shipment_ids:
            try:
                shipment_id = int(value)
            except (TypeError, ValueError):
                raise AppError("批量打印包含无效的订单 ID。")
            if shipment_id > 0 and shipment_id not in normalized_ids:
                normalized_ids.append(shipment_id)
        if not normalized_ids:
            raise AppError("请至少选择一张待打印面单。")

        placeholders = ",".join("?" for _ in normalized_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, business_id, label_url, label_print_status
                FROM shipments
                WHERE id IN ({placeholders})
                """,
                normalized_ids,
            ).fetchall()
        rows_by_id = {int(row["id"]): dict(row) for row in rows}
        missing = [shipment_id for shipment_id in normalized_ids if shipment_id not in rows_by_id]
        if missing:
            raise AppError("部分发货单不存在，无法批量打印。", 404, {"shipment_ids": missing})
        shipments = [rows_by_id[shipment_id] for shipment_id in normalized_ids]
        invalid = [
            row["business_id"]
            for row in shipments
            if row["label_print_status"] != "待打印" or not str(row["label_url"] or "").strip()
        ]
        if invalid:
            raise AppError(
                "只有待打印且已生成 PDF 的订单可以批量打印。",
                409,
                {"business_ids": invalid},
            )
        return shipments

    def mark_labels_printed(self, shipment_ids: Iterable[int]) -> None:
        normalized_ids = list(dict.fromkeys(int(value) for value in shipment_ids))
        if not normalized_ids:
            return
        placeholders = ",".join("?" for _ in normalized_ids)
        now = now_text()
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE shipments
                SET label_print_status = '打印成功', label_print_error = '', updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [now, *normalized_ids],
            )

    def mark_booking_cancelled(self, shipment_id: int, raw: str = "") -> Dict[str, Any]:
        now = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipments
                SET status = '待处理', tracking_no = '', tracking_provider = '',
                    tracking_status = '', tracking_state_code = '', tracking_last_event = '',
                    tracking_last_checked_at = '', tracking_signed_at = '', tracking_error = '', tracking_raw = '',
                    shipped_at = '', booking_status = '已取消', booking_task_id = '', booking_order_id = '',
                    booking_request_id = '', booking_poll_token = '', booking_salt = '',
                    booking_error = '', booking_raw = ?,
                    label_url = '', label_print_status = '', label_print_error = '', label_print_type = '',
                    label_carrier_order_no = '', label_child_no = '', label_return_no = '',
                    booking_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (raw, now, now, shipment_id),
            )
            item = conn.execute(
                "SELECT * FROM shipping_batch_items WHERE shipment_id = ? ORDER BY id DESC LIMIT 1",
                (shipment_id,),
            ).fetchone()
            if item:
                conn.execute(
                    "UPDATE shipping_batch_items SET status = '已取消', response_raw = ?, updated_at = ? WHERE id = ?",
                    (raw, now, item["id"]),
                )
                self._refresh_shipping_batch_status(conn, int(item["batch_id"]), now)
        return {"id": shipment_id, "booking_status": "已取消"}

    def tracking_candidates(self, stale_before: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        where = [
            "shipments.status = '已发货'",
            "shipments.tracking_no <> ''",
            "shipments.tracking_signed_at = ''",
        ]
        params: List[Any] = []
        if stale_before:
            where.append("(shipments.tracking_last_checked_at = '' OR shipments.tracking_last_checked_at <= ?)")
            params.append(stale_before)
        sql = f"""
            SELECT shipments.*
            FROM shipments
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE WHEN shipments.tracking_last_checked_at = '' THEN 0 ELSE 1 END,
                shipments.tracking_last_checked_at,
                shipments.shipped_at,
                shipments.id
        """
        if int(limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(max(1, min(int(limit), 5000)))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def tracking_candidate_count(self, stale_before: str = "") -> int:
        where = [
            "shipments.status = '已发货'",
            "shipments.tracking_no <> ''",
            "shipments.tracking_signed_at = ''",
        ]
        params: List[Any] = []
        if stale_before:
            where.append("(shipments.tracking_last_checked_at = '' OR shipments.tracking_last_checked_at <= ?)")
            params.append(stale_before)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM shipments WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
        return int(row["count"] or 0)

    def apply_tracking_result(self, shipment_id: int, result: Dict[str, Any]) -> Dict[str, Any]:
        checked_at = str(result.get("checked_at") or now_text())
        signed_at = str(result.get("signed_at") or "")
        tracking_status = str(result.get("tracking_status") or "")
        is_signed = bool(result.get("is_signed"))
        if is_signed and not signed_at:
            signed_at = checked_at
        status_update = "已签收" if is_signed else None
        now = now_text()
        with self.connect() as conn:
            existing = conn.execute("SELECT status FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
            if not existing:
                raise AppError("发货单不存在。", 404)
            status = status_update or ("已发货" if existing["status"] == "待处理" and tracking_status and tracking_status != "查询失败" else existing["status"])
            cursor = conn.execute(
                """
                UPDATE shipments
                SET tracking_provider = ?, tracking_status = ?, tracking_state_code = ?,
                    tracking_last_event = ?, tracking_last_checked_at = ?,
                    tracking_signed_at = ?, tracking_error = ?, tracking_raw = ?,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(result.get("provider") or ""),
                    tracking_status,
                    str(result.get("state_code") or ""),
                    str(result.get("last_event") or ""),
                    checked_at,
                    signed_at,
                    str(result.get("error") or ""),
                    str(result.get("raw") or ""),
                    status,
                    now,
                    shipment_id,
                ),
            )
            if cursor.rowcount == 0:
                raise AppError("发货单不存在。", 404)
        return {"id": shipment_id, "status": status, "tracking_status": tracking_status}

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
        now = now_text()
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
            if not existing:
                raise AppError("发货单不存在。", 404)
            if existing["booking_status"] not in BOOKING_EDITABLE_STATUSES:
                raise AppError("快递已下单。如需修改，请先取消快递下单。", 409)

        auto_shipped = status == "待处理" and tracking_no
        if auto_shipped:
            status = "已发货"
        if status in {"已发货", "已签收"} and not shipped_at:
            shipped_at = existing["shipped_at"] or now
        if status not in {"已发货", "已签收"}:
            shipped_at = ""

        tracking_changed = tracking_no != existing["tracking_no"] or express_company != existing["express_company"]
        tracking_provider = existing["tracking_provider"]
        tracking_status = existing["tracking_status"]
        tracking_state_code = existing["tracking_state_code"]
        tracking_last_event = existing["tracking_last_event"]
        tracking_last_checked_at = existing["tracking_last_checked_at"]
        tracking_signed_at = existing["tracking_signed_at"]
        tracking_error = existing["tracking_error"]
        tracking_raw = existing["tracking_raw"]
        tracking_reset = (tracking_changed or auto_shipped) and status == "已发货" and tracking_no
        if tracking_reset:
            tracking_provider = ""
            tracking_status = "待查询"
            tracking_state_code = ""
            tracking_last_event = ""
            tracking_last_checked_at = ""
            tracking_signed_at = ""
            tracking_error = ""
            tracking_raw = ""
        if status == "已签收":
            tracking_status = "已签收"
            tracking_signed_at = tracking_signed_at or now
            tracking_error = ""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE shipments
                SET status = ?, express_company = ?, tracking_no = ?, shipping_note = ?,
                    shipped_at = ?, tracking_provider = ?, tracking_status = ?,
                    tracking_state_code = ?, tracking_last_event = ?,
                    tracking_last_checked_at = ?, tracking_signed_at = ?,
                    tracking_error = ?, tracking_raw = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    express_company,
                    tracking_no,
                    shipping_note,
                    shipped_at,
                    tracking_provider,
                    tracking_status,
                    tracking_state_code,
                    tracking_last_event,
                    tracking_last_checked_at,
                    tracking_signed_at,
                    tracking_error,
                    tracking_raw,
                    now,
                    shipment_id,
                ),
            )
            if cursor.rowcount == 0:
                raise AppError("发货单不存在。", 404)
        return {
            "id": shipment_id,
            "status": status,
            "tracking_changed": tracking_changed,
            "should_refresh_tracking": tracking_reset,
        }

    def delete_shipment(self, shipment_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        with self.connect() as conn:
            shipment = conn.execute(
                "SELECT store_id, status, booking_status FROM shipments WHERE id = ?",
                (shipment_id,),
            ).fetchone()
            if not shipment:
                raise AppError("发货单不存在。", 404)
            if user.get("role") == "staff" and shipment["store_id"] != user.get("store_id"):
                raise AppError("无权删除这个发货单。", 403)
            if shipment["status"] != "待处理":
                raise AppError("只有尚未发货的待处理订单可以删除。", 409)
            if shipment["booking_status"] not in BOOKING_EDITABLE_STATUSES:
                raise AppError("快递下单处理中，不能直接删除。请先取消快递下单。", 409)
            cursor = conn.execute("DELETE FROM shipments WHERE id = ?", (shipment_id,))
        return {"id": shipment_id, "deleted": True}

    def create_return_order(self, user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        store_id = user.get("store_id") if user.get("role") == "staff" else payload.get("store_id")
        try:
            store_id = int(store_id)
        except (TypeError, ValueError):
            raise AppError("请选择门店。")

        tracking_no = str(payload.get("tracking_no", "")).strip()
        sender_phone = str(payload.get("sender_phone", "")).strip()
        remark = str(payload.get("remark", "")).strip()
        raw_items = payload.get("items") or []

        if not tracking_no:
            raise AppError("请输入退货快递单号。")
        if sender_phone and not re_phone_ok(sender_phone):
            raise AppError("请输入有效联系电话。")
        if not isinstance(raw_items, list) or not raw_items:
            raise AppError("请至少选择一个退货商品。")

        now = now_text()
        with self.connect() as conn:
            store = conn.execute("SELECT * FROM stores WHERE id = ? AND active = 1", (store_id,)).fetchone()
            if not store:
                raise AppError("门店不存在或已停用。")

            items = self._resolve_items(conn, raw_items)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO return_orders (
                        store_id, store_name_snapshot, created_by, express_company, express_company_source, tracking_no,
                        sender_phone, remark, status, tracking_status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, '', 'auto_pending', ?, ?, ?, '待查询', '待查询', ?, ?)
                    """,
                    (
                        store_id,
                        store["name"],
                        user["id"],
                        tracking_no,
                        sender_phone,
                        remark,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AppError("这个退货快递单号已经提交过。", 409) from exc

            return_id = cursor.lastrowid
            for item in items:
                conn.execute(
                    """
                    INSERT INTO return_items (
                        return_order_id, product_barcode, product_name, product_category, unit_price, quantity
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        return_id,
                        item["barcode"],
                        item["name"],
                        item["category"],
                        item["price"],
                        item["quantity"],
                    ),
                )
        return self.get_return_order(return_id, user)

    def get_return_order(self, return_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        returns = self.list_return_orders(user, {"id": return_id})
        if not returns:
            raise AppError("退货单不存在。", 404)
        return returns[0]

    def list_return_orders(self, user: Dict[str, Any], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []

        if filters.get("id"):
            where.append("return_orders.id = ?")
            params.append(filters["id"])

        if user.get("role") == "staff":
            where.append("return_orders.store_id = ?")
            params.append(user["store_id"])
        elif filters.get("store_id"):
            where.append("return_orders.store_id = ?")
            params.append(filters["store_id"])

        if filters.get("status"):
            where.append("return_orders.status = ?")
            params.append(filters["status"])
        if filters.get("date_from"):
            where.append("datetime(return_orders.created_at) >= datetime(?)")
            params.append(local_day_start(str(filters["date_from"])))
        if filters.get("date_to"):
            where.append("datetime(return_orders.created_at) <= datetime(?)")
            params.append(local_day_end(str(filters["date_to"])))
        if filters.get("q"):
            q = f"%{filters['q']}%"
            where.append(
                """
                (
                    return_orders.tracking_no LIKE ? OR return_orders.sender_phone LIKE ? OR
                    return_orders.store_name_snapshot LIKE ? OR return_orders.remark LIKE ?
                )
                """
            )
            params.extend([q, q, q, q])

        sql = "SELECT return_orders.* FROM return_orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY return_orders.created_at DESC, return_orders.id DESC"

        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            item_rows = conn.execute(
                f"SELECT * FROM return_items WHERE return_order_id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()

        items_by_return: Dict[int, List[Dict[str, Any]]] = {}
        for item in item_rows:
            items_by_return.setdefault(item["return_order_id"], []).append(dict(item))

        for row in rows:
            row["items"] = items_by_return.get(row["id"], [])
            row["item_summary"] = "；".join(
                f"{item['product_category']} / {item['product_name']} x{item['quantity']}"
                for item in row["items"]
            )
        return rows

    def return_tracking_candidates(self, stale_before: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        where = [
            "return_orders.status NOT IN ('已签收', '已取消')",
            "return_orders.tracking_no <> ''",
            "return_orders.tracking_signed_at = ''",
        ]
        params: List[Any] = []
        if stale_before:
            where.append("(return_orders.tracking_last_checked_at = '' OR return_orders.tracking_last_checked_at <= ?)")
            params.append(stale_before)
        sql = f"""
            SELECT return_orders.*
            FROM return_orders
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE WHEN return_orders.tracking_last_checked_at = '' THEN 0 ELSE 1 END,
                return_orders.tracking_last_checked_at,
                return_orders.created_at,
                return_orders.id
            LIMIT ?
        """
        params.append(max(1, min(int(limit or 20), 100)))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def apply_return_tracking_result(self, return_id: int, result: Dict[str, Any]) -> Dict[str, Any]:
        checked_at = str(result.get("checked_at") or now_text())
        signed_at = str(result.get("signed_at") or "")
        tracking_status = str(result.get("tracking_status") or "")
        is_signed = bool(result.get("is_signed"))
        if is_signed and not signed_at:
            signed_at = checked_at
        status = return_status_from_tracking(tracking_status, is_signed)
        now = now_text()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE return_orders
                SET express_company = CASE WHEN ? <> '' THEN ? ELSE express_company END,
                    express_company_code = CASE WHEN ? <> '' THEN ? ELSE express_company_code END,
                    express_company_source = CASE WHEN ? <> '' THEN ? ELSE express_company_source END,
                    tracking_provider = ?, tracking_status = ?, tracking_state_code = ?,
                    tracking_last_event = ?, tracking_last_checked_at = ?,
                    tracking_signed_at = ?, tracking_error = ?, tracking_raw = ?,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(result.get("express_company") or ""),
                    str(result.get("express_company") or ""),
                    str(result.get("express_company_code") or ""),
                    str(result.get("express_company_code") or ""),
                    str(result.get("express_company_source") or ""),
                    str(result.get("express_company_source") or ""),
                    str(result.get("provider") or ""),
                    tracking_status,
                    str(result.get("state_code") or ""),
                    str(result.get("last_event") or ""),
                    checked_at,
                    signed_at,
                    str(result.get("error") or ""),
                    str(result.get("raw") or ""),
                    status,
                    now,
                    return_id,
                ),
            )
            if cursor.rowcount == 0:
                raise AppError("退货单不存在。", 404)
        return {"id": return_id, "status": status, "tracking_status": tracking_status}


def re_phone_ok(phone: str) -> bool:
    compact = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    digits = "".join(ch for ch in compact if ch.isdigit())
    return 7 <= len(digits) <= 20 and compact.replace("+", "", 1).isdigit()


def return_status_from_tracking(tracking_status: str, is_signed: bool) -> str:
    if is_signed or tracking_status == "已签收":
        return "已签收"
    if tracking_status in {"查询失败", "问题件"}:
        return "异常"
    if tracking_status in {"已揽收", "运输中", "转寄"}:
        return "运输中"
    return "待查询"
