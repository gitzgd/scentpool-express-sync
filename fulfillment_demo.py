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
            names = {"kunming": "昆明中台中通", "kunming_sf": "昆明中台顺丰", "banna": "版纳门店圆通"}
            db.save_fulfillment_profile(users["admin"], {**profile, "name": names[profile["id"]] + "（仅合成演示）",
                "third_template_url": "" if profile["id"] == "kunming_sf" else profile["third_template_url"]})
        for code in ("CASE1-必须手动选择", "CASE2-顺丰模板待补充", "CASE3-长内容与配置锁定-" + "合成" * 30):
            order(db, users, code)
        server.DB = db
        def blocked(*_args, **_kwargs):
            raise RuntimeError("Local synthetic demo blocks all external requests")

        class Handler(server.Handler):
            def log_message(self, *_args): pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/demo":
                    self.send_bytes("""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>昆明顺丰与手动选择 · 仅本地测试</title>
                    <style>body{font:16px system-ui;line-height:1.7;max-width:820px;margin:40px auto;padding:20px}section{border:1px solid #ddd;border-radius:16px;margin:16px 0;padding:18px}a{display:inline-block;background:#066cff;color:white;padding:10px 16px;border-radius:9px;text-decoration:none}</style>
                    <h1>昆明顺丰与手动选择：三个合成案例</h1><p>没有真实联系人、地址、订单或网点。本机专用，已封锁所有真实快递调用。创建的批次只保存在临时演示库。</p>
                    <section><h2>1. 每批手动选择</h2><p>进入后台点“批量打单”，初始没有任何默认方案，不能提交。选择昆明中通或版纳圆通后重新勾选；关闭再打开仍为空。</p><a href="/demo/admin">进入演示后台</a></section>
                    <section><h2>2. 顺丰模板待补充</h2><p>选择昆明顺丰，提示先配置基础模板，不能误用中通模板下单。可在设置页填写测试 URL：https://cloudprint.cainiao.com/template/standard/SYNTHETIC-SF 。此地址仅作本地测试，不是真实模板。</p><a href="/demo/settings">填写合成模板</a></section>
                    <section><h2>3. 三方案切换与锁定</h2><p>模板保存后可选择顺丰。方案切换清空旧勾选，确认弹窗显示网点、地址、快递；提交的合成批次保留原配置。此演示不启动真实取号或打印。</p><a href="/demo/admin">体验长内容与切换</a></section>
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
