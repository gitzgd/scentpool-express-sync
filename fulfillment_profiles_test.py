"""Synthetic-only safety contracts for origin selection and immutable retries."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from database import Database
from special_shipments_test import seed, expect_error
from shipping import Kuaidi100LabelClient
from tracking import EXPRESS_COMPANY_CODES


def configure(db, users):
    db.update_shipping_settings({"sender_name": "旧寄件人（合成）", "sender_mobile": "13800000000",
        "sender_address": "本地合成版纳地址，禁止寄送", "default_company": "圆通", "carrier_settings": {
            "圆通": {"tbNet": "合成圆通网点,TEST-YTO"}, "中通": {"tbNet": "合成中通网点,TEST-ZTO"}}})
    db.save_label_authorization({"partnerId": "SYNTHETIC-ACCOUNT", "partnerKey": "SYNTHETIC-KEY", "net": "cainiao"})
    db.save_label_branches([
        {"kuaidicom": "yuantong", "branchAccounts": [{"branchName": "合成圆通网点", "branchCode": "TEST-YTO", "tbNet": "合成圆通网点,TEST-YTO", "quantity": 999}]},
        {"kuaidicom": "zhongtong", "branchAccounts": [{"branchName": "合成中通网点", "branchCode": "TEST-ZTO", "tbNet": "合成中通网点,TEST-ZTO", "quantity": 999}]},
        {"kuaidicom": "shunfeng", "branchAccounts": [{"branchName": "合成顺丰网点", "branchCode": "TEST-SF", "tbNet": "合成顺丰网点,TEST-SF", "quantity": 999}]},
    ])
    for identity, company, code in [("banna", "圆通", "TEST-YTO"), ("kunming", "中通", "TEST-ZTO"), ("kunming_sf", "顺丰", "TEST-SF")]:
        db.save_fulfillment_profile(users["admin"], {"id": identity, "name": "合成" + identity,
            "sender_name": "合成寄件人", "sender_mobile": "13800000000", "sender_address": f"合成{identity}地址，禁止真实寄送",
            "sender_company": "合成公司", "tbNet": f"合成{company}网点,{code}",
            "third_template_url": "https://cloudprint.cainiao.com/template/standard/SYNTHETIC-SF" if company == "顺丰" else ""})


def order(db, users, code):
    return db.create_shipment(users["store"], {"store_order_no": code, "recipient_name": "合成收件人",
        "phone": "13800000000", "address": "本地合成收件地址，禁止寄送", "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}]})


def book(db, users, row, profile_id="kunming"):
    filters = {"id": str(row["id"])}
    preview = db.preview_shipping_batch(users["admin"], filters, profile_id=profile_id)
    return db.create_shipping_batch(users["admin"], [{"id": row["id"]}], filters,
        profile_id=profile_id, preview_fingerprint=preview["preview_fingerprint"])


def main():
    with tempfile.TemporaryDirectory(prefix="fulfillment-tests-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db)
        configure(db, users)
        public = db.get_shipping_settings(public=True)
        assert public["default_profile_id"] == ""
        assert len(public["branch_options"]) == 3
        assert public["branch_options"][1]["branchCode"] == "TEST-ZTO"
        assert EXPRESS_COMPANY_CODES["中通"] == "zhongtong"
        assert "SYNTHETIC-KEY" not in json.dumps(public)
        kunming = next(p for p in public["fulfillment_profiles"] if p["id"] == "kunming")
        expect_error(lambda: db.save_fulfillment_profile(users["store"], kunming), 403)
        expect_error(lambda: db.preview_shipping_batch(users["store"], {}), 403)
        expect_error(lambda: db.save_fulfillment_profile(users["admin"], {**kunming, "tbNet": "陌生网点,FAKE"}), 409)
        expect_error(lambda: db.save_fulfillment_profile(users["admin"], {**kunming, "revision": 0}), 409)
        expect_error(lambda: db.save_fulfillment_profile(users["admin"], {**kunming, "sender_mobile": "wrong"}))
        for unselected in (None, ""):
            preview = db.preview_shipping_batch(users["admin"], {}, profile_id=unselected)
            assert preview["profile"] is None and not preview["settings_ready"]
            assert "手动选择" in preview["profile_error"]
        expect_error(lambda: db.save_fulfillment_profile(users["admin"], {**kunming, "make_default": True}), 409)

        row = order(db, users, "MANUAL-ZTO")
        preview = db.preview_shipping_batch(users["admin"], {"id": row["id"]}, profile_id="kunming")
        assert preview["profile"]["id"] == "kunming" and preview["company_counts"]["中通"] == 1
        assert preview["eligible"][0]["express_company"] == "中通"
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"]}]), 409)
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"], "express_company": "圆通"}], {"id": row["id"]}, profile_id="kunming", preview_fingerprint=preview["preview_fingerprint"]), 409)
        batch = book(db, users, row)
        assert batch["batch"]["fulfillment_name"] == "合成kunming"
        assert "settings_snapshot_json" not in json.dumps(batch)
        # Editing address/default/auth after submission cannot change queued jobs.
        db.save_fulfillment_profile(users["admin"], {**kunming, "sender_address": "合成新地址"})
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"]}], {"id": row["id"]}, profile_id="kunming", preview_fingerprint=preview["preview_fingerprint"]), 409)
        db.save_label_authorization({"partnerId": "SYNTHETIC-ACCOUNT", "partnerKey": "SYNTHETIC-ROTATED", "net": "cainiao"})
        job = db.claim_next_shipping_job()
        assert job["express_company"] == "中通"
        snapshot = job["shipping_settings_snapshot"]
        assert snapshot["sender_address"] == kunming["sender_address"]
        assert snapshot["partnerKey"] == "SYNTHETIC-KEY" and snapshot["print_mode"] == "PDF"
        assert snapshot["tbNet"] == "合成中通网点,TEST-ZTO"
        calls = []
        client = Kuaidi100LabelClient(key="SYNTHETIC", secret="SYNTHETIC")
        def provider(endpoint, method, param, **kwargs):
            calls.append((method, param))
            return {"success": True, "raw": "", "data": {"success": True, "code": 200,
                "data": {"kuaidinum": "SYNTHETIC-NUMBER", "taskId": "SYNTHETIC-TASK", "label": "https://example.test/synthetic.pdf"}}}
        with patch.object(client, "_post", provider):
            result = client.create_label(job, snapshot)
        assert calls[0][1]["kuaidicom"] == "zhongtong"
        assert calls[0][1]["sendMan"]["printAddr"] == kunming["sender_address"]
        assert calls[0][1]["partnerKey"] == "SYNTHETIC-KEY"
        db.complete_shipping_job(job["batch_item_id"], {"success": False, "error": "合成响应超时"})
        expect_error(lambda: book(db, users, row, "banna"), 409)
        db.retry_shipping_batch(batch["batch"]["id"])
        retried = db.claim_next_shipping_job()
        assert retried["booking_request_id"] == job["booking_request_id"]
        assert retried["shipping_settings_snapshot"] == snapshot
        db.complete_shipping_job(retried["batch_item_id"], result)
        expect_error(lambda: db.retry_shipping_batch(batch["batch"]["id"]), 409)
        shipped = db.get_shipment(row["id"], users["admin"])
        assert shipped["fulfillment_name"] == "合成kunming"
        assert "SYNTHETIC-KEY" not in json.dumps(shipped)
        booking = db.booking_for_cancel(row["id"])
        with patch.object(client, "_post", provider):
            client.cancel_label(booking, db.shipping_settings_for_company("中通"))
        assert calls[-1][1]["partnerKey"] == "SYNTHETIC-KEY"
        # Other origin uses original branch and independently editable address.
        second = order(db, users, "BANNA-YTO")
        book(db, users, second, "banna")
        job2 = db.claim_next_shipping_job()
        assert job2["express_company"] == "圆通" and job2["shipping_settings_snapshot"]["tbNet"].endswith("TEST-YTO")
        assert job2["shipping_settings_snapshot"]["sender_address"] != snapshot["sender_address"]
        db.complete_shipping_job(job2["batch_item_id"], {"success": False, "error": "合成失败"})
        # A saved profile is not valid under a different Cainiao account.
        db.save_label_authorization({"partnerId": "OTHER-ACCOUNT", "partnerKey": "OTHER-KEY", "net": "cainiao"})
        expect_error(lambda: db.preview_shipping_batch(users["admin"], {}, profile_id="kunming"), 409)
        # Idempotent additive migration never changes existing shipment data.
        before = db.get_shipment(row["id"], users["admin"])
        with db.connect() as conn:
            conn.execute("UPDATE shipping_settings SET default_profile_id='kunming'")
        db.initialize(product_file="/nonexistent", production=True, admin_password="unused")
        after = db.get_shipment(row["id"], users["admin"])
        for key in ("tracking_no", "status", "recipient_name", "fulfillment_name", "booking_request_id"):
            assert before[key] == after[key]
        assert len(db.get_shipping_settings(public=True)["fulfillment_profiles"]) == 3
        assert db.get_shipping_settings()["default_profile_id"] == ""

    with tempfile.TemporaryDirectory(prefix="fulfillment-legacy-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db)
        row = order(db, users, "LEGACY")
        # Even an installation without profiles cannot silently book via globals.
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"]}]), 409)
        configure(db, users)
        batch = book(db, users, row)
        with db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json='{}'")
        expect_error(lambda: db.save_label_authorization({"partnerId":"new", "partnerKey":"new"}),409)
        expect_error(lambda: db.save_label_branches([]),409)
        job = db.claim_next_shipping_job()
        assert job["shipping_settings_snapshot"]["profile_id"] == ""
        db.complete_shipping_job(job["batch_item_id"], {"success": False})
        with db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json='{}'")
        expect_error(lambda: db.retry_shipping_batch(batch["batch"]["id"]),409)

    with tempfile.TemporaryDirectory(prefix="fulfillment-boundaries-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db)
        configure(db, users)
        row = order(db, users, "STALE-CONFIG")
        filters = {"id": row["id"]}
        preview = db.preview_shipping_batch(users["admin"], filters, profile_id="kunming")
        profiles = db.get_shipping_settings(public=True)["fulfillment_profiles"]
        banna = next(p for p in profiles if p["id"] == "banna")
        db.save_fulfillment_profile(users["admin"], {**banna, "name": "合成新版纳方案"})
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"]}], filters,
            profile_id="kunming", preview_fingerprint=preview["preview_fingerprint"]),409)
        assert db.get_shipment(row["id"], users["admin"])["booking_status"] == "未下单"
        # Missing old batch evidence must fail closed, even if an order ID exists.
        with db.connect() as conn:
            conn.execute("UPDATE shipments SET booking_request_id='SYNTHETIC-ORPHAN' WHERE id=?", (row["id"],))
        expect_error(lambda: book(db, users, row, "kunming"),409)
        db.save_label_branches([{"kuaidicom": "zhongtong", "branchAccounts": [
            {"tbNet": "合成中通网点,TEST-ZTO", "quantity": 0}]}])
        empty = db.preview_shipping_batch(users["admin"], filters, profile_id="kunming")
        assert empty["profile_error"] and not empty["settings_ready"]
        expect_error(lambda: book(db, users, row, "kunming"),409)
        missing = db.preview_shipping_batch(users["admin"], filters, profile_id="banna")
        assert missing["profile_error"]

    with tempfile.TemporaryDirectory(prefix="fulfillment-pages-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db)
        configure(db, users)
        for number in range(53):
            order(db, users, f"MULTIPAGE-{number}")
        orphan = order(db, users, "HISTORICAL-UNKNOWN")
        with db.connect() as conn:
            conn.execute("UPDATE shipments SET booking_request_id='HISTORICAL-UNKNOWN' WHERE id=?", (orphan["id"],))
        preview = db.preview_shipping_batch(users["admin"], {}, profile_id="kunming")
        assert len(preview["eligible"]) == 50 and preview["eligible_count"] == 53
        assert preview["excluded_count"] == 1 and "缺少原配置" in preview["excluded"][0]["reason"]
        assert preview["company_counts"]["中通"] == 53
        db.create_shipping_batch(users["admin"], [], {}, selection_mode="all_matching",
            preview_fingerprint=preview["preview_fingerprint"], profile_id="kunming")
        with db.connect() as conn:
            rows = conn.execute("SELECT express_company,settings_snapshot_json FROM shipping_batch_items").fetchall()
            assert len(rows) == 53
            assert all(r["express_company"] == "中通" and json.loads(r["settings_snapshot_json"])["profile_id"] == "kunming" for r in rows)
        assert db.get_shipment(orphan["id"], users["admin"])["booking_status"] == "未下单"
    with tempfile.TemporaryDirectory(prefix="fulfillment-sf-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db); configure(db, users)
        sf = next(p for p in db.get_shipping_settings(public=True)["fulfillment_profiles"] if p["id"] == "kunming_sf")
        row = order(db, users, "MANUAL-SF")
        # Ignore an old default even before the upgrade migration clears it.
        with db.connect() as conn:
            conn.execute("UPDATE shipping_settings SET default_profile_id='kunming'")
        for missing in (None, ""):
            preview = db.preview_shipping_batch(users["admin"], {}, profile_id=missing)
            assert preview["profile"] is None and not preview["settings_ready"]
            expect_error(lambda: book(db, users, row, missing), 409)
        db.save_fulfillment_profile(users["admin"], {**sf, "third_template_url": ""})
        preview = db.preview_shipping_batch(users["admin"], {}, profile_id="kunming_sf")
        assert not preview["settings_ready"] and "模板" in preview["profile_error"]
        expect_error(lambda: book(db, users, row, "kunming_sf"), 409)
        sf = next(p for p in db.get_shipping_settings(public=True)["fulfillment_profiles"] if p["id"] == "kunming_sf")
        db.save_fulfillment_profile(users["admin"], {**sf, "third_template_url": "https://cloudprint.cainiao.com/template/standard/SYNTHETIC-SF"})
        sf = next(p for p in db.get_shipping_settings(public=True)["fulfillment_profiles"] if p["id"] == "kunming_sf")
        expect_error(lambda: db.save_fulfillment_profile(users["admin"], {**sf, "monthly_account": "API-SECRET"}))
        from fulfillment_profiles import branch_balance_warning
        assert not branch_balance_warning(sf, {"company": "顺丰", "tbNet": "SF", "branchCode": "TEST-SF", "quantity": 0})
        assert not branch_balance_warning(sf, {"company": "顺丰", "tbNet": "OTHER", "quantity": 0})
        assert not branch_balance_warning(sf, {"company": "中通", "tbNet": "SF", "quantity": 0})
        # The actual SF direct-provider shape uses SF / no branchCode / 0.
        db.save_label_branches([{"kuaidicom": "shunfeng", "branchAccounts": [{"tbNet": "SF", "branchName": "SF", "quantity": 0}]}])
        db.save_fulfillment_profile(users["admin"], {**sf, "tbNet": "SF", "monthly_account": "0000000000"})
        ready = db.preview_shipping_batch(users["admin"], {}, profile_id="kunming_sf")
        assert ready["settings_ready"] and ready["profile_warning"]
        batch = book(db, users, row, "kunming_sf")
        job = db.claim_next_shipping_job(); frozen = job["shipping_settings_snapshot"]
        assert frozen["profile_id"] == "kunming_sf" and job["express_company"] == "顺丰"
        assert frozen["exp_type"] == "顺丰标快" and frozen["tbNet"] == "SF"
        calls = []
        client = Kuaidi100LabelClient(key="SYNTHETIC", secret="SYNTHETIC")
        def capture(endpoint, method, param, **kwargs):
            calls.append(param)
            return {"success": False, "error": "合成超时"}
        with patch.object(client, "_post", capture):
            client.create_label(job, frozen)
        assert calls[0]["kuaidicom"] == "shunfeng" and calls[0]["net"] == "cainiao"
        assert calls[0]["expType"] == "顺丰标快"
        assert calls[0]["code"] == "0000000000" and calls[0]["partnerId"] == "SYNTHETIC-ACCOUNT"
        assert calls[0]["thirdTemplateURL"].endswith("SYNTHETIC-SF")
        db.complete_shipping_job(job["batch_item_id"], {"success": False, "error": "合成超时"})
        sf = next(p for p in db.get_shipping_settings(public=True)["fulfillment_profiles"] if p["id"] == "kunming_sf")
        db.save_fulfillment_profile(users["admin"], {**sf, "sender_address": "合成更新地址", "third_template_url": ""})
        db.retry_shipping_batch(batch["batch"]["id"])
        retry = db.claim_next_shipping_job()
        assert retry["shipping_settings_snapshot"] == frozen
        assert retry["booking_request_id"] == job["booking_request_id"]
    print("fulfillment profile safety tests passed")


if __name__ == "__main__":
    main()
