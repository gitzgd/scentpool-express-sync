"""Private service-to-service bridge. No inventory accounting or carrier calls here.

The feed is a coalescing state changefeed, NOT an immutable accounting ledger.
SQL triggers atomically retain notifications, including hard deletes. A reader
sees only the latest committed state, never half of a multi-item transaction.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

from database import AppError, now_text, re_phone_ok

PREFIX = "/api/inventory/v1"
SLOTS = threading.BoundedSemaphore(2)
MAX_BODY = 65536
BOOKABLE = {"kunming", "kunming_sf"}
SOURCE_KINDS = {"store_restock": "inventory_store_restock", "channel": "inventory_channel"}
PUBLIC_FIELDS = ("id", "business_id", "store_id", "shipment_type", "status", "booking_status",
                 "tracking_no", "express_company", "tracking_status", "shipped_at", "updated_at")
STATE_COLUMNS = PUBLIC_FIELDS + ("inventory_source_kind", "inventory_source_ref", "store_name_snapshot", "booking_request_id")


def migrate(conn):
    """All new tables/columns/triggers and baseline share one rollback boundary."""
    conn.execute("SAVEPOINT inventory_migration")
    try:
        _migrate(conn)
        conn.execute("RELEASE inventory_migration")
    except Exception:
        conn.execute("ROLLBACK TO inventory_migration")
        conn.execute("RELEASE inventory_migration")
        raise


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(shipments)")}
    for name, declaration in (
        ("inventory_source_kind", "TEXT NOT NULL DEFAULT 'express'"),
        ("inventory_source_ref", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE shipments ADD COLUMN {name} {declaration}")
    for statement in (
        "CREATE TABLE IF NOT EXISTS inventory_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS inventory_events(
            cursor INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            kind TEXT NOT NULL, source_ref TEXT NOT NULL, source_kind TEXT NOT NULL,
            historical_snapshot INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))))""",
        """CREATE TABLE IF NOT EXISTS inventory_latest(
            shipment_id INTEGER PRIMARY KEY, cursor INTEGER NOT NULL UNIQUE)""",
        "CREATE TABLE IF NOT EXISTS inventory_historical(shipment_id INTEGER PRIMARY KEY)",
        """CREATE TABLE IF NOT EXISTS inventory_dispatches(
            reference TEXT PRIMARY KEY, shipment_id INTEGER UNIQUE REFERENCES shipments(id) ON DELETE SET NULL,
            payload_hash TEXT NOT NULL, purpose TEXT NOT NULL, owner_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS inventory_bookings(
            request_key TEXT PRIMARY KEY, shipment_id INTEGER NOT NULL, profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL, batch_id INTEGER NOT NULL REFERENCES shipping_batches(id))""",
        "CREATE INDEX IF NOT EXISTS idx_inventory_events_shipment ON inventory_events(shipment_id,cursor)",
        "CREATE INDEX IF NOT EXISTS idx_inventory_source ON shipments(inventory_source_kind,id)",
    ):
        conn.execute(statement)
    # The draft schema was used only in synthetic development. Upgrade it
    # transactionally before reinstalling append-only guards.
    if "checkpoint" not in {r["name"] for r in conn.execute("PRAGMA table_info(inventory_events)")}:
        conn.execute("DROP TRIGGER IF EXISTS inventory_events_no_update")
        conn.execute("ALTER TABLE inventory_events ADD COLUMN checkpoint TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE inventory_events SET checkpoint=lower(hex(randomblob(16)))")
    conn.execute("INSERT OR IGNORE INTO inventory_meta VALUES('stream_id',?)", (str(uuid.uuid4()),))
    # Install before historical seeding. No account is created by migration.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS inventory_event_latest AFTER INSERT ON inventory_events
        BEGIN INSERT INTO inventory_latest VALUES(NEW.shipment_id,NEW.cursor)
        ON CONFLICT(shipment_id) DO UPDATE SET cursor=excluded.cursor; END""")
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS inventory_events_no_{action.lower()}
            BEFORE {action} ON inventory_events BEGIN SELECT RAISE(ABORT,'inventory_events_append_only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS inventory_source_immutable
        BEFORE UPDATE OF inventory_source_kind,inventory_source_ref ON shipments
        WHEN NEW.inventory_source_kind<>OLD.inventory_source_kind OR NEW.inventory_source_ref<>OLD.inventory_source_ref
        BEGIN SELECT RAISE(ABORT,'inventory_source_immutable'); END""")
    def notification(expression, kind="shipment.upsert", source="NEW"):
        if kind == "shipment.deleted":
            return f"""INSERT INTO inventory_events(shipment_id,kind,source_ref,source_kind,checkpoint)
                VALUES(OLD.id,'{kind}',OLD.inventory_source_ref,OLD.inventory_source_kind,lower(hex(randomblob(16))));"""
        return f"""INSERT INTO inventory_events(shipment_id,kind,source_ref,source_kind,checkpoint)
            SELECT id,'{kind}',inventory_source_ref,inventory_source_kind,lower(hex(randomblob(16))) FROM shipments WHERE id={expression};"""
    for action, source in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        conn.execute(f"DROP TRIGGER IF EXISTS inventory_shipment_{action.lower()}")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS inventory_shipment_{action.lower()}
            AFTER {action} ON shipments BEGIN
            {notification(source+'.id', 'shipment.deleted' if action == 'DELETE' else 'shipment.upsert')} END""")
        for table in ("shipment_items", "shipping_batch_items"):
            # Changes to items and successful-origin evidence belong to the same feed.
            extra = notification("OLD.shipment_id") if action == "UPDATE" else ""
            conn.execute(f"DROP TRIGGER IF EXISTS inventory_{table}_{action.lower()}")
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS inventory_{table}_{action.lower()}
                AFTER {action} ON {table} BEGIN {extra}{notification(source+'.shipment_id')} END""")
    if not conn.execute("SELECT 1 FROM inventory_meta WHERE key='initialized'").fetchone():
        conn.execute("INSERT INTO inventory_historical SELECT id FROM shipments")
        conn.execute("""INSERT INTO inventory_events(shipment_id,kind,source_ref,source_kind,historical_snapshot,checkpoint)
            SELECT id,'shipment.upsert',inventory_source_ref,inventory_source_kind,1,lower(hex(randomblob(16))) FROM shipments ORDER BY id""")
        conn.execute("INSERT INTO inventory_meta VALUES('initialized',?)", (now_text(),))
    # Querying a local view exposes no recipient, notes, configuration or raw payload.


