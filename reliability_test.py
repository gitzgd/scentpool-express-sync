"""Synthetic-only reliability regressions; never calls a carrier or production."""
import json
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from database import Database, AppError, now_text, APP_TZ
from special_shipments_test import seed
from tracking_queue import TrackingQueue, COOLDOWN, LEASE_SECONDS
import server


def fails(fn,status):
    try:
        fn()
    except AppError as exc:
        assert exc.status==status,(exc.status,exc.message)
    else:
        raise AssertionError("expected rejection")


def shipment(db, users, suffix):
    return db.create_shipment(users["store"], {"recipient_name":"本地合成收件人","phone":"13800000000",
        "address":"本地合成地址，不寄送","store_order_no":str(suffix),"submission_key":"request_"+str(suffix).zfill(20),
        "items":[{"barcode":"DEMO-PRODUCT","quantity":1}]})


def time_boundaries():
    moment=datetime(2030,3,4,12,tzinfo=APP_TZ)
    values=[moment.isoformat(),moment.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
            moment.astimezone(timezone(timedelta(hours=-4))).isoformat(),moment.replace(tzinfo=None).isoformat()]
    for value in values:
        with tempfile.TemporaryDirectory(prefix="scentpool-time-boundary-") as tmp:
            db=Database(str(Path(tmp)/"test.db")); users=seed(db); q=TrackingQueue(db)
            row=shipment(db,users,"1")
            db.update_shipment(row["id"],{"status":"已发货","express_company":"圆通","tracking_no":"LOCAL-TIME"})
            returned=db.create_return_order(users["store"],{"tracking_no":"LOCAL-TIME-R","items":[{"barcode":"DEMO-PRODUCT","quantity":1}]})
            now=moment.timestamp()
            with db.connect() as c:
                c.execute("DELETE FROM tracking_jobs")
                c.execute("UPDATE shipments SET tracking_last_checked_at=?",(value,))
                c.execute("UPDATE return_orders SET tracking_last_checked_at=?",(value,))
                row=c.execute("SELECT * FROM shipments WHERE id=?",(row["id"],)).fetchone()
                identifier=q.enqueue(c,"shipment",row,"manual",now)
                due=c.execute("SELECT due_at FROM tracking_jobs WHERE id=?",(identifier,)).fetchone()[0]
                assert abs(due-now-COOLDOWN)<.001,(value,due-now)
            c=db.connect()
            try:
                assert q.claim(c,now+COOLDOWN-1) is None
                assert q.claim(c,now+COOLDOWN+1),value
                c.execute("DELETE FROM tracking_jobs")
                c.execute("UPDATE tracking_provider_gate SET next_allowed_at=0,lease_until=0,lease_token='' WHERE id=1")
                c.commit()
                q.schedule(c,{"shipment":6*3600,"return":12*3600},now+6*3600-1)
                assert c.execute("SELECT COUNT(*) FROM tracking_jobs").fetchone()[0]==0,value
                q.schedule(c,{"shipment":6*3600,"return":12*3600},now+6*3600+1)
                assert c.execute("SELECT COUNT(*) FROM tracking_jobs WHERE kind='shipment'").fetchone()[0]==1,value
                q.schedule(c,{"shipment":6*3600,"return":12*3600},now+12*3600-1)
                assert c.execute("SELECT COUNT(*) FROM tracking_jobs WHERE kind='return'").fetchone()[0]==0,value
                q.schedule(c,{"shipment":6*3600,"return":12*3600},now+12*3600+1)
                assert c.execute("SELECT COUNT(*) FROM tracking_jobs WHERE kind='return'").fetchone()[0]==1,value
                assert c.execute("SELECT tracking_last_checked_at FROM shipments").fetchone()[0]==value
            finally:
                c.close()


