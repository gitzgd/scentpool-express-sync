"""Durable, bounded read-only-provider work. No customer/provider payloads in queue tables."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

MAX_ACTIVE = 5000
COOLDOWN = 30 * 60
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
TABLES = {"shipment": "shipments", "return": "return_orders"}


def checked_epoch(value):
    from database import timestamp_value
    parsed = timestamp_value(value, assume_local=True)
    return parsed.timestamp() if parsed else 0


def checked_epoch_sql(column):
    # Current values have an explicit offset. Only legacy naive values use the
    # same Shanghai assumption as database.normalize_timestamp; never shift an
    # already offset-aware timestamp for a second time.
    value = f"trim({column})"
    normalized = (f"CASE WHEN substr({value},-1) IN ('Z','z') OR "
                  f"substr({value},-6,1) IN ('+','-') THEN {value} "
                  f"ELSE {value}||'+08:00' END")
    return f"COALESCE((julianday({normalized})-2440587.5)*86400,0)"


def migrate(conn):
    for table in TABLES.values():
        if "tracking_revision" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tracking_revision INTEGER NOT NULL DEFAULT 0")
    if "content_revision" not in {r["name"] for r in conn.execute("PRAGMA table_info(shipments)")}:
        conn.execute("ALTER TABLE shipments ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 0")
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS shipment_content_revision AFTER UPDATE OF
            recipient_name,phone,address,remark,internal_note,cooperation_subject,related_return_id,
            original_shipment_id,shipment_type ON shipments
        BEGIN UPDATE shipments SET content_revision=content_revision+1 WHERE id=NEW.id; END;
        CREATE TRIGGER IF NOT EXISTS shipment_items_revision_insert AFTER INSERT ON shipment_items
        BEGIN UPDATE shipments SET content_revision=content_revision+1 WHERE id=NEW.shipment_id; END;
        CREATE TRIGGER IF NOT EXISTS shipment_items_revision_update AFTER UPDATE ON shipment_items
        BEGIN UPDATE shipments SET content_revision=content_revision+1 WHERE id IN (OLD.shipment_id,NEW.shipment_id); END;
        CREATE TRIGGER IF NOT EXISTS shipment_items_revision_delete AFTER DELETE ON shipment_items
        BEGIN UPDATE shipments SET content_revision=content_revision+1 WHERE id=OLD.shipment_id; END;
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracking_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('shipment','return')),
            record_id INTEGER NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','failed','superseded')),
            origin TEXT NOT NULL DEFAULT 'initial' CHECK(origin IN ('initial','manual','automatic')),
            created_at REAL NOT NULL, due_at REAL NOT NULL,
            started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            failure_category TEXT NOT NULL DEFAULT '', duration_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS tracking_jobs_active ON tracking_jobs(kind,record_id,revision)
            WHERE state IN ('queued','running');
        CREATE INDEX IF NOT EXISTS tracking_jobs_due ON tracking_jobs(state,due_at,created_at);
        CREATE INDEX IF NOT EXISTS tracking_jobs_record ON tracking_jobs(kind,record_id,id);
        CREATE INDEX IF NOT EXISTS tracking_jobs_created ON tracking_jobs(created_at);
        CREATE INDEX IF NOT EXISTS tracking_jobs_finished ON tracking_jobs(finished_at);
        CREATE TABLE IF NOT EXISTS tracking_provider_gate (
            id INTEGER PRIMARY KEY CHECK(id=1), next_allowed_at REAL NOT NULL DEFAULT 0,
            pause_until REAL NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
            last_kind TEXT NOT NULL DEFAULT 'return'
        );
        INSERT OR IGNORE INTO tracking_provider_gate(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS tracking_tasks (
            id TEXT PRIMARY KEY, created_by INTEGER NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
            created_at REAL NOT NULL, UNIQUE(created_by,request_key)
        );
        CREATE TABLE IF NOT EXISTS tracking_task_jobs (
            task_id TEXT NOT NULL REFERENCES tracking_tasks(id) ON DELETE CASCADE,
            job_id INTEGER NOT NULL REFERENCES tracking_jobs(id), PRIMARY KEY(task_id,job_id)
        );
        CREATE TABLE IF NOT EXISTS return_submissions (
            store_id INTEGER NOT NULL REFERENCES stores(id), request_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL, return_id INTEGER REFERENCES return_orders(id) ON DELETE SET NULL,
            PRIMARY KEY(store_id,request_key)
        );
    """)
    # Created only after legacy migrations. Installation does not queue/change historical business rows.
    for kind, table in TABLES.items():
        identity = ["tracking_no", "phone", "express_company", "booking_request_id", "status"] if kind == "shipment" else ["tracking_no", "sender_phone", "status"]
        changes = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in identity)
        eligible = "NEW.status='已发货'" if kind == "shipment" else "NEW.status NOT IN ('已签收','已取消')"
        # A status change caused by a tracking result invalidates the completed job, not an extra query.
        should_queue = ("NEW.tracking_no IS NOT OLD.tracking_no OR NEW.express_company IS NOT OLD.express_company "
                        "OR NEW.phone IS NOT OLD.phone OR (NEW.status='已发货' AND OLD.status<>'已发货')") if kind == "shipment" else "NEW.tracking_no IS NOT OLD.tracking_no OR NEW.sender_phone IS NOT OLD.sender_phone"
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_tracking_revision AFTER UPDATE OF {','.join(identity)} ON {table}
            WHEN {changes}
            BEGIN
                UPDATE {table} SET tracking_revision=OLD.tracking_revision+1 WHERE id=NEW.id;
                UPDATE tracking_jobs SET state='superseded', finished_at=strftime('%s','now')
                    WHERE kind='{kind}' AND record_id=NEW.id AND state IN ('queued','running');
                INSERT INTO tracking_jobs(kind,record_id,revision,created_at,due_at)
                    SELECT '{kind}',NEW.id,OLD.tracking_revision+1,strftime('%s','now'),strftime('%s','now')
                    WHERE ({should_queue}) AND {eligible} AND NEW.tracking_no<>'';
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_tracking_insert AFTER INSERT ON {table}
            WHEN {eligible} AND NEW.tracking_no<>''
            BEGIN
                INSERT INTO tracking_jobs(kind,record_id,revision,created_at,due_at)
                    VALUES('{kind}',NEW.id,NEW.tracking_revision,strftime('%s','now'),strftime('%s','now'));
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_tracking_delete AFTER DELETE ON {table}
            BEGIN
                UPDATE tracking_jobs SET state='superseded', finished_at=strftime('%s','now')
                    WHERE kind='{kind}' AND record_id=OLD.id AND state IN ('queued','running');
            END;
        """)


class TrackingQueue:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def eligible(kind):
        return "tracking_no<>'' AND tracking_signed_at='' AND " + ("status='已发货'" if kind == "shipment" else "status NOT IN ('已签收','已取消')")

    def enqueue(self, conn, kind, row, origin, now):
        previous = conn.execute("SELECT * FROM tracking_jobs WHERE kind=? AND record_id=? AND revision=? AND state IN ('queued','running')", (kind,row["id"],row["tracking_revision"])).fetchone()
        if previous:
            return int(previous["id"])
        if conn.execute("SELECT COUNT(*) FROM tracking_jobs WHERE state IN ('queued','running')").fetchone()[0] >= MAX_ACTIVE:
            from database import AppError
            raise AppError("物流查询队列暂时繁忙，请稍后重试。已保存的订单不会丢失。", 503)
        # Per-record cooldown includes failed/crashed attempts and identity changes (no bypass by ABA).
        last = conn.execute("SELECT COALESCE(MAX(started_at),0) FROM tracking_jobs WHERE kind=? AND record_id=?",(kind,row["id"])).fetchone()[0]
        due = max(now, float(last or 0)+COOLDOWN)
        checked = checked_epoch(row["tracking_last_checked_at"])
        due = max(due, float(checked or 0)+COOLDOWN)
        cur = conn.execute("INSERT INTO tracking_jobs(kind,record_id,revision,origin,created_at,due_at) VALUES(?,?,?,?,?,?)", (kind,row["id"],row["tracking_revision"],origin,now,due))
        return int(cur.lastrowid)

    def request(self, user, kind, record_id=None, request_key="", *, subscribe=False):
        from database import AppError
        if kind not in TABLES:
            raise AppError("物流任务类型无效。")
        if record_id is None and user.get("role") != "admin":
            raise AppError("需要总部权限。",403)
        if request_key and not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",request_key):
            raise AppError("提交标识无效。")
        fingerprint=hashlib.sha256(json.dumps([kind,record_id]).encode()).hexdigest()
        now=time.time()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key:
                old=conn.execute("SELECT * FROM tracking_tasks WHERE created_by=? AND request_key=?",(user["id"],request_key)).fetchone()
                if old:
                    if old["payload_hash"] != fingerprint:
                        raise AppError("提交内容与上次不同，请先确认原任务结果。",409)
                    return self.task_with_connection(conn,old["id"],user)
            # Repeated clicks without a client key reuse the active same-scope task.
            old=conn.execute("SELECT t.id FROM tracking_tasks t WHERE created_by=? AND payload_hash=? AND EXISTS(SELECT 1 FROM tracking_task_jobs x JOIN tracking_jobs j ON j.id=x.job_id WHERE x.task_id=t.id AND j.state IN ('queued','running')) ORDER BY created_at DESC LIMIT 1",(user["id"],fingerprint)).fetchone()
            if old:
                return self.task_with_connection(conn,old["id"],user)
            params=[]
            where=self.eligible(kind)
            if record_id is not None:
                where="id=?"; params=[record_id]
            if user.get("role")=="staff":
                where+=" AND store_id=?"; params.append(user["store_id"])
            rows=conn.execute(f"SELECT id,tracking_revision,tracking_last_checked_at,tracking_no,status,tracking_signed_at FROM {TABLES[kind]} WHERE {where} ORDER BY id LIMIT ?",[*params,MAX_ACTIVE+1]).fetchall()
            if record_id is not None and not rows:
                raise AppError("记录不存在或无权查看。",404)
            if len(rows)>MAX_ACTIVE:
                raise AppError("待查记录超过单次安全范围，请联系总部缩小范围。",413)
            task_id=secrets.token_urlsafe(18)
            conn.execute("INSERT INTO tracking_tasks VALUES(?,?,?,?,?,?)",(task_id,user["id"],kind,request_key or task_id,fingerprint,now))
            for row in rows:
                if subscribe:
                    latest=conn.execute("SELECT id FROM tracking_jobs WHERE kind=? AND record_id=? AND revision=? ORDER BY id DESC LIMIT 1",(kind,row["id"],row["tracking_revision"])).fetchone()
                    if latest:
                        conn.execute("INSERT OR IGNORE INTO tracking_task_jobs VALUES(?,?)",(task_id,latest["id"]))
                    continue
                if not row["tracking_no"] or row["status"] in ("已签收","已取消") or row["tracking_signed_at"]:
                    continue
                job_id=self.enqueue(conn,kind,row,"manual",now)
                conn.execute("INSERT OR IGNORE INTO tracking_task_jobs VALUES(?,?)",(task_id,job_id))
            return self.task_with_connection(conn,task_id,user)

    def task_with_connection(self,conn,task_id,user):
        from database import AppError
        task=conn.execute("SELECT * FROM tracking_tasks WHERE id=?",(task_id,)).fetchone()
        if not task or (user.get("role")!="admin" and task["created_by"]!=user.get("id")):
            raise AppError("任务不存在、已过期或无权查看。请回看订单的最新物流状态。",404)
        counts={r["state"]:r["n"] for r in conn.execute("SELECT j.state,COUNT(*) n FROM tracking_task_jobs t JOIN tracking_jobs j ON j.id=t.job_id WHERE t.task_id=? GROUP BY j.state",(task_id,))}
        total=sum(counts.values()); remaining=counts.get("queued",0)+counts.get("running",0)
        categories={r["failure_category"]:r["n"] for r in conn.execute("SELECT j.failure_category,COUNT(*) n FROM tracking_task_jobs t JOIN tracking_jobs j ON j.id=t.job_id WHERE t.task_id=? AND j.failure_category<>'' GROUP BY j.failure_category",(task_id,))}
        due=conn.execute("SELECT MIN(j.due_at) FROM tracking_task_jobs t JOIN tracking_jobs j ON j.id=t.job_id WHERE t.task_id=? AND j.state='queued'",(task_id,)).fetchone()[0]
        pause=conn.execute("SELECT pause_until FROM tracking_provider_gate WHERE id=1").fetchone()[0]
        wait=max(0,int(max(float(due or 0),pause)-time.time())) if remaining else 0
        message="已加入查询队列，无需重复点击。" if remaining else "本次查询已结束。"
        if counts.get("running"):
            message="物流正在后台查询，您可继续其他操作。"
        elif wait>0:
            message=("物流服务暂时不可用，系统将按规则重试。" if pause>time.time() else "已排队，遵守同单 30 分钟查询间隔。")+f"预计至少等待 {(wait+59)//60} 分钟，无需重复点击。"
        elif counts.get("failed"):
            message=f"查询已结束，其中 {counts['failed']} 项未完成。请查看异常提醒中的原因与处理建议。"
        return {"id":task_id,"kind":task["kind"],"status":"running" if counts.get("running") else ("queued" if remaining else ("failed" if counts.get("failed") else "completed")),"total":total,"completed":counts.get("completed",0),"failed":counts.get("failed",0),"skipped":counts.get("superseded",0),"remaining":remaining,"failure_categories":categories,"wait_seconds":wait,"provider_paused":pause>time.time(),"message":message}

    def task(self,task_id,user):
        with self.db.connect() as conn:
            return self.task_with_connection(conn,task_id,user)

    def schedule(self,conn,intervals,now=None):
        now=time.time() if now is None else now
        conn.execute("BEGIN IMMEDIATE")
        try:
            for kind,seconds in intervals.items():
                rows=conn.execute(f"""SELECT id,tracking_revision,tracking_last_checked_at FROM {TABLES[kind]} r
                    WHERE {self.eligible(kind)} AND {checked_epoch_sql('tracking_last_checked_at')}<=?
                    AND NOT EXISTS(SELECT 1 FROM tracking_jobs j WHERE j.kind=? AND j.record_id=r.id AND
                        (j.state IN ('queued','running') OR j.started_at>? OR j.finished_at>?))
                    ORDER BY tracking_last_checked_at,id LIMIT 20""",(now-seconds,kind,now-seconds,now-seconds)).fetchall()
                for row in rows:
                    if conn.execute("SELECT COUNT(*) FROM tracking_jobs WHERE state IN ('queued','running')").fetchone()[0]>=MAX_ACTIVE:
                        break
                    self.enqueue(conn,kind,row,"automatic",now)
            conn.commit()
        except Exception:
            conn.rollback(); raise

    def claim(self,conn,now=None):
        now=time.time() if now is None else now
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE tracking_jobs SET state=CASE WHEN attempt_count>=? THEN 'failed' ELSE 'queued' END, due_at=MAX(due_at,started_at+?), failure_category='interrupted', finished_at=CASE WHEN attempt_count>=? THEN ? ELSE 0 END,lease_token='' WHERE state='running' AND lease_until<=?",(MAX_ATTEMPTS,COOLDOWN,MAX_ATTEMPTS,now,now))
            gate=conn.execute("SELECT * FROM tracking_provider_gate WHERE id=1").fetchone()
            if max(gate["next_allowed_at"],gate["pause_until"],gate["lease_until"])>now:
                conn.commit(); return None
            preferred="return" if gate["last_kind"]=="shipment" else "shipment"
            job=conn.execute("SELECT * FROM tracking_jobs WHERE state='queued' AND due_at<=? ORDER BY CASE WHEN kind=? THEN 0 ELSE 1 END,created_at,id LIMIT 1",(now,preferred)).fetchone()
            if not job:
                conn.commit(); return None
            row=conn.execute(f"SELECT * FROM {TABLES[job['kind']]} WHERE id=? AND tracking_revision=? AND {self.eligible(job['kind'])}",(job["record_id"],job["revision"])).fetchone()
            if not row:
                conn.execute("UPDATE tracking_jobs SET state='superseded',finished_at=? WHERE id=?",(now,job["id"]))
                conn.commit(); return None
            # Trigger-generated jobs must obey the same per-record cooldown as manual/automatic ones.
            recent=conn.execute("SELECT COALESCE(MAX(started_at),0) FROM tracking_jobs WHERE kind=? AND record_id=? AND id<>?",(job["kind"],job["record_id"],job["id"])).fetchone()[0]
            checked=checked_epoch(row["tracking_last_checked_at"])
            allowed=max(float(recent),float(checked))+COOLDOWN
            if allowed>now:
                conn.execute("UPDATE tracking_jobs SET due_at=? WHERE id=?",(allowed,job["id"]))
                conn.commit(); return None
            token=secrets.token_hex(16)
            conn.execute("UPDATE tracking_jobs SET state='running',attempt_count=attempt_count+1,started_at=?,lease_until=?,lease_token=? WHERE id=?",(now,now+LEASE_SECONDS,token,job["id"]))
            conn.execute("UPDATE tracking_provider_gate SET next_allowed_at=?,lease_until=?,lease_token=?,last_kind=? WHERE id=1",(now+1,now+LEASE_SECONDS,token,job["kind"]))
            conn.commit()
            return {**dict(job),"lease_token":token,"attempt_count":job["attempt_count"]+1,"started_at":now,"row":dict(row)}
        except Exception:
            conn.rollback(); raise

    def finish(self,conn,job,result,duration_ms,now=None):
        now=time.time() if now is None else now
        conn.execute("BEGIN IMMEDIATE")
        try:
            current=conn.execute("SELECT * FROM tracking_jobs WHERE id=? AND lease_token=? AND state='running'",(job["id"],job["lease_token"])).fetchone()
            gate=conn.execute("SELECT * FROM tracking_provider_gate WHERE id=1").fetchone()
            owns_gate=gate["lease_token"]==job["lease_token"]
            # A lease is a fencing token, not only a scheduling hint: an expired
            # worker must never publish results or alter the provider circuit.
            if not current or current["lease_until"]<=now or not owns_gate or gate["lease_until"]<=now:
                if owns_gate:
                    conn.execute("UPDATE tracking_provider_gate SET lease_until=0,lease_token='' WHERE id=1")
                conn.commit(); return {"discarded":True}
            row=conn.execute(f"SELECT tracking_revision FROM {TABLES[job['kind']]} WHERE id=?",(job["record_id"],)).fetchone()
            if not row or row["tracking_revision"]!=job["revision"]:
                state="superseded"; category="superseded"
            elif result.get("system_error"):
                state="queued" if current["attempt_count"]<MAX_ATTEMPTS else "failed"
                category="provider_unavailable"
                if owns_gate:
                    failures=gate["consecutive_failures"]+1
                    conn.execute("UPDATE tracking_provider_gate SET pause_until=?,consecutive_failures=? WHERE id=1",(now+min(1800,60*(2**min(failures-1,5))),failures))
            else:
                apply=self.db.apply_tracking_result if job["kind"]=="shipment" else self.db.apply_return_tracking_result
                apply(job["record_id"],result,connection=conn,expected_revision=job["revision"])
                state="failed" if result.get("tracking_status")=="查询失败" else "completed"
                category="no_result_or_invalid" if state=="failed" else ""
                if owns_gate:
                    conn.execute("UPDATE tracking_provider_gate SET pause_until=0,consecutive_failures=0 WHERE id=1")
            conn.execute("UPDATE tracking_jobs SET state=?,failure_category=?,finished_at=?,due_at=?,duration_ms=?,lease_until=0,lease_token='' WHERE id=?",(state,category,0 if state=="queued" else now,max(now,job["started_at"]+COOLDOWN),max(0,int(duration_ms)),job["id"]))
            if owns_gate:
                conn.execute("UPDATE tracking_provider_gate SET lease_until=0,lease_token='',next_allowed_at=MAX(next_allowed_at,?) WHERE id=1",(now+1,))
            conn.commit(); return {"state":state,"category":category,"discarded":state=="superseded"}
        except Exception:
            conn.rollback(); raise

    def summary(self):
        with self.db.connect() as conn:
            now=time.time()
            row=conn.execute("""SELECT COUNT(*) total,
                COALESCE(SUM(state='queued'),0) queued, COALESCE(SUM(state='running'),0) running,
                COALESCE(SUM(state='failed'),0) failed, COALESCE(SUM(attempt_count>1),0) retried,
                COALESCE(MAX(CASE WHEN state IN ('queued','running') THEN ?-created_at ELSE 0 END),0) oldest_wait_seconds,
                COALESCE(MAX(duration_ms),0) max_provider_ms
                FROM tracking_jobs WHERE state IN ('queued','running') OR created_at>?""",(now,now-86400)).fetchone()
            gate=conn.execute("SELECT pause_until FROM tracking_provider_gate WHERE id=1").fetchone()
            return {**dict(row),"provider_paused":gate[0]>now,"capacity":MAX_ACTIVE}

    def prune(self, conn, now=None):
        """Bounded, low-frequency cleanup of internal execution metadata, never business rows."""
        now = time.time() if now is None else now
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""DELETE FROM tracking_tasks WHERE id IN (
                SELECT t.id FROM tracking_tasks t WHERE created_at<? AND NOT EXISTS(
                    SELECT 1 FROM tracking_task_jobs x JOIN tracking_jobs j ON j.id=x.job_id
                    WHERE x.task_id=t.id AND j.state IN ('queued','running')) LIMIT 200)""",(now-90*86400,))
            conn.execute("""DELETE FROM tracking_jobs WHERE id IN (
                SELECT j.id FROM tracking_jobs j WHERE state NOT IN ('queued','running') AND finished_at<?
                AND NOT EXISTS(SELECT 1 FROM tracking_task_jobs t WHERE t.job_id=j.id) LIMIT 200)""",(now-30*86400,))
            conn.commit()
        except Exception:
            conn.rollback(); raise
