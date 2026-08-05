from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, Optional

import server
from database import Database


AUDIT_DATE = "2026-07-15"
AUDIT_TOKEN = "synthetic-audit-token-never-log"


def request(
    opener: Any,
    base: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[int, Any]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, method=method, headers=request_headers)
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def insert_synthetic_rows(db: Database) -> Dict[str, Any]:
    with db.connect() as conn:
        admin_id = int(conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()[0])
        now = "2026-07-01T00:00:00+08:00"
        alpha_id = int(
            conn.execute(
                "INSERT INTO stores (name, active, created_at, updated_at) VALUES ('审计门店甲', 1, ?, ?)",
                (now, now),
            ).lastrowid
        )
        beta_id = int(
            conn.execute(
                "INSERT INTO stores (name, active, created_at, updated_at) VALUES ('审计门店乙', 1, ?, ?)",
                (now, now),
            ).lastrowid
        )
        blank_id = int(
            conn.execute(
                "INSERT INTO stores (name, active, created_at, updated_at) VALUES ('   ', 1, ?, ?)",
                (now, now),
            ).lastrowid
        )

        identifiers: list[str] = []

        def add_shipment(
            code: str,
            store_id: int,
            store_name: str,
            created_at: str,
            *,
            status: str = "待处理",
            shipped_at: str = "",
            signed_at: str = "",
            booking_status: str = "未下单",
            booking_updated_at: str = "",
            tracking_status: str = "",
            tracking_checked_at: str = "",
            print_status: str = "",
            updated_at: str = "",
        ) -> None:
            business_id = f"PRIVATE-BUSINESS-{code}"
            store_order_no = f"PRIVATE-ORDER-{code}"
            recipient_name = f"PRIVATE-RECIPIENT-{code}"
            phone = f"1390000{len(identifiers):04d}"
            address = f"PRIVATE-ADDRESS-{code}"
            tracking_no = f"PRIVATE-TRACKING-{code}" if shipped_at else ""
            raw_marker = f"PRIVATE-RAW-{code}"
            identifiers.extend(
                [business_id, store_order_no, recipient_name, phone, address, tracking_no, raw_marker]
            )
            order_date = created_at[:10] if len(created_at) >= 10 and created_at[:4].isdigit() else "2026-07-01"
            conn.execute(
                """
                INSERT INTO shipments (
                    order_date, business_id, store_id, store_name_snapshot, created_by,
                    recipient_name, phone, address, store_order_no, status, tracking_no,
                    tracking_status, tracking_last_checked_at, tracking_signed_at, tracking_raw,
                    shipped_at, booking_status, booking_error, booking_raw, booking_updated_at,
                    label_print_status, label_print_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_date,
                    business_id,
                    store_id,
                    store_name,
                    admin_id,
                    recipient_name,
                    phone,
                    address,
                    store_order_no,
                    status,
                    tracking_no,
                    tracking_status,
                    tracking_checked_at,
                    signed_at,
                    raw_marker,
                    shipped_at,
                    booking_status,
                    "PRIVATE-BOOKING-ERROR" if booking_status == "下单失败" else "",
                    raw_marker,
                    booking_updated_at,
                    print_status,
                    "PRIVATE-PRINT-ERROR" if print_status == "打印失败" else "",
                    created_at,
                    updated_at or created_at,
                ),
            )

        add_shipment(
            "BEFORE-WINDOW",
            alpha_id,
            "审计门店甲",
            "2026-07-08T23:59:59+08:00",
            status="已签收",
            shipped_at="2026-07-08T23:59:59+08:00",
            signed_at="2026-07-08T23:59:59+08:00",
        )
        add_shipment(
            "WINDOW-START",
            alpha_id,
            "审计门店甲",
            "2026-07-09T00:00:00+08:00",
            status="已签收",
            shipped_at="2026-07-15T00:00:00+08:00",
            signed_at="2026-07-15T23:59:59+08:00",
        )
        add_shipment(
            "DAY-START",
            alpha_id,
            "审计门店甲",
            "2026-07-15T00:00:00+08:00",
            status="已签收",
            shipped_at="2026-07-15T23:59:59+08:00",
            signed_at="2026-07-16T00:00:00+08:00",
        )
        add_shipment(
            "DAY-END",
            beta_id,
            "审计门店乙",
            "2026-07-15T23:59:59+08:00",
            booking_status="提交中",
            booking_updated_at="2000-01-01T00:00:00+08:00",
        )
        add_shipment("NEXT-DAY", beta_id, "审计门店乙", "2026-07-16T00:00:00+08:00")
        add_shipment(
            "DAILY-FAILURES",
            beta_id,
            "审计门店乙",
            "2026-07-15T12:00:00+08:00",
            status="已发货",
            shipped_at="2026-07-15T12:30:00+08:00",
            booking_status="下单失败",
            booking_updated_at="2026-07-15T13:00:00+08:00",
            tracking_status="查询失败",
            tracking_checked_at="2026-07-15T14:00:00+08:00",
            print_status="打印失败",
            updated_at="2026-07-15T15:00:00+08:00",
        )
        add_shipment("PRIOR-BACKLOG", alpha_id, "审计门店甲", "2026-07-10T12:00:00+08:00")
        add_shipment(
            "PRIOR-LABEL-FAILURE",
            beta_id,
            "审计门店乙",
            "2026-07-14T12:00:00+08:00",
            booking_status="下单失败",
            booking_updated_at="2026-07-14T13:00:00+08:00",
        )
        add_shipment(
            "PRIOR-TRACKING-FAILURE",
            alpha_id,
            "审计门店甲",
            "2026-07-14T10:00:00+08:00",
            status="已发货",
            shipped_at="2026-07-14T10:30:00+08:00",
            tracking_status="查询失败",
            tracking_checked_at="2026-07-14T11:00:00+08:00",
        )
        add_shipment(
            "PRIOR-PRINT-FAILURE",
            beta_id,
            "审计门店乙",
            "2026-07-14T09:00:00+08:00",
            status="已发货",
            shipped_at="2026-07-14T09:30:00+08:00",
            print_status="打印失败",
            updated_at="2026-07-14T10:00:00+08:00",
        )
        add_shipment(
            "PRIOR-SIGNED",
            alpha_id,
            "审计门店甲",
            "2026-07-13T08:00:00+08:00",
            status="已签收",
            shipped_at="2026-07-13T09:00:00+08:00",
            signed_at="2026-07-14T09:00:00+08:00",
        )

        add_shipment("INVALID-CREATED", blank_id, "   ", "not-a-date")
        add_shipment("MISSING-SHIPPED", alpha_id, "审计门店甲", "2026-07-01T00:00:00+08:00", status="已发货")
        add_shipment(
            "INVALID-SHIPPED-MISSING-SIGNED",
            alpha_id,
            "审计门店甲",
            "2026-07-01T01:00:00+08:00",
            status="已签收",
            shipped_at="broken-shipped-at",
        )
        add_shipment(
            "REVERSED-EVENTS",
            alpha_id,
            "审计门店甲",
            "2026-07-01T02:00:00+08:00",
            status="已签收",
            shipped_at="2026-06-30T02:00:00+08:00",
            signed_at="2026-06-29T02:00:00+08:00",
        )
        add_shipment(
            "INVALID-SIGNED",
            alpha_id,
            "审计门店甲",
            "2026-06-30T00:00:00+08:00",
            status="已签收",
            shipped_at="2026-07-01T00:00:00+08:00",
            signed_at="broken-signed-at",
        )

        def add_return(code: str, checked_at: str) -> None:
            tracking_no = f"PRIVATE-RETURN-TRACKING-{code}"
            identifiers.append(tracking_no)
            conn.execute(
                """
                INSERT INTO return_orders (
                    store_id, store_name_snapshot, created_by, express_company, tracking_no,
                    sender_phone, remark, status, tracking_status, tracking_last_checked_at,
                    tracking_error, tracking_raw, created_at, updated_at
                ) VALUES (?, '审计门店甲', ?, '圆通', ?, '13800000000', 'PRIVATE-RETURN-REMARK',
                    '异常', '查询失败', ?, 'PRIVATE-RETURN-ERROR', 'PRIVATE-RETURN-RAW', ?, ?)
                """,
                (alpha_id, admin_id, tracking_no, checked_at, checked_at, checked_at),
            )

        add_return("DAILY", "2026-07-15T16:00:00+08:00")
        add_return("PRIOR", "2026-07-14T16:00:00+08:00")

        return {
            "identifiers": [value for value in identifiers if value],
            "store_count": int(conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]),
            "shipment_count": int(conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]),
        }


def assert_readonly_connection(db: Database, expected_shipments: int) -> None:
    with db.connect_readonly() as conn:
        assert int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
        try:
            conn.execute("INSERT INTO stores (name, active, created_at, updated_at) VALUES ('blocked', 1, '', '')")
            raise AssertionError("query_only reporting connection accepted a write")
        except sqlite3.OperationalError:
            pass

    with db.connect_readonly() as conn:
        conn.execute("PRAGMA query_only = OFF")
        try:
            conn.execute("DELETE FROM shipments")
            raise AssertionError("mode=ro reporting connection accepted a write after query_only was disabled")
        except sqlite3.OperationalError:
            pass

    with db.connect() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]) == expected_shipments


def assert_contract(report: Dict[str, Any]) -> None:
    assert report["date"] == AUDIT_DATE
    assert report["timezone"] == "Asia/Shanghai"
    assert report["metrics"] == {
        "new_shipments": 3,
        "shipped_shipments": 3,
        "signed_shipments": 1,
        "backlog_current_snapshot": 3,
    }
    expected_by_store = {
        "审计门店甲": {
            "new_shipments": 1,
            "shipped_shipments": 2,
            "signed_shipments": 1,
            "backlog_current_snapshot": 1,
        },
        "审计门店乙": {
            "new_shipments": 2,
            "shipped_shipments": 1,
            "signed_shipments": 0,
            "backlog_current_snapshot": 2,
        },
    }
    actual_by_store = {
        row["store_name"]: {key: value for key, value in row.items() if key != "store_name"}
        for row in report["by_store"]
    }
    assert actual_by_store == expected_by_store, report["by_store"]
    assert report["exceptions"]["current_snapshot"] == {
        "label_booking_failures": 2,
        "tracking_failures": 4,
        "shipment_tracking_failures": 2,
        "return_tracking_failures": 2,
        "printing_failures": 2,
    }
    assert report["exceptions"]["current_snapshot_updated_on_date"] == {
        "label_booking_failures": 1,
        "tracking_failures": 2,
        "shipment_tracking_failures": 1,
        "return_tracking_failures": 1,
        "printing_failures": 1,
    }
    assert report["long_waiting"] == {"label_tasks_over_30_minutes_current_snapshot": 1}
    assert report["recent_7_day_average"] == {
        "calendar_days": 7,
        "new_shipments": 1.29,
        "shipped_shipments": 0.86,
        "signed_shipments": 0.29,
    }
    assert report["data_quality"] == {
        "total_issues": 8,
        "invalid_created_at": 1,
        "invalid_shipped_at": 1,
        "invalid_tracking_signed_at": 1,
        "shipped_state_missing_shipped_at": 1,
        "signed_state_missing_tracking_signed_at": 1,
        "shipped_before_created": 1,
        "signed_before_shipped": 1,
        "missing_store_name": 1,
    }
    assert report["basis"]["backlog"].endswith("_not_historical")
    assert "not_historical_event_log" in report["basis"]["exceptions"]


def assert_no_sensitive_data(value: Any, identifiers: list[str]) -> None:
    forbidden_keys = (
        "recipient",
        "phone",
        "address",
        "business_id",
        "store_order_no",
        "tracking_no",
        "database",
        "cookie",
        "session",
        "token",
        "secret",
        "raw",
        "record_id",
        "shipment_id",
        "order_id",
        "task_id",
    )

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                assert not any(fragment in lowered for fragment in forbidden_keys), key
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            assert not any(identifier and identifier in item for identifier in identifiers), item

    walk(value)
    serialized = json.dumps(value, ensure_ascii=False)
    assert AUDIT_TOKEN not in serialized
    for identifier in identifiers:
        assert identifier not in serialized


def main() -> None:
    previous_token = os.environ.get("SCENTPOOL_AUDIT_TOKEN")
    httpd = None
    thread = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "synthetic-audit.db")
            db = Database(db_path)
            db.initialize(str(Path(tmp) / "missing-products.xlsx"))
            fixture = insert_synthetic_rows(db)

            assert_readonly_connection(db, fixture["shipment_count"])
            direct_report = db.daily_audit(AUDIT_DATE)
            assert_contract(direct_report)
            assert_no_sensitive_data(direct_report, fixture["identifiers"] + [db_path])

            empty_db = Database(str(Path(tmp) / "empty.db"))
            empty_db.initialize(str(Path(tmp) / "missing-empty-products.xlsx"))
            empty_report = empty_db.daily_audit(AUDIT_DATE)
            assert empty_report["recent_7_day_average"] == {
                "calendar_days": 7,
                "new_shipments": 0.0,
                "shipped_shipments": 0.0,
                "signed_shipments": 0.0,
            }
            assert empty_report["by_store"] == []

            os.environ["SCENTPOOL_AUDIT_TOKEN"] = AUDIT_TOKEN
            server.DB = db
            server.SESSION_SECURE = False
            server.ALLOW_DB_RESTORE = False
            httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", 0), server.Handler, 4)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            time.sleep(0.05)

            anonymous = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            bearer = {"Authorization": f"Bearer {AUDIT_TOKEN}"}
            response_bodies: list[Any] = []
            captured_logs = io.StringIO()
            with contextlib.redirect_stdout(captured_logs):
                status, missing_body = request(anonymous, base, "GET", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}")
                response_bodies.append(missing_body)
                assert status == 401

                status, wrong_body = request(
                    anonymous,
                    base,
                    "GET",
                    f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}",
                    headers={"Authorization": "Bearer wrong-audit-token"},
                )
                response_bodies.append(wrong_body)
                assert status == 401
                assert wrong_body == missing_body

                del os.environ["SCENTPOOL_AUDIT_TOKEN"]
                status, unconfigured_body = request(
                    anonymous, base, "GET", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}", headers=bearer
                )
                response_bodies.append(unconfigured_body)
                assert status == 401
                assert unconfigured_body == missing_body
                os.environ["SCENTPOOL_AUDIT_TOKEN"] = AUDIT_TOKEN

                status, report = request(
                    anonymous, base, "GET", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}", headers=bearer
                )
                response_bodies.append(report)
                assert status == 200, report
                assert_contract(report)

                status, login_body = request(
                    admin,
                    base,
                    "POST",
                    "/api/login",
                    {"username": "admin", "password": "scentpool2026"},
                )
                response_bodies.append(login_body)
                assert status == 200, login_body
                status, admin_without_bearer = request(
                    admin, base, "GET", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}"
                )
                response_bodies.append(admin_without_bearer)
                assert status == 401

                status, body = request(
                    anonymous, base, "GET", "/api/admin/system/diagnostics", headers=bearer
                )
                response_bodies.append(body)
                assert status == 401

                status, body = request(
                    anonymous,
                    base,
                    "POST",
                    "/api/stores",
                    {"name": "不应创建", "username": "blocked", "password": "blocked-pass"},
                    bearer,
                )
                response_bodies.append(body)
                assert status == 401
                with db.connect() as conn:
                    assert int(conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]) == fixture["store_count"]

                status, body = request(
                    anonymous, base, "POST", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}", {}, bearer
                )
                response_bodies.append(body)
                assert status == 405
                with db.connect() as conn:
                    assert int(conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]) == fixture["shipment_count"]

                invalid_paths = [
                    f"{server.DAILY_AUDIT_PATH}?date=2026-02-30",
                    f"{server.DAILY_AUDIT_PATH}?date=2026-07-15%27%20OR%201%3D1--",
                    f"{server.DAILY_AUDIT_PATH}?date=",
                    f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}&date=2026-07-16",
                    f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}&extra=1",
                    f"{server.DAILY_AUDIT_PATH}?date={urllib.parse.quote('２０２６-０７-１５')}",
                    f"{server.DAILY_AUDIT_PATH}?date={'9' * 200}",
                ]
                for path in invalid_paths:
                    status, body = request(anonymous, base, "GET", path, headers=bearer)
                    response_bodies.append(body)
                    assert status == 400, (path, status, body)

                unavailable_path = str(Path(tmp) / "PRIVATE-DATABASE-PATH.db")
                server.DB = Database(unavailable_path)
                status, body = request(
                    anonymous, base, "GET", f"{server.DAILY_AUDIT_PATH}?date={AUDIT_DATE}", headers=bearer
                )
                response_bodies.append(body)
                assert status == 503, body
                assert unavailable_path not in json.dumps(body, ensure_ascii=False)
                server.DB = db

            logs = captured_logs.getvalue()
            assert AUDIT_TOKEN not in logs
            for body in response_bodies:
                assert AUDIT_TOKEN not in json.dumps(body, ensure_ascii=False)
            assert_no_sensitive_data(report, fixture["identifiers"] + [db_path])

            httpd.shutdown()
            thread.join(timeout=2)
            httpd = None
            thread = None
    finally:
        if httpd is not None:
            httpd.shutdown()
        if thread is not None:
            thread.join(timeout=2)
        if previous_token is None:
            os.environ.pop("SCENTPOOL_AUDIT_TOKEN", None)
        else:
            os.environ["SCENTPOOL_AUDIT_TOKEN"] = previous_token

    print("daily audit test passed")


if __name__ == "__main__":
    main()