def run():
    time_boundaries()
    with tempfile.TemporaryDirectory(prefix="scentpool-reliability-") as tmp:
        db=Database(str(Path(tmp)/"test.db")); users=seed(db); q=TrackingQueue(db)
        row=shipment(db,users,"1")
        assert shipment(db,users,"1")["id"]==row["id"]
        assert db.submission_status(users["store"],{"kind":"shipment","submission_key":"request_"+"1".zfill(20)})["found"]
        assert not db.submission_status(users["other"],{"kind":"shipment","submission_key":"request_"+"1".zfill(20),"store_id":users["store"]["store_id"]})["found"]
        # Save + enqueue rollback together.
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE shipments SET tracking_no='LOCAL-ATOMIC',status='已发货',shipped_at=created_at,shipped_at_quality='estimated',shipped_at_source='manual_status_observed_at' WHERE id=?",(row["id"],))
            assert c.execute("SELECT COUNT(*) FROM tracking_jobs").fetchone()[0]==1
            c.rollback()
        assert q.summary()["total"]==0
        db.update_shipment(row["id"],{"status":"已发货","express_company":"圆通","tracking_no":"LOCAL-A"})
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks=list(pool.map(lambda _:q.request(users["admin"],"shipment",row["id"]),range(12)))
        assert len({t["id"] for t in tasks})==1
        fails(lambda:q.task(tasks[0]["id"],users["other"]),404)
        fails(lambda:q.request(users["other"],"shipment",row["id"]),404)
        c=db.connect()
        try:
            job=q.claim(c); assert job and not c.in_transaction
            # ABA (A->B->A) and phone changes cannot allow old results to overwrite.
            db.update_shipment(row["id"],{"status":"已发货","express_company":"圆通","tracking_no":"LOCAL-B"})
            db.update_shipment(row["id"],{"status":"已发货","express_company":"圆通","tracking_no":"LOCAL-A"})
            result={"tracking_status":"已签收","is_signed":True,"checked_at":now_text(),"provider":"synthetic"}
            assert q.finish(c,job,result,1).get("discarded")
            assert db.get_shipment(row["id"],users["admin"])["status"]=="已发货"
            assert q.claim(c) is None # changed identity cannot bypass cooldown
            future=time.time()+COOLDOWN+2
            job=q.claim(c,future); assert job
            with db.connect() as other:
                other.execute("UPDATE shipments SET phone='13900000000' WHERE id=?",(row["id"],))
            assert q.finish(c,job,result,1,future+1).get("discarded")
            # Recover an expired lease but do not repeat a provider attempt inside 30 minutes.
            later=future+COOLDOWN+2
            job=q.claim(c,later); assert job
            # A result arriving after its lease cannot publish even before recovery
            # has run; it may only release its own lock, not reset circuit state.
            with db.connect() as other:
                other.execute("UPDATE tracking_provider_gate SET pause_until=?,consecutive_failures=2 WHERE id=1",(later+LEASE_SECONDS+2,))
            assert q.finish(c,job,result,1,later+LEASE_SECONDS+1).get("discarded")
            with db.connect() as other:
                gate=other.execute("SELECT * FROM tracking_provider_gate WHERE id=1").fetchone()
                assert gate["consecutive_failures"]==2 and gate["pause_until"]==later+LEASE_SECONDS+2
            assert db.get_shipment(row["id"],users["admin"])["status"]=="已发货"
            assert q.claim(c,later+LEASE_SECONDS+1) is None
            recovered=q.claim(c,later+COOLDOWN+2); assert recovered["id"]==job["id"]
            assert recovered["attempt_count"]==2
            q.finish(c,recovered,{"system_error":True},12,later+COOLDOWN+3)
            assert q.claim(c,later+COOLDOWN+4) is None
            third=q.claim(c,later+2*COOLDOWN+4); assert third["attempt_count"]==3
            q.finish(c,third,{"system_error":True},12,later+2*COOLDOWN+5)
            assert q.task(tasks[0]["id"],users["admin"])["status"]=="completed" # original superseded
        finally:
            c.close()
        # Return retries are isolated by store and hash, no duplicate records/jobs.
        payload={"submission_key":"local_return_request_001","tracking_no":"LOCAL-RET-1","items":[{"barcode":"DEMO-PRODUCT","quantity":1}]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            returns=list(pool.map(lambda _:db.create_return_order(users["store"],payload),range(8)))
        assert len({r["id"] for r in returns})==1
        fails(lambda:db.create_return_order(users["store"],{**payload,"remark":"changed"}),409)
        # Initialization does not enqueue history or change business records.
        before=q.summary()["total"]
        db.initialize(product_file="/nonexistent/synthetic-products.xlsx",production=True,admin_password="local-demo-only-2026")
        assert q.summary()["total"]==before
        print("durable queue: atomic save, dedupe, isolation, ABA, cooldown, bounded retries, restart recovery PASS")
    with tempfile.TemporaryDirectory(prefix="scentpool-capacity-") as tmp:
        db=Database(str(Path(tmp)/"test.db")); users=seed(db); q=TrackingQueue(db)
        from fulfillment_profiles_test import configure
        configure(db, users)
        rows=[shipment(db,users,str(i)) for i in range(1,322)]
        with db.connect() as c:
            c.execute("UPDATE shipping_settings SET default_company='京东' WHERE id=1")
            c.execute("UPDATE shipments SET express_company='顺丰' WHERE id=?",(rows[0]["id"],))
        preview=db.preview_shipping_batch(users["admin"],{},profile_id="kunming_sf")
        assert preview["eligible_count"]==321 and len(preview["eligible"])==50
        assert len(db.preview_shipping_batch(users["admin"],{},page=7)["eligible"])==21
        # Item changes within the same second still invalidate the preview.
        db.update_shipment_items(rows[1]["id"],users["store"],{"items":[{"barcode":"DEMO-PRODUCT","quantity":2}]})
        fails(lambda:db.create_shipping_batch(users["admin"],[],{},profile_id="kunming_sf",selection_mode="all_matching",preview_fingerprint=preview["preview_fingerprint"]),409)
        preview=db.preview_shipping_batch(users["admin"],{},profile_id="kunming_sf")
        batch=db.create_shipping_batch(users["admin"],[],{},profile_id="kunming_sf",selection_mode="all_matching",preview_fingerprint=preview["preview_fingerprint"])
        assert batch["batch"]["total_count"]==321 and len(batch["items"])==50
        assert db.get_shipping_batch(batch["batch"]["id"],status="失败")["items"]==[]
        all_items=[item for p in range(1,8) for item in db.get_shipping_batch(batch["batch"]["id"],page=p)["items"]]
        assert next(i for i in all_items if i["shipment_id"]==rows[0]["id"])["express_company"]=="顺丰"
        assert all(i["express_company"]=="顺丰" for i in all_items)  # explicit profile beats row/global defaults
        assert not {"response_raw","callback_salt","cancel_param_json","request_id"}.intersection(batch["items"][0])
        # Provider success is never rejected when the scheduling admission capacity is reached.
        with patch("tracking_queue.MAX_ACTIVE",0):
            job=db.claim_next_shipping_job()
            result=db.complete_shipping_job(job["batch_item_id"],{"success":True,"tracking_no":"LOCAL-LABEL-OK","task_id":"LOCAL-TASK"})
            assert result["tracking_no"]=="LOCAL-LABEL-OK"
        assert q.summary()["queued"]==1
        for i in range(55):
            db.create_return_order(users["store"],{"tracking_no":f"LOCAL-R-{i}","items":[{"barcode":"DEMO-PRODUCT","quantity":1}]})
        assert len(db.list_return_orders_page(users["store"],{},page=1)["returns"])==50
        assert len(db.list_return_orders_page(users["store"],{},page=2)["returns"])==5
        assert db.return_status_counts(users["store"],{})["counts"]["total"]==55
        assert db.return_status_counts(users["other"],{})["counts"]["total"]==0
        # Initial shipment and return intents share the same gate and alternate when both due.
        for i in range(12):
            job=db.claim_next_shipping_job()
            db.complete_shipping_job(job["batch_item_id"],{"success":True,"tracking_no":f"LOCAL-LABEL-{i}","task_id":f"LOCAL-TASK-{i}"})
        # More than 300 real durable intents remain bounded by one provider lease;
        # a large shipment backlog does not starve return queries.
        with db.connect() as setup:
            setup.execute("UPDATE shipments SET status='已发货',tracking_no='LOCAL-QUEUED-'||id,shipped_at=created_at,shipped_at_quality='estimated',shipped_at_source='manual_status_observed_at' WHERE tracking_no=''")
        assert q.summary()["queued"]>300
        c=db.connect(); base=time.time(); kinds=[]
        try:
            for i in range(24):
                job=q.claim(c,base+i*2); assert job
                kinds.append(job["kind"])
                assert q.claim(c,base+i*2) is None # shared lease permits one provider call only
                q.finish(c,job,{"tracking_status":"运输中","provider":"synthetic","checked_at":now_text()},1,base+i*2+0.01)
            assert kinds==["shipment","return"]*12,kinds
            assert not c.in_transaction
        finally:
            c.close()
        print("321-row pagination, full-filter preview, fingerprint, mixed carriers, queue-full label success, fair rate-limited queue PASS")


if __name__=="__main__":
    run()
