"""Synthetic-only manual priority, bounded fairness, and old-gate migration tests."""
import tempfile
import time
from pathlib import Path

from database import Database, now_text
from special_shipments_test import seed
from tracking_queue import COOLDOWN, MANUAL_BURST, TrackingQueue


def create_record(db, users, kind, number):
    if kind == "return":
        return db.create_return_order(users["store"], {
            "tracking_no": f"LOCAL-PRIORITY-RETURN-{number}",
            "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}],
        })
    row = db.create_shipment(users["store"], {
        "recipient_name": "合成测试收件人", "phone": "13800000000",
        "address": "合成测试地址，不寄送", "store_order_no": f"PRIORITY-{number}",
        "items": [{"barcode": "DEMO-PRODUCT", "quantity": 1}],
    })
    return db.update_shipment(row["id"], {
        "status": "已发货", "express_company": "圆通",
        "tracking_no": f"LOCAL-PRIORITY-SHIPMENT-{number}",
    })


def finish(queue, conn, job, now):
    result = queue.finish(conn, job, {
        "tracking_status": "运输中", "provider": "synthetic", "checked_at": now_text(),
    }, 1, now + .01)
    assert result["state"] == "completed", result


def promotion(db, users):
    queue = TrackingQueue(db)
    row = create_record(db, users, "shipment", 1)
    older = create_record(db, users, "shipment", 2)
    with db.connect() as conn:
        conn.execute("UPDATE tracking_jobs SET origin='automatic',created_at=created_at-60 WHERE record_id=?", (older["id"],))
    # Saving subscribes to the initial intent, without marking it manual.
    saved_task = queue.request(users["admin"], "shipment", row["id"], subscribe=True)
    with db.connect() as conn:
        before = dict(conn.execute("SELECT * FROM tracking_jobs WHERE record_id=?", (row["id"],)).fetchone())
    assert before["origin"] == "initial"
    manual = queue.request(users["admin"], "shipment", row["id"])
    assert manual["id"] == saved_task["id"]
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM tracking_jobs WHERE id=?", (before["id"],)).fetchone())
    assert after == {**before, "origin": "manual"}
    assert queue.request(users["admin"], "shipment", row["id"])["id"] == saved_task["id"]
    conn = db.connect()
    try:
        now = time.time() + 2
        job = queue.claim(conn, now)
        assert job["record_id"] == row["id"], "manual must overtake an older automatic job"
        assert queue.claim(conn, now) is None, "priority cannot bypass the provider lease"
        finish(queue, conn, job, now)
        # An automatic job already running is neither replaced nor re-leased.
        automatic = queue.claim(conn, now + 2)
        assert automatic["record_id"] == older["id"]
        before_running = dict(conn.execute("SELECT * FROM tracking_jobs WHERE id=?", (automatic["id"],)).fetchone())
        queue.request(users["admin"], "shipment", older["id"])
        assert dict(conn.execute("SELECT * FROM tracking_jobs WHERE id=?", (automatic["id"],)).fetchone()) == before_running
        finish(queue, conn, automatic, now + 2)
    finally:
        conn.close()
    # An existing automatic intent without a receipt is also reused in place;
    # promotion must leave its cooldown and previous attempts intact.
    returned = create_record(db, users, "return", 1)
    with db.connect() as conn:
        conn.execute("UPDATE tracking_jobs SET origin='automatic',due_at=?,attempt_count=1 WHERE kind='return'", (now + COOLDOWN,))
        automatic = dict(conn.execute("SELECT * FROM tracking_jobs WHERE kind='return'").fetchone())
    queue.request(users["store"], "return", returned["id"])
    with db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM tracking_jobs WHERE kind='return'").fetchone()) == {**automatic, "origin": "manual"}


def fairness(db, users):
    queue = TrackingQueue(db)
    now = time.time() + 2
    expected = {}
    for kind in ("shipment", "return"):
        background = [create_record(db, users, kind, n) for n in (1, 2)]
        manual = [create_record(db, users, kind, n) for n in range(3, 11)]
        for row in manual:
            queue.request(users["admin"], kind, row["id"])
        with db.connect() as conn:
            conn.execute("UPDATE tracking_jobs SET created_at=id, origin='automatic' WHERE kind=? AND record_id=?", (kind, background[0]["id"]))
            conn.execute("UPDATE tracking_jobs SET created_at=id WHERE kind=?", (kind,))
        expected[kind] = ([r["id"] for r in manual[:3]] + [background[0]["id"]]
                          + [r["id"] for r in manual[3:6]] + [background[1]["id"]]
                          + [r["id"] for r in manual[6:]])
    conn = db.connect()
    seen = {"shipment": [], "return": []}
    try:
        for index in range(20):
            if index == 6:
                # Both kinds reached their manual quota. A restart cannot reset
                # their debt to the older, already-due background work.
                conn.close()
                queue = TrackingQueue(Database(db.path))
                conn = queue.db.connect()
            stamp = now + index * 2
            job = queue.claim(conn, stamp)
            assert job, index
            assert job["kind"] == ("shipment" if index % 2 == 0 else "return")
            seen[job["kind"]].append(job["record_id"])
            finish(queue, conn, job, stamp)
        assert seen == expected, (seen, expected)
        assert queue.claim(conn, now + 100) is None
    finally:
        conn.close()


