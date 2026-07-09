from __future__ import annotations

import argparse
import csv
import tempfile
import io
import json
import mimetypes
import os
import threading
import time
import zipfile
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape

from database import AppError, Database, DEFAULT_PRODUCT_FILE, RETURN_STATUSES, STATUSES, now_text
from tracking import manual_refresh_stale_before, query_tracking, tracking_auto_enabled, tracking_config_public, tracking_interval_minutes, tracking_stale_before


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB: Database
PRODUCT_FILE_PATH = DEFAULT_PRODUCT_FILE
SESSION_SECURE = False
ALLOW_DB_RESTORE = False
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def format_items_for_export(items: Any) -> str:
    grouped: Dict[str, list[str]] = {}
    for item in items or []:
        category = str(item.get("product_category") or "未分类")
        name = str(item.get("product_name") or item.get("product_barcode") or "")
        quantity = item.get("quantity") or 0
        grouped.setdefault(category, []).append(f"{name} x{quantity}")
    return "\n".join(f"{category}：{'、'.join(names)}" for category, names in grouped.items())


EXPORT_HEADERS = [
    "发货单ID",
    "提交时间",
    "门店",
    "门店订单号",
    "状态",
    "收件人",
    "联系电话",
    "快递地址",
    "商品明细",
    "备注",
    "快递公司",
    "快递单号",
    "物流状态",
    "最新轨迹",
    "上次查询",
    "签收时间",
    "发货备注",
    "发货时间",
]

EXPORT_COLUMN_WIDTHS = [10, 20, 14, 18, 10, 12, 16, 36, 52, 24, 12, 22, 14, 46, 20, 20, 24, 20]


def export_rows(shipments: Any) -> list[list[Any]]:
    rows = []
    for row in shipments:
        rows.append(
            [
                row["id"],
                row["created_at"],
                row["store_name_snapshot"],
                row["store_order_no"],
                row["status"],
                row["recipient_name"],
                row["phone"],
                row["address"],
                format_items_for_export(row["items"]),
                row["remark"],
                row["express_company"],
                row["tracking_no"],
                row["tracking_status"],
                row["tracking_last_event"],
                row["tracking_last_checked_at"],
                row["tracking_signed_at"],
                row["shipping_note"],
                row["shipped_at"],
            ]
        )
    return rows


def excel_col(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def xlsx_cell(row_index: int, col_index: int, value: Any, style: int) -> str:
    ref = f"{excel_col(col_index)}{row_index}"
    text = xml_escape("" if value is None else str(value))
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{text}</t></is></c>'


def build_shipments_xlsx(shipments: Any) -> bytes:
    rows = [EXPORT_HEADERS, *export_rows(shipments)]
    max_col = excel_col(len(EXPORT_HEADERS))
    dimension = f"A1:{max_col}{max(len(rows), 1)}"
    cols = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(EXPORT_COLUMN_WIDTHS, start=1)
    )
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        style = 1 if row_index == 1 else 2
        cells = "".join(xlsx_cell(row_index, col_index, value, style) for col_index, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dimension}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{cols}</cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  <autoFilter ref="{dimension}"/>
  <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Arial"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1D1D1F"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFE5E5EA"/></left><right style="thin"><color rgb="FFE5E5EA"/></right><top style="thin"><color rgb="FFE5E5EA"/></top><bottom style="thin"><color rgb="FFE5E5EA"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="发货明细" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def safe_filename_part(value: Any) -> str:
    text = str(value or "").strip()
    for char in '\\/:*?"<>|':
        text = text.replace(char, "-")
    text = "_".join(text.split())
    return text or "未命名"


def export_store_part(query: Dict[str, str], shipments: Any) -> str:
    store_id = str(query.get("store_id", "")).strip()
    if store_id:
        for store in DB.list_stores(include_inactive=True):
            if str(store["id"]) == store_id:
                return safe_filename_part(store["name"])

    store_names = sorted({str(row.get("store_name_snapshot") or "").strip() for row in shipments if row.get("store_name_snapshot")})
    if len(store_names) == 1:
        return safe_filename_part(store_names[0])
    return "全部门店"


def export_date_part(query: Dict[str, str]) -> str:
    date_from = str(query.get("date_from", "")).strip()
    date_to = str(query.get("date_to", "")).strip()
    if date_from and date_to:
        return safe_filename_part(date_from if date_from == date_to else f"{date_from}至{date_to}")
    if date_from:
        return safe_filename_part(f"{date_from}起")
    if date_to:
        return safe_filename_part(f"至{date_to}")
    return local_now().strftime("%Y-%m-%d")


