from __future__ import annotations

import contextlib
import io
import json
import urllib.parse
from typing import Any, Dict, Optional

from tools import scentpool_daily_audit_probe as probe


PRIVATE_MARKERS = (
    "PRIVATE-RECIPIENT",
    "13900000000",
    "PRIVATE-ADDRESS",
    "PRIVATE-BUSINESS-ID",
    "PRIVATE-TRACKING-NO",
    "PRIVATE-RAW-PAYLOAD",
    "synthetic-audit-secret",
    "synthetic-render-secret",
)


def ok(data: Any) -> Dict[str, Any]:
    return {
        "status": "ok",
        "message": "synthetic ok",
        "http_status": 200,
        "elapsed_ms": 1,
        "data": data,
    }


class FakeClient:
    def __init__(self, *, service_name: str = probe.RENDER_SERVICE_NAME, permission_error: bool = False):
        self.service_name = service_name
        self.permission_error = permission_error
        self.log_page = 0
        self.urls: list[str] = []

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        probe.validate_get_url(url)
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/health":
            return ok({"ok": True, "database": True, "ignored": "PRIVATE-RAW-PAYLOAD"})
        if path == "/api/admin/system/daily-audit":
            return ok(
                {
                    "date": "2026-08-14",
                    "timezone": "Asia/Shanghai",
                    "metrics": {"new_shipments": 3},
                    "data_quality": {"missing_store_name": 0},
                    "historical_end_of_day": {"new_shipments_unshipped_at_day_end": {"count": 1}},
                    "failures": {},
                    "completeness": {"requested_day": "partial"},
                }
            )
        if path == f"/v1/services/{probe.RENDER_SERVICE_ID}":
            if self.permission_error:
                return {"status": "permission_denied", "message": "synthetic denied", "http_status": 403}
            return ok(
                {
                    "id": probe.RENDER_SERVICE_ID,
                    "name": self.service_name,
                    "ownerId": "tea-synthetic-owner",
                    "branch": "main",
                    "serviceDetails": {
                        "url": probe.BASE_URL,
                        "region": "singapore",
                        "disk": {"sizeGB": 1},
                    },
                }
            )
        if path.endswith("/deploys"):
            return ok(
                [
                    {
                        "cursor": "deploy-cursor",
                        "deploy": {"status": "live", "createdAt": "2026-08-14T01:00:00Z"},
                    }
                ]
            )
        if path.endswith("/events"):
            return ok([])
        if path == "/v1/logs":
            self.log_page += 1
            if self.log_page == 1:
                return ok(
                    {
                        "hasMore": True,
                        "nextStartTime": "2026-08-14T06:00:00Z",
                        "nextEndTime": "2026-08-14T16:00:00Z",
                        "logs": [
                            {
                                "id": "log-private-id",
                                "timestamp": "2026-08-14T01:00:00Z",
                                "message": "Traceback (most recent call last): PRIVATE-RECIPIENT 13900000000 PRIVATE-ADDRESS",
                                "labels": [
                                    {"name": "resource", "value": probe.RENDER_SERVICE_ID},
                                    {"name": "type", "value": "app"},
                                ],
                            },
                            {
                                "id": "request-private-id",
                                "timestamp": "2026-08-14T02:00:00Z",
                                "message": "PRIVATE-BUSINESS-ID PRIVATE-TRACKING-NO",
                                "labels": [
                                    {"name": "resource", "value": probe.RENDER_SERVICE_ID},
                                    {"name": "type", "value": "request"},
                                    {"name": "statusCode", "value": "503"},
                                ],
                            },
                        ],
                    }
                )
            return ok(
                {
                    "hasMore": False,
                    "nextStartTime": "2026-08-14T06:00:00Z",
                    "nextEndTime": "2026-08-14T16:00:00Z",
                    "logs": [
                        {
                            "id": "slow-private-id",
                            "timestamp": "2026-08-14T08:00:00Z",
                            "message": "[slow-request] PRIVATE-RAW-PAYLOAD database is locked timeout",
                            "labels": [
                                {"name": "resource", "value": probe.RENDER_SERVICE_ID},
                                {"name": "type", "value": "app"},
                            ],
                        }
                    ],
                }
            )
        if path.startswith("/v1/metrics/"):
            labels = [{"field": "service", "value": probe.RENDER_SERVICE_ID}]
            if path.endswith("http-requests"):
                labels.append({"field": "statusCode", "value": "200"})
            if path.endswith("http-latency"):
                labels.append({"field": "quantile", "value": "0.99"})
            series = [
                {
                    "labels": labels,
                    "values": [
                        {"timestamp": "2026-08-14T01:00:00Z", "value": 1},
                        {"timestamp": "2026-08-14T01:05:00Z", "value": 2},
                    ],
                    "unit": "synthetic-unit",
                }
            ]
            if path.endswith("http-requests"):
                series.append(
                    {
                        "labels": [
                            {"field": "service", "value": probe.RENDER_SERVICE_ID},
                            {"field": "statusCode", "value": "503"},
                        ],
                        "values": [
                            {"timestamp": "2026-08-14T01:00:00Z", "value": 1},
                            {"timestamp": "2026-08-14T01:05:00Z", "value": 2},
                        ],
                        "unit": "synthetic-unit",
                    }
                )
            return ok(series)
        raise AssertionError(url)