def cooldown_and_circuit(db, users):
    queue = TrackingQueue(db)
    returned = create_record(db, users, "return", 1)
    now = time.time() + 2
    conn = db.connect()
    try:
        job = queue.claim(conn, now)
        finish(queue, conn, job, now)
        task = queue.request(users["store"], "return", returned["id"], request_key="local_priority_request_001")
        with db.connect() as other:
            queued = dict(other.execute("SELECT * FROM tracking_jobs WHERE state='queued'").fetchone())
            other.execute("UPDATE tracking_provider_gate SET pause_until=?", (now + COOLDOWN + 10,))
        assert queued["due_at"] >= now + COOLDOWN
        assert queue.request(users["store"], "return", returned["id"], request_key="local_priority_request_001")["id"] == task["id"]
        assert dict(conn.execute("SELECT * FROM tracking_jobs WHERE id=?", (queued["id"],)).fetchone()) == queued
        assert queue.claim(conn, now + COOLDOWN + 1) is None, "manual cannot bypass provider backoff"
        assert queue.claim(conn, now + COOLDOWN + 11)["id"] == queued["id"]
    finally:
        conn.close()


def legacy_migration(db, users):
    create_record(db, users, "shipment", 1)
    create_record(db, users, "return", 1)
    with db.connect() as conn:
        jobs = [dict(row) for row in conn.execute("SELECT * FROM tracking_jobs ORDER BY id")]
        business = [dict(row) for row in conn.execute("SELECT * FROM shipments")]
        # Reproduce the schema shipped in 09d23ba with an active provider lease.
        # Only this disposable synthetic database is ever modified directly.
        conn.executescript("""
            DROP TABLE tracking_provider_gate;
            CREATE TABLE tracking_provider_gate (
                id INTEGER PRIMARY KEY CHECK(id=1), next_allowed_at REAL NOT NULL DEFAULT 0,
                pause_until REAL NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                last_kind TEXT NOT NULL DEFAULT 'return'
            );
            INSERT INTO tracking_provider_gate VALUES(1,100,200,2,300,'synthetic-lease','shipment');
        """)
    db.initialize(product_file="/nonexistent/synthetic-products.xlsx", production=True, admin_password="local-demo-only-2026")
    with db.connect() as conn:
        gate = dict(conn.execute("SELECT * FROM tracking_provider_gate").fetchone())
        assert gate == {"id": 1, "next_allowed_at": 100, "pause_until": 200, "consecutive_failures": 2,
                        "lease_until": 300, "lease_token": "synthetic-lease", "last_kind": "shipment",
                        "manual_streak_shipment": 0, "manual_streak_return": 0}
        assert [dict(row) for row in conn.execute("SELECT * FROM tracking_jobs ORDER BY id")] == jobs
        assert [dict(row) for row in conn.execute("SELECT * FROM shipments")] == business
        conn.execute("UPDATE tracking_provider_gate SET manual_streak_shipment=?,manual_streak_return=1", (MANUAL_BURST,))
    db.initialize(product_file="/nonexistent/synthetic-products.xlsx", production=True, admin_password="local-demo-only-2026")
    with db.connect() as conn:
        gate = conn.execute("SELECT * FROM tracking_provider_gate").fetchone()
        assert gate["manual_streak_shipment"] == MANUAL_BURST and gate["manual_streak_return"] == 1


def run():
    for scenario in (promotion, fairness, cooldown_and_circuit, legacy_migration):
        with tempfile.TemporaryDirectory(prefix="scentpool-priority-test-") as tmp:
            db = Database(str(Path(tmp) / "synthetic.db"))
            users = seed(db)
            scenario(db, users)
            assert db.connection_diagnostics()["active"] == 0
    print("tracking priority: task reuse, bounded manual priority, business fairness, restart, cooldown/circuit and old schema PASS")


if __name__ == "__main__":
    run()
