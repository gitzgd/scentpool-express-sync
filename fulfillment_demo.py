"""Loopback-only synthetic dispatch-profile demo. Never opens a production DB."""
import argparse
import os
import socket
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

os.environ["SCENTPOOL_SESSION_SECURE"] = "0"
os.environ["SCENTPOOL_TRACKING_AUTO"] = "0"
import server
from database import Database
from fulfillment_profiles_test import configure, order
from special_shipments_test import seed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8878)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="scentpool-fulfillment-demo-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db); configure(db, users)
        for profile in db.get_shipping_settings(public=True)["fulfillment_profiles"]:
            db.save_fulfillment_profile(users["admin"], {**profile, "name": "昆明中台发货（仅合成演示）" if profile["id"] == "kunming" else "版纳门店发货（仅合成演示）"})
        for code in ("CASE1-默认中台中通", "CASE2-手动选择版纳圆通", "CASE3-长内容与配置锁定-" + "合成" * 30):
            order(db, users, code)
        server.DB = db
        def blocked(*_args, **_kwargs):
            raise RuntimeError("Local synthetic demo blocks all external requests")

        class Handler(server.Handler):
            def log_message(self, *_args): pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/demo":
                    self.send_bytes("""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>双发货方案 · 仅本地测试</title>
                    <style>body{font:16px system-ui;line-height:1.7;max-width:820px;margin:40px auto;padding:20px}section{border:1px solid #ddd;border-radius:16px;margin:16px 0;padding:18px}a{display:inline-block;background:#066cff;color:white;padding:10px 16px;border-radius:9px;text-decoration:none}</style>
                    <h1>双发货方案：三个合成案例</h1><p>没有真实联系人、地址、订单或网点。本机专用，已封锁所有真实快递调用。创建的批次只保存在临时演示库。</p>
                    <section><h2>1. 默认中台／中通</h2><p>进入后台点“批量打单”，核对默认中台、合成中通网点和合成地址；确认弹窗会再次显示发货方案。</p><a href="/demo/admin">进入演示后台</a></section>
                    <section><h2>2. 切换版纳／圆通</h2><p>在打单预览选择“版纳门店发货”。系统清空旧勾选，重新选择后可看到地址、网点、快递公司一起切换。</p><a href="/demo/admin">体验方案切换</a></section>
                    <section><h2>3. 地址编辑与锁定</h2><p>先创建一个合成批次，再编辑中台方案地址。原批次仍保持提交时的配置；新预览使用新地址。此演示不启动真实取号或打印。</p><a href="/demo/settings">编辑合成发货方案</a></section>
                    <p>演示库退出时删除；正式环境不安装这些案例。测试账号仅本地：admin / local-demo-only-2026。</p></html>""".encode(), "text/html; charset=utf-8")
                    return
                if path in {"/demo/admin", "/demo/settings"}:
                    token = db.create_session(users["admin"]["id"])
                    self.send_response(302)
                    self.send_header("Location", "/admin/shipping" if path.endswith("settings") else "/admin")
                    self.send_header("Set-Cookie", self.session_cookie(token))
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                super().do_GET()

            def do_POST(self):
                if any(text in self.path for text in ("/label/", "/labels/", "label-auth", "label-branches", "/tracking/")):
                    self.error_json("合成演示不执行真实取号、打印、授权或物流查询。", 403); return
                super().do_POST()

        with patch.object(urllib.request, "urlopen", blocked), patch.object(socket, "create_connection", blocked), \
             patch.object(server, "label_config_public", lambda: {"enabled":True, "configured":True, "key_configured":True, "secret_configured":True, "public_base_url_configured":True, "missing":[]}):
            httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", args.port), Handler, 8)
            print(f"Local synthetic preview: http://127.0.0.1:{args.port}/demo", flush=True)
            try: httpd.serve_forever()
            except KeyboardInterrupt: pass
            finally: httpd.server_close()


if __name__ == "__main__": main()
