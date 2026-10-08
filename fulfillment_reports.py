"""Read-only, current-effective dispatch reporting. No inventory mutations."""
from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import time
import threading
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from database import APP_TZ, AppError, Database, timestamp_value
from shipment_types import SHIPMENT_TYPES
from bounded_exports import EXPORT_SECONDS, MAX_EXPORT_BYTES, MAX_SNAPSHOT_BYTES

CHANNELS = {"banna": "版纳", "kunming": "昆明"}
READ_SLOTS = threading.BoundedSemaphore(2)
POLICY = "按当前有效面单及北京时间登记发货日期统计；等待揽收包含在内，不等同于实际出库或库存余额。取消成功移除原统计，重新出单按新日期和新渠道计入。"


def migrate(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_batch_request ON shipping_batch_items(shipment_id,request_id,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_item_shipment ON shipment_items(shipment_id)")


def filters_for(user, query):
    if user.get("role") not in {"admin", "staff"}:
        raise AppError("请登录后查看发货统计。", 403)
    today = datetime.now(APP_TZ).date().isoformat()
    result = {key: str(query.get(key) or default).strip() for key, default in (
        ("date_from", today), ("date_to", today), ("channel", "all"),
        ("store_id", ""), ("shipment_type", ""), ("q", ""), ("view", "summary"))}
    try:
        start, end = (date.fromisoformat(result[k]) for k in ("date_from", "date_to"))
        if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result[k]) for k in ("date_from", "date_to")):
            raise ValueError
        if not 0 <= (end - start).days <= 3660:
            raise ValueError
        page = int(query.get("page", 1)); size = int(query.get("page_size", 50))
        if page < 1 or not 1 <= size <= 50:
            raise ValueError
        store_id = int(result["store_id"]) if result["store_id"] else None
        if store_id is not None and store_id < 1:
            raise ValueError
    except (ValueError, TypeError):
        raise AppError("请检查日期、门店和页码；结束日期不能早于开始日期，日期范围最多十年，每页最多 50 行。")
    if user["role"] == "staff":
        if not user.get("store_id") or (store_id and store_id != user["store_id"]):
            raise AppError("只能查看所属门店或团队的发货统计。", 403)
        result["store_id"] = str(user["store_id"])
    if result["channel"] not in {"all", *CHANNELS} or result["view"] not in {"summary", "daily_products"}:
        raise AppError("发货渠道或统计视图无效。")
    if result["shipment_type"] and result["shipment_type"] not in SHIPMENT_TYPES:
        raise AppError("发货类型无效。")
    if len(result["q"]) > 100:
        raise AppError("商品搜索最多 100 字。")
    result.update(page=page, page_size=size)
    return result


def _instant(value):
    parsed = timestamp_value(value, assume_local=True)
    return parsed.timestamp() if parsed else None


def _day(value):
    parsed = timestamp_value(value, assume_local=True)
    return parsed.astimezone(APP_TZ).date().isoformat() if parsed else None


