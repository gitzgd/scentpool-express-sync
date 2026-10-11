"""Synthetic recipient editing: permissions, optimistic concurrency and booking locks."""
import json
import tempfile
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from database import Database, AppError
from special_shipments_test import seed, payload, expect_error
from fulfillment_profiles_test import configure, order, book


def edit_payload(row, **changes):
    return {"recipient_name": "合成新收件人", "phone": "13900000000", "address": "合成新地址，禁止寄送",
            "content_revision": row["content_revision"], **changes}


def http_checks(db, users, row):
    import server
    class Quiet(server.Handler):
        def log_message(self, *_args): pass
    with patch.object(server, "DB", db, create=True):
        httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", 0), Quiet, 4)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
        def request(user, body, path=None):
            headers = {"Content-Type": "application/json"}
            if user:
                headers["Cookie"] = "scentpool_session=" + db.create_session(user["id"])
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}" + (path or f"/api/shipments/{row['id']}/recipient"),
                data=json.dumps(body).encode(), headers=headers, method="PATCH")
            try:
                with urllib.request.urlopen(req, timeout=5) as response: return response.status, json.load(response)
            except urllib.error.HTTPError as exc: return exc.code, json.load(exc)
        try:
            assert request(None, edit_payload(row))[0] == 401
            assert request(users["other"], edit_payload(row))[0] == 404
            assert request(users["team"], edit_payload(row))[0] == 404
            status, result = request(users["store"], edit_payload(row))
            assert status == 200, (status, result)
            status, result = request(users["admin"], edit_payload(result["shipment"], phone="13700000000"))
            assert status == 200, (status, result)
            assert request(users["store"], {**edit_payload(result["shipment"]), "store_id": 99})[0] == 400
        finally:
            httpd.shutdown(); worker.join(); httpd.server_close()


def main():
    with tempfile.TemporaryDirectory(prefix="recipient-edit-test-") as directory:
        db = Database(str(Path(directory) / "synthetic.db")); users = seed(db); configure(db, users)
        row = order(db, users, "RECIPIENT-EDIT")
        before_preview = db.preview_shipping_batch(users["admin"], {"id": row["id"]}, profile_id="kunming")
        updated = db.update_shipment_recipient(row["id"], users["store"], edit_payload(row))
        assert updated["content_revision"] > row["content_revision"]
        for key in ("id", "business_id", "store_id", "items", "status", "remark", "created_at"):
            assert updated[key] == row[key], key
        # A response lost after commit can be retried without changing the record again.
        same = db.update_shipment_recipient(row["id"], users["store"], edit_payload(row))
        assert same["content_revision"] == updated["content_revision"]
        expect_error(lambda: db.update_shipment_recipient(row["id"], users["admin"], edit_payload(row, address="不同合成地址")), 409)
        expect_error(lambda: db.create_shipping_batch(users["admin"], [{"id": row["id"]}], {"id": row["id"]},
            profile_id="kunming", preview_fingerprint=before_preview["preview_fingerprint"]), 409)
        for key, bad in (("phone", "bad"), ("phone", None), ("phone", "1" * 81), ("recipient_name", " "),
                         ("address", []), ("address", "x" * 1001), ("address", "bad\0address"),
                         ("content_revision", True), ("content_revision", -1)):
            expect_error(lambda: db.update_shipment_recipient(row["id"], users["admin"], edit_payload(updated, **{key: bad})), 400)
        expect_error(lambda: db.update_shipment_recipient(row["id"], {"role": "unknown"}, edit_payload(updated)), 403)
        expect_error(lambda: db.update_shipment_recipient(999999, users["store"], edit_payload(updated)), 404)
        for user in (users["other"], users["team"]):
            expect_error(lambda: db.update_shipment_recipient(row["id"], user, edit_payload(updated)), 404)
        team_row = db.create_shipment(users["team"], payload("sample", cooperation_subject="合成项目"))
        db.update_shipment_recipient(team_row["id"], users["team"], edit_payload(team_row))
        # Two simultaneous, different edits cannot silently overwrite each other.
        barrier = threading.Barrier(2)
        def race(number):
            barrier.wait()
            try:
                db.update_shipment_recipient(row["id"], users["admin"], edit_payload(updated, address=f"并发合成地址{number}"))
                return 200
            except AppError as exc: return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(race, (1, 2))) == [200, 409]
        # Queueing under the same write lock wins before an old open form saves.
        latest = db.get_shipment(row["id"], users["admin"])
        batch = book(db, users, latest)
        expect_error(lambda: db.update_shipment_recipient(row["id"], users["admin"], edit_payload(latest)), 409)
        job = db.claim_next_shipping_job()
        assert job["address"] == latest["address"]
        db.complete_shipping_job(job["batch_item_id"], {"success": False, "error": "合成未知结果"})
        failed = db.get_shipment(row["id"], users["admin"])
        assert failed["status"] == "待处理"
        expect_error(lambda: db.update_shipment_recipient(row["id"], users["admin"], edit_payload(failed)), 409)
        # Confirmed cancellation clears request identity through the existing workflow.
        db.retry_shipping_batch(batch["batch"]["id"]); job = db.claim_next_shipping_job()
        db.complete_shipping_job(job["batch_item_id"], {"success": True, "tracking_no": "SYNTHETIC-NUMBER", "task_id": "SYNTHETIC-TASK", "label_url": "https://example.test/label.pdf"})
        shipped = db.get_shipment(row["id"], users["admin"])
        expect_error(lambda: db.update_shipment_recipient(row["id"], users["admin"], edit_payload(shipped)), 409)
        db.mark_booking_cancelled(row["id"])
        cancelled = db.get_shipment(row["id"], users["admin"])
        db.update_shipment_recipient(row["id"], users["admin"], edit_payload(cancelled))
        http_checks(db, users, order(db, users, "HTTP-RECIPIENT"))
    print("recipient edit tests passed")


if __name__ == "__main__": main()
