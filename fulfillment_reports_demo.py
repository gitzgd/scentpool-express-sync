"""Loopback-only three-case report demo; temporary synthetic data, no providers."""
import argparse
import os
import socket
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

os.environ["SCENTPOOL_SESSION_SECURE"] = "0"
os.environ["SCENTPOOL_TRACKING_AUTO"] = "0"
import server
from database import Database, APP_TZ
from fulfillment_profiles_test import configure, book
from fulfillment_reports_test import shipment
from fulfillment_reports import export_report
from special_shipments_test import seed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="scentpool-report-demo-") as directory:
        db = Database(str(Path(directory)/"synthetic.db")); users = seed(db); configure(db,users)
        today = datetime.now(APP_TZ).date(); yesterday = today-timedelta(days=1)
        shipment(db,users,"CASE1-BANNA","banna",day=today.isoformat(),quantity=2)
        shipment(db,users,"CASE1-KUNMING",day=today.isoformat(),quantity=3)
        original,_ = shipment(db,users,"CASE2-CHANGE",day=yesterday.isoformat(),quantity=4)
        shipment(db,users,"CASE3-OTHER","banna",user="other",day=today.isoformat(),quantity=6,special=True)
        shipment(db,users,"CASE3-TEAM",user="team",day=today.isoformat(),quantity=1,special=True)
        with db.connect() as conn:
            conn.execute("UPDATE shipment_items SET product_barcode='0000123456',product_name=? WHERE item_kind='product'",("合成商品 · 带前导零条码与历史快照名称的长名称验收示例",))
            conn.execute("UPDATE shipment_items SET material_spec=? WHERE item_kind='material'",("合成物料规格 · 拍摄背景板／赠品包装 · 长内容换行测试"*3,))
        server.DB = db
        output,_ = export_report(db,users["admin"],{"date_from":yesterday.isoformat(),"date_to":today.isoformat()},directory)
        print(f"Synthetic XLSX: {output}",flush=True)
        def blocked(*_args,**_kwargs): raise RuntimeError("Synthetic preview blocks external providers")
        class Handler(server.Handler):
            def log_message(self,*_args): pass
            def do_GET(self):
                path=urlsplit(self.path).path
                if path=="/demo":
                    self.send_bytes('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>发货统计 · 本地合成验收</title>
<style>body{font:16px system-ui;line-height:1.7;max-width:880px;margin:32px auto;padding:18px;background:#f6f8fc;color:#263043}section{background:white;border:1px solid #dce3ef;border-radius:16px;margin:18px 0;padding:22px}a,button{display:inline-block;background:#1765da;color:white;padding:10px 16px;border-radius:9px;text-decoration:none;border:0;margin:4px;font:inherit;cursor:pointer}p{overflow-wrap:anywhere}#status{color:#b42318}</style>
<h1>发货统计：三个本地合成案例</h1><p>仅本机临时数据库；已封锁真实快递调用，不影响生产订单。建议将统计页另开标签，观察自动刷新。</p>
<section><h2>1. 同一门店、两个发货渠道</h2><p>演示门店今日版纳 1 单／2 件、昆明 1 单／3 件。可切换全部、版纳、昆明，检查去重订单数及商品数量。</p><a href="/demo/admin">总部看板</a><a href="/demo/store">仅看本门店</a></section>
<section><h2>2. 昆明取消 → 次日改版纳</h2><p>初始记录在昨日昆明：1 单／4 件。先选“昨日”或自定义两天查看。以下按钮只改变演示库：取消后原记录消失；再次出单后按今日版纳及 5 件计入。</p><button data-action="cancel">模拟取消成功</button><button data-action="rebook">模拟今日版纳重发</button><p id="status" role="status"></p></section>
<section><h2>3. 多门店、合作与临时物料</h2><p>另一个门店版纳补发 6 件商品＋2 件物料；合作团队昆明寄送 1 件商品＋2 件物料。条码含前导零；物料有长规格。切换“门店商品汇总”及“每日商品明细”，可导出四个工作表。</p><a href="/demo/admin">查看全部并导出</a><a href="/demo/team">合作团队权限</a></section>
<p>演示账号仅本地：admin、demo-store、demo-team；统一测试密码 local-demo-only-2026。退出演示进程会清理数据。</p>
<script>document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{b.disabled=true;let s=document.getElementById('status');s.textContent='正在更新合成案例…';try{let r=await fetch('/demo/'+b.dataset.action,{method:'POST'});let d=await r.json();s.textContent=d.message||d.error;new BroadcastChannel('fulfillment-report-refresh').postMessage('refresh')}catch(e){s.textContent='演示请求失败：'+e.message}finally{b.disabled=false}})</script></html>'''.encode(),"text/html; charset=utf-8");return
                if path in {"/demo/admin","/demo/store","/demo/team"}:
                    token=db.create_session(users[path.rsplit("/",1)[1]]["id"])
                    self.send_response(302);self.send_header("Location","/reports/fulfillment")
                    self.send_header("Set-Cookie",self.session_cookie(token));self.send_header("Content-Length","0");self.end_headers();return
                super().do_GET()
            def do_POST(self):
                if self.path=="/demo/cancel":
                    if db.get_shipment(original["id"],users["admin"])["booking_status"]=="已出单":
                        db.mark_booking_cancelled(original["id"])
                    self.send_json({"message":"合成面单已取消，原渠道已移除。"});return
                if self.path=="/demo/rebook":
                    row=db.get_shipment(original["id"],users["admin"])
                    if row["booking_status"]=="已出单": self.error_json("请先模拟取消成功。",409);return
                    db.update_shipment_items(original["id"],users["store"],{"items":[{"barcode":"DEMO-PRODUCT","quantity":5}]})
                    with patch("database.now_text",return_value=f"{today.isoformat()}T12:00:00+08:00"):
                        book(db,users,original,"banna");job=db.claim_next_shipping_job()
                        db.complete_shipping_job(job["batch_item_id"],{"success":True,"tracking_no":"SYNTHETIC-REBOOK","task_id":"SYNTHETIC-REBOOK"})
                    self.send_json({"message":"合成订单今日从版纳重新出单，计入 1 单／5 件。"});return
                self.error_json("此预览只允许只读统计与两个合成案例按钮。",403)
        with patch.object(urllib.request,"urlopen",blocked),patch.object(socket,"create_connection",blocked):
            httpd=server.FixedThreadPoolHTTPServer(("127.0.0.1",args.port),Handler,8)
            print(f"Local report preview: http://127.0.0.1:{args.port}/demo",flush=True)
            try: httpd.serve_forever()
            except KeyboardInterrupt: pass
            finally: httpd.server_close()


if __name__=="__main__": main()