def export_filename(query: Dict[str, str], shipments: Any, extension: str) -> str:
    return f"{export_store_part(query, shipments)}_{export_date_part(query)}.{extension}"


def attachment_header(filename: str, fallback: str) -> str:
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def local_now() -> datetime:
    return datetime.fromisoformat(now_text())


def return_tracking_interval_minutes() -> int:
    raw = os.environ.get("SCENTPOOL_RETURN_TRACKING_INTERVAL_MINUTES", "720").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 720
    return max(60, value)


def return_tracking_stale_before() -> str:
    from datetime import timedelta

    return (local_now() - timedelta(minutes=return_tracking_interval_minutes())).isoformat(timespec="seconds")


def save_database_backup() -> Path:
    backup_dir = Path(DB.path).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"scentpool-before-restore-{local_now().strftime('%Y%m%d-%H%M%S')}.db"
    backup_path.write_bytes(DB.backup_bytes())
    return backup_path


def validate_database_file(path: Path) -> None:
    required_tables = {"stores", "users", "sessions", "products", "shipments", "shipment_items"}
    probe = Database(str(path))
    try:
        with probe.connect() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except Exception as exc:
        raise AppError("数据库备份文件无法读取。") from exc
    tables = {row["name"] for row in rows}
    missing = sorted(required_tables - tables)
    if missing:
        raise AppError(f"数据库备份缺少必要数据表：{', '.join(missing)}")