class ServiceFailureClient(FakeClient):
    def __init__(self, status: str):
        super().__init__()
        self.status = status

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.path == f"/v1/services/{probe.RENDER_SERVICE_ID}":
            return {"status": self.status, "message": "synthetic safe failure"}
        return super().fetch(url, headers)


class MissingCursorClient:
    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return ok([{"deploy": {"status": "live"}} for _index in range(100)])


def assert_private_markers_absent(report: Dict[str, Any]) -> None:
    serialized = json.dumps(report, ensure_ascii=False)
    for marker in PRIVATE_MARKERS:
        assert marker not in serialized, marker
    forbidden_keys = ("message_raw", "raw_payload", "authorization")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert str(key).lower() not in forbidden_keys
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(report)


def main() -> None:
    assert probe.requested_date(["2026-02-28"]) == "2026-02-28"
    for invalid in ("2026-02-30", "2026-8-1", "2026-08-14&extra=1"):
        try:
            probe.requested_date([invalid])
            raise AssertionError(invalid)
        except ValueError:
            pass

    try:
        probe.render_url("/services/unsafe")
        raise AssertionError("unsafe Render path was accepted")
    except ValueError:
        pass
    try:
        probe.render_url(f"/services/{probe.RENDER_SERVICE_ID}", {"includeSecret": "1"})
        raise AssertionError("unsafe Render query was accepted")
    except ValueError:
        pass

    client = FakeClient()
    report = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=client,
    )
    assert report["overall_status"] == "partial", report
    assert report["render_service"]["status"] == "ok"
    assert report["render_events"]["status"] == "no_data"
    assert report["render_logs"]["pagination_complete"] is True
    assert report["render_logs"]["total_logs"] == 3
    assert report["render_logs"]["http_5xx_request_logs"] == 1
    assert report["render_logs"]["categories"] == {
        "oom": 0,
        "exception_stack": 1,
        "database_locked": 1,
        "timeout": 1,
        "slow_request": 1,
    }
    assert report["render_metrics"]["http_requests"]["total"] == 6.0
    assert report["render_metrics"]["http_requests"]["http_5xx"] == 3.0
    assert all(url.startswith((probe.BASE_URL, probe.RENDER_API_URL)) for url in client.urls)
    assert_private_markers_absent(report)

    mismatch = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=FakeClient(service_name="wrong-service"),
    )
    assert mismatch["render_service"]["status"] == "target_mismatch"
    assert mismatch["render_logs"]["status"] == "process_error"

    denied = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=FakeClient(permission_error=True),
    )
    assert denied["render_service"]["status"] == "permission_denied"
    assert denied["overall_status"] == "error"

    for failure_status in ("http_error", "network_restricted", "process_error"):
        failed = probe.collect_report(
            "2026-08-14",
            audit_token="synthetic-audit-secret",
            render_token="synthetic-render-secret",
            client=ServiceFailureClient(failure_status),
        )
        assert failed["render_service"]["status"] == failure_status
        assert failed["overall_status"] == "error"

    cursor_status, _rows, cursor_complete = probe.collect_cursor_pages(
        MissingCursorClient(),
        f"/services/{probe.RENDER_SERVICE_ID}/deploys",
        {"limit": 100},
        wrapper_key="deploy",
        headers={},
    )
    assert cursor_status["status"] == "schema_changed"
    assert cursor_complete is False

    changed = probe.metric_summary(ok({"unexpected": "shape"}), mode="resource")
    assert changed["status"] == "schema_changed"
    empty = probe.metric_summary(ok([]), mode="resource")
    assert empty["status"] == "no_data"
    unsafe_daily = probe.app_summary(
        ok(
            {
                "date": "2026-08-14",
                "timezone": "Asia/Shanghai",
                "metrics": {"new_shipments": 1},
                "failures": {"label": {"booking_raw": "PRIVATE-RAW-PAYLOAD"}},
            }
        ),
        daily=True,
    )
    assert unsafe_daily["status"] == "schema_changed"
    assert_private_markers_absent(unsafe_daily)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = probe.main(["invalid-date"])
    assert exit_code == 2
    emitted = json.loads(output.getvalue())
    assert emitted["collector"]["status"] == "process_error"
    assert output.getvalue().strip(), "collector failed silently"
    assert_private_markers_absent(emitted)

    print("daily audit probe test passed")


if __name__ == "__main__":
    main()
