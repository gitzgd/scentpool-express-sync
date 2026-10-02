"""Disposable loopback-only UI demonstration. All providers are synthetic.

Run: python3 reliability_demo.py --port 8877
The database is new for every run and deleted on exit. Never accepts a DB path.
"""
import argparse
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import patch

os.environ["SCENTPOOL_SESSION_SECURE"] = "0"
os.environ["SCENTPOOL_TRACKING_AUTO"] = "0"

import server
from database import Database, now_text
from special_shipments_test import seed
from tracking_queue import TrackingQueue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    stop = threading.Event()
    recovered = threading.Event()
    label_enabled = threading.Event()
    with tempfile.TemporaryDirectory(prefix="scentpool-local-reliability-demo-") as directory:
        db = Database(str(Path(directory) / "synthetic.db"))
        users = seed(db)
        for code, name in [("DEMO-BOX", "长名称合成包装盒，仅用于验证手机多商品换行"), ("DEMO-CARD", "合成卡片与演示说明")]:
            db.upsert_product({"barcode": code, "name": name, "category": "合成商品", "price": "1.00"})
        rows = []
        for index in range(1, 76):
            rows.append(db.create_shipment(users["store"], {
                "store_order_no": f"LOCAL-DEMO-{index:03}", "recipient_name": "合成演示收件人，不是真实客户",
                "phone": "13800000000", "address": "仅本地测试地址，禁止实际寄送。" * (8 if index == 75 else 1),
                "remark": "请在本行输入一段未保存发货备注，然后观察批次进度更新。" * (4 if index == 75 else 1),
                "items": [{"barcode": "DEMO-PRODUCT", "quantity": 2}] + ([{"barcode":"DEMO-BOX","quantity":3},{"barcode":"DEMO-CARD","quantity":5}] if index == 75 else []),
            }))
        # Fixture-only queue preparation; no real carrier, label URL or print call.
        db.update_shipment(rows[6]["id"], {"status": "已发货", "express_company": "圆通", "tracking_no": "LOCAL-DEMO-FAIL"})
        failing_task = TrackingQueue(db).request(users["admin"], "shipment", rows[6]["id"])
        for index in range(55):
            db.create_return_order(users["store"], {"tracking_no": f"LOCAL-DEMO-RET-{index:03}", "remark": "本地分页合成记录", "items": [{"barcode":"DEMO-PRODUCT", "quantity":1}]})
        with db.connect() as connection:
            connection.execute("UPDATE tracking_jobs SET state='completed',finished_at=? WHERE kind='return'", (time.time(),))
            connection.execute("UPDATE shipping_settings SET sender_name='本地演示',sender_mobile='13800000000',sender_address='本地合成地址禁止寄送',partner_id='SYNTHETIC',partner_key='SYNTHETIC-NOT-A-SECRET' WHERE id=1")
        server.DB = db

        def forbid_external(*_args, **_kwargs):
            raise RuntimeError("Local demo prohibits all real external requests")

        def query(row):
            # Deliberate third-party delay; the ordinary save must return first.
            stop.wait(20 if "SLOW" in row.get("tracking_no", "") else 2)
            if row.get("tracking_no", "").startswith("LOCAL-DEMO-FAIL") and not recovered.is_set():
                return {"system_error": True, "provider_reached": False, "error": "本地模拟：物流服务暂时不可用，订单已保留。"}
            return {"provider": "synthetic", "provider_reached": True, "tracking_status": "运输中", "state_code": "0",
                    "last_event": "本地模拟物流：已恢复并进入运输中，不是真实快递轨迹。", "checked_at": now_text(),
                    "signed_at": "", "error": "", "raw": "", "is_signed": False}

        class DemoLabelClient:
            @classmethod
            def from_env(cls): return cls()

            def create_label(self, job, _settings):
                stop.wait(4)
                return {"success": True, "tracking_no": f"LOCAL-DEMO-LABEL-{job['id']}",
                        "task_id": f"LOCAL-DEMO-TASK-{job['id']}", "label_url": "", "raw": ""}

        class DemoHandler(server.Handler):
            def log_message(self, *_args): pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/demo":
                    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
                    <title>操作体验升级 · 仅本地测试</title><style>body{font:16px system-ui;max-width:850px;margin:40px auto;padding:20px;line-height:1.7}section{border:1px solid #ddd;border-radius:16px;padding:20px;margin:18px 0}a,button{display:inline-block;padding:10px;background:#066cff;color:white;border:0;border-radius:8px;text-decoration:none}</style>
                    <h1>三个安全合成案例</h1><p>仅在本机运行；不会读取生产数据库、调用真实快递或打印。测试库在退出后删除。</p>
                    <section><h2>1. 慢物流，快速保存</h2><p>进入新增退货，单号填写 LOCAL-DEMO-SLOW-1，选择演示商品。模拟查询耗时20秒，保存应立即返回，后台显示排队和进度。</p><a href="/demo/1">打开案例1</a></section>
                    <section><h2>2. 批次刷新，输入不丢失</h2><p>在任意未下单行的发货备注输入内容，不保存。顶部6笔模拟面单每4秒完成一笔，页面不应跳动，输入不应消失。另有75笔合成订单可检查分页预览。</p><a href="/demo/2">打开案例2并启动模拟批次</a></section>
                    <section><h2>3. 服务失败与恢复</h2><p>打开案例后观察异常提示、保留的订单和队列。点下方按钮模拟服务恢复；仅本地将重试等待时间快进，不改变生产保护间隔。</p><a href="/demo/3">打开案例3</a> <button id="recover">模拟服务恢复（仅本地）</button><p id="result"></p></section>
                    <p>本地测试账号：admin / demo-store / demo-team；密码均为 local-demo-only-2026，不能用于生产。</p>
                    <script>document.getElementById('recover').onclick=async()=>{const r=await fetch('/demo/recover',{method:'POST'});document.getElementById('result').textContent=r.ok?'模拟服务已恢复，回到案例3等待约5秒更新。':'恢复失败，请重启演示。'}</script></html>"""
                    self.send_bytes(html.encode(), "text/html; charset=utf-8"); return
                if path in {"/demo/1", "/demo/2", "/demo/3"}:
                    user = users["store"] if path.endswith("1") else users["admin"]
                    token = db.create_session(user["id"])
                    target = "/returns/new" if path.endswith("1") else "/admin"
                    values = {}
                    if path.endswith("2"):
                        replay = time.time_ns()
                        replay_rows = [db.create_shipment(users["store"], {
                            "store_order_no": f"LOCAL-CASE2-{replay}-{number}", "recipient_name": "本地合成批次收件人",
                            "phone": "13800000000", "address": "仅本地演示，禁止真实寄送。",
                            "remark": "本条只用于模拟批次进度。可编辑其他未下单行来验证输入保留。",
                            "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}],
                        }) for number in range(6)]
                        batch = db.create_shipping_batch(users["admin"], [{"id": row["id"], "express_company": "圆通"} for row in replay_rows], {})
                        label_enabled.set()
                        values[f"scentpool_shipping_batch_id:{user['id']}"] = str(batch["batch"]["id"])
                    if path.endswith("3"):
                        recovered.clear()
                        replay = time.time_ns()
                        row = db.create_shipment(users["store"], {
                            "store_order_no": f"LOCAL-CASE3-{replay}", "recipient_name": "本地合成故障恢复案例",
                            "phone": "13800000000", "address": "仅本地合成，不是真实发货地址。",
                            "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}],
                        })
                        db.update_shipment(row["id"], {"status":"已发货", "express_company":"圆通", "tracking_no":f"LOCAL-DEMO-FAIL-{replay}"})
                        failing_task = TrackingQueue(db).request(users["admin"], "shipment", row["id"])
                        with db.connect() as connection:
                            connection.execute("UPDATE tracking_provider_gate SET pause_until=0,next_allowed_at=0,lease_until=0,lease_token='' WHERE id=1")
                        server.TRACKING_QUEUE_EVENT.set()
                        values[f"scentpool_tracking_tasks:{user['id']}"] = json.dumps([failing_task["id"]])
                    script = "".join(f"sessionStorage.setItem({json.dumps(key)},{json.dumps(value)});" for key, value in values.items())
                    html = f"<!doctype html><meta charset='utf-8'><script>{script}location.replace({json.dumps(target)});</script>"
                    payload = html.encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload))); self.send_header("Set-Cookie", self.session_cookie(token))
                    self.end_headers(); self.wfile.write(payload); return
                super().do_GET()

            def do_POST(self):
                if urlsplit(self.path).path == "/demo/recover":
                    recovered.set()
                    with db.connect() as connection:
                        connection.execute("UPDATE tracking_provider_gate SET pause_until=0,next_allowed_at=0,lease_until=0,lease_token='' WHERE id=1")
                        connection.execute("UPDATE tracking_jobs SET due_at=0,started_at=0 WHERE state='queued'")
                    server.TRACKING_QUEUE_EVENT.set()
                    self.send_json({"ok": True}); return
                if "/labels/" in self.path or "label-auth" in self.path or "/label/" in self.path:
                    self.error_json("本地演示不执行打印、真实面单操作或授权。", 403); return
                super().do_POST()

        def label_worker():
            while not stop.is_set():
                if label_enabled.wait(.25):
                    with patch.object(server.Kuaidi100LabelClient, "from_env", DemoLabelClient.from_env):
                        server.process_next_shipping_job()
                    stop.wait(.25)

        with patch.object(urllib.request, "urlopen", forbid_external), patch.object(socket, "create_connection", forbid_external), patch.object(server, "query_tracking", query), \
             patch.object(server, "label_config_public", lambda: {"enabled": True, "configured": True, "missing": [], "mode": "synthetic"}), \
             patch.object(server, "detect_tracking_company", lambda _number: {"express_company":"圆通", "company_code":"yuantong", "source":"kuaidi100"}):
            httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", args.port), DemoHandler, 8)
            workers = [threading.Thread(target=server.tracking_worker, args=(stop,), daemon=True), threading.Thread(target=label_worker, daemon=True)]
            for worker in workers: worker.start()
            print(f"Synthetic local demo: http://127.0.0.1:{args.port}/demo", flush=True)
            try: httpd.serve_forever()
            except KeyboardInterrupt: pass
            finally:
                stop.set(); server.TRACKING_QUEUE_EVENT.set(); server.SHIPPING_QUEUE_EVENT.set()
                httpd.server_close()
                for worker in workers: worker.join(timeout=5)


if __name__ == "__main__": main()
