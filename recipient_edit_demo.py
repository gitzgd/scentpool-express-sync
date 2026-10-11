"""Loopback-only recipient editing preview with three synthetic orders; no carrier IO."""
import argparse
import os
import socket
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

os.environ["SCENTPOOL_SESSION_SECURE"] = "0"
os.environ["SCENTPOOL_TRACKING_AUTO"] = "0"
import server
from database import Database
from fulfillment_profiles_test import configure, order, book
from special_shipments_test import seed, payload
from recipient_edit_test import edit_payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8883)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="recipient-demo-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db); configure(db, users)
        first = order(db, users, "CASE1-门店修改-" + "合成长编号" * 12)
        db.update_shipment_recipient(first["id"], users["store"], edit_payload(first,
            recipient_name="合成长姓名" * 10, address="本地合成地址，禁止寄送。" * 25))
        db.create_shipment(users["team"], payload("sample", cooperation_subject="合成合作项目",
            items=[{"barcode": "DEMO-PRODUCT", "quantity": 2}, {"item_kind": "material", "name": "合成包装", "material_spec": "大号", "quantity": 3}]))
        locked = order(db, users, "CASE3-已进入面单流程-不可修改"); book(db, users, locked)
        server.DB = db
        def blocked(*_args, **_kwargs): raise RuntimeError("Synthetic demo blocks external requests")
        class Handler(server.Handler):
            def log_message(self, *_args): pass
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/demo":
                    self.send_bytes('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>收件信息编辑 · 仅本地测试</title>
                    <style>body{font:16px system-ui;line-height:1.7;max-width:850px;margin:24px auto;padding:20px}section{border:1px solid #ddd;border-radius:16px;padding:18px;margin:16px 0}a{display:inline-block;padding:10px 16px;background:#06f;color:white;border-radius:8px;text-decoration:none;margin:4px}</style>
                    <h1>待处理订单：编辑收件信息</h1><p>只有三个合成案例，没有真实客户资料。所有快递请求已封锁，不会生成或打印真实面单。</p>
                    <section><h2>1. 门店修改自己的待处理订单</h2><p>点击收件信息下的“编辑收件信息”，修改姓名、电话和地址，保存后在原位置显示。含长姓名、长地址和长订单号。</p><a href="/demo/store">门店视角</a><a href="/demo/admin">总部视角</a></section>
                    <section><h2>2. 总部与合作团队权限</h2><p>总部可以编辑合作寄送；合作团队仅看到本团队订单，不能看到门店订单。混合商品和临时物料保持不变。</p><a href="/demo/team">合作团队视角</a><a href="/demo/admin">总部查看全部</a></section>
                    <section><h2>3. 面单锁定与冲突保护</h2><p>第三单处于合成排队状态，不显示编辑按钮。多人同时编辑时，旧表单会被拒绝并保留输入，可主动“重新读取当前信息”核对。</p><a href="/demo/admin">查看锁定提示</a></section>
                    <p>测试账号仅本地：admin、demo-store、demo-team；密码均为 local-demo-only-2026。退出演示进程后临时数据删除。</p></html>'''.encode(), "text/html; charset=utf-8"); return
                if path in {"/demo/admin", "/demo/store", "/demo/team"}:
                    key = path.rsplit("/", 1)[-1]; token = db.create_session(users[key]["id"])
                    self.send_response(302); self.send_header("Location", "/admin" if key == "admin" else "/shipments")
                    self.send_header("Set-Cookie", self.session_cookie(token)); self.send_header("Content-Length", "0"); self.end_headers(); return
                super().do_GET()
            def do_POST(self):
                if urlsplit(self.path).path == "/demo/conflict":
                    row = db.get_shipment(first["id"], users["admin"])
                    db.update_shipment_recipient(first["id"], users["admin"], edit_payload(row, address="另一位合成员工刚修改的地址"))
                    self.send_json({"ok": True}); return
                if any(part in self.path for part in ("/label/", "/labels/", "label-auth", "label-branches", "/tracking/", "shipping-batches")):
                    self.error_json("合成演示不执行下单、打印、取消、授权或物流查询。", 403); return
                super().do_POST()
        with patch.object(urllib.request, "urlopen", blocked), patch.object(socket, "create_connection", blocked):
            httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", args.port), Handler, 8)
            print(f"Synthetic preview: http://127.0.0.1:{args.port}/demo", flush=True)
            try: httpd.serve_forever()
            except KeyboardInterrupt: pass
            finally: httpd.server_close()


if __name__ == "__main__": main()
