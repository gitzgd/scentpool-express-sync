"""Synthetic-only contracts for business classification, ownership and retry safety."""
import json
import secrets
import tempfile
import threading
import urllib.request
import urllib.error
from http.cookiejar import CookieJar
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from database import AppError, Database, now_text
from shipment_types import SHIPMENT_TYPES
from shipping import build_label_remark, build_label_item_summary
from tools.scentpool_daily_audit_probe import app_summary


def expect_error(call, status=None):
    try:
        call()
    except AppError as exc:
        if status is not None:
            assert exc.status == status, (exc.status, exc.message)
    else:
        raise AssertionError("unsafe operation was accepted")


def seed(db):
    """No actual order, recipient or carrier data. Also used by local demo."""
    db.initialize(product_file="/nonexistent/synthetic-products.xlsx", production=True, admin_password="local-demo-only-2026")
    db.create_store("示例门店甲", "demo-store", "local-demo-only-2026")
    db.create_store("示例门店乙", "demo-other", "local-demo-only-2026")
    db.create_store("合作团队（本地示例）", "demo-team", "local-demo-only-2026", "team")
    db.upsert_product({"barcode": "DEMO-PRODUCT", "name": "演示香包", "category": "合成商品", "price": "1.00"})
    return {key: db.authenticate(username, "local-demo-only-2026") for key, username in (
        ("admin", "admin"), ("store", "demo-store"), ("other", "demo-other"), ("team", "demo-team"))}


def payload(kind="resend", **extra):
    return {"shipment_type": kind, "submission_key": secrets.token_hex(16), "recipient_name": "本地测试收件人",
            "phone": "13800000000", "address": "本地合成测试地址，请勿寄送", "internal_note": "INTERNAL_ONLY_破损补寄外盒",
            "remark": "包装请防压", "items": [{"item_kind": "material", "name": "包装盒", "material_spec": "小号", "quantity": 1}], **extra}


