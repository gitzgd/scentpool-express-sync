"""Loopback API regressions with synthetic records and a 20-second fake provider."""
import json
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from http.cookiejar import CookieJar
from unittest.mock import patch

from database import Database, now_text
from special_shipments_test import seed
import server


def run():
    with tempfile.TemporaryDirectory(prefix="scentpool-http-reliability-") as tmp:
        db=Database(str(Path(tmp)/"test.db")); users=seed(db)
        class Quiet(server.Handler):
            def log_message(self,*_args): pass
        started=threading.Event(); stop=threading.Event()
        def slow_provider(row,*,persist=True):
            started.set(); time.sleep(20)
            return {"tracking_status":"运输中","provider":"synthetic","checked_at":now_text(),"provider_reached":True}
        with patch.object(server,"DB",db,create=True), patch.object(server,"tracking_auto_enabled",return_value=False), patch.object(server,"refresh_tracking_for_return",side_effect=slow_provider):
            httpd=server.FixedThreadPoolHTTPServer(("127.0.0.1",0),Quiet,4)
            serving=threading.Thread(target=httpd.serve_forever,daemon=True); serving.start()
            base=f"http://127.0.0.1:{httpd.server_address[1]}"
            clients={key:urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar())) for key in users}
            def request(key,path,payload=None):
                req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers={"Content-Type":"application/json"})
                try:
                    with clients[key].open(req,timeout=3) as response:
                        return response.status,json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code,json.loads(exc.read())
            worker=None
            try:
                for key,user in users.items():
                    assert request(key,"/api/login",{"username":user["username"],"password":"local-demo-only-2026"})[0]==200
                payload={"submission_key":"local_http_return_001","tracking_no":"LOCAL-HTTP-R1","items":[{"barcode":"DEMO-PRODUCT","quantity":1}]}
                begin=time.monotonic(); status,body=request("store","/api/returns",payload)
                assert status==201 and time.monotonic()-begin<1 and body["task"]["id"]
                assert body["return_order"]["tracking_status"]=="待查询"
                task=body["task"]; return_id=body["return_order"]["id"]
                worker=threading.Thread(target=server.tracking_worker,args=(stop,),daemon=True); worker.start()
                assert started.wait(2)
                # Provider spends 20 real seconds waiting. Saving and unrelated API calls must still finish.
                begin=time.monotonic()
                assert request("store","/api/returns",payload)[1]["return_order"]["id"]==return_id
                assert request("store","/api/returns/summary")[1]["counts"]["total"]==1
                assert request("other",f"/api/tracking/tasks/{task['id']}")[0]==404
                assert request("store",f"/api/tracking/tasks/{task['id']}")[1]["task"]["status"]=="running"
                assert request("store",f"/api/submissions/status?kind=return&submission_key={payload['submission_key']}")[1]["found"]
                assert request("store",f"/api/returns/{return_id}/tracking")[0]==200
                assert request("other",f"/api/returns/{return_id}/tracking")[0]==404
                assert request("store","/api/returns?page_size=51")[0]==400
                with patch.object(db,"claim_next_shipping_job",side_effect=[{"batch_item_id":1,"express_company":"圆通"},{"batch_item_id":2,"express_company":"圆通"}]),patch.object(db,"shipping_settings_for_company",return_value={}),patch.object(db,"complete_shipping_job",return_value={"tracking_no":"LOCAL-ONLY"}),patch.object(server.Kuaidi100LabelClient,"from_env") as factory:
                    factory.return_value.create_label.return_value={"success":True,"tracking_no":"LOCAL-ONLY"}
                    assert server.process_next_shipping_job() and server.process_next_shipping_job()
                    assert factory.return_value.create_label.call_count==2
                assert time.monotonic()-begin<2
                deadline=time.monotonic()+23
                while time.monotonic()<deadline:
                    current=request("store",f"/api/tracking/tasks/{task['id']}")[1]["task"]
                    if current["status"]=="completed": break
                    time.sleep(.2)
                assert current["status"]=="completed",current
                assert request("store",f"/api/returns/{return_id}/tracking")[1]["return_order"]["tracking_status"]=="运输中"
                with db.connect() as c:
                    before=c.execute("SELECT COUNT(*) FROM tracking_jobs").fetchone()[0]
                # Replayed POST subscribes to the completed intent, never creates a second query.
                assert request("store","/api/returns",payload)[0]==201
                with db.connect() as c:
                    assert c.execute("SELECT COUNT(*) FROM tracking_jobs").fetchone()[0]==before
            finally:
                stop.set(); server.TRACKING_QUEUE_EVENT.set()
                if worker: worker.join(25)
                httpd.shutdown(); httpd.server_close(); serving.join(2)
        assert db.connection_diagnostics()["active"]==0
    print("HTTP queue: real 20s provider does not block saves/next label, auth, replay and task completion PASS")


if __name__=="__main__": run()