def text(value, label, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise AppError(f"{label}无效或过长。")
    if any(ord(c) < 32 for c in value):
        raise AppError(f"{label}包含非法控制字符。")
    return value.strip()


def exact(body, keys):
    if not isinstance(body, dict) or set(body) != set(keys):
        raise AppError("请求字段不完整或含不支持的字段。")


def identity(value):
    value = text(value, "幂等标识", 36)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise AppError("幂等标识必须是标准小写 UUID。") from None
    return value


def positive(value, maximum=100000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise AppError("数量或版本必须是范围内的正整数。")
    return value


def authorize(headers):
    expected = os.environ.get("SCENTPOOL_INVENTORY_TOKEN", "")
    # Fail closed if mistakenly provisioned with the audit credential.
    if not re.fullmatch(r"[A-Za-z0-9_-]{64,256}", expected) or expected == os.environ.get("SCENTPOOL_AUDIT_TOKEN"):
        raise AppError("库存集成接口未启用。", 503)
    values = headers.get_all("Authorization", [])
    supplied = values[0][7:] if len(values) == 1 and values[0].startswith("Bearer ") else ""
    if len(supplied) > 256 or not secrets.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest()):
        raise AppError("库存集成认证失败。", 401)


def source_evidence(conn, row):
    if not row["booking_request_id"] or not row["tracking_no"] or row["booking_status"] != "已出单":
        return "", "unknown"
    evidence = conn.execute("""SELECT
        COUNT(*) n, COUNT(DISTINCT CASE WHEN json_valid(settings_snapshot_json)
            THEN json_extract(settings_snapshot_json,'$.profile_id') END) origins,
        MIN(CASE WHEN json_valid(settings_snapshot_json) THEN json_extract(settings_snapshot_json,'$.profile_id') END) profile,
        SUM(CASE WHEN json_valid(settings_snapshot_json) THEN
            CASE WHEN json_extract(settings_snapshot_json,'$.profile_id') IN ('banna','kunming','kunming_sf') THEN 0 ELSE 1 END ELSE 1 END) bad
        FROM shipping_batch_items WHERE shipment_id=? AND request_id=? AND status='成功'""",
        (row["id"], row["booking_request_id"])).fetchone()
    if not evidence["n"] or evidence["origins"] != 1 or evidence["bad"]:
        return "", "unknown"
    profile = evidence["profile"]
    return profile, "kunming" if profile in BOOKABLE else "banna"


def shipment_state(conn, row):
    result = {key: row[key] for key in PUBLIC_FIELDS}
    result.update(source_ref=row["inventory_source_ref"], source_kind=row["inventory_source_kind"],
        historical_reference=bool(conn.execute("SELECT 1 FROM inventory_historical WHERE shipment_id=?", (row["id"],)).fetchone()),
        store_name=row["store_name_snapshot"])
    # Store-entered business IDs sometimes contain phone numbers. Stable surrogate
    # is sufficient for reconciliation; never export that free-text identifier.
    result["business_id"] = f"EXP-{row['id']}" if row["inventory_source_kind"] == "express" else row["business_id"]
    result["profile_id"], result["channel"] = source_evidence(conn, row)
    item_size = conn.execute("SELECT COUNT(*),COALESCE(SUM(length(product_name)+length(product_barcode)),0) FROM shipment_items WHERE shipment_id=?", (row["id"],)).fetchone()
    if item_size[0] > 500 or item_size[1] > 262144:
        raise AppError("单据明细超过接口安全上限，请人工核对；游标未推进。", 413)
    result["items"] = [dict(item) for item in conn.execute("""SELECT product_barcode barcode,
        product_name name,quantity,item_kind FROM shipment_items WHERE shipment_id=? ORDER BY id LIMIT 501""", (row["id"],))]
    if len(result["items"]) > 500:
        raise AppError("单据明细超过接口安全上限，请人工核对；游标未推进。", 413)
    return result


def read_events(db, cursor, limit, stream_id="", checkpoint=""):
    if not re.fullmatch(r"0|[1-9][0-9]{0,17}", cursor) or not re.fullmatch(r"[1-9][0-9]{0,2}", limit) or int(limit) > 100:
        raise AppError("游标或页大小无效，每页最多 100 条。")
    if ((stream_id and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", stream_id))
            or (checkpoint and not re.fullmatch(r"[0-9a-f]{32}", checkpoint))):
        raise AppError("事件流或检查点格式无效。")
    after, size = int(cursor), int(limit)
    with db.connect_readonly() as conn:
        conn.execute("BEGIN")
        deadline = time.monotonic() + 10
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
        watermark = conn.execute("SELECT COALESCE(MAX(cursor),0) FROM inventory_events").fetchone()[0]
        stream = conn.execute("SELECT value FROM inventory_meta WHERE key='stream_id'").fetchone()[0]
        previous = conn.execute("SELECT checkpoint FROM inventory_events WHERE cursor=?", (after,)).fetchone()
        if ((stream_id and stream_id != stream) or (after and (
                stream_id != stream or not previous or not secrets.compare_digest(previous[0], checkpoint)))):
            raise AppError("事件流校验不一致或缺少续读校验，请停止同步并核对数据库恢复；禁止自动清零重扣。", 409)
        if after > watermark:
            raise AppError("游标超过当前事件流，请检查是否恢复过数据库；禁止自动清零重扣库存。", 409)
        rows = conn.execute("""SELECT e.* FROM inventory_latest l JOIN inventory_events e ON e.cursor=l.cursor
            WHERE l.cursor>? ORDER BY l.cursor LIMIT ?""", (after, size+1)).fetchall()
        result = []
        response_bytes = 0
        for event in rows[:size]:
            row = conn.execute("SELECT "+",".join(STATE_COLUMNS)+" FROM shipments WHERE id=?", (event["shipment_id"],)).fetchone()
            state = shipment_state(conn, row) if row else dict(id=event["shipment_id"],source_ref=event["source_ref"],source_kind=event["source_kind"])
            response_bytes += len(json.dumps(state, ensure_ascii=False).encode())
            if response_bytes > 4*1024*1024:
                raise AppError("本页超过安全上限，请缩小 limit；游标未推进。", 413)
            result.append(dict(cursor=event["cursor"],event_id=f"{stream}:{event['cursor']}:{event['checkpoint']}",
                kind="shipment.upsert" if row else "shipment.deleted",historical_snapshot=bool(event["historical_snapshot"]),shipment=state))
        next_cursor = result[-1]["cursor"] if len(rows)>size else watermark
        next_row = conn.execute("SELECT checkpoint FROM inventory_events WHERE cursor=?", (next_cursor,)).fetchone()
        return dict(schema_version=1,events=result,next_cursor=next_cursor,
                    stream_id=stream,next_checkpoint=next_row[0] if next_row else "",
                    has_more=len(rows)>size,watermark=watermark)


def catalog(db):
    from fulfillment_profiles import profiles, selected, ensure_branch, ensure_template, branch_balance_warning
    with db.connect_readonly() as conn:
        conn.execute("BEGIN")
        deadline = time.monotonic() + 10
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
        settings = db.get_shipping_settings(connection=conn)
        result = dict(schema_version=1)
        size = conn.execute("SELECT COALESCE(SUM(length(barcode)+length(name)+length(category)+length(spec)),0) FROM products").fetchone()[0]
        if size > 1000000:
            raise AppError("主档超过安全快照容量，请协调分页升级。",413)
        # Explicit bounds: fail rather than silently truncate a master-data snapshot.
        stores = conn.execute("SELECT id,name,kind,active FROM stores WHERE id NOT IN (SELECT CAST(value AS INTEGER) FROM inventory_meta WHERE key='store_id') ORDER BY id LIMIT 1001").fetchall()
        products = conn.execute("SELECT barcode,name,category,spec,status FROM products ORDER BY barcode LIMIT 20001").fetchall()
        if len(stores)>1000 or len(products)>20000:
            raise AppError("主档超过安全快照上限，请协调分页升级，不会返回截断主档。", 413)
        result["stores"] = [{**dict(r),"active":bool(r["active"])} for r in stores]
        result["products"] = [dict(r) for r in products]
        result["profiles"] = []
        for p in profiles(conn):
            ready = True
            try:
                selected(conn,settings,p["id"]); ensure_template(p); option=ensure_branch(settings,p)
                ready = bool(settings.get("partner_id") and settings.get("partner_key") and p.get("sender_name") and p.get("sender_mobile") and p.get("sender_address"))
                ready = ready and (int(option.get("quantity") or 0)>0 or bool(branch_balance_warning(p,option)))
            except AppError:
                ready = False
            result["profiles"].append(dict(id=p["id"],name=p["name"],revision=p["revision"],ready=ready))
        return result


def actor(conn):
    existing = conn.execute("SELECT value FROM inventory_meta WHERE key='user_id'").fetchone()
    if existing:
        return {"id":int(existing[0]),"role":"admin","inventory_service":True}
    now = now_text()
    # Inactive principal, not a login-capable admin account. No real user is impersonated.
    suffix = secrets.token_hex(8)
    sid = conn.execute("INSERT INTO stores(name,kind,active,created_at,updated_at) VALUES(?,'team',0,?,?)",
        ("库存系统专用-"+suffix,now,now)).lastrowid
    uid = conn.execute("""INSERT INTO users(username,password_hash,role,store_id,active,created_at,updated_at)
        VALUES(?,'!disabled-inventory-service','staff',?,0,?,?)""", ("inventory-service-"+suffix,sid,now,now)).lastrowid
    conn.executemany("INSERT INTO inventory_meta VALUES(?,?)", [("store_id",str(sid)),("user_id",str(uid))])
    return {"id":uid,"role":"admin","inventory_service":True}


def create_dispatch(db, body):
    exact(body, ("reference","purpose","owner","recipient","items","note"))
    reference = identity(body["reference"])
    purpose = body["purpose"]
    if not isinstance(purpose,str) or purpose not in SOURCE_KINDS:
        raise AppError("发货用途无效。")
    exact(body["owner"], ("kind","external_id","name")); exact(body["recipient"],("name","phone","address"))
    owner = {k:text(v,"归属信息",100) for k,v in body["owner"].items()}
    if owner["kind"] != ("store" if purpose == "store_restock" else "channel"):
        raise AppError("用途与归属类型不匹配。")
    recipient = {k:text(body["recipient"][k],"收件信息",limit) for k,limit in (("name",100),("phone",80),("address",1000))}
    if not re_phone_ok(recipient["phone"]):
        raise AppError("联系电话无效。")
    note = text(body["note"],"内部备注",500,empty=True)
    if not isinstance(body["items"],list) or not 1 <= len(body["items"]) <= 100:
        raise AppError("商品明细必须为 1 至 100 行。")
    items = []
    for item in body["items"]:
        exact(item,("barcode","quantity"))
        items.append(dict(barcode=text(item["barcode"],"条码",100),quantity=positive(item["quantity"])))
    if len({i["barcode"] for i in items}) != len(items):
        raise AppError("商品条码重复，请合并数量。")
    items.sort(key=lambda i:i["barcode"])
    normalized = dict(reference=reference,purpose=purpose,owner=owner,recipient=recipient,items=items,note=note)
    digest = hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM inventory_dispatches WHERE reference=?",(reference,)).fetchone()
        if old:
            if old["payload_hash"] != digest or old["shipment_id"] is None:
                raise AppError("原请求内容不同或任务已删除；不会重复创建。",409)
            sid=old["shipment_id"]
        else:
            resolved = db._resolve_items(conn,items)
            user=actor(conn); store_id=int(conn.execute("SELECT value FROM inventory_meta WHERE key='store_id'").fetchone()[0])
            now=now_text(); number="INV-"+reference
            sid=conn.execute("""INSERT INTO shipments(order_date,business_id,store_id,store_name_snapshot,created_by,
                recipient_name,phone,address,store_order_no,status,created_at,updated_at,shipment_type,
                internal_note,inventory_source_kind,inventory_source_ref)
                VALUES(?,?,?,?,?,?,?,?,?,'待处理',?,?,'legacy',?,?,?)""",
                (now[:10],number,store_id,"库存补货" if purpose=='store_restock' else "库存渠道发货",user["id"],
                 recipient["name"],recipient["phone"],recipient["address"],number,now,now,note,SOURCE_KINDS[purpose],reference)).lastrowid
            db._insert_shipment_items(conn,sid,resolved)
            conn.execute("INSERT INTO inventory_dispatches VALUES(?,?,?,?,?)",(reference,sid,digest,purpose,json.dumps(owner,ensure_ascii=False)))
        status=conn.execute("SELECT status FROM shipments WHERE id=?",(sid,)).fetchone()[0]
        return dict(id=sid,reference=reference,status=status)


def owned(conn, sid):
    projection = ",".join("s."+key for key in STATE_COLUMNS + ("label_url",))
    row = conn.execute(f"""SELECT {projection} FROM shipments s JOIN inventory_dispatches d ON d.shipment_id=s.id
        WHERE s.id=? AND s.inventory_source_kind IN ('inventory_store_restock','inventory_channel')""",(sid,)).fetchone()
    if not row:
        raise AppError("库存发货任务不存在。",404)
    return row


def get_dispatch(db, sid):
    with db.connect_readonly() as conn:
        conn.execute("BEGIN")
        row=owned(conn,sid); profile,channel=source_evidence(conn,row)
        available=bool(row["booking_status"]=='已出单' and row["tracking_no"] and row["label_url"])
        failed=row["booking_status"]=='下单失败'
        return dict(id=sid,reference=row["inventory_source_ref"],status=row["status"],booking_status=row["booking_status"],
            tracking_no=row["tracking_no"],tracking_status=row["tracking_status"],express_company=row["express_company"],profile_id=profile,channel=channel,
            label_available=available,label_path=f"{PREFIX}/dispatches/{sid}/label" if available else "",
            failure_category="booking_failed" if failed else "",message="面单未成功；人工确认重试时沿用原请求标识和配置，禁止换新标识重复取号。" if failed else "")


def admin_task(db, sid, user):
    """Narrow, headquarters-session-only view. Never available to service tokens."""
    if user.get("role") != "admin":
        raise AppError("需要总部权限。", 403)
    row = db.get_shipment(sid, user)
    if row.get("inventory_source_kind") not in SOURCE_KINDS.values():
        raise AppError("库存任务不存在。", 404)
    fields = ("id", "business_id", "inventory_source_kind", "inventory_source_ref", "status", "booking_status",
              "recipient_name", "phone", "address", "tracking_no", "express_company", "tracking_status")
    result = {key: row[key] for key in fields}
    result["items"] = [{"name": i["product_name"], "quantity": i["quantity"]} for i in row["items"]]
    result["can_cancel"] = bool(row["booking_status"] == "已出单" and row["booking_task_id"] and row["tracking_no"] and row["status"] != "已签收")
    result["cancel_version"] = cancel_version(row)
    return result


def cancel_version(row):
    return hashlib.sha256(json.dumps([row[key] for key in ("id", "booking_request_id", "booking_task_id", "tracking_no")]).encode()).hexdigest()


def book_dispatch(db, sid, body):
    exact(body,("profile_id","profile_revision","request_key"))
    profile=text(body["profile_id"],"发货方案",32)
    if profile not in BOOKABLE:
        raise AppError("库存任务仅支持昆明中通或昆明顺丰。")
    revision=positive(body["profile_revision"],2147483647); key=identity(body["request_key"])
    with db.connect_readonly() as conn:
        owned(conn,sid)
        user_id=conn.execute("SELECT value FROM inventory_meta WHERE key='user_id'").fetchone()[0]
    user={"id":int(user_id),"role":"admin","inventory_service":True}
    # Existing method performs request replay check, revision check, preview and
    # queue insertion under ONE immediate transaction (including idempotency row).
    batch=db.create_shipping_batch(user,[{"id":sid}],{"id":sid},profile_id=profile,
        inventory_request=dict(request_key=key,profile_revision=revision,shipment_id=sid))
    return {**get_dispatch(db,sid),"batch_id":batch["batch"]["id"]}


def put_product(db, barcode, body):
    barcode=text(barcode,"条码",100)
    exact(body,("name","category","spec","status"))
    fields={k:text(body[k],"商品信息",limit,empty=k in {"category","spec"}) for k,limit in (("name",200),("category",100),("spec",200),("status",10))}
    if fields["status"] not in {"启用","停用"}:
        raise AppError("商品状态无效。")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO products(barcode,name,category,spec,status,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(barcode) DO UPDATE SET name=excluded.name,category=excluded.category,spec=excluded.spec,status=excluded.status,updated_at=excluded.updated_at
            WHERE products.name<>excluded.name OR products.category<>excluded.category OR products.spec<>excluded.spec OR products.status<>excluded.status""",
            (barcode,fields["name"],fields["category"],fields["spec"],fields["status"],now_text()))
    return dict(barcode=barcode,**fields)


def handle(handler, db):
    authorize(handler.headers)
    if not SLOTS.acquire(blocking=False):
        raise AppError("库存集成正在处理其他请求，请稍后沿用原标识重试。",503)
    try:
        parsed=urlparse(handler.path); path=parsed.path
        query=parse_qs(parsed.query,keep_blank_values=True)
        allowed={"cursor","limit","stream_id","checkpoint"} if path==PREFIX+"/events" else set()
        if len(parsed.query)>300 or set(query)-allowed or any(len(v)!=1 for v in query.values()):
            raise AppError("查询参数无效。")
        body={}
        if handler.command in {"POST","PUT"}:
            lengths = handler.headers.get_all("Content-Length", [])
            if len(lengths)!=1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or handler.headers.get("Transfer-Encoding"):
                handler.close_connection=True
                raise AppError("请求长度无效。",400)
            if int(lengths[0])>MAX_BODY:
                handler.close_connection=True
                raise AppError("库存请求不能超过 64 KiB。",413)
            body=handler.read_json()
        if path==PREFIX+"/catalog" and handler.command=="GET": result=catalog(db)
        elif path==PREFIX+"/events" and handler.command=="GET": result=read_events(db,query.get("cursor",["0"])[0],query.get("limit",["100"])[0],query.get("stream_id",[""])[0],query.get("checkpoint",[""])[0])
        elif path==PREFIX+"/dispatches" and handler.command=="POST": result=create_dispatch(db,body)
        elif re.fullmatch(PREFIX+r"/products/[^/]+",path) and handler.command=="PUT": result=put_product(db,unquote(path.rsplit("/",1)[-1]),body)
        elif match:=re.fullmatch(PREFIX+r"/dispatches/([1-9][0-9]{0,11})(/book|/label)?",path):
            sid=int(match[1]); action=match[2] or ""
            if not action and handler.command=="GET": result=get_dispatch(db,sid)
            elif action=="/book" and handler.command=="POST":
                result=book_dispatch(db,sid,body)
                import server
                server.SHIPPING_QUEUE_EVENT.set()
            elif action=="/label" and handler.command=="GET":
                serve_label(handler,db,sid);return
            else: raise AppError("该接口不支持此操作。",405)
        else: raise AppError("库存接口不存在或不支持此操作。",404)
        handler.send_json(result)
    finally:
        SLOTS.release()


def serve_label(handler, db, sid):
    import tempfile
    from pathlib import Path
    import server
    with db.connect_readonly() as conn:
        row=dict(owned(conn,sid))
        if row["booking_status"]!='已出单' or not row["tracking_no"] or not row["label_url"]:
            raise AppError("当前任务没有有效面单。",409)
    if not server.LABEL_BATCH_PRINT_LOCK.acquire(blocking=False):
        raise AppError("面单文件正在生成，请稍后重试。",503)
    try:
        server.ensure_batch_print_memory_available()
        with tempfile.TemporaryDirectory(prefix="inventory-label-") as directory:
            path=server.build_batch_label_pdf_file([row],Path(directory))
            # Do not return a label cancelled/replaced while download was running.
            with db.connect_readonly() as conn:
                current=owned(conn,sid)
                if any(current[k]!=row[k] for k in ("booking_request_id","tracking_no","label_url","booking_status")):
                    raise AppError("面单状态已变化，请重新核对。",409)
            handler.send_file(path,"application/pdf",'inline; filename="inventory-label.pdf"')
    finally:
        server.LABEL_BATCH_PRINT_LOCK.release()