def cases(db, users):
    original = db.create_shipment(users["store"], {"recipient_name": "本地测试收件人", "phone": "13800000000",
        "address": "本地合成测试地址，请勿寄送", "store_order_no": "DEMO-ORIGINAL", "items": [{"barcode": "DEMO-PRODUCT", "quantity": 4}]})
    returned = db.create_return_order(users["store"], {"tracking_no": "LOCAL-DEMO-RETURN", "sender_phone": "13800000000", "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}]})
    resend_payload = payload(original_shipment_id=original["id"])
    resend = db.create_shipment(users["store"], resend_payload)
    exchange = db.create_shipment(users["store"], payload("exchange", original_shipment_id=original["id"], related_return_id=returned["id"], internal_note="换货寄出，退回包裹尚未签收；总部确认可先寄新件。", items=[{"barcode": "DEMO-PRODUCT", "quantity": 1}]))
    cooperation = db.create_shipment(users["team"], payload("influencer", cooperation_subject="INTERNAL_ONLY_示例博主拍摄项目", internal_note="仅用于本地测试：合作拍摄物料，不是真实发货。" * 12,
        items=[{"barcode": "DEMO-PRODUCT", "quantity": 2}, {"item_kind": "material", "name": "摄影背景纸", "material_spec": "浅米色 A3", "quantity": 3}]))
    return original, returned, resend, exchange, cooperation, resend_payload


def http_contract(db, users, resend, cooperation):
    import server
    class QuietHandler(server.Handler):
        def log_message(self, *_args):
            pass
    with patch.object(server, "DB", db, create=True):
        httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", 0), QuietHandler, 4)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        def request(client, path, payload=None, method=None):
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"}, method=method or ("POST" if payload is not None else "GET"))
            try:
                with client.open(req, timeout=5) as response:
                    data = response.read()
                    return response.status, json.loads(data) if "json" in response.headers.get("Content-Type", "") else data
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        try:
            clients = {}
            for key in users:
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
                assert request(client, "/api/login", {"username": users[key]["username"], "password": "local-demo-only-2026"})[0] == 200
                clients[key] = client
            assert request(clients["team"], "/special/new")[0] == 200
            assert request(clients["team"], "/api/me")[1]["user"]["store_kind"] == "team"
            status, stores = request(clients["team"], "/api/stores?all=1")
            assert status == 200 and len(stores["stores"]) == 1 and stores["stores"][0]["kind"] == "team"
            assert request(clients["team"], f"/api/shipments?id={resend['id']}")[1]["shipments"] == []
            assert request(clients["store"], f"/api/shipments?id={cooperation['id']}")[1]["shipments"] == []
            assert request(clients["team"], f"/api/shipments/{resend['id']}/context", {"internal_note": "bad"}, "PATCH")[0] == 404
            assert request(clients["team"], "/api/admin/shipping-batches/preview", {"filters": {}})[0] == 403
            filtered = "shipment_group=cooperation"
            csv = request(clients["admin"], f"/api/export/shipments.csv?{filtered}")[1].decode("utf-8-sig")
            assert cooperation["business_id"] in csv and resend["business_id"] not in csv and "博主合作" in csv
            assert "INTERNAL_ONLY" not in csv
            status, preview = request(clients["admin"], "/api/admin/shipping-batches/preview", {"filters": {"shipment_group": "cooperation"}})
            assert status == 200 and preview["preview"]["type_counts"]["influencer"] == 1
            repeated = payload("sample", cooperation_subject="本地 HTTP 幂等测试")
            first = request(clients["team"], "/api/shipments", repeated)
            second = request(clients["team"], "/api/shipments", repeated)
            assert first[0] == second[0] == 201 and first[1]["shipment"]["id"] == second[1]["shipment"]["id"]
        finally:
            httpd.shutdown(); thread.join(timeout=2); httpd.server_close()


def main():
    with tempfile.TemporaryDirectory(prefix="special-shipments-test-") as tmp:
        db = Database(str(Path(tmp) / "synthetic.db")); users = seed(db)
        original, returned, resend, exchange, cooperation, resend_payload = cases(db, users)
        assert original["shipment_type"] == "standard" and "AS-" in resend["business_id"] and "CO-" in cooperation["business_id"]
        assert db.get_shipment(original["id"], users["store"])["aftersales_count"] == 2
        assert exchange["return_unsigned_warning"] and len(resend["items"]) == 1 and resend["items"][0]["quantity"] == 1
        assert db.count_products() == 1 and len(cooperation["items"]) == 2
        assert db.create_shipment(users["store"], resend_payload)["id"] == resend["id"]
        expect_error(lambda: db.create_shipment(users["store"], {**resend_payload, "internal_note": "different"}), 409)
        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicate = payload()
            ids = list(pool.map(lambda _: db.create_shipment(users["store"], duplicate)["id"], range(8)))
        assert len(set(ids)) == 1
        db.delete_shipment(ids[0], users["store"])
        expect_error(lambda: db.create_shipment(users["store"], duplicate), 409)
        expect_error(lambda: db.delete_shipment(original["id"], users["store"]), 409)
        for user in (users["other"], users["team"]):
            assert db.list_shipments(user, {"id": resend["id"], "store_id": users["store"]["store_id"]}) == []
            expect_error(lambda: db.get_shipment(resend["id"], user), 404)
            expect_error(lambda: db.update_shipment_items(resend["id"], user, {"items": resend_payload["items"]}), 403)
            expect_error(lambda: db.update_shipment_context(resend["id"], user, {"internal_note": "bad"}), 404)
        expect_error(lambda: db.create_shipment(users["other"], payload(original_shipment_id=original["id"])), 403)
        expect_error(lambda: db.create_shipment(users["admin"], payload(store_id=users["other"]["store_id"], related_return_id=returned["id"])), 403)
        expect_error(lambda: db.create_shipment(users["team"], payload()), 403)
        for invalid_owner in (True, 1.5, "1.0", -1, "9999999999999"):
            expect_error(lambda: db.create_shipment(users["admin"], payload(store_id=invalid_owner)))
        expect_error(lambda: db.create_shipment(users["store"], payload("sample", cooperation_subject="test")), 403)
        expect_error(lambda: db.create_return_order(users["team"], {"tracking_no": "FAKE", "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}]}), 403)
        for quantity in (0, -1, 1.5, True, "1.0", "abc", 1000000):
            expect_error(lambda: db.create_shipment(users["store"], payload(items=[{"item_kind": "material", "name": "x", "material_spec": "y", "quantity": quantity}])))
        for invalid in ([], [{"item_kind": "material", "name": "x", "quantity": 1}], [None], [{"item_kind": "injected", "barcode": "DEMO-PRODUCT", "quantity": 1}]):
            expect_error(lambda: db.create_shipment(users["store"], payload(items=invalid)))
        expect_error(lambda: db.create_shipment(users["store"], payload("legacy")))
        expect_error(lambda: db.create_shipment(users["store"], payload("standard", internal_note="", store_order_no="custom-in-standard")))
        for invalid_filter in ({"shipment_group": "sql'"}, {"shipment_group": "aftersales", "shipment_type": "sample"}):
            expect_error(lambda: db.list_shipments(users["admin"], invalid_filter))
        filtered = {"shipment_group": "cooperation"}
        listing = db.list_shipments_page(users["admin"], filtered, page=1, page_size=50)
        assert [row["id"] for row in listing["shipments"]] == [cooperation["id"]]
        assert db.shipment_status_counts(users["admin"], filtered)["total"] == 1
        preview = db.preview_shipping_batch(users["admin"], filtered)
        assert preview["type_counts"]["influencer"] == 1 and preview["matched"] == 1
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": resend["id"], "express_company": "圆通"}], filtered), 409)
        expect_error(lambda: db.create_shipping_batch(users["store"], [{"id": resend["id"], "express_company": "圆通"}]), 403)
        db.update_shipment_context(resend["id"], users["store"], {"internal_note": "updated internal reason"})
        # Editing cannot change the original submission identity or its retry outcome.
        assert db.create_shipment(users["store"], resend_payload)["id"] == resend["id"]
        db.update_shipment_items(resend["id"], users["store"], {"items": payload()["items"]})
        db.create_shipping_batch(users["admin"], [{"id": resend["id"], "express_company": "圆通"}], {"shipment_group": "aftersales"})
        expect_error(lambda: db.update_shipment_items(resend["id"], users["store"], {"items": payload()["items"]}), 409)
        expect_error(lambda: db.update_shipment_context(resend["id"], users["store"], {"internal_note": "locked"}), 409)
        for row in (resend, exchange, cooperation):
            label = build_label_remark(row); summary = build_label_item_summary(row["items"], 500)
            assert "INTERNAL_ONLY" not in label and "博主" not in label
            assert "包装请防压" in label
            if row["id"] == cooperation["id"]:
                assert "摄影背景纸" in summary and "3" in summary and "演示香包" in summary
        with db.connect() as conn:
            conn.execute("UPDATE return_orders SET status='已签收' WHERE id=?", (returned["id"],))
        assert not db.get_shipment(exchange["id"], users["admin"])["return_unsigned_warning"]
        report = db.daily_audit(now_text()[:10])
        by_type = report["shipment_classification"]["by_type"]
        assert set(by_type) == set(SHIPMENT_TYPES)
        for metric, total in report["metrics"].items():
            assert sum(item[metric] for item in by_type.values()) == total
            assert sum(item[metric] for item in report["shipment_classification"]["by_group"].values()) == total
        assert "INTERNAL_ONLY" not in json.dumps(report, ensure_ascii=False)
        assert "13800000000" not in json.dumps(report)
        assert any(row["store_kind"] == "team" for row in report["by_store"])
        assert app_summary({"status": "ok", "data": report}, daily=True)["result"]["shipment_classification"] == report["shipment_classification"]
        http_contract(db, users, resend, cooperation)
        before = [(row["id"], row["business_id"], row["shipment_type"]) for row in db.list_shipments(users["admin"], {})]
        db.initialize(product_file="/nonexistent/synthetic-products.xlsx", production=True)
        after = [(row["id"], row["business_id"], row["shipment_type"]) for row in db.list_shipments(users["admin"], {})]
        assert before == after
        with db.connect_readonly() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    print("special shipments tests passed")


if __name__ == "__main__":
    main()
