from __future__ import annotations

import json
import sqlite3
import tempfile
from argparse import Namespace
from pathlib import Path

import manage
from database import AppError, Database


CREATED = "2026-07-01T08:00:00+08:00"


def insert_shipment(
    conn: sqlite3.Connection,
    *,
    code: str,
    store_id: int,
    user_id: int,
    status: str = "待处理",
    shipped_at: str = "",
    shipped_quality: str = "",
    signed_at: str = "",
    signed_quality: str = "",
    tracking_raw: str = "",
    tracking_checked: str = "",
    booking_requested: str = "",
    booking_updated: str = "",
    updated_at: str = CREATED,
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO shipments (
                order_date, business_id, store_id, store_name_snapshot, created_by,
                recipient_name, phone, address, store_order_no, status, shipped_at,
                shipped_at_quality, shipped_at_source, tracking_signed_at,
                tracking_signed_at_quality, tracking_signed_at_source, tracking_raw,
                tracking_last_checked_at, booking_requested_at, booking_updated_at,
                created_at, updated_at
            ) VALUES (
                '2026-07-01', ?, ?, '合成门店', ?, '合成收件人', '13000000000',
                '合成地址', ?, ?, ?, ?, 'synthetic', ?, ?, 'synthetic', ?, ?, ?, ?, ?, ?
            )
            """,
            (
                f"SYNTHETIC-{code}", store_id, user_id, f"ORDER-{code}", status,
                shipped_at, shipped_quality, signed_at, signed_quality, tracking_raw,
                tracking_checked, booking_requested, booking_updated, CREATED, updated_at,
            ),
        ).lastrowid
    )


def setup_db(path: Path) -> tuple[Database, int, int]:
    db = Database(str(path))
    db.initialize(str(path.parent / "missing.xlsx"))
    with db.connect() as conn:
        user_id = int(conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()[0])
        store_id = int(conn.execute("SELECT id FROM stores LIMIT 1").fetchone()[0])
    return db, user_id, store_id


def assert_raises(callback, text: str = "") -> AppError:
    try:
        callback()
    except AppError as exc:
        if text:
            assert text in exc.message, exc.message
        return exc
    raise AssertionError("expected AppError")


def test_write_paths(tmp: Path) -> None:
    db, user_id, store_id = setup_db(tmp / "writes.db")
    with db.connect() as conn:
        transit_id = insert_shipment(conn, code="TRANSIT", store_id=store_id, user_id=user_id)
        direct_signed_id = insert_shipment(conn, code="DIRECT-SIGNED", store_id=store_id, user_id=user_id)
        observed_signed_id = insert_shipment(conn, code="OBSERVED-SIGNED", store_id=store_id, user_id=user_id)
        manual_id = insert_shipment(conn, code="MANUAL", store_id=store_id, user_id=user_id)
        manual_exact_id = insert_shipment(conn, code="MANUAL-EXACT", store_id=store_id, user_id=user_id)
        label_id = insert_shipment(conn, code="LABEL", store_id=store_id, user_id=user_id)
        guarded_id = insert_shipment(conn, code="GUARDED", store_id=store_id, user_id=user_id)
        try:
            conn.execute("UPDATE shipments SET status = '已发货' WHERE id = ?", (guarded_id,))
            raise AssertionError("database trigger must reject missing shipped_at")
        except sqlite3.IntegrityError:
            pass

    db.apply_tracking_result(
        transit_id,
        {
            "provider": "kuaidi100", "tracking_status": "运输中", "checked_at": "2026-07-01T09:00:00+08:00",
            "signed_at": "", "raw": "{}", "is_signed": False,
        },
    )
    db.apply_tracking_result(
        direct_signed_id,
        {
            "provider": "kuaidi100", "tracking_status": "已签收", "checked_at": "2026-07-02T10:00:00+08:00",
            "signed_at": "2026-07-02 09:30:00", "signed_at_source": "provider_event",
            "raw": "{}", "is_signed": True,
        },
    )
    db.apply_tracking_result(
        observed_signed_id,
        {
            "provider": "kuaidi100", "tracking_status": "已签收", "checked_at": "2026-07-02T11:00:00+08:00",
            "signed_at": "", "raw": "{}", "is_signed": True,
        },
    )
    db.update_shipment(
        manual_id,
        {"status": "已发货", "express_company": "圆通", "tracking_no": "", "shipping_note": ""},
    )
    db.update_shipment(
        manual_exact_id,
        {
            "status": "已发货", "express_company": "圆通", "tracking_no": "", "shipping_note": "",
            "shipped_at": "2026-07-01T09:00:00+08:00",
        },
    )

    with db.connect() as conn:
        rows = {
            int(row["id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM shipments WHERE id IN (?, ?, ?, ?, ?)",
                (transit_id, direct_signed_id, observed_signed_id, manual_id, manual_exact_id),
            )
        }
    assert rows[transit_id]["status"] == "已发货"
    assert rows[transit_id]["shipped_at_quality"] == "estimated"
    assert rows[direct_signed_id]["tracking_signed_at_quality"] == "exact"
    assert rows[direct_signed_id]["shipped_at_quality"] == "estimated"
    assert rows[observed_signed_id]["tracking_signed_at_quality"] == "estimated"
    assert rows[manual_id]["shipped_at_quality"] == "estimated"
    assert rows[manual_exact_id]["shipped_at_quality"] == "exact"
    assert rows[manual_exact_id]["shipped_at_source"] == "manual_input"

    assert_raises(
        lambda: db.update_shipment(
            manual_id,
            {"status": "已发货", "express_company": "圆通", "shipped_at": "2026-07-01T10:00:00"},
        ),
        "时区",
    )
    assert_raises(
        lambda: db.update_shipment(
            manual_id,
            {"status": "已发货", "express_company": "圆通", "shipped_at": "2026-06-30T10:00:00+08:00"},
        ),
        "不能早于",
    )

    with db.connect() as conn:
        batch_id = int(
            conn.execute(
                """
                INSERT INTO shipping_batches (
                    created_by, pickup_day, pickup_start_time, pickup_end_time,
                    status, total_count, created_at, updated_at
                ) VALUES (?, '', '', '', '处理中', 1, ?, ?)
                """,
                (user_id, CREATED, CREATED),
            ).lastrowid
        )
        item_id = int(
            conn.execute(
                """
                INSERT INTO shipping_batch_items (
                    batch_id, shipment_id, status, created_at, updated_at
                ) VALUES (?, ?, '提交中', ?, ?)
                """,
                (batch_id, label_id, CREATED, CREATED),
            ).lastrowid
        )
    db.complete_shipping_job(item_id, {"success": True, "tracking_no": "SYNTHETIC-LABEL"})
    with db.connect() as conn:
        label = dict(conn.execute("SELECT * FROM shipments WHERE id = ?", (label_id,)).fetchone())
    assert label["status"] == "已发货"
    assert label["shipped_at_quality"] == "estimated"
    assert label["shipped_at_source"] == "label_success_observed_at"
    db.mark_booking_cancelled(label_id)
    with db.connect() as conn:
        label = dict(conn.execute("SELECT * FROM shipments WHERE id = ?", (label_id,)).fetchone())
    assert label["status"] == "待处理"
    assert label["shipped_at"] == label["shipped_at_quality"] == ""


def seed_repair_cases(db: Database, user_id: int, store_id: int) -> None:
    provider_raw = json.dumps(
        {"status": "200", "state": "3", "ischeck": "1", "data": [{"ftime": "2026-07-02 10:00:00"}]},
        ensure_ascii=False,
    )
    with db.connect() as conn:
        conn.execute("DROP TRIGGER shipments_time_integrity_insert")
        conn.execute("DROP TRIGGER shipments_time_integrity_update")
        insert_shipment(
            conn, code="EXACT-SIGNED", store_id=store_id, user_id=user_id, status="已签收",
            shipped_at="2026-07-01T09:00:00+08:00", shipped_quality="exact", tracking_raw=provider_raw,
        )
        insert_shipment(
            conn, code="EST-SHIPPED", store_id=store_id, user_id=user_id, status="已发货",
            booking_updated="2026-07-01T10:00:00+08:00", updated_at="2026-07-01T11:00:00+08:00",
        )
        insert_shipment(
            conn, code="EST-BOTH", store_id=store_id, user_id=user_id, status="已签收",
            tracking_raw=provider_raw, booking_requested="2026-07-01T09:30:00+08:00",
        )
        insert_shipment(
            conn, code="INVALID-SHIPPED", store_id=store_id, user_id=user_id, status="已发货",
            shipped_at="broken", booking_updated="2026-07-01T10:30:00+08:00",
        )
        insert_shipment(
            conn, code="REVERSED", store_id=store_id, user_id=user_id, status="已签收",
            shipped_at="2026-07-02T12:00:00+08:00", shipped_quality="exact",
            signed_at="2026-07-02T11:00:00+08:00", signed_quality="estimated",
            tracking_checked="2026-07-02T13:00:00+08:00",
        )
        unsafe_id = insert_shipment(
            conn, code="UNSAFE", store_id=store_id, user_id=user_id, status="已签收",
            updated_at="broken",
        )
        conn.execute("UPDATE shipments SET created_at = 'broken' WHERE id = ?", (unsafe_id,))
        db._ensure_shipment_time_integrity(conn)


def test_repair_workflow(tmp: Path) -> None:
    db, user_id, store_id = setup_db(tmp / "repairs.db")
    seed_repair_cases(db, user_id, store_id)
    with db.connect_readonly() as conn:
        before = [tuple(row) for row in conn.execute(
            "SELECT id, shipped_at, tracking_signed_at FROM shipments ORDER BY id"
        ).fetchall()]
        assert conn.execute("SELECT COUNT(*) FROM shipment_time_repair_events").fetchone()[0] == 0

    preview = db.preview_shipment_time_repairs()
    assert preview["mode"] == "dry_run"
    assert preview["repairability"] == {
        "exact_records": 1,
        "estimated_records": 4,
        "unsafe_records": 1,
    }, preview
    assert preview["planned_change_records"] == 5
    serialized = json.dumps(preview, ensure_ascii=False)
    for forbidden in ("SYNTHETIC-", "ORDER-", "合成收件人", "13000000000", "合成地址"):
        assert forbidden not in serialized
    with db.connect_readonly() as conn:
        after = [tuple(row) for row in conn.execute(
            "SELECT id, shipped_at, tracking_signed_at FROM shipments ORDER BY id"
        ).fetchall()]
    assert after == before

    exact_result = db.apply_shipment_time_repairs(
        preview_fingerprint=preview["preview_fingerprint"], max_rows=1, include_estimated=False
    )
    assert exact_result["applied_records"] == 1
    next_preview = db.preview_shipment_time_repairs()
    assert_raises(
        lambda: db.apply_shipment_time_repairs(
            preview_fingerprint=preview["preview_fingerprint"], max_rows=10, include_estimated=True
        ),
        "已变化",
    )
    assert_raises(
        lambda: db.apply_shipment_time_repairs(
            preview_fingerprint=next_preview["preview_fingerprint"], max_rows=1, include_estimated=True
        ),
        "超过",
    )
    result = db.apply_shipment_time_repairs(
        preview_fingerprint=next_preview["preview_fingerprint"], max_rows=10, include_estimated=True
    )
    assert result["applied_records"] == 4
    final_preview = db.preview_shipment_time_repairs()
    assert final_preview["planned_change_records"] == 0
    assert final_preview["repairability"]["unsafe_records"] == 1
    repeated = db.apply_shipment_time_repairs(
        preview_fingerprint=final_preview["preview_fingerprint"], max_rows=10, include_estimated=True
    )
    assert repeated["applied_records"] == 0
    with db.connect_readonly() as conn:
        events = conn.execute("SELECT * FROM shipment_time_repair_events").fetchall()
        assert len(events) >= 5
        columns = {description[0] for description in conn.execute("SELECT * FROM shipment_time_repair_events").description}
    assert not columns.intersection({"recipient_name", "phone", "address", "business_id", "tracking_no", "tracking_raw"})


def test_preview_change_and_legacy_normalization(tmp: Path) -> None:
    db, user_id, store_id = setup_db(tmp / "change.db")
    with db.connect() as conn:
        conn.execute("DROP TRIGGER shipments_time_integrity_insert")
        conn.execute("DROP TRIGGER shipments_time_integrity_update")
        shipment_id = insert_shipment(
            conn, code="CHANGE", store_id=store_id, user_id=user_id, status="已发货",
            booking_updated="2026-07-01T09:00:00+08:00",
        )
        pending_id = insert_shipment(conn, code="LEGACY", store_id=store_id, user_id=user_id)
        conn.execute("UPDATE shipments SET tracking_no = 'SYNTHETIC' WHERE id = ?", (pending_id,))
        db._ensure_shipment_time_integrity(conn)
    preview = db.preview_shipment_time_repairs()
    with db.connect() as conn:
        conn.execute(
            "UPDATE shipments SET booking_updated_at = '2026-07-01T09:30:00+08:00' WHERE id = ?",
            (shipment_id,),
        )
    assert_raises(
        lambda: db.apply_shipment_time_repairs(
            preview_fingerprint=preview["preview_fingerprint"], max_rows=10, include_estimated=True
        ),
        "已变化",
    )
    db.initialize(str(tmp / "missing.xlsx"))
    with db.connect_readonly() as conn:
        legacy = dict(conn.execute("SELECT * FROM shipments WHERE id = ?", (pending_id,)).fetchone())
    assert legacy["status"] == "已发货"
    assert legacy["shipped_at_quality"] == "estimated"
    assert legacy["shipped_at_source"] == "legacy_status_updated_at"


def test_cli_safety_and_backup_failures(tmp: Path) -> None:
    db_path = (tmp / "cli.db").resolve()
    db, user_id, store_id = setup_db(db_path)
    seed_repair_cases(db, user_id, store_id)
    preview = db.preview_shipment_time_repairs()
    wrong_db = tmp / "wrong-schema.db"
    sqlite3.connect(wrong_db).close()
    assert_raises(
        lambda: manage.command_repair_shipment_times(Namespace(db=wrong_db, apply=False)),
        "尚未具备",
    )

    def args(**overrides):
        values = {
            "db": db_path,
            "apply": True,
            "preview_fingerprint": preview["preview_fingerprint"],
            "max_rows": 10,
            "backup_output": tmp / "cli-backup.db",
            "confirm_db_path": str(db_path),
            "confirm": "APPLY_SHIPMENT_TIME_REPAIR",
            "include_estimated": True,
        }
        values.update(overrides)
        return Namespace(**values)

    assert_raises(
        lambda: manage.command_repair_shipment_times(args(confirm_db_path=str(tmp / "wrong.db"))),
        "路径二次确认",
    )
    original_backup = manage.Database.backup_to
    try:
        manage.Database.backup_to = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic backup failure"))
        assert_raises(lambda: manage.command_repair_shipment_times(args()), "备份或完整性检查失败")
        manage.Database.backup_to = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AppError("数据库备份完整性校验失败：synthetic", 500)
        )
        assert_raises(lambda: manage.command_repair_shipment_times(args()), "备份或完整性检查失败")
    finally:
        manage.Database.backup_to = original_backup
    with db.connect_readonly() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shipment_time_repair_events").fetchone()[0] == 0


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="shipment-time-integrity-") as directory:
        tmp = Path(directory)
        test_write_paths(tmp)
        test_repair_workflow(tmp)
        test_preview_change_and_legacy_normalization(tmp)
        test_cli_safety_and_backup_failures(tmp)
    print("shipment time integrity test passed")


if __name__ == "__main__":
    main()
