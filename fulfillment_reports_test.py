"""Synthetic regression contracts for both origins and effective-state reporting."""
import json
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch, Mock
from xml.etree import ElementTree as ET

from database import Database
from fulfillment_profiles_test import configure, book
from fulfillment_reports import read_report, export_report, Report, filters_for, READ_SLOTS
from special_shipments_test import seed, payload, expect_error

RANGE = {"date_from":"2026-10-01","date_to":"2026-10-10"}


def shipment(db, users, code, channel="kunming", *, user="store", day="2026-10-08", quantity=3, special=False):
    with patch("database.now_text",return_value=f"{day}T12:00:00+08:00"):
        data = payload("influencer" if user=="team" else "resend", cooperation_subject="INTERNAL_ONLY_SYNTHETIC" if user=="team" else "", items=[{"barcode":"DEMO-PRODUCT","quantity":quantity},
            {"item_kind":"material","name":"=合成包装物料","material_spec":"大号","quantity":2}]) if special else {
                "store_order_no":code,"recipient_name":"PRIVATE-RECIPIENT","phone":"13800000000",
                "address":"PRIVATE-ADDRESS","items":[{"barcode":"DEMO-PRODUCT","quantity":quantity}]}
        row=db.create_shipment(users[user],data)
        book(db,users,row,channel); job=db.claim_next_shipping_job()
        db.complete_shipping_job(job["batch_item_id"],{"success":True,"tracking_no":"SYNTHETIC-"+code,"task_id":"SYNTHETIC-TASK-"+code})
    return row,job


def http_tests(db, users):
    import server
    class Quiet(server.Handler):
        def log_message(self,*_args): pass
    with patch.object(server,"DB",db,create=True):
        httpd=server.FixedThreadPoolHTTPServer(("127.0.0.1",0),Quiet,4)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True); thread.start()
        def get(path, user=None, headers=None, method="GET", body=None):
            auth={"Cookie":"scentpool_session="+db.create_session(users[user]["id"])} if user else {}
            req=urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}"+path,headers={**auth,**(headers or {})},method=method,data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req,timeout=15) as r: return r.status,r.read()
            except urllib.error.HTTPError as e: return e.code,e.read()
        endpoint="/api/reports/fulfillment-items?date_from=2026-10-01&date_to=2026-10-10"
        try:
            assert get(endpoint)[0]==401
            assert get(endpoint,headers={"Authorization":"Bearer SYNTHETIC-AUDIT"})[0]==401
            assert get("/reports/fulfillment","store")[0]==200
            for who in users:
                status,body=get(endpoint,who); assert status==200,body
                parsed=json.loads(body)
                if who!="admin":
                    assert all(x["store_id"]==users[who]["store_id"] for x in parsed["rows"])
                    assert len(parsed["stores"])==1
            assert get(endpoint+"&store_id="+str(users["other"]["store_id"]),"store")[0]==403
            assert get(endpoint.replace("items?","items.xlsx?"),"store")[0]==200
            assert get("/api/export/shipments.xlsx","store")[0]==403
            assert get(endpoint+"&channel=evil","admin")[0]==400
            with server.EXPORT_LOCK:
                assert get(endpoint.replace("items?","items.xlsx?"),"admin")[0]==503
            with READ_SLOTS, READ_SLOTS:
                assert get(endpoint,"admin")[0]==503
            cancelled,_=shipment(db,users,"CANCEL-REJECTION")
            before=read_report(db,users["admin"],RANGE)["summary"]
            fake=Mock(); fake.cancel_label.return_value={"success":False,"error":"SYNTHETIC provider rejection or unknown result"}
            with patch.object(server.Kuaidi100LabelClient,"from_env",return_value=fake):
                assert get(f"/api/shipments/{cancelled['id']}/label/cancel","admin",method="POST",body={})[0]==502
            assert read_report(db,users["admin"],RANGE)["summary"]==before
        finally:
            httpd.shutdown();thread.join(timeout=3);httpd.server_close()


