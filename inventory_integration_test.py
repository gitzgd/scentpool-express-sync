"""Synthetic integration regression; never starts provider workers or uses real data."""
import copy
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import inventory_integration as api
from database import AppError, Database
from fulfillment_profiles_test import configure, order
from inventory_integration_demo import sample_payload, synthetic_pdf
from special_shipments_test import seed


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="inventory-test-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "synthetic.db"))
        self.users = seed(self.db)
        configure(self.db, self.users)

    def create(self):
        payload = sample_payload(str(uuid.uuid4()))
        return api.create_dispatch(self.db, payload), payload

    def booking(self, sid, profile="kunming"):
        p = next(p for p in api.catalog(self.db)["profiles"] if p["id"] == profile)
        body = dict(profile_id=profile, profile_revision=p["revision"], request_key=str(uuid.uuid4()))
        return api.book_dispatch(self.db, sid, body), body

    def fail_job(self, job):
        return self.db.complete_shipping_job(job["batch_item_id"], {"success": False, "error": "SYNTHETIC"}, expected_attempt=job["booking_attempt"])

    def succeed(self, job):
        return self.db.complete_shipping_job(job["batch_item_id"], {"success": True,
            "tracking_no": "SYNTHETIC-"+str(job["batch_item_id"]), "task_id": "TASK-"+str(job["batch_item_id"]),
            "label_url": "https://api.kuaidi100.com/synthetic.pdf"}, expected_attempt=job["booking_attempt"])

    def error(self, status, call):
        with self.assertRaises(AppError) as context:
            call()
        self.assertEqual(context.exception.status, status)

    def next_page(self, page, db=None):
        return api.read_events(db or self.db, str(page["next_cursor"]), "100", page["stream_id"], page["next_checkpoint"])

    def test_auth_separation(self):
        token = "x"*64
        headers = Message()
        with patch.dict(os.environ, {"SCENTPOOL_INVENTORY_TOKEN": token, "SCENTPOOL_AUDIT_TOKEN": "y"*64}):
            self.error(401, lambda: api.authorize(headers))
            headers["Authorization"] = "Bearer "+"y"*64
            self.error(401, lambda: api.authorize(headers))
            del headers["Authorization"]
            headers["Authorization"] = "Bearer "+token
            api.authorize(headers)
            headers["Authorization"] = "Bearer "+token
            self.error(401, lambda: api.authorize(headers))
        with patch.dict(os.environ, {"SCENTPOOL_INVENTORY_TOKEN": "y"*64, "SCENTPOOL_AUDIT_TOKEN": "y"*64}):
            self.error(503, lambda: api.authorize(headers))

    def test_parallel_create_conflicts_and_no_cross_owner(self):
        payload = sample_payload(str(uuid.uuid4()))
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: api.create_dispatch(self.db, payload), range(8)))
        sid = rows[0]["id"]
        self.assertEqual({r["id"] for r in rows}, {sid})
        changed = copy.deepcopy(payload); changed["owner"]["external_id"] = "different"
        self.error(409, lambda: api.create_dispatch(self.db, changed))
        for user in self.users.values():
            self.assertEqual(self.db.list_shipments(user, {"id": sid}), [])
        for user in (self.users["store"], self.users["other"], self.users["team"]):
            self.error(404, lambda: self.db.get_shipment(sid, user))
        self.assertEqual(self.db.get_shipment(sid, self.users["admin"])["id"], sid)
        with self.db.connect_readonly() as conn:
            service = conn.execute("SELECT id,active FROM stores WHERE name LIKE '库存系统专用-%'").fetchone()
            self.assertEqual(service["active"], 0)
            principal = conn.execute("SELECT active,role FROM users WHERE store_id=?", (service["id"],)).fetchone()
            self.assertEqual(tuple(principal), (0, "staff"))
        self.assertNotIn(service["id"], [s["id"] for s in self.db.list_stores(include_inactive=True)])

    def test_invalid_create_rolls_back(self):
        initial = api.read_events(self.db, "0", "100")
        for value in (True, 1.5, 0, -1, 100001, "2"):
            payload = sample_payload(str(uuid.uuid4())); payload["items"][0]["quantity"] = value
            self.error(400, lambda: api.create_dispatch(self.db, payload))
        payload = sample_payload(str(uuid.uuid4())); payload["items"][0]["barcode"] = "missing"
        self.error(400, lambda: api.create_dispatch(self.db, payload))
        self.assertEqual(self.next_page(initial)["events"], [])
        with self.db.connect_readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_dispatches").fetchone()[0], 0)

    def test_global_booking_key_scope_and_immutable_source(self):
        first, _ = self.create(); other, _ = self.create()
        _, body = self.booking(first["id"])
        self.error(409, lambda: api.book_dispatch(self.db, other["id"], body))
        self.error(409, lambda: api.book_dispatch(self.db, first["id"], {**body, "profile_id": "kunming_sf"}))
        with self.assertRaises(sqlite3.IntegrityError), self.db.connect() as conn:
            conn.execute("UPDATE shipments SET inventory_source_kind='express' WHERE id=?", (first["id"],))
        with self.db.connect_readonly() as conn:
            store_id = int(conn.execute("SELECT value FROM inventory_meta WHERE key='store_id'").fetchone()[0])
        self.error(403, lambda: self.db.update_store(store_id, True))

    def test_migration_failure_rolls_back_schema_and_baseline(self):
        legacy = Database(str(Path(self.tmp.name) / "migration-failure.db"))
        with patch.object(api, "migrate", lambda _conn: None):
            seed(legacy)
        with legacy.connect() as conn:
            class FailConnection:
                def execute(self, sql, *args):
                    if "INSERT INTO inventory_meta VALUES('initialized'" in sql:
                        raise RuntimeError("synthetic migration failure")
                    return conn.execute(sql, *args)
            with self.assertRaises(RuntimeError):
                api.migrate(FailConnection())
            self.assertNotIn("inventory_source_kind", {r["name"] for r in conn.execute("PRAGMA table_info(shipments)")})
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='inventory_events'").fetchone())
            api.migrate(conn)
        self.assertEqual(api.read_events(legacy, "0", "100")["next_checkpoint"], "")

    def test_bounded_page_load(self):
        for index in range(250):
            order(self.db, self.users, "LOAD-"+str(index))
        started = time.monotonic()
        page = api.read_events(self.db, "0", "100")
        self.assertEqual(len(page["events"]), 100)
        seen = list(page["events"])
        while page["has_more"]:
            page = self.next_page(page); seen.extend(page["events"])
        self.assertEqual(len({e["shipment"]["id"] for e in seen}), 250)
        self.assertLess(time.monotonic()-started, 10)
        with self.db.connect_readonly() as conn:
            plan = " ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM inventory_latest WHERE cursor>100 ORDER BY cursor LIMIT 101"))
        self.assertIn("INDEX", plan.upper())

    def test_parallel_retry_same_identity_and_snapshot(self):
        row, _ = self.create(); sid = row["id"]
        _, body = self.booking(sid)
        first = self.db.claim_next_shipping_job(); self.fail_job(first)
        # Live configuration changes do not overwrite the original authorization snapshot.
        with self.db.connect() as conn:
            conn.execute("UPDATE fulfillment_profiles SET revision=revision+1 WHERE id='kunming'")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: api.book_dispatch(self.db, sid, body), range(8)))
        self.assertEqual({r["batch_id"] for r in results}, {first["batch_id"]})
        retry = self.db.claim_next_shipping_job()
        self.assertEqual(retry["booking_request_id"], first["booking_request_id"])
        self.assertEqual(retry["shipping_settings_snapshot"], first["shipping_settings_snapshot"])
        self.assertIsNone(self.db.claim_next_shipping_job())
        self.assertTrue(self.succeed(first)["ignored"])
        self.succeed(retry)
        self.assertTrue(self.fail_job(retry)["ignored"])
        self.assertEqual(api.get_dispatch(self.db, sid)["booking_status"], "已出单")
        self.error(409, lambda: api.book_dispatch(self.db, sid, {**body, "request_key": str(uuid.uuid4())}))

    def test_cancel_rebook_and_late_results(self):
        row, _ = self.create(); sid = row["id"]
        _, body = self.booking(sid)
        first = self.db.claim_next_shipping_job(); self.succeed(first)
        before = api.read_events(self.db, "0", "100")
        # Failed/unknown external cancellation never calls the local clear method.
        self.assertEqual(api.get_dispatch(self.db, sid)["booking_status"], "已出单")
        self.db.mark_booking_cancelled(sid, expected_request_id=first["booking_request_id"])
        cancelled = self.next_page(before)["events"][-1]["shipment"]
        self.assertEqual((cancelled["status"], cancelled["channel"], cancelled["tracking_no"]), ("待处理", "unknown", ""))
        api.book_dispatch(self.db, sid, body)  # old key is a replay, not a new booking
        self.assertIsNone(self.db.claim_next_shipping_job())
        self.assertTrue(self.succeed(first)["ignored"])
        self.booking(sid, "kunming_sf")
        second = self.db.claim_next_shipping_job()
        self.assertNotEqual(first["booking_request_id"], second["booking_request_id"])
        self.succeed(second)
        self.error(409, lambda: self.db.mark_booking_cancelled(sid, expected_request_id=first["booking_request_id"]))
        self.assertTrue(self.fail_job(first)["ignored"])
        current = api.get_dispatch(self.db, sid)
        self.assertEqual((current["profile_id"], current["booking_status"]), ("kunming_sf", "已出单"))
        self.assertEqual(current["tracking_status"], "等待揽收")
        with self.db.connect() as conn:
            conn.execute("UPDATE shipments SET tracking_status='运输中' WHERE id=?", (sid,))
        self.assertEqual(api.get_dispatch(self.db, sid)["tracking_status"], "运输中")
        self.assertTrue(self.db.apply_label_print_callback("TASK-"+str(first["batch_item_id"]), "synthetic late callback", {"status":"200"})["ignored"])

    def test_events_transaction_rollback_items_and_delete(self):
        row, payload = self.create(); sid = row["id"]
        page = api.read_events(self.db, "0", "1")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE shipment_items SET quantity=5 WHERE shipment_id=?", (sid,))
            self.assertEqual(self.next_page(page)["events"], [])
            conn.rollback()
        self.assertEqual(self.next_page(page)["events"], [])
        with self.db.connect() as conn:
            conn.execute("UPDATE shipment_items SET quantity=7 WHERE shipment_id=?", (sid,))
        changed = self.next_page(page)
        self.assertEqual(len(changed["events"]), 1)
        self.assertEqual(changed["events"][0]["shipment"]["items"][0]["quantity"], 7)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM shipments WHERE id=?", (sid,))
        event = self.next_page(changed)["events"][0]
        self.assertEqual(event["kind"], "shipment.deleted")
        self.assertEqual(set(event["shipment"]), {"id", "source_ref", "source_kind"})
        self.error(409, lambda: api.create_dispatch(self.db, payload))

    def test_checkpoint_restart_restore_regrowth(self):
        row, _ = self.create()
        initial = api.read_events(self.db, "0", "100")
        backup = str(Path(self.tmp.name) / "backup.db")
        with self.db.connect_readonly() as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
        with self.db.connect() as conn:
            conn.execute("UPDATE shipments SET internal_note='branch one' WHERE id=?", (row["id"],))
        latest = self.next_page(initial)
        self.db.initialize("/nonexistent", production=True, admin_password="local-demo-only-2026")
        self.next_page(latest)  # normal restart retains identity
        restored = Database(backup)
        self.error(409, lambda: self.next_page(latest, restored))
        with restored.connect() as conn:
            for _ in range(10):
                conn.execute("UPDATE shipments SET internal_note='branch two' WHERE id=?", (row["id"],))
        self.error(409, lambda: self.next_page(latest, restored))
        self.next_page(initial, restored)  # common prefix remains valid
        self.error(409, lambda: api.read_events(self.db, str(latest["next_cursor"]), "100"))
        self.error(409, lambda: api.read_events(self.db, "0", "100", str(uuid.uuid4())))

    def test_legacy_migration_reference_only_and_idempotent(self):
        legacy = Database(str(Path(self.tmp.name) / "legacy.db"))
        with patch.object(api, "migrate", lambda _conn: None):
            users = seed(legacy)
            with patch.object(legacy, "get_shipment", return_value={}):
                order(legacy, users, "LEGACY-NOT-EXPORTED")
        legacy.initialize("/nonexistent", production=True, admin_password="local-demo-only-2026")
        page = api.read_events(legacy, "0", "100")
        self.assertEqual(len(page["events"]), 1)
        state = page["events"][0]["shipment"]
        self.assertTrue(state["historical_reference"])
        self.assertTrue(page["events"][0]["historical_snapshot"])
        self.assertEqual(state["business_id"], "EXP-1")
        legacy.initialize("/nonexistent", production=True, admin_password="local-demo-only-2026")
        with legacy.connect_readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_historical").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(sqlite3.IntegrityError), legacy.connect() as conn:
            conn.execute("DELETE FROM inventory_events")

    def test_product_snapshot_and_disabled_replay(self):
        row, payload = self.create()
        product = dict(name="新合成名称", category="新分类", spec="新规格", status="停用")
        api.put_product(self.db, "DEMO-PRODUCT", product)
        self.assertEqual(api.create_dispatch(self.db, payload)["id"], row["id"])
        self.error(400, lambda: self.create())
        state = api.read_events(self.db, "0", "100")["events"][0]["shipment"]
        self.assertEqual(state["items"][0]["name"], "演示香包")
        with self.db.connect_readonly() as conn:
            self.assertEqual(float(conn.execute("SELECT price FROM products WHERE barcode='DEMO-PRODUCT'").fetchone()[0]), 1.0)

    def test_conflicting_source_is_unknown_and_privacy(self):
        row, _ = self.create(); self.booking(row["id"])
        job = self.db.claim_next_shipping_job(); self.succeed(job)
        with self.db.connect() as conn:
            conn.execute("UPDATE shipping_batch_items SET settings_snapshot_json='{}' WHERE id=?", (job["batch_item_id"],))
        state = api.read_events(self.db, "0", "100")["events"][0]["shipment"]
        self.assertEqual(state["channel"], "unknown")
        forbidden = {"recipient_name", "phone", "address", "label_url", "internal_note", "settings_snapshot_json", "booking_raw", "partner_key"}
        def check(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for item in value.values(): check(item)
            elif isinstance(value, list):
                for item in value: check(item)
        check(state)

    def test_http_permission_size_semaphore_pdf(self):
        import server
        row, _ = self.create(); sid = row["id"]
        self.booking(sid); job = self.db.claim_next_shipping_job(); self.succeed(job)
        token = "z"*64
        class Handler(server.Handler):
            def log_message(self, *_args): pass
        with patch.object(server, "DB", self.db, create=True), patch.dict(os.environ, {"SCENTPOOL_INVENTORY_TOKEN": token, "SCENTPOOL_AUDIT_TOKEN": "a"*64}):
            httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", 0), Handler, 4)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            def request(path, method="GET", body=None, auth=token, extra=None):
                connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                try:
                    headers = {"Authorization": "Bearer "+auth, "Content-Type": "application/json", **(extra or {})}
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    return response.status, response.read()
                finally: connection.close()
            try:
                prefix = api.PREFIX
                self.assertEqual(request("/api/admin/shipping-settings")[0], 401)
                cookie = self.db.create_session(self.users["admin"]["id"])
                admin_cookie = {"Cookie": "scentpool_session="+cookie}
                self.assertEqual(request("/api/me", auth="", extra=admin_cookie)[0], 200)
                task_path = f"/api/admin/inventory-tasks/{sid}"
                self.assertEqual(request(task_path)[0], 401)
                self.assertEqual(request(task_path, auth="a"*64)[0], 401)
                staff_cookie = {"Cookie": "scentpool_session="+self.db.create_session(self.users["store"]["id"])}
                self.assertEqual(request(task_path, auth="", extra=staff_cookie)[0], 403)
                self.assertEqual(request(f"/?inventory_task={sid}", auth="", extra=staff_cookie)[0], 403)
                self.assertEqual(request(f"/?inventory_task={sid}", auth="", extra=admin_cookie)[0], 200)
                self.assertEqual(request(f"/?inventory_task={sid}&inventory_task={sid}")[0], 400)
                ordinary = order(self.db, self.users, "NOT-INVENTORY")
                self.assertEqual(request(f"/api/admin/inventory-tasks/{ordinary['id']}", auth="", extra=admin_cookie)[0], 404)
                status, content = request(task_path, auth="", extra=admin_cookie)
                self.assertEqual(status, 200)
                task = json.loads(content)["task"]
                self.assertTrue(task["can_cancel"])
                self.assertFalse({"settings_snapshot_json", "booking_raw", "label_url"}.intersection(task))
                cancel_body = json.dumps({"expected_cancel_version": task["cancel_version"]}).encode()
                self.assertEqual(request(prefix+"/catalog", auth="", extra=admin_cookie)[0], 401)
                self.assertEqual(request(prefix+"/catalog", auth="a"*64)[0], 401)
                self.assertEqual(request(prefix+"/dispatches", "POST", b"x"*65537)[0], 413)
                self.assertEqual(request(prefix+"/events?cursor=0&cursor=1")[0], 400)
                api.SLOTS.acquire(); api.SLOTS.acquire()
                try: self.assertEqual(request(prefix+"/catalog")[0], 503)
                finally: api.SLOTS.release(); api.SLOTS.release()
                with patch.object(server, "build_batch_label_pdf_file", synthetic_pdf):
                    status, content = request(prefix+f"/dispatches/{sid}/label")
                    self.assertEqual(status, 200); self.assertTrue(content.startswith(b"%PDF"))
                with patch.object(server.Kuaidi100LabelClient, "from_env") as factory:
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", b"{}", auth="", extra=admin_cookie)[0], 409)
                    factory.assert_not_called()
                    factory.return_value.cancel_label.return_value = {"success":False,"error":"合成取消拒绝"}
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", cancel_body, auth="", extra=admin_cookie)[0], 502)
                    self.assertEqual(api.get_dispatch(self.db, sid)["booking_status"], "已出单")
                    factory.return_value.cancel_label.side_effect = TimeoutError("synthetic timeout")
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", cancel_body, auth="", extra=admin_cookie)[0], 500)
                    self.assertEqual(api.get_dispatch(self.db, sid)["booking_status"], "已出单")
                def cancelled(rows, directory):
                    path = synthetic_pdf(rows, directory)
                    self.db.mark_booking_cancelled(sid)
                    return path
                with patch.object(server, "build_batch_label_pdf_file", cancelled):
                    self.assertEqual(request(prefix+f"/dispatches/{sid}/label")[0], 409)
                self.assertEqual(request(prefix+f"/dispatches/{sid}/label?url=https://example.com")[0], 400)
                self.booking(sid); self.succeed(self.db.claim_next_shipping_job())
                with patch.object(server.Kuaidi100LabelClient, "from_env") as factory:
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", cancel_body, auth="", extra=admin_cookie)[0], 409)
                    factory.assert_not_called()
                    current = api.admin_task(self.db, sid, self.users["admin"])
                    fresh_body = json.dumps({"expected_cancel_version": current["cancel_version"]}).encode()
                    factory.return_value.cancel_label.return_value = {"success": True}
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", fresh_body, auth="", extra=staff_cookie)[0], 403)
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", fresh_body)[0], 401)
                    factory.assert_not_called()
                    self.assertEqual(request(f"/api/shipments/{sid}/label/cancel", "POST", fresh_body, auth="", extra=admin_cookie)[0], 200)
                    self.assertEqual(api.get_dispatch(self.db, sid)["booking_status"], "已取消")
                    self.assertFalse(api.admin_task(self.db, sid, self.users["admin"])["can_cancel"])
            finally:
                httpd.shutdown(); thread.join(timeout=5); httpd.server_close()


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IntegrationTests))
    if not result.wasSuccessful():
        raise AssertionError("inventory integration regression failed")


if __name__ == "__main__":
    main()