class Report:
    def __init__(self, conn, user, filters, *, seconds=10):
        self.conn, self.filters = conn, filters
        self.staff_store = user.get("store_id") if user["role"] == "staff" else None
        deadline = time.monotonic() + seconds
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
        conn.create_function("report_day", 1, _day, deterministic=True)
        conn.create_function("report_time", 1, _instant, deterministic=True)
        where = ["s.status IN ('已发货','已签收')"]
        self.params = dict(filters)
        if filters["store_id"]:
            where.append("s.store_id=:store_id")
        if filters["shipment_type"]:
            where.append("s.shipment_type=:shipment_type")
        self.params["search"] = filters["q"].casefold()
        conn.create_function("report_fold", 1, lambda v: str(v or "").casefold(), deterministic=True)
        match = "(:search='' OR instr(report_fold(i.product_name),:search)>0 OR instr(report_fold(i.product_barcode),:search)>0)"
        where.append(f"EXISTS (SELECT 1 FROM shipment_items i WHERE i.shipment_id=s.id AND {match})")
        # Inspect only the profile identifier inside private snapshots. A current
        # request can have repeated attempts, but must have one consistent origin.
        self.cte = f"""WITH scoped AS (
            SELECT s.id,s.store_id,s.store_name_snapshot,st.kind AS store_kind,
                s.shipped_at,s.shipped_at_quality,s.shipped_at_source,s.booking_status,s.tracking_no,
                s.booking_request_id,s.tracking_status,s.tracking_state_code,
                report_day(s.shipped_at) AS day,
                CASE WHEN report_time(s.shipped_at) IS NOT NULL
                    AND report_time(s.created_at) IS NOT NULL
                    AND report_time(s.shipped_at)>=report_time(s.created_at)
                    AND (s.status<>'已签收' OR (report_time(s.tracking_signed_at) IS NOT NULL
                        AND report_time(s.tracking_signed_at)>=report_time(s.shipped_at)))
                    THEN 1 ELSE 0 END AS valid_time
            FROM shipments s JOIN stores st ON st.id=s.store_id WHERE {' AND '.join(where)}
        ), evidence AS (
            SELECT s.id,COUNT(b.id) AS attempts,
                COUNT(DISTINCT CASE WHEN json_valid(b.settings_snapshot_json)
                    THEN json_extract(b.settings_snapshot_json,'$.profile_id') END) AS origins,
                MIN(CASE WHEN json_valid(b.settings_snapshot_json)
                    THEN json_extract(b.settings_snapshot_json,'$.profile_id') END) AS channel,
                SUM(CASE WHEN json_valid(b.settings_snapshot_json) THEN
                    CASE WHEN json_extract(b.settings_snapshot_json,'$.profile_id') IN ('banna','kunming')
                        THEN 0 ELSE 1 END ELSE 1 END) AS bad
            FROM scoped s LEFT JOIN shipping_batch_items b
                ON b.shipment_id=s.id AND b.request_id=s.booking_request_id
                AND s.booking_request_id<>'' AND b.status='成功' GROUP BY s.id
        ), classified AS (
            SELECT s.*, CASE WHEN e.attempts>0 AND e.origins=1 AND e.bad=0
                AND s.booking_status='已出单' AND s.tracking_no<>''
                THEN e.channel ELSE 'unknown' END AS channel
            FROM scoped s JOIN evidence e ON s.id=e.id
        ), eligible AS (
            SELECT * FROM classified WHERE valid_time=1 AND channel IN ('banna','kunming')
                AND day BETWEEN :date_from AND :date_to
                AND (:channel='all' OR channel=:channel)
        ), lines AS (
            SELECT e.*,i.id AS item_id,i.item_kind,i.product_barcode,i.product_name,
                i.product_category,i.material_spec,i.quantity,
                CASE WHEN i.item_kind='material' THEN json_array('material',i.product_name,i.material_spec)
                    ELSE json_array('product',i.product_barcode) END AS item_key
            FROM eligible e JOIN shipment_items i ON i.shipment_id=e.id WHERE {match}
        ), names AS (
            SELECT item_key,product_name,product_category,
                ROW_NUMBER() OVER (PARTITION BY item_key ORDER BY report_time(shipped_at) DESC,id DESC,item_id DESC) AS rn
            FROM lines
        ), variants AS (
            SELECT item_key,COUNT(DISTINCT product_name) AS name_count FROM lines GROUP BY item_key
        ) """

    def execute(self, sql, **params):
        try:
            return self.conn.execute(self.cte + sql, {**self.params, **params})
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise AppError("统计查询超过安全时间，请缩小日期范围后重试。订单未被修改。", 503) from None
            raise

    def summary(self):
        return dict(self.execute("""SELECT COUNT(DISTINCT id) AS orders,
            COUNT(DISTINCT CASE WHEN item_kind='product' THEN item_key END) AS product_kinds,
            COALESCE(SUM(CASE WHEN item_kind='product' THEN quantity ELSE 0 END),0) AS product_quantity,
            COALESCE(SUM(CASE WHEN item_kind='material' THEN quantity ELSE 0 END),0) AS material_quantity,
            COUNT(DISTINCT CASE WHEN tracking_status IN ('等待揽收','待揽收')
                THEN id END) AS waiting_orders FROM lines""").fetchone())

    def daily(self):
        found = {(r["day"], r["channel"]): dict(r) for r in self.execute("""SELECT day,channel,
            COUNT(DISTINCT id) AS orders,
            SUM(CASE WHEN item_kind='product' THEN quantity ELSE 0 END) AS product_quantity,
            SUM(CASE WHEN item_kind='material' THEN quantity ELSE 0 END) AS material_quantity
            FROM lines GROUP BY day,channel""")}
        current = date.fromisoformat(self.filters["date_from"])
        end = date.fromisoformat(self.filters["date_to"])
        while current <= end:
            day = current.isoformat()
            row = {"date": day}
            for channel in CHANNELS:
                values = found.get((day, channel), {})
                for key in ("orders", "product_quantity", "material_quantity"):
                    row[f"{channel}_{key}"] = values.get(key, 0)
            for key in ("orders", "product_quantity", "material_quantity"):
                row[key] = sum(row[f"{c}_{key}"] for c in CHANNELS)
            yield row
            if current == end: break
            current += timedelta(days=1)

    def quality(self):
        return dict(self.execute("""SELECT
            COALESCE(SUM(CASE WHEN day BETWEEN :date_from AND :date_to AND channel='unknown' THEN 1 ELSE 0 END),0) AS unknown_channel,
            COALESCE(SUM(CASE WHEN day BETWEEN :date_from AND :date_to AND valid_time=0
                AND (:channel='all' OR channel=:channel) THEN 1 ELSE 0 END),0) AS invalid_time,
            COALESCE(SUM(CASE WHEN day IS NULL AND (:channel='all' OR channel=:channel OR channel='unknown') THEN 1 ELSE 0 END),0) AS undated_all_time,
            COALESCE(SUM(CASE WHEN valid_time=1 AND channel IN ('banna','kunming')
                AND day BETWEEN :date_from AND :date_to AND (:channel='all' OR channel=:channel)
                AND shipped_at_quality='estimated' THEN 1 ELSE 0 END),0) AS estimated_time,
            COALESCE(SUM(CASE WHEN valid_time=1 AND channel IN ('banna','kunming')
                AND day BETWEEN :date_from AND :date_to AND (:channel='all' OR channel=:channel)
                AND shipped_at_quality NOT IN ('exact','estimated') THEN 1 ELSE 0 END),0) AS unclassified_time
            FROM classified""").fetchone())

    def products_sql(self, daily=False):
        day = "l.day" if daily else "''"
        return f"""SELECT {day} AS date,l.channel,l.store_id,MAX(l.store_name_snapshot) AS store_name,
            l.store_kind,l.item_kind,l.product_barcode,n.product_name,n.product_category,l.material_spec,
            SUM(l.quantity) AS quantity,COUNT(DISTINCT l.id) AS orders,(v.name_count>1) AS name_changed
            FROM lines l JOIN names n ON n.item_key=l.item_key AND n.rn=1
            JOIN variants v ON v.item_key=l.item_key
            GROUP BY {day},l.channel,l.store_id,l.item_key
            ORDER BY date DESC,l.channel,l.store_id,l.item_kind,l.item_key"""

    def result(self):
        sql = self.products_sql(self.filters["view"] == "daily_products")
        total = self.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
        size = self.filters["page_size"]; pages = max(1, (total + size - 1) // size)
        page = min(self.filters["page"], pages)
        rows = [dict(r) for r in self.execute(sql + " LIMIT :limit OFFSET :offset", limit=size, offset=(page-1)*size)]
        # Includes inactive stores with history; never leaks other store choices to staff.
        scope = "WHERE id=?" if self.staff_store else ""
        stores = [dict(r) for r in self.conn.execute(f"SELECT id,name,kind FROM stores {scope} ORDER BY id", (self.staff_store,) if self.staff_store else ())]
        return {"generated_at": datetime.now(APP_TZ).isoformat(timespec="seconds"), "timezone": "Asia/Shanghai",
            "policy": POLICY, "filters": self.filters, "summary": self.summary(), "daily": list(self.daily()),
            "rows": rows, "stores": stores, "quality": self.quality(),
            "pagination": {"page": page,"page_size":size,"total":total,"total_pages":pages}}


def read_report(db, user, query):
    filters = filters_for(user, query)
    if not READ_SLOTS.acquire(blocking=False):
        raise AppError("统计正在被其他同事查询，请稍后刷新。订单处理不受影响。", 503)
    try:
        with db.connect_readonly() as conn:
            conn.execute("BEGIN")
            return Report(conn, user, filters).result()
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise AppError("统计查询超时，请缩小日期范围后重试。", 503) from None
        raise
    finally:
        READ_SLOTS.release()


def export_report(db, user, query, directory, *, workbook_template=None):
    """One private backup snapshot; stream all four sheets, never collect all items."""
    # The running server passes its template to avoid importing server.py a
    # second time when it is running as __main__ (an unnecessary resident copy).
    if workbook_template is None:
        from server import build_table_xlsx
        workbook_template = build_table_xlsx([], [], [], "template")
    filters = filters_for(user, query)
    started = time.monotonic(); directory = Path(directory)
    snapshot = directory / "report-snapshot.db"; output = directory / "fulfillment-report.xlsx"
    def check(*_args):
        if time.monotonic() - started > EXPORT_SECONDS:
            raise AppError("导出超时，请缩小日期范围。订单未被修改。", 503)
        if output.exists() and output.stat().st_size > MAX_EXPORT_BYTES:
            raise AppError("导出过大，请缩小日期范围。", 413)
    with db.connect_readonly() as source:
        size = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
        if size > MAX_SNAPSHOT_BYTES or shutil.disk_usage(directory).free < size + MAX_EXPORT_BYTES + 64*1024*1024:
            raise AppError("导出所需临时空间不足或数据库超出安全范围，请联系总部。", 413)
        dest = sqlite3.connect(snapshot)
        try:
            os.chmod(snapshot, 0o600)
            source.backup(dest, pages=128, progress=check, sleep=.01)
        finally:
            dest.close()
    generated = datetime.now(APP_TZ).isoformat(timespec="seconds")
    with Database(str(snapshot)).connect_readonly() as conn:
        report = Report(conn, user, filters, seconds=max(1, EXPORT_SECONDS-(time.monotonic()-started)))
        summary, quality = report.summary(), report.quality()
        def items(daily):
            for r in report.execute(report.products_sql(daily)):
                yield ([r["date"]] if daily else []) + [CHANNELS[r["channel"]],r["store_name"],
                    "合作团队" if r["store_kind"] == "team" else "门店",
                    "临时物料" if r["item_kind"] == "material" else "正式商品",r["product_barcode"],
                    r["product_name"],r["product_category"],r["material_spec"],r["quantity"],r["orders"],
                    "历史名称有变化" if r["name_changed"] else ""]
        daily_keys = ["date","banna_orders","kunming_orders","orders","banna_product_quantity","kunming_product_quantity",
                      "product_quantity","banna_material_quantity","kunming_material_quantity","material_quantity"]
        headers = ["发货渠道","门店／团队","归属类型","明细类型","商品条码","商品名称","品类","物料规格","数量","涉及订单数","名称提示"]
        selected_store = conn.execute("SELECT name FROM stores WHERE id=?", (filters["store_id"],)).fetchone() if filters["store_id"] else None
        notes = [["统计口径",POLICY],["快照时间",generated],["时区","Asia/Shanghai"],
            ["发货渠道",CHANNELS.get(filters["channel"],"全部（版纳＋昆明）")],
            ["开始日期",filters["date_from"]],["结束日期",filters["date_to"]],
            ["归属范围",selected_store[0] if selected_store else ("所选归属无记录" if filters["store_id"] else "权限范围内全部门店／团队")],
            ["发货类型",SHIPMENT_TYPES[filters["shipment_type"]][0] if filters["shipment_type"] else "全部"],
            ["商品搜索",filters["q"]],["发货订单数",summary["orders"]],["商品种数",summary["product_kinds"]],
            ["商品数量",summary["product_quantity"]],["临时物料数量",summary["material_quantity"]],
            ["等待揽收订单数",summary["waiting_orders"]],
            ["所选期间渠道待核实（无法按渠道归属）",quality["unknown_channel"]],
            ["所选期间时间证据冲突",quality["invalid_time"]],
            ["全部日期范围内无法归属日期的记录",quality["undated_all_time"]],
            ["已计入：估算时间",quality["estimated_time"]],["已计入：时间精度未分类",quality["unclassified_time"]],
            ["注意","已下载文件不自动更新；取消或重新出单后请重新导出。商品行的订单数不能相加作为总订单数。"]]
        sheets = [
            ("每日渠道概览",["发货日期","版纳订单数","昆明订单数","合计订单数","版纳商品数量","昆明商品数量","商品合计","版纳物料数量","昆明物料数量","物料合计"],
             ([r[k] for k in daily_keys] for r in report.daily())),
            ("门店商品汇总",headers,items(False)),
            ("每日商品明细",["发货日期",*headers],items(True)),
            ("统计说明",["项目","内容"],iter(notes))]
        with zipfile.ZipFile(io.BytesIO(workbook_template)) as initial, zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as z:
            os.chmod(output,0o600)
            # Explicit text format as well as inlineStr protects barcode zeros in
            # spreadsheet viewers which otherwise auto-detect numeric-looking text.
            styles = initial.read("xl/styles.xml").decode().replace('<cellXfs count="3">', '<cellXfs count="4">')
            styles = styles.replace('</cellXfs>', '<xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf></cellXfs>')
            z.writestr("xl/styles.xml", styles)
            z.writestr("_rels/.rels", initial.read("_rels/.rels"))
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,5))+'</Types>')
            z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(sheets,1))+'</sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,5))+'<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            for index, (_, columns, rows) in enumerate(sheets,1):
                with z.open(f"xl/worksheets/sheet{index}.xml","w",force_zip64=True) as out:
                    out.write(('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'+''.join(f'<col min="{i}" max="{i}" width="{60 if index==4 and i==2 else 24}" customWidth="1"/>' for i in range(1,len(columns)+1))+'</cols><sheetData>').encode())
                    def write_row(n, values):
                        if n > 1048576: raise AppError("超过 Excel 行数上限，请缩小范围。",413)
                        check(); cells=[]
                        for c,value in enumerate(values,1):
                            ref=f"{chr(64+c)}{n}"; style=1 if n==1 else (2 if isinstance(value,int) else 3)
                            if isinstance(value,int): cells.append(f'<c r="{ref}" s="{style}" t="n"><v>{value}</v></c>')
                            else:
                                text=escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]","",str(value or "")))
                                cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
                        out.write((f'<row r="{n}">'+''.join(cells)+'</row>').encode())
                    write_row(1,columns); last=1
                    for last, row in enumerate(rows,2): write_row(last,row)
                    out.write(f'</sheetData><autoFilter ref="A1:{chr(64+len(columns))}{last}"/></worksheet>'.encode())
        check()
    return output, filters