def main():
    with tempfile.TemporaryDirectory(prefix="fulfillment-reports-test-") as directory:
        db=Database(str(Path(directory)/"synthetic.db"));users=seed(db);configure(db,users)
        a,job=shipment(db,users,"K1")
        b,_=shipment(db,users,"B1","banna",quantity=2,day="2026-10-09")
        shipment(db,users,"OTHER","banna",user="other",quantity=4)
        shipment(db,users,"COOP",user="team",quantity=1,special=True)
        report=lambda **kw:read_report(db,users["admin"],{**RANGE,**kw})
        data=report()
        assert data["summary"]=={"orders":4,"product_kinds":1,"product_quantity":10,"material_quantity":2,"waiting_orders":4}
        assert len(data["daily"])==10 and data["daily"][0]["orders"]==0
        assert report(channel="banna")["summary"]["orders"]==2
        assert report(channel="kunming")["summary"]["orders"]==2
        assert report(store_id=users["store"]["store_id"])["summary"]["product_quantity"]==5
        assert report(q="=合成包装")["summary"]["product_quantity"]==0
        assert report(q="=合成包装")["summary"]["orders"]==1
        assert report(shipment_type="influencer")["summary"]["orders"]==1
        assert report(date_from="2026-09-01",date_to="2026-09-30")["summary"]["orders"]==0
        assert report(page_size=1)["pagination"]["total"]==5
        assert len(report(page_size=1)["rows"])==1
        assert report(page=999,page_size=1)["pagination"]["page"]==5
        for options in ({"channel":"unknown"},{"date_from":"2026-02-30"},{"date_to":"2025-01-01"},{"page_size":51},{"store_id":"nope"},{"view":"bad"}):
            expect_error(lambda:report(**options),400)
        expect_error(lambda:read_report(db,users["store"],{**RANGE,"store_id":users["other"]["store_id"]}),403)
        # Reprint and a provider rejection do not change reporting; only a confirmed cancellation does.
        db.mark_label_reprint(a["id"])
        assert report()["summary"]["orders"]==4
        revision=db.get_shipment(a["id"],users["admin"])["tracking_revision"]
        db.mark_booking_cancelled(a["id"])
        assert report()["summary"]["product_quantity"]==7
        assert report(channel="kunming")["summary"]["orders"]==1
        expect_error(lambda:db.apply_tracking_result(a["id"],{"tracking_status":"已签收","is_signed":True},expected_revision=revision),409)
        assert db.apply_label_print_callback("SYNTHETIC-TASK-K1","synthetic late print",{"status":"200"})["ignored"]
        assert report()["summary"]["orders"]==3
        db.update_shipment_items(a["id"],users["store"],{"items":[{"barcode":"DEMO-PRODUCT","quantity":5}]})
        with patch("database.now_text",return_value="2026-10-10T12:00:00+08:00"):
            book(db,users,a,"banna");retry=db.claim_next_shipping_job()
            db.complete_shipping_job(retry["batch_item_id"],{"success":False,"error":"SYNTHETIC FAILURE"})
            assert report()["summary"]["orders"]==3
            db.retry_shipping_batch(retry["batch_id"]);retry=db.claim_next_shipping_job()
            db.complete_shipping_job(retry["batch_item_id"],{"success":True,"tracking_no":"SYNTHETIC-NEW","task_id":"NEW"})
        assert report(channel="banna")["summary"]["product_quantity"]==11
        assert report(date_to="2026-10-08")["summary"]["product_quantity"]==5
        assert report()["daily"][-1]["banna_product_quantity"]==5
        # Duplicate successful evidence for same current request must not duplicate items.
        with db.connect() as conn:
            batch_id=conn.execute("INSERT INTO shipping_batches(created_by,pickup_day,pickup_start_time,pickup_end_time,total_count,created_at,updated_at) VALUES (?,'','','',1,'x','x')",(users["admin"]["id"],)).lastrowid
            conn.execute("INSERT INTO shipping_batch_items(batch_id,shipment_id,request_id,status,settings_snapshot_json,created_at,updated_at) SELECT ?,shipment_id,request_id,'成功',settings_snapshot_json,'x','x' FROM shipping_batch_items WHERE id=?",(batch_id,retry["batch_item_id"]))
        assert report()["summary"]["orders"]==4
        # Conflicting origins are excluded, not guessed from a carrier or a display name.
        with db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json=? WHERE batch_id=?",(json.dumps({"profile_id":"kunming"}),batch_id))
        assert report()["summary"]["orders"]==3 and report()["quality"]["unknown_channel"]==1
        with db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json='{}' WHERE batch_id=?",(batch_id,))
        assert report()["quality"]["unknown_channel"]==1
        with db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json=? WHERE batch_id=?",(json.dumps({"profile_id":"banna"}),batch_id))
            # Existing snapshots remain statistical truth after catalog changes.
            conn.execute("UPDATE shipment_items SET product_name='历史别名' WHERE shipment_id=?",(b["id"],))
        db.upsert_product({"barcode":"DEMO-PRODUCT","name":"NEW-CATALOG-NAME","category":"新分类","price":"2.00"})
        assert report()["summary"]["product_kinds"]==1
        assert any(r["name_changed"] for r in report()["rows"])
        assert "NEW-CATALOG-NAME" not in json.dumps(report(),ensure_ascii=False)
        # ISO UTC near midnight belongs to the next Shanghai date.
        with db.connect() as conn:
            conn.execute("UPDATE shipments SET shipped_at='2026-10-09T16:01:00Z' WHERE id=?",(b["id"],))
        assert report(date_from="2026-10-10")["summary"]["orders"]==2
        # Legacy defects are synthetic: bypass only the test DB's trigger to emulate old rows.
        with db.connect() as conn:
            conn.execute("DROP TRIGGER shipments_time_integrity_update")
            conn.execute("UPDATE shipments SET shipped_at='' WHERE id=?",(b["id"],))
        assert report()["quality"]["undated_all_time"]==1
        assert report()["summary"]["orders"]==3
        output,_=export_report(db,users["admin"],{**RANGE,"page_size":1},directory)
        with zipfile.ZipFile(output) as z:
            for name in z.namelist():
                if name.endswith(".xml"): ET.fromstring(z.read(name))
            all_xml="".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xml"))
            for secret in ("PRIVATE-RECIPIENT","PRIVATE-ADDRESS","SYNTHETIC-KEY","SYNTHETIC-NEW","INTERNAL_ONLY"):
                assert secret not in all_xml
            assert '<f>' not in all_xml and '=合成包装物料' in all_xml
            ns={"s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            rows=ET.fromstring(z.read("xl/worksheets/sheet2.xml")).findall("s:sheetData/s:row",ns)
            assert len(rows)-1==report()["pagination"]["total"]
        assert output.stat().st_mode & 0o777==0o600
        # Even an interrupted online snapshot must close its raw destination connection.
        failed_dir=Path(directory)/"failed-export";failed_dir.mkdir()
        original_connect=sqlite3.connect;destinations=[]
        def tracked_connect(*args,**kwargs):
            conn=original_connect(*args,**kwargs)
            if str(args[0]).endswith("report-snapshot.db"): destinations.append(conn)
            return conn
        with patch("fulfillment_reports.sqlite3.connect",tracked_connect),patch("fulfillment_reports.EXPORT_SECONDS",-1):
            expect_error(lambda:export_report(db,users["admin"],RANGE,failed_dir),503)
        assert len(destinations)==1
        try: destinations[0].execute("SELECT 1");raise AssertionError("snapshot connection leaked")
        except sqlite3.ProgrammingError: pass
        # A read transaction must keep a coherent old view if a cancellation commits mid-read.
        with db.connect_readonly() as conn:
            conn.execute("BEGIN");r=Report(conn,users["admin"],filters_for(users["admin"],RANGE))
            before=r.summary()["orders"]
            db.mark_booking_cancelled(a["id"])
            assert r.summary()["orders"]==before
        assert report()["summary"]["orders"]==before-1
        http_tests(db,users)
        # Incremental indexes remain idempotent and do not rewrite existing rows.
        with db.connect_readonly() as conn:
            before_rows=[tuple(r) for r in conn.execute("SELECT * FROM shipments ORDER BY id")]
        from fulfillment_reports import migrate
        with db.connect() as conn: migrate(conn); migrate(conn)
        with db.connect_readonly() as conn:
            assert before_rows==[tuple(r) for r in conn.execute("SELECT * FROM shipments ORDER BY id")]
        print("fulfillment reports tests passed")


if __name__=="__main__": main()
