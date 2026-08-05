from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape

from database import AppError, Database, DEFAULT_PRODUCT_FILE, RETURN_STATUSES, STATUSES, now_text
from shipping import (
    Kuaidi100LabelClient,
    label_config_public,
    label_enabled,
    parse_auth_callback,
    verify_callback_signature,
)
from tracking import detect_tracking_company, manual_refresh_stale_before, query_tracking, tracking_auto_enabled, tracking_config_public, tracking_interval_minutes, tracking_stale_before


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB: Database
PRODUCT_FILE_PATH = DEFAULT_PRODUCT_FILE
SESSION_SECURE = False
ALLOW_DB_RESTORE = False


def bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_FORM_BODY_BYTES = 1024 * 1024
MAX_BATCH_PRINT_ORDERS = bounded_env_int("SCENTPOOL_MAX_BATCH_PRINT_ORDERS", 200, 1, 300)
MAX_LABEL_PDF_BYTES = 10 * 1024 * 1024
MAX_BATCH_PDF_BYTES = 60 * 1024 * 1024
MAX_REQUEST_THREADS = bounded_env_int("SCENTPOOL_MAX_REQUEST_THREADS", 8, 2, 16)
LABEL_MERGE_TIMEOUT_SECONDS = bounded_env_int("SCENTPOOL_LABEL_MERGE_TIMEOUT_SECONDS", 600, 60, 1200)
SLOW_REQUEST_MILLISECONDS = bounded_env_int("SCENTPOOL_SLOW_REQUEST_MILLISECONDS", 1000, 250, 10000)
SHIPPING_TRANSIENT_RETRIES = bounded_env_int("SCENTPOOL_SHIPPING_TRANSIENT_RETRIES", 2, 0, 3)
DAILY_AUDIT_PATH = "/api/admin/system/daily-audit"
MAX_AUDIT_QUERY_LENGTH = 64
MAX_AUDIT_AUTHORIZATION_LENGTH = 512
TRACKING_SYNC_LOCK = threading.Lock()
RETURN_TRACKING_SYNC_LOCK = threading.Lock()


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
    "业务ID",
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

EXPORT_COLUMN_WIDTHS = [28, 10, 20, 14, 18, 10, 12, 16, 36, 52, 24, 12, 22, 14, 46, 20, 20, 24, 20]