def restore_database(payload: bytes) -> None:
    db_path = Path(DB.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="scentpool-restore-", suffix=".db", dir=str(db_path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        validate_database_file(temp_path)
        save_database_backup()
        os.replace(temp_path, db_path)
        DB.initialize(
            PRODUCT_FILE_PATH,
            production=os.environ.get("SCENTPOOL_ENV", "").strip().lower() == "production",
            admin_password=os.environ.get("SCENTPOOL_ADMIN_PASSWORD", ""),
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def refresh_tracking_for_shipment(shipment: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = query_tracking(shipment)
    except AppError as exc:
        result = {
            "provider": "kuaidi100",
            "tracking_status": "查询失败",
            "state_code": "",
            "last_event": shipment.get("tracking_last_event") or "",
            "checked_at": now_text(),
            "signed_at": "",
            "error": exc.message,
            "raw": "",
            "is_signed": False,
        }
    return DB.apply_tracking_result(int(shipment["id"]), result)


def refresh_tracking_for_return(return_order: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = query_tracking(return_order)
    except AppError as exc:
        result = {
            "provider": "kuaidi100",
            "tracking_status": "查询失败",
            "state_code": "",
            "last_event": return_order.get("tracking_last_event") or "",
            "checked_at": now_text(),
            "signed_at": "",
            "error": exc.message,
            "raw": "",
            "is_signed": False,
        }
    return DB.apply_return_tracking_result(int(return_order["id"]), result)


def recently_checked(row: Dict[str, Any]) -> bool:
    checked_at = str(row.get("tracking_last_checked_at") or "").strip()
    if not checked_at:
        return False
    try:
        return datetime.fromisoformat(checked_at) > datetime.fromisoformat(manual_refresh_stale_before())
    except ValueError:
        return False


def require_manual_tracking_allowed(row: Dict[str, Any]) -> None:
    if recently_checked(row):
        raise AppError("该单号 30 分钟内已查询过，请稍后再试，避免快递100锁单。", 429)


def sync_tracking_batch(*, force: bool = False, limit: int = 20) -> Dict[str, Any]:
    candidates = DB.tracking_candidates(stale_before="" if force else tracking_stale_before(), limit=limit)
    results = []
    signed = 0
    errors = 0
    for shipment in candidates:
        result = refresh_tracking_for_shipment(shipment)
        signed += 1 if result.get("status") == "已签收" else 0
        errors += 1 if result.get("tracking_status") == "查询失败" else 0
        results.append(result)
    return {"checked": len(results), "signed": signed, "errors": errors, "results": results}


def sync_return_tracking_batch(*, force: bool = False, limit: int = 20) -> Dict[str, Any]:
    candidates = DB.return_tracking_candidates(stale_before="" if force else return_tracking_stale_before(), limit=limit)
    results = []
    signed = 0
    errors = 0
    for return_order in candidates:
        result = refresh_tracking_for_return(return_order)
        signed += 1 if result.get("status") == "已签收" else 0
        errors += 1 if result.get("tracking_status") == "查询失败" else 0
        results.append(result)
    return {"checked": len(results), "signed": signed, "errors": errors, "results": results}


def tracking_worker() -> None:
    time.sleep(60)
    while True:
        try:
            sync_tracking_batch(force=False, limit=50)
            sync_return_tracking_batch(force=False, limit=50)
        except Exception as exc:
            print(f"[tracking] 自动同步失败：{exc}")
        time.sleep(1800)


def start_tracking_worker() -> None:
    if not tracking_auto_enabled():
        return
    thread = threading.Thread(target=tracking_worker, name="scentpool-tracking", daemon=True)
    thread.start()


class Handler(BaseHTTPRequestHandler):
    server_version = "ScentpoolExpress/1.0"

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def do_PATCH(self) -> None:
        self.route()

    def do_DELETE(self) -> None:
        self.route()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def route(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}

            if path.startswith("/api/"):
                self.route_api(path, query)
                return
            if path.startswith("/static/"):
                self.serve_static(path)
                return
            if path in {"/", "/login", "/submit", "/shipments", "/returns/new", "/returns", "/admin", "/admin/returns", "/admin/stores", "/admin/products"}:
                self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            self.error_json("页面不存在。", 404)
        except AppError as exc:
            self.error_json(exc.message, exc.status)
        except Exception as exc:  # pragma: no cover - final safety net for local prototype
            self.error_json(f"服务器错误：{exc}", 500)

    def route_api(self, path: str, query: Dict[str, str]) -> None:
        if path == "/api/health" and self.command == "GET":
            self.send_health()
            return

        if path == "/api/login" and self.command == "POST":
            body = self.read_json()
            user = DB.authenticate(str(body.get("username", "")), str(body.get("password", "")))
            if not user:
                raise AppError("账号或密码不正确。", 401)
            token = DB.create_session(user["id"])
            self.send_json({"user": user}, headers={"Set-Cookie": self.session_cookie(token)})
            return

        if path == "/api/logout" and self.command == "POST":
            token = self.session_token()
            if token:
                DB.delete_session(token)
            self.send_json({"ok": True}, headers={"Set-Cookie": self.expired_session_cookie()})
            return

        user = self.require_user()

        if path == "/api/me" and self.command == "GET":
            self.send_json({"user": user})
            return

        if path == "/api/stores" and self.command == "GET":
            include_inactive = user["role"] == "admin" and query.get("all") == "1"
            self.send_json({"stores": DB.list_stores(include_inactive=include_inactive)})
            return

        if path == "/api/stores" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            store = DB.create_store(
                str(body.get("name", "")),
                str(body.get("username", "")),
                str(body.get("password", "")),
            )
            self.send_json({"store": store}, status=201)
            return

        if path.startswith("/api/stores/") and self.command == "PATCH":
            self.require_admin(user)
            store_id = int(path.rsplit("/", 1)[-1])
            body = self.read_json()
            self.send_json({"store": DB.update_store(store_id, bool(body.get("active")))})
            return

        if path == "/api/products" and self.command == "GET":
            if query.get("all") == "1":
                self.require_admin(user)
                products = DB.list_products(active_only=False)
                self.send_json({"products": products})
            else:
                self.send_json({"categories": DB.grouped_products()})
            return

        if path == "/api/products" and self.command == "POST":
            self.require_admin(user)
            product = DB.upsert_product(self.read_json())
            self.send_json({"product": product, "products": DB.list_products(active_only=False)}, status=201)
            return

        if path.startswith("/api/products/") and self.command == "DELETE":
            self.require_admin(user)
            barcode = unquote(path.split("/", 3)[-1])
            self.send_json({"product": DB.delete_product(barcode), "products": DB.list_products(active_only=False)})
            return

        if path == "/api/products/import" and self.command == "POST":
            self.require_admin(user)
            if self.is_multipart():
                filename, payload = self.read_multipart_file("product_file")
                if not filename.lower().endswith(".xlsx"):
                    raise AppError("请上传 .xlsx 商品文件。")
                target = Path(PRODUCT_FILE_PATH)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                product_path = str(target)
            else:
                body = self.read_json()
                product_path = str(body.get("path") or PRODUCT_FILE_PATH)
            result = DB.import_products(product_path)
            self.send_json({"result": result, "products": DB.list_products(active_only=False)})
            return

        if path == "/api/admin/backup.db" and self.command == "GET":
            self.require_admin(user)
            filename = f"scentpool-backup-{local_now().strftime('%Y%m%d-%H%M%S')}.db"
            self.send_bytes(
                DB.backup_bytes(),
                "application/octet-stream",
                attachment_header(filename, "scentpool-backup.db"),
            )
            return

        if path == "/api/admin/restore-db" and self.command == "POST":
            self.require_admin(user)
            if not ALLOW_DB_RESTORE:
                raise AppError("数据库恢复接口未开启。需要临时设置 SCENTPOOL_ALLOW_DB_RESTORE=1。", 403)
            filename, payload = self.read_multipart_file("backup_file")
            if not filename.lower().endswith(".db"):
                raise AppError("请上传 .db 数据库备份文件。")
            restore_database(payload)
            self.send_json({"ok": True, "summary": DB.database_summary()})
            return

        if path == "/api/admin/tracking/config" and self.command == "GET":
            self.require_admin(user)
            config = tracking_config_public()
            config["return_interval_minutes"] = return_tracking_interval_minutes()
            self.send_json({"tracking": config})
            return

        if path == "/api/admin/tracking/sync" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            result = sync_tracking_batch(force=bool(body.get("force")), limit=int(body.get("limit") or 20))
            self.send_json({"result": result})
            return

        if path == "/api/admin/return-tracking/sync" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            result = sync_return_tracking_batch(force=bool(body.get("force")), limit=int(body.get("limit") or 20))
            self.send_json({"result": result})
            return

        if path == "/api/shipments" and self.command == "POST":
            shipment = DB.create_shipment(user, self.read_json())
            self.send_json({"shipment": shipment}, status=201)
            return

        if path == "/api/shipments" and self.command == "GET":
            self.send_json({"shipments": DB.list_shipments(user, query), "statuses": STATUSES})
            return

        if path.startswith("/api/shipments/") and path.endswith("/tracking/refresh") and self.command == "POST":
            self.require_admin(user)
            shipment_id = int(path.split("/")[3])
            shipment = DB.get_shipment(shipment_id, user)
            require_manual_tracking_allowed(shipment)
            refresh_tracking_for_shipment(shipment)
            self.send_json({"shipment": DB.get_shipment(shipment_id, user)})
            return

        if path.startswith("/api/shipments/") and path.endswith("/items") and self.command == "PATCH":
            shipment_id = int(path.split("/")[3])
            shipment = DB.update_shipment_items(shipment_id, user, self.read_json())
            self.send_json({"shipment": shipment})
            return

        if path.startswith("/api/shipments/") and self.command == "PATCH":
            self.require_admin(user)
            shipment_id = int(path.rsplit("/", 1)[-1])
            update_result = DB.update_shipment(shipment_id, self.read_json())
            shipment = DB.get_shipment(shipment_id, user)
            if update_result.get("should_refresh_tracking"):
                refresh_tracking_for_shipment(shipment)
                shipment = DB.get_shipment(shipment_id, user)
            self.send_json({"shipment": shipment})
            return

        if path.startswith("/api/shipments/") and self.command == "DELETE":
            self.require_admin(user)
            shipment_id = int(path.rsplit("/", 1)[-1])
            self.send_json({"shipment": DB.delete_shipment(shipment_id)})
            return

        if path == "/api/returns" and self.command == "POST":
            return_order = DB.create_return_order(user, self.read_json())
            self.send_json({"return_order": return_order}, status=201)
            return

        if path == "/api/returns" and self.command == "GET":
            self.send_json({"returns": DB.list_return_orders(user, query), "statuses": RETURN_STATUSES})
            return

        if path.startswith("/api/returns/") and path.endswith("/tracking/refresh") and self.command == "POST":
            return_id = int(path.split("/")[3])
            return_order = DB.get_return_order(return_id, user)
            require_manual_tracking_allowed(return_order)
            refresh_tracking_for_return(return_order)
            self.send_json({"return_order": DB.get_return_order(return_id, user)})
            return

        if path == "/api/export/shipments.csv" and self.command == "GET":
            self.require_admin(user)
            shipments = DB.list_shipments(user, query)
            self.send_csv(shipments, export_filename(query, shipments, "csv"))
            return

        if path == "/api/export/shipments.xlsx" and self.command == "GET":
            self.require_admin(user)
            shipments = DB.list_shipments(user, query)
            self.send_xlsx(shipments, export_filename(query, shipments, "xlsx"))
            return

        self.error_json("接口不存在。", 404)

    def serve_static(self, path: str) -> None:
        rel = path.removeprefix("/static/").strip("/")
        file_path = (STATIC_DIR / rel).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self.error_json("文件不存在。", 404)
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript"}:
            content_type += "; charset=utf-8"
        self.serve_file(file_path, content_type)

    def serve_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        payload = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_csv(self, shipments: Any, filename: str) -> None:
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(EXPORT_HEADERS)
        writer.writerows(export_rows(shipments))
        payload = ("\ufeff" + stream.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", attachment_header(filename, "scentpool-shipments.csv"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_xlsx(self, shipments: Any, filename: str) -> None:
        payload = build_shipments_xlsx(shipments)
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", attachment_header(filename, "scentpool-shipments.xlsx"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_bytes(self, payload: bytes, content_type: str, content_disposition: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if content_disposition:
            self.send_header("Content-Disposition", content_disposition)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_health(self) -> None:
        try:
            summary = DB.database_summary()
            self.send_json(
                {
                    "ok": True,
                    "database": True,
                    "products": summary["products"],
                    "shipments": summary["shipments"],
                    "returns": summary["returns"],
                    "time": now_text(),
                }
            )
        except Exception as exc:
            self.send_json({"ok": False, "database": False, "error": str(exc)}, status=503)

    def error_json(self, message: str, status: int) -> None:
        self.send_json({"error": message}, status=status)

    def is_multipart(self) -> bool:
        return self.headers.get("Content-Type", "").lower().startswith("multipart/form-data")

    def read_multipart_file(self, field_name: str) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if not content_type.lower().startswith("multipart/form-data"):
            raise AppError("请使用 multipart/form-data 上传文件。")
        if length <= 0:
            raise AppError("上传文件为空。")
        if length > MAX_UPLOAD_BYTES:
            raise AppError("上传文件不能超过 20MB。", 413)

        raw = self.rfile.read(length)
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = BytesParser(policy=email_policy).parsebytes(header + raw)
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            name = part.get_param("name", header="content-disposition")
            if disposition == "form-data" and name == field_name:
                filename = part.get_filename() or ""
                payload = part.get_payload(decode=True) or b""
                if not filename or not payload:
                    raise AppError("请选择要上传的文件。")
                return filename, payload
        raise AppError("没有找到上传文件。")

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppError("请求 JSON 格式不正确。") from exc
        if not isinstance(data, dict):
            raise AppError("请求内容必须是对象。")
        return data

    def cookie_attributes(self, max_age: int) -> str:
        attrs = f"Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        if SESSION_SECURE:
            attrs += "; Secure"
        return attrs

    def session_cookie(self, token: str) -> str:
        return f"scentpool_session={token}; {self.cookie_attributes(14 * 24 * 3600)}"

    def expired_session_cookie(self) -> str:
        return f"scentpool_session=; {self.cookie_attributes(0)}"

    def session_token(self) -> str:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get("scentpool_session")
        return morsel.value if morsel else ""

    def require_user(self) -> Dict[str, Any]:
        user = DB.user_for_session(self.session_token())
        if not user:
            raise AppError("请先登录。", 401)
        return user

    def require_admin(self, user: Dict[str, Any]) -> None:
        if user.get("role") != "admin":
            raise AppError("需要总部权限。", 403)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def main() -> None:
    production = os.environ.get("SCENTPOOL_ENV", "").strip().lower() == "production"
    default_host = os.environ.get("HOST") or ("0.0.0.0" if production or os.environ.get("PORT") else "127.0.0.1")
    default_port = int(os.environ.get("PORT") or 8765)
    default_db = os.environ.get("SCENTPOOL_DB_PATH") or (
        "/var/data/scentpool.db" if production else str(BASE_DIR / "data" / "scentpool.db")
    )
    default_products = os.environ.get("SCENTPOOL_PRODUCT_FILE") or (
        "/var/data/products.xlsx" if production else DEFAULT_PRODUCT_FILE
    )

    parser = argparse.ArgumentParser(description="Scentpool Express Sync")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", default=default_port, type=int)
    parser.add_argument("--db", default=default_db)
    parser.add_argument("--products", default=default_products)
    args = parser.parse_args()

    global ALLOW_DB_RESTORE, DB, PRODUCT_FILE_PATH, SESSION_SECURE
    ALLOW_DB_RESTORE = env_flag("SCENTPOOL_ALLOW_DB_RESTORE")
    PRODUCT_FILE_PATH = args.products
    SESSION_SECURE = env_flag("SCENTPOOL_SESSION_SECURE") or production
    DB = Database(args.db)
    DB.initialize(
        args.products,
        production=production,
        admin_password=os.environ.get("SCENTPOOL_ADMIN_PASSWORD", ""),
    )
    if production and DB.default_credentials_active():
        raise RuntimeError("生产数据库仍可使用默认账号密码登录，请先重置 admin 和门店账号密码。")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"万物香铺快递同步已启动：http://{args.host}:{args.port}")
    print(f"数据库：{args.db}")
    print(f"商品文件：{args.products}")
    if not production:
        print("本地开发默认账号：admin / scentpool2026、store01 / scentpool2026")
    start_tracking_worker()
    server.serve_forever()


if __name__ == "__main__":
    main()