def export_rows(shipments: Any) -> list[list[Any]]:
    rows = []
    for row in shipments:
        rows.append(
            [
                row["business_id"],
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


def build_table_xlsx(headers: list[str], data_rows: list[list[Any]], widths: list[int], sheet_name: str) -> bytes:
    rows = [headers, *data_rows]
    max_col = excel_col(len(headers))
    dimension = f"A1:{max_col}{max(len(rows), 1)}"
    cols = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
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
    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
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


def build_shipments_xlsx(shipments: Any) -> bytes:
    return build_table_xlsx(EXPORT_HEADERS, export_rows(shipments), EXPORT_COLUMN_WIDTHS, "发货明细")


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


def inline_header(filename: str, fallback: str) -> str:
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def build_batch_label_pdf_file(shipments: list[Dict[str, Any]], work_dir: Path) -> Path:
    job_path = work_dir / "job.json"
    output_path = work_dir / "labels.pdf"
    job_shipments = []
    for shipment in shipments:
        business_id = str(shipment.get("business_id") or shipment.get("id") or "未知")
        label_urls = [part.strip() for part in str(shipment.get("label_url") or "").split(",") if part.strip()]
        if not label_urls:
            raise AppError(f"订单 {business_id} 没有可打印的面单。", 409)
        job_shipments.append(
            {
                "id": shipment.get("id"),
                "business_id": business_id,
                "label_urls": label_urls,
            }
        )
    job_path.write_text(
        json.dumps(
            {
                "shipments": job_shipments,
                "max_label_bytes": MAX_LABEL_PDF_BYTES,
                "max_total_bytes": MAX_BATCH_PDF_BYTES,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(BASE_DIR / "label_pdf.py"),
        "--job",
        str(job_path),
        "--output",
        str(output_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=LABEL_MERGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("批量面单合并超时，请减少勾选数量后重试。", 504) from exc
    child_result: Dict[str, Any] = {}
    for line in reversed((completed.stdout or "").splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            child_result = parsed
            break
    if completed.returncode != 0 or not output_path.is_file():
        message = ""
        if child_result:
            message = str(child_result.get("error") or "")
        raise AppError(
            message or "批量面单合并没有完成，请减少勾选数量后重试；订单尚未标记为已打印。",
            502,
        )
    if output_path.stat().st_size > MAX_BATCH_PDF_BYTES:
        raise AppError("合并后的面单文件超过限制，请减少勾选数量后重试。", 413)
    print(
        "[labels] merged "
        f"orders={len(shipments)} pages={int(child_result.get('pages') or 0)} "
        f"source_bytes={int(child_result.get('source_bytes') or 0)} "
        f"output_bytes={output_path.stat().st_size}",
        flush=True,
    )
    return output_path


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
    return DB.backup_to(backup_path)


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
    if (
        result.get("tracking_status") == "查询失败"
        and shipment.get("booking_status") == "已出单"
        and shipment.get("booking_task_id")
        and not shipment.get("tracking_last_event")
        and not result.get("last_event")
    ):
        result = {
            **result,
            "tracking_status": "等待揽收",
            "state_code": "",
            "last_event": "",
            "error": "",
            "is_signed": False,
        }
    return DB.apply_tracking_result(int(shipment["id"]), result)


def refresh_tracking_for_return(return_order: Dict[str, Any]) -> Dict[str, Any]:
    company = str(return_order.get("express_company") or "").strip()
    company_code = str(return_order.get("express_company_code") or "").strip()
    company_source = str(return_order.get("express_company_source") or "manual").strip()
    tracking_status = str(return_order.get("tracking_status") or "").strip()
    needs_detection = (
        not company
        or not company_code
        or company_source in {"manual", "auto_pending"}
        or tracking_status == "查询失败"
    )
    detection_error = ""

    if needs_detection:
        try:
            detected = detect_tracking_company(str(return_order.get("tracking_no") or ""))
            company = str(detected.get("express_company") or "").strip()
            company_code = str(detected.get("company_code") or "").strip()
            company_source = "kuaidi100"
            return_order = {
                **return_order,
                "express_company": company,
                "express_company_code": company_code,
                "express_company_source": company_source,
            }
        except AppError as exc:
            detection_error = exc.message
        except Exception as exc:
            print(f"[tracking] 退货快递公司识别异常：{type(exc).__name__}")
            detection_error = "系统暂时无法完成快递公司识别，请稍后在退货看板重试。"
        if detection_error and not company and not company_code:
            result = {
                "provider": "kuaidi100",
                "tracking_status": "查询失败",
                "state_code": "",
                "last_event": return_order.get("tracking_last_event") or "",
                "checked_at": now_text(),
                "signed_at": "",
                "error": f"快递公司自动识别失败：{detection_error}",
                "raw": "",
                "is_signed": False,
            }
            return DB.apply_return_tracking_result(int(return_order["id"]), result)

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
    except Exception as exc:
        print(f"[tracking] 退货物流查询异常：{type(exc).__name__}")
        result = {
            "provider": "kuaidi100",
            "tracking_status": "查询失败",
            "state_code": "",
            "last_event": return_order.get("tracking_last_event") or "",
            "checked_at": now_text(),
            "signed_at": "",
            "error": "系统暂时无法完成退货物流查询，请稍后在退货看板重试。",
            "raw": "",
            "is_signed": False,
        }
    if detection_error and result.get("tracking_status") == "查询失败":
        detection_detail = detection_error.rstrip("。；; ")
        query_error = str(result.get("error") or "暂时没有取得物流信息。").rstrip("。；; ")
        saved_company = company or company_code or "未知快递公司"
        result = {
            **result,
            "error": f"快递公司重新识别失败：{detection_detail}；按已保存的“{saved_company}”查询也失败：{query_error}。",
        }
    if company_source == "kuaidi100" and company and company_code:
        result = {
            **result,
            "express_company": company,
            "express_company_code": company_code,
            "express_company_source": company_source,
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
    if not TRACKING_SYNC_LOCK.acquire(blocking=False):
        return {"checked": 0, "signed": 0, "errors": 0, "skipped_recent": 0, "remaining": 0, "busy": True}
    stale_before = manual_refresh_stale_before() if force else tracking_stale_before()
    try:
        total = DB.tracking_candidate_count()
        eligible = DB.tracking_candidate_count(stale_before=stale_before)
        candidates = DB.tracking_candidates(stale_before=stale_before, limit=limit)
        checked = 0
        signed = 0
        errors = 0
        for shipment in candidates:
            result = refresh_tracking_for_shipment(shipment)
            checked += 1
            signed += 1 if result.get("status") == "已签收" else 0
            errors += 1 if result.get("tracking_status") == "查询失败" else 0
        return {
            "checked": checked,
            "signed": signed,
            "errors": errors,
            "skipped_recent": max(0, total - eligible),
            "remaining": max(0, eligible - checked),
            "busy": False,
        }
    finally:
        TRACKING_SYNC_LOCK.release()


def sync_return_tracking_batch(*, force: bool = False, limit: int = 20) -> Dict[str, Any]:
    if not RETURN_TRACKING_SYNC_LOCK.acquire(blocking=False):
        return {"checked": 0, "signed": 0, "errors": 0, "busy": True}
    try:
        candidates = DB.return_tracking_candidates(
            stale_before="" if force else return_tracking_stale_before(), limit=limit
        )
        checked = 0
        signed = 0
        errors = 0
        for return_order in candidates:
            result = refresh_tracking_for_return(return_order)
            checked += 1
            signed += 1 if result.get("status") == "已签收" else 0
            errors += 1 if result.get("tracking_status") == "查询失败" else 0
        return {"checked": checked, "signed": signed, "errors": errors, "busy": False}
    finally:
        RETURN_TRACKING_SYNC_LOCK.release()


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


def process_next_shipping_job() -> bool:
    job = DB.claim_next_shipping_job()
    if not job:
        return False
    client = Kuaidi100LabelClient.from_env()
    settings = DB.shipping_settings_for_company(str(job.get("express_company") or ""))
    result: Dict[str, Any] = {"success": False, "error": "电子面单下单失败。", "raw": ""}
    max_attempts = SHIPPING_TRANSIENT_RETRIES + 1
    for attempt in range(max_attempts):
        try:
            result = client.create_label(job, settings)
        except Exception as exc:
            print(f"[shipping] unexpected label error: {type(exc).__name__}", flush=True)
            result = {
                "success": False,
                "error": "系统处理电子面单时发生内部错误，订单没有被删除。请联系管理员并提供业务ID。",
                "raw": "",
            }
        if result.get("success") or not result.get("retryable"):
            break
        if attempt + 1 < max_attempts:
            print(
                f"[shipping] transient failure; retry={attempt + 1}/{SHIPPING_TRANSIENT_RETRIES}",
                flush=True,
            )
            time.sleep(1 + attempt * 2)
    if not result.get("success") and result.get("retryable") and SHIPPING_TRANSIENT_RETRIES:
        original_error = str(result.get("error") or "电子面单请求失败。")
        result["error"] = (
            f"{original_error} 系统已自动重试 {SHIPPING_TRANSIENT_RETRIES} 次仍未成功，"
            "请稍后点击“重试失败订单”。"
        )
    completed = DB.complete_shipping_job(int(job["batch_item_id"]), result)
    if completed.get("tracking_no"):
        try:
            shipment = DB.get_shipment(int(completed["shipment_id"]), {"role": "admin"})
            refresh_tracking_for_shipment(shipment)
        except Exception as exc:
            print(f"[shipping] 首次物流查询失败：{exc}")
    return True


def shipping_worker() -> None:
    recovered = DB.reset_stale_shipping_jobs()
    next_stale_check = time.monotonic() + 60
    if recovered.get("requeued") or recovered.get("failed"):
        print(f"[shipping] recovered stale jobs: {recovered}", flush=True)
    while True:
        try:
            if time.monotonic() >= next_stale_check:
                recovered = DB.reset_stale_shipping_jobs()
                next_stale_check = time.monotonic() + 60
                if recovered.get("requeued") or recovered.get("failed"):
                    print(f"[shipping] recovered stale jobs: {recovered}", flush=True)
            if label_enabled() and process_next_shipping_job():
                continue
        except Exception as exc:
            print(f"[shipping] 批量下单任务失败：{exc}")
        time.sleep(2)


def start_shipping_worker() -> None:
    thread = threading.Thread(target=shipping_worker, name="scentpool-shipping", daemon=True)
    thread.start()


class FixedThreadPoolHTTPServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], max_workers: int):
        self.max_workers = max(2, int(max_workers))
        self._request_slots = threading.BoundedSemaphore(self.max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="scentpool-http",
        )
        self._executor_closed = False
        super().__init__(server_address, handler_class)

    def process_request(self, request: Any, client_address: Any) -> None:
        self._request_slots.acquire()
        try:
            self._executor.submit(self._process_request, request, client_address)
        except Exception:
            self._request_slots.release()
            self.shutdown_request(request)
            raise

    def _process_request(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._request_slots.release()

    def server_close(self) -> None:
        super().server_close()
        if not self._executor_closed:
            self._executor_closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)


def process_memory_diagnostics() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "active_threads": threading.active_count(),
        "request_thread_limit": MAX_REQUEST_THREADS,
    }
    status_path = Path("/proc/self/status")
    if status_path.exists():
        for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                result["rss_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("VmHWM:"):
                result["peak_rss_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("Threads:"):
                result["os_threads"] = int(line.split()[1])
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "ScentpoolExpress/1.0"

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def do_PATCH(self) -> None:
        self.route()

    def do_PUT(self) -> None:
        self.route()

    def do_DELETE(self) -> None:
        self.route()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def route(self) -> None:
        started_at = time.perf_counter()
        self._request_started_at = started_at
        path = urlparse(self.path).path
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
            if path in {"/", "/login", "/submit", "/shipments", "/returns/new", "/returns", "/admin", "/admin/returns", "/admin/stores", "/admin/products", "/admin/shipping"}:
                self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            self.error_json("页面不存在。", 404)
        except AppError as exc:
            self.error_json(exc.message, exc.status, exc.details)
        except Exception as exc:  # pragma: no cover - final safety net for local prototype
            self.error_json(f"服务器错误：{exc}", 500)
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            if path.startswith("/api/") and path != "/api/health" and elapsed_ms >= SLOW_REQUEST_MILLISECONDS:
                print(f"[slow-request] {self.command} {path} {elapsed_ms:.0f}ms")

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

        if path == "/api/integrations/kuaidi100/label-auth-callback" and self.command == "POST":
            state = str(query.get("state") or "")
            DB.consume_label_auth_session(state)
            credentials = parse_auth_callback(str(self.read_form().get("param") or ""))
            DB.save_label_authorization(credentials)
            html = """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>菜鸟授权成功</title>
            <style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:48px;background:#f5f5f7;color:#1d1d1f}main{max-width:520px;margin:auto;background:white;padding:32px;border-radius:12px}a{color:#06c}</style>
            <main><h1>菜鸟电子面单授权成功</h1><p>授权信息已经写入万物香铺快递系统。</p><a href=\"/admin/shipping\">返回面单设置</a></main></html>"""
            self.send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/integrations/kuaidi100/label-print-callback" and self.command == "POST":
            form = self.read_form()
            task_id = str(form.get("taskId") or "").strip()
            param_raw = str(form.get("param") or "")
            sign = str(form.get("sign") or "")
            if not task_id or not param_raw:
                raise AppError("打印回调参数不完整。")
            salt = DB.booking_salt_for_task(task_id)
            if not verify_callback_signature(param_raw, sign, salt):
                raise AppError("打印回调签名不正确。", 403)
            try:
                callback_param = json.loads(param_raw)
            except json.JSONDecodeError as exc:
                raise AppError("打印回调内容不是有效 JSON。") from exc
            DB.apply_label_print_callback(task_id, param_raw, callback_param)
            self.send_json({"result": True, "returnCode": "200", "message": "成功"})
            return

        if path == DAILY_AUDIT_PATH:
            self.require_audit_token()
            if self.command != "GET":
                self.send_json({"error": "该接口仅支持 GET 请求。"}, status=405, headers={"Allow": "GET"})
                return
            date_text = self.audit_date_parameter()
            try:
                report = DB.daily_audit(date_text)
            except (OSError, sqlite3.Error):
                raise AppError("业务日报暂时不可用。", 503) from None
            self.send_json(report)
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
            with tempfile.TemporaryDirectory(prefix="scentpool-download-backup-") as directory:
                backup_path = DB.backup_to(Path(directory) / "scentpool.db")
                self.send_file(
                    backup_path,
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
            self.send_json({"tracking": config, "shipping": label_config_public()})
            return

        if path == "/api/admin/system/diagnostics" and self.command == "GET":
            self.require_admin(user)
            self.send_json(
                {
                    "time": now_text(),
                    "process": process_memory_diagnostics(),
                    "storage": DB.storage_diagnostics(),
                }
            )
            return

        if path == "/api/admin/task-alerts" and self.command == "GET":
            self.require_admin(user)
            self.send_json(DB.task_alerts())
            return

        if path == "/api/admin/shipping-settings" and self.command == "GET":
            self.require_admin(user)
            self.send_json({"settings": DB.get_shipping_settings(public=True), "shipping": label_config_public()})
            return

        if path == "/api/admin/shipping-settings" and self.command == "PUT":
            self.require_admin(user)
            self.send_json({"settings": DB.update_shipping_settings(self.read_json()), "shipping": label_config_public()})
            return

        if path == "/api/admin/label-auth/cainiao" and self.command == "POST":
            self.require_admin(user)
            state = DB.create_label_auth_session()
            current = DB.get_shipping_settings()
            result = Kuaidi100LabelClient.from_env().begin_cainiao_authorization(
                state, str(current.get("partner_id") or "")
            )
            if not result.get("success"):
                raise AppError(str(result.get("error") or "无法创建菜鸟授权链接。"), 502)
            if result.get("authorized") and result.get("credentials"):
                DB.save_label_authorization(result["credentials"])
            self.send_json({"authorization": result, "settings": DB.get_shipping_settings(public=True)})
            return

        if path == "/api/admin/label-branches/refresh" and self.command == "POST":
            self.require_admin(user)
            current = DB.get_shipping_settings()
            result = Kuaidi100LabelClient.from_env().get_third_info(
                {
                    "partnerId": current.get("partner_id"),
                    "partnerKey": current.get("partner_key"),
                    "net": current.get("partner_net"),
                }
            )
            if not result.get("success"):
                raise AppError(str(result.get("error") or "刷新面单余额失败。"), 502)
            self.send_json({"settings": DB.save_label_branches(result.get("branches") or [])})
            return

        if path == "/api/admin/shipping-batches/preview" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
            self.send_json({"preview": DB.preview_shipping_batch(user, filters)})
            return

        if path == "/api/admin/shipping-batches" and self.command == "POST":
            self.require_admin(user)
            shipping_config = label_config_public()
            if not shipping_config.get("enabled"):
                raise AppError("电子面单服务未开启，请在 Render 设置 SCENTPOOL_KUAIDI100_LABEL_ENABLED=1。", 503)
            if not shipping_config.get("configured"):
                missing = "、".join(shipping_config.get("missing") or [])
                raise AppError(f"电子面单配置不完整，Render 缺少：{missing or '必要环境变量'}。", 503)
            settings = DB.get_shipping_settings()
            if not settings.get("sender_name") or not settings.get("sender_mobile") or not settings.get("sender_address"):
                raise AppError("请先完成总部发货设置。", 409)
            if not settings.get("partner_id") or not settings.get("partner_key"):
                raise AppError("请先在电子面单设置中完成菜鸟账号授权。", 409)
            body = self.read_json()
            choices = body.get("shipments") if isinstance(body.get("shipments"), list) else []
            batch = DB.create_shipping_batch(
                user,
                choices,
                body.get("filters") if isinstance(body.get("filters"), dict) else {},
            )
            self.send_json(batch, status=202)
            return

        if path.startswith("/api/admin/shipping-batches/") and path.endswith("/retry") and self.command == "POST":
            self.require_admin(user)
            batch_id = int(path.split("/")[4])
            self.send_json(DB.retry_shipping_batch(batch_id))
            return

        if path.startswith("/api/admin/shipping-batches/") and self.command == "GET":
            self.require_admin(user)
            batch_id = int(path.rsplit("/", 1)[-1])
            self.send_json(DB.get_shipping_batch(batch_id))
            return

        if path == "/api/admin/tracking/sync" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            result = sync_tracking_batch(force=bool(body.get("force")), limit=int(body.get("limit") or 0))
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

        if path == "/api/shipments/summary" and self.command == "GET":
            self.send_json({"counts": DB.shipment_status_counts(user, query), "statuses": STATUSES})
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

        if path.startswith("/api/shipments/") and path.endswith("/label/cancel") and self.command == "POST":
            self.require_admin(user)
            shipment_id = int(path.split("/")[3])
            body = self.read_json()
            booking = DB.booking_for_cancel(shipment_id)
            result = Kuaidi100LabelClient.from_env().cancel_label(
                booking,
                DB.shipping_settings_for_company(str(booking.get("express_company") or "")),
                str(body.get("reason") or "订单信息需要修改"),
            )
            if not result.get("success"):
                raise AppError(str(result.get("error") or "取消电子面单失败。"), 502)
            DB.mark_booking_cancelled(shipment_id, str(result.get("raw") or ""))
            self.send_json({"shipment": DB.get_shipment(shipment_id, user), "provider_code": result.get("code", "")})
            return

        if path.startswith("/api/shipments/") and path.endswith("/label/reprint") and self.command == "POST":
            self.require_admin(user)
            shipment_id = int(path.split("/")[3])
            shipment = DB.get_shipment(shipment_id, user)
            if not shipment.get("booking_task_id"):
                raise AppError("这个订单没有可复打的电子面单。", 409)
            settings = DB.get_shipping_settings()
            result = Kuaidi100LabelClient.from_env().reprint(
                str(shipment.get("booking_task_id") or ""), str(settings.get("printer_siid") or "")
            )
            if not result.get("success"):
                raise AppError(str(result.get("error") or "电子面单复打失败。"), 502)
            DB.mark_label_reprint(shipment_id, str(result.get("raw") or ""))
            self.send_json({"shipment": DB.get_shipment(shipment_id, user)})
            return

        if path.startswith("/api/shipments/") and path.endswith("/label/printed") and self.command == "POST":
            self.require_admin(user)
            shipment_id = int(path.split("/")[3])
            DB.mark_label_printed(shipment_id)
            self.send_json({"shipment": DB.get_shipment(shipment_id, user)})
            return

        if path == "/api/admin/labels/batch-print" and self.command == "POST":
            self.require_admin(user)
            body = self.read_json()
            shipment_ids = body.get("shipment_ids") if isinstance(body.get("shipment_ids"), list) else []
            if len(shipment_ids) > MAX_BATCH_PRINT_ORDERS:
                raise AppError(f"单次最多合并 {MAX_BATCH_PRINT_ORDERS} 张面单，请分批打印。", 413)
            shipments = DB.batch_print_shipments(shipment_ids)
            with tempfile.TemporaryDirectory(prefix="scentpool-label-merge-") as directory:
                payload_path = build_batch_label_pdf_file(shipments, Path(directory))
                filename = f"面单_{local_now().strftime('%Y-%m-%d_%H%M')}_{len(shipments)}单.pdf"
                self.send_file(
                    payload_path,
                    "application/pdf",
                    inline_header(filename, "scentpool-labels.pdf"),
                )
                DB.mark_labels_printed([int(row["id"]) for row in shipments])
            return

        if path.startswith("/api/shipments/") and path.endswith("/items") and self.command == "PATCH":
            shipment_id = int(path.split("/")[3])
            shipment = DB.update_shipment_items(shipment_id, user, self.read_json())
            self.send_json({"shipment": shipment})
            return

        if path.startswith("/api/shipments/") and path.endswith("/remark") and self.command == "PATCH":
            shipment_id = int(path.split("/")[3])
            shipment = DB.update_shipment_remark(shipment_id, user, self.read_json())
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
            shipment_id = int(path.rsplit("/", 1)[-1])
            self.send_json({"shipment": DB.delete_shipment(shipment_id, user)})
            return

        if path == "/api/returns" and self.command == "POST":
            return_order = DB.create_return_order(user, self.read_json())
            refresh_tracking_for_return(return_order)
            return_order = DB.get_return_order(int(return_order["id"]), user)
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
        self.serve_file(file_path, content_type, cache_control="public, max-age=300")

    def serve_file(self, path: Path, content_type: str, *, cache_control: str = "no-cache") -> None:
        data = path.read_bytes()
        data, content_encoding = self.compressible_payload(data, content_type)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        payload = json_bytes(data)
        payload, content_encoding = self.compressible_payload(payload, "application/json")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
            self.send_header("Vary", "Accept-Encoding")
        started_at = getattr(self, "_request_started_at", None)
        if started_at is not None:
            self.send_header("Server-Timing", f"app;dur={(time.perf_counter() - started_at) * 1000:.1f}")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def compressible_payload(self, payload: bytes, content_type: str) -> tuple[bytes, str]:
        accepts_gzip = "gzip" in str(self.headers.get("Accept-Encoding") or "").lower()
        is_compressible = content_type.startswith("text/") or content_type.startswith("application/json") or "javascript" in content_type
        if not accepts_gzip or not is_compressible or len(payload) < 1024:
            return payload, ""
        return gzip.compress(payload, compresslevel=5), "gzip"

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

    def send_file(self, path: Path, content_type: str, content_disposition: str = "") -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if content_disposition:
            self.send_header("Content-Disposition", content_disposition)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def send_health(self) -> None:
        try:
            DB.health_check()
            self.send_json(
                {
                    "ok": True,
                    "database": True,
                    "time": now_text(),
                }
            )
        except Exception as exc:
            self.send_json({"ok": False, "database": False, "error": str(exc)}, status=503)

    def error_json(self, message: str, status: int, details: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"error": message}
        if details:
            payload.update(details)
        self.send_json(payload, status=status)

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
        if length > MAX_JSON_BODY_BYTES:
            raise AppError("请求内容不能超过 1MB。", 413)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppError("请求 JSON 格式不正确。") from exc
        if not isinstance(data, dict):
            raise AppError("请求内容必须是对象。")
        return data

    def read_form(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_FORM_BODY_BYTES:
            raise AppError("表单内容不能超过 1MB。", 413)
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}

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

    def require_audit_token(self) -> None:
        authorization_values = self.headers.get_all("Authorization") or []
        supplied = ""
        valid_header = False
        if len(authorization_values) == 1:
            authorization = str(authorization_values[0])
            if len(authorization) <= MAX_AUDIT_AUTHORIZATION_LENGTH:
                scheme, separator, candidate = authorization.partition(" ")
                valid_header = bool(
                    separator
                    and scheme.lower() == "bearer"
                    and candidate
                    and candidate == candidate.strip()
                    and " " not in candidate
                )
                if valid_header:
                    supplied = candidate

        configured = os.environ.get("SCENTPOOL_AUDIT_TOKEN", "")
        configured_valid = bool(configured.strip()) and len(configured) <= MAX_AUDIT_AUTHORIZATION_LENGTH
        expected = configured if configured_valid else ""
        supplied_digest = hashlib.sha256(supplied.encode("utf-8")).digest()
        expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
        authenticated = secrets.compare_digest(supplied_digest, expected_digest)
        if not (valid_header and configured_valid and authenticated):
            raise AppError("审计接口认证失败。", 401)

    def audit_date_parameter(self) -> str:
        raw_query = urlparse(self.path).query
        if len(raw_query) > MAX_AUDIT_QUERY_LENGTH:
            raise AppError("date 参数必须是 YYYY-MM-DD 格式。")
        try:
            params = parse_qs(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        except ValueError:
            raise AppError("date 参数必须是 YYYY-MM-DD 格式。") from None
        if set(params) != {"date"} or len(params["date"]) != 1:
            raise AppError("date 参数必须是 YYYY-MM-DD 格式。")
        date_text = params["date"][0]
        if len(date_text) != 10 or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date_text):
            raise AppError("date 参数必须是 YYYY-MM-DD 格式。")
        try:
            parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            raise AppError("date 参数必须是有效日期。") from None
        if parsed_date.strftime("%Y-%m-%d") != date_text:
            raise AppError("date 参数必须是有效日期。")
        return date_text

    def require_user(self) -> Dict[str, Any]:
        user = DB.user_for_session(self.session_token())
        if not user:
            raise AppError("请先登录。", 401)
        return user

    def require_admin(self, user: Dict[str, Any]) -> None:
        if user.get("role") != "admin":
            raise AppError("需要总部权限。", 403)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path == "/api/health" and args and str(args[0]).startswith("GET /api/health"):
            return
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

    server = FixedThreadPoolHTTPServer((args.host, args.port), Handler, MAX_REQUEST_THREADS)
    print(f"万物香铺快递同步已启动：http://{args.host}:{args.port}")
    print(f"数据库：{args.db}")
    print(f"商品文件：{args.products}")
    if not production:
        print("本地开发默认账号：admin / scentpool2026、store01 / scentpool2026")
    start_tracking_worker()
    start_shipping_worker()
    server.serve_forever()


if __name__ == "__main__":
    main()
