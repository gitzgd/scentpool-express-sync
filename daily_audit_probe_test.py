from __future__ import annotations

import contextlib
import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest import mock

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
        self.connection_sample = 0
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
        if path == probe.AUDIT_DIAGNOSTICS_PATH:
            self.connection_sample += 1
            return ok(
                {
                    "sampled_at": f"2026-08-14T08:00:{self.connection_sample:02d}+08:00",
                    "storage": {
                        "connections": {
                            "opened_total": 100 + self.connection_sample,
                            "closed_total": 100 + self.connection_sample,
                            "active": 0,
                            "peak_active": 4,
                        }
                    },
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
                        },
                        {
                            "id": "print-safe-id",
                            "timestamp": "2026-08-14T08:05:00Z",
                            "message": "[audit-print] kind=batch_print outcome=failure duration_ms=1500 slow=1",
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
                    "unit": "seconds" if path.endswith("http-latency") else "bytes",
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
                        "unit": "count",
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


class StuckLogCursorClient:
    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        return ok(
            {
                "hasMore": True,
                "nextStartTime": query["startTime"][0],
                "nextEndTime": query["endTime"][0],
                "logs": [],
            }
        )


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def sampling_kwargs() -> Dict[str, Any]:
    clock = FakeClock()
    return {"sleeper": clock.sleep, "monotonic": clock.monotonic}


class SequenceClient:
    def __init__(self, results: list[Dict[str, Any]]):
        self.results = list(results)

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        probe.validate_get_url(url)
        return self.results.pop(0)


def connection_payload(opened: int, closed: int, active: int, peak: int) -> Dict[str, Any]:
    return ok(
        {
            "sampled_at": "2026-08-14T08:00:00+08:00",
            "storage": {
                "connections": {
                    "opened_total": opened,
                    "closed_total": closed,
                    "active": active,
                    "peak_active": peak,
                }
            },
        }
    )


class LatencyClient:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.quantile_queries: list[list[str]] = []

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        probe.validate_get_url(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        quantiles = query.get("quantile", [])
        self.quantile_queries.append(quantiles)
        if self.scenario == "plan_restricted":
            return {
                "status": "permission_denied", "http_status": 400,
                "availability": "unavailable", "reason": "plan_restricted",
                "message": "synthetic plan restriction",
            }
        if self.scenario == "multi_wrapper_success":
            return ok(
                {
                    "data": [
                        {
                            "labels": {"quantile": "0.99"},
                            "values": [{"timestamp": "2026-08-14T00:00:00Z", "value": 0.4}],
                            "unit": "seconds",
                        }
                    ]
                }
            )
        if len(quantiles) > 1:
            return {"status": "http_error", "message": "synthetic 400", "http_status": 400}
        if self.scenario == "all_400":
            return {"status": "http_error", "message": "synthetic 400", "http_status": 400}
        if self.scenario == "no_data":
            return ok([])
        if self.scenario == "schema_changed":
            return ok({"unexpected": []})
        if self.scenario == "partial_plan" and quantiles != ["0.5"]:
            return {
                "status": "permission_denied", "http_status": 400,
                "availability": "unavailable", "reason": "plan_restricted",
            }
        if self.scenario == "partial_success" and quantiles != ["0.5"]:
            return {"status": "http_error", "message": "synthetic failure", "http_status": 500}
        return ok(
            [
                {
                    "labels": [{"field": "quantile", "value": quantiles[0]}],
                    "values": [{"timestamp": "2026-08-14T00:00:00Z", "value": 0.3}],
                    "unit": "seconds",
                }
            ]
        )


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


def test_timestamp_compatibility() -> None:
    for length in range(1, 10):
        fraction = "123456789"[:length]
        parsed = probe.parsed_timestamp(f"2026-08-14T23:59:59.{fraction}+08:00")
        assert parsed == datetime(
            2026, 8, 14, 15, 59, 59, int(fraction[:6].ljust(6, "0")), tzinfo=timezone.utc
        )
    before = probe.parsed_timestamp("2026-08-14T23:59:59.999999999+08:00")
    after = probe.parsed_timestamp("2026-08-15T00:00:00.000000001+08:00")
    assert before < probe.parsed_timestamp(probe.day_window("2026-08-14")[1]) <= after
    assert before.astimezone(probe.APP_TZ).date().isoformat() == "2026-08-14"
    assert after.astimezone(probe.APP_TZ).date().isoformat() == "2026-08-15"
    assert probe.safe_timestamp("2026-08-14T00:00:00.12345678-03:30") == "2026-08-14T03:30:00.123456Z"
    assert probe.safe_timestamp("2026-08-14T00:00:00Z") == "2026-08-14T00:00:00Z"
    for invalid in (
        None, True, 0, [], {}, "2026-08-14", "2026-08-14T12:00:00",
        "2026-08-14 12:00:00Z", "2026-08-14T12:00:00.1234567890Z",
        "2026-08-14T12:00:00+24:00", "2026-08-14T12:00:00+01:60",
        "2026-08-14T12:00:00+00:00:01", "2026-08-14T12:00:00Z\n",
        "2026-08-14T12:00:00ZPRIVATE-RECIPIENT", "PRIVATE-RECIPIENT" * 10000,
        "2026-02-30T12:00:00Z", "2026-08-14T24:00:00Z", "2026-08-14T23:59:60Z",
        "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
    ):
        assert probe.parsed_timestamp(invalid) is None
    event, missing = probe.print_event_from_log(
        "[audit-print] kind=batch_print outcome=success duration_ms=1500 slow=1",
        {}, "2026-08-14T15:59:59.999999999Z",
    )
    assert missing is False and event["timestamp"] == "2026-08-14T15:59:59.999999Z"
    event, missing = probe.print_event_from_log(
        "[audit-print] kind=batch_print outcome=success duration_ms=" + "1" * 10000 + " slow=0",
        {}, "2026-08-14T00:00:00Z",
    )
    assert event is None and missing is False


def test_http_error_privacy() -> None:
    class BoundedReader(io.BytesIO):
        def read(self, count: int = -1) -> bytes:
            assert 0 < count <= probe.MAX_ERROR_RESPONSE_BYTES + 1
            return super().read(count)

    url = probe.render_url("/metrics/http-latency", {"quantile": 0.5})
    known = {"message": "Response latency metrics require Pro or higher plans."}
    for code, payload, expected in (
        (400, known, "plan_restricted"), (403, known, "plan_restricted"),
        (400, {"message": "query is not allowed for plan: Hobby"}, "plan_restricted"),
        (400, {"error": "query is not allowed for plan: Hobby"}, "plan_restricted"),
        (400, {"detail": "query is not allowed for plan: Hobby"}, "plan_restricted"),
        (400, {"message": "query is not allowed for plan: Hobby PRIVATE-RECIPIENT"}, None),
        (500, known, None),
        (400, {"message": "latency metrics are not available for the Hobby plan"}, "plan_restricted"),
        (400, {"message": "quantile is required"}, None),
        (400, {"message": "PRIVATE-RECIPIENT hobby plan invalid"}, None),
        (400, {"message": known["message"] + " PRIVATE-RAW-PAYLOAD"}, None),
        (400, {"message": known["message"], "url": "PRIVATE-RAW-PAYLOAD"}, None),
        (400, {"message": "hobby plan " + "PRIVATE-RAW-PAYLOAD" * 10000}, None),
        (400, [known], None),
    ):
        error = urllib.error.HTTPError(url, code, "PRIVATE-RAW-PAYLOAD", {}, BoundedReader(json.dumps(payload).encode()))
        with (
            mock.patch.object(probe.urllib.request, "urlopen", side_effect=error),
            mock.patch.object(probe, "https_context", return_value=object()),
        ):
            result = probe.JsonClient(attempts=1).fetch(url)
        assert result["http_status"] == code
        assert result.get("reason") == expected, result
        assert_private_markers_absent(result)
        assert error.closed
    assert not probe.latency_plan_restriction(probe.render_url("/metrics/memory"), 400, json.dumps(known).encode())
    for payload in (b"not json hobby plan", b"\xff", b'{"message":'):
        assert not probe.latency_plan_restriction(url, 400, payload)
    # Success responses are bounded too, and an oversized body is never parsed
    # or reflected into an error payload.
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b"PRIVATE-RAW-PAYLOAD" * 10
    with (
        mock.patch.object(probe, "MAX_JSON_RESPONSE_BYTES", 64),
        mock.patch.object(probe.urllib.request, "urlopen", return_value=response),
        mock.patch.object(probe, "https_context", return_value=object()),
    ):
        result = probe.JsonClient(attempts=1).fetch(url)
    response.read.assert_called_once_with(65)
    assert result["status"] == "schema_changed" and result["error_type"] == "response_too_large"
    assert_private_markers_absent(result)


def test_assessment_separation() -> None:
    available = probe.app_summary(ok({"ok": True, "database": True}))
    unavailable_metric = {
        "status": "permission_denied", "http_status": 400,
        "reason": "plan_restricted", "availability": "unavailable",
    }
    result = probe.report_assessments({"health": available, "http_latency": unavailable_metric})
    assert result["website_health"]["availability"] == "available"
    assert result["website_health"]["assessment"] == "no_issues_observed_in_available_evidence"
    assert result["evidence_completeness"]["status"] == "partial"
    assert result["evidence_completeness"]["gaps"][0]["reason"] == "plan_restricted"
    incidents = probe.report_assessments({
        "health": available, "http_latency": unavailable_metric,
        "http_requests": {"status": "ok", "http_5xx": 3},
    })
    assert incidents["website_health"]["assessment"] == "issues_observed"
    assert incidents["website_health"]["observations"] == ["http_5xx_observed"]
    assert incidents["evidence_completeness"]["status"] == "partial"
    partial_logs = probe.report_assessments({
        "health": available,
        "render_logs": {"status": "schema_changed", "pagination_complete": False, "categories": {"oom": 1}},
    })
    assert partial_logs["website_health"]["assessment"] == "issues_observed"
    assert "log_oom_observed" in partial_logs["website_health"]["observations"]
    assert partial_logs["evidence_completeness"]["status"] == "partial"
    legacy_peak = probe.report_assessments({
        "health": available,
        "connection_diagnostics": {"status": "ok", "peak_active_abnormal": True,
                                   "connection_limit_source": "legacy_fixed_baseline"},
    })
    assert "connection_peak_above_reviewed_bound" not in legacy_peak["website_health"]["observations"]
    assert legacy_peak["website_health"]["limitations"] == ["legacy_connection_capacity_requires_verification"]
    assert legacy_peak["evidence_completeness"]["status"] == "partial"
    failed_health = probe.report_assessments({
        "health": probe.app_summary(ok({"ok": False, "database": False})),
        "http_latency": {"status": "ok", "series": []},
    })
    assert failed_health["website_health"]["availability"] == "unavailable"
    assert failed_health["website_health"]["assessment"] == "issues_observed"
    unknown = probe.report_assessments({"health": {"status": "network_restricted"}})
    assert unknown["website_health"]["availability"] == "unknown"
    assert unknown["evidence_completeness"]["status"] == "unavailable"
    for false_like in ("false", 0, None, []):
        assert probe.app_summary(ok({"ok": false_like, "database": True}))["status"] == "schema_changed"


def test_resource_time_alignment() -> None:
    def series(points: list[tuple[str, Any]], unit: str = "bytes") -> Dict[str, Any]:
        return {
            "labels": {"instance": "PRIVATE-RECIPIENT"}, "unit": unit,
            "values": [{"timestamp": stamp, "value": value} for stamp, value in points],
        }
    old_instance = series([
        ("2026-08-14T00:00:00Z", 90), ("2026-08-14T00:05:00Z", 400),
    ])
    new_instance = series([
        ("2026-08-14T00:15:00Z", 50), ("2026-08-14T00:10:00Z", 40),
    ])
    result = probe.metric_summary(ok([old_instance, new_instance]), mode="resource")
    assert result["latest"] == 50, "old instance last value was carried forward"
    assert result["maximum"] == 400
    assert result["latest_timestamp"] == "2026-08-14T00:15:00Z"
    assert result["latest_series_count"] == 1 and result["stale_series_excluded"] == 1
    assert_private_markers_absent(result)
    simultaneous = probe.metric_summary(ok([
        series([("2026-08-14T00:00:00.123456789Z", 50), ("2026-08-14T00:05:00Z", 40)]),
        series([("2026-08-14T08:00:00.123456789+08:00", 60), ("2026-08-14T08:05:00+08:00", 50)]),
    ]), mode="resource")
    assert simultaneous["maximum"] == 110 and simultaneous["latest"] == 90
    assert simultaneous["latest_series_count"] == 2
    mixed_units = probe.metric_summary(ok([
        series([("2026-08-14T00:00:00Z", 1)], "MiB"),
        series([("2026-08-14T00:00:00Z", 1024)], "KiB"),
    ]), mode="resource")
    assert mixed_units["latest"] == 2 * 1024 * 1024 and mixed_units["units"] == ["bytes"]
    for invalid in (True, False, float("nan"), float("inf"), float("-inf"), -1, 10**1000):
        invalid_metric = probe.metric_summary(ok([series([("2026-08-14T00:00:00Z", invalid)])]), mode="resource")
        assert invalid_metric["status"] == "schema_changed"
    for unit in ("", "PRIVATE-RAW-PAYLOAD", "percentage"):
        unknown = probe.metric_summary(ok([series([("2026-08-14T00:00:00Z", 1)], unit)]), mode="resource")
        assert unknown["status"] == "schema_changed"
        assert_private_markers_absent(unknown)
    duplicate_time = probe.metric_summary(ok([series([
        ("2026-08-14T00:00:00Z", 1), ("2026-08-14T08:00:00+08:00", 2),
    ])]), mode="resource")
    assert duplicate_time["status"] == "schema_changed"
    overflow = probe.metric_summary(ok([
        series([("2026-08-14T00:00:00Z", 1e308)]),
        series([("2026-08-14T00:00:00Z", 1e308)]),
    ]), mode="resource")
    assert overflow["status"] == "schema_changed"
    latency_bad_label = series([("2026-08-14T00:00:00Z", 1)], "seconds")
    latency_bad_label["labels"]["quantile"] = "PRIVATE-RAW-PAYLOAD"
    result = probe.metric_summary(ok([latency_bad_label]), mode="series")
    assert result["status"] == "schema_changed"
    assert_private_markers_absent(result)


def main() -> None:
    test_timestamp_compatibility()
    test_http_error_privacy()
    test_assessment_separation()
    test_resource_time_alignment()
    fake_context = object()
    with (
        mock.patch.object(probe.sys, "platform", "darwin"),
        mock.patch.object(probe.os.path, "isfile", return_value=True),
        mock.patch.object(probe.ssl, "create_default_context", return_value=fake_context) as create_context,
    ):
        assert probe.https_context() is fake_context
        create_context.assert_called_once_with(cafile=probe.MACOS_SYSTEM_CA_FILE)

    with (
        mock.patch.object(probe.sys, "platform", "linux"),
        mock.patch.object(probe.ssl, "create_default_context", return_value=fake_context) as create_context,
    ):
        assert probe.https_context() is fake_context
        create_context.assert_called_once_with()

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
    probe.validate_get_url(f"{probe.BASE_URL}{probe.AUDIT_DIAGNOSTICS_PATH}")
    try:
        probe.validate_get_url(f"{probe.BASE_URL}{probe.AUDIT_DIAGNOSTICS_PATH}?extra=1")
        raise AssertionError("audit diagnostics query was accepted")
    except ValueError:
        pass

    client = FakeClient()
    report = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=client,
        **sampling_kwargs(),
    )
    assert report["overall_status"] == "partial", report
    assert report["website_health"]["availability"] == "available"
    assert report["website_health"]["assessment"] == "issues_observed"
    assert report["evidence_completeness"]["status"] == "complete"
    assert report["render_service"]["status"] == "ok"
    assert report["render_events"]["status"] == "no_data"
    assert report["render_logs"]["pagination_complete"] is True
    assert report["render_logs"]["total_logs"] == 4
    assert report["render_logs"]["http_5xx_request_logs"] == 1
    assert report["render_logs"]["categories"] == {
        "oom": 0,
        "exception_stack": 1,
        "database_locked": 1,
        "timeout": 1,
        "slow_request": 1,
    }
    assert report["render_logs"]["print_activity"]["counts"]["requests"] == 1
    assert report["render_logs"]["print_activity"]["counts"]["failure"] == 1
    assert report["render_logs"]["print_activity"]["counts"]["slow"] == 1
    assert report["render_metrics"]["http_requests"]["total"] == 6.0
    assert report["render_metrics"]["http_requests"]["http_5xx"] == 3.0
    assert report["connection_diagnostics"]["status"] == "ok"
    assert report["connection_diagnostics"]["interval_requirement_met"] is True
    assert report["connection_diagnostics"]["all_samples_conserved"] is True
    assert report["connection_diagnostics"]["active_recovered"] is True
    assert "0.9" in next(
        urllib.parse.urlparse(url).query
        for url in client.urls
        if url.startswith(f"{probe.RENDER_API_URL}/metrics/http-latency")
    )
    assert all(url.startswith((probe.BASE_URL, probe.RENDER_API_URL)) for url in client.urls)
    assert_private_markers_absent(report)

    mismatch = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=FakeClient(service_name="wrong-service"),
        **sampling_kwargs(),
    )
    assert mismatch["render_service"]["status"] == "target_mismatch"
    assert mismatch["render_logs"]["status"] == "process_error"

    denied = probe.collect_report(
        "2026-08-14",
        audit_token="synthetic-audit-secret",
        render_token="synthetic-render-secret",
        client=FakeClient(permission_error=True),
        **sampling_kwargs(),
    )
    assert denied["render_service"]["status"] == "permission_denied"
    assert denied["overall_status"] == "error"

    for failure_status in ("http_error", "network_restricted", "process_error"):
        failed = probe.collect_report(
            "2026-08-14",
            audit_token="synthetic-audit-secret",
            render_token="synthetic-render-secret",
            client=ServiceFailureClient(failure_status),
            **sampling_kwargs(),
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
    stuck_logs = probe.collect_logs(
        StuckLogCursorClient(),
        owner_id="tea-synthetic-owner",
        start_time="2026-08-13T16:00:00Z",
        end_time="2026-08-14T16:00:00Z",
        headers={},
    )
    assert stuck_logs["status"] == "schema_changed"

    changed = probe.metric_summary(ok({"unexpected": "shape"}), mode="resource")
    assert changed["status"] == "schema_changed"
    empty = probe.metric_summary(ok([]), mode="resource")
    assert empty["status"] == "no_data"
    wrapped_series = [
        {
            "labels": {"service": probe.RENDER_SERVICE_ID},
            "values": [{"timestamp": "2026-08-14T00:00:00Z", "value": 1}],
            "unit": "bytes",
        }
    ]
    assert probe.metric_summary(ok({"series": wrapped_series}), mode="resource")["status"] == "ok"
    assert probe.metric_summary(ok({"data": {"series": wrapped_series}}), mode="resource")["status"] == "ok"

    balanced = probe.collect_connection_samples(
        SequenceClient([connection_payload(10, 8, 2, 5), connection_payload(14, 13, 1, 5)]),
        headers={},
        **sampling_kwargs(),
    )
    assert balanced["status"] == "ok"
    assert balanced["all_samples_conserved"] is True
    assert balanced["active_recovered"] is True
    assert balanced["expected_peak_active_upper_bound"] == 9
    assert balanced["connection_limit_source"] == "legacy_fixed_baseline"
    capacities = {"request_workers": 8, "background_connections": 2, "peak_active_upper_bound": 10}
    def capacity_sample(opened: int, closed: int, active: int, peak: int) -> Dict[str, Any]:
        sample = connection_payload(opened, closed, active, peak)
        sample["data"]["storage"]["connection_limits"] = dict(capacities)
        return sample
    bounded_connections = probe.collect_connection_samples(
        SequenceClient([capacity_sample(20, 18, 2, 10), capacity_sample(22, 20, 2, 10)]),
        headers={}, **sampling_kwargs(),
    )
    assert bounded_connections["peak_active_abnormal"] is False
    assert bounded_connections["expected_peak_active_upper_bound"] == 10
    assert bounded_connections["active_recovered"] is True
    transient_sample = capacity_sample(30, 28, 2, 12)
    transient_sample["data"]["storage"]["connection_limits"].update(
        transient_background_connections=2, peak_active_upper_bound=12,
    )
    transient_connections = probe.collect_connection_samples(
        SequenceClient([transient_sample, transient_sample]), headers={}, **sampling_kwargs(),
    )
    assert transient_connections["peak_active_abnormal"] is False
    assert transient_connections["expected_peak_active_upper_bound"] == 12
    # A reviewed allowance of 12 must not silently accept 13, or permit
    # arbitrary caller-supplied allowance inflation.
    transient_sample["data"]["storage"]["connections"]["peak_active"] = 13
    exceeded_connections = probe.collect_connection_samples(
        SequenceClient([transient_sample, transient_sample]), headers={}, **sampling_kwargs(),
    )
    assert exceeded_connections["peak_active_abnormal"] is True
    transient_sample["data"]["storage"]["connection_limits"].update(
        transient_background_connections=3, peak_active_upper_bound=13,
    )
    assert probe.connection_sample(transient_sample)[0] is None
    still_leaking = probe.collect_connection_samples(
        SequenceClient([capacity_sample(20, 18, 2, 10), capacity_sample(25, 22, 3, 11)]),
        headers={}, **sampling_kwargs(),
    )
    assert still_leaking["peak_active_abnormal"] is True
    assert still_leaking["active_recovered"] is False
    changing_baseline = probe.collect_connection_samples(
        SequenceClient([connection_payload(20, 19, 1, 9), capacity_sample(22, 20, 2, 10)]),
        headers={}, **sampling_kwargs(),
    )
    assert changing_baseline["completeness"] == "partial"
    assert changing_baseline["active_recovered"] is None
    assert changing_baseline["expected_peak_active_upper_bound"] is None
    for key, value in (
        ("request_workers", 17), ("request_workers", True), ("background_connections", 3),
        ("peak_active_upper_bound", 1000), ("unexpected", "PRIVATE-RECIPIENT"),
    ):
        invalid_limit = capacity_sample(20, 18, 2, 10)
        invalid_limit["data"]["storage"]["connection_limits"][key] = value
        sample, result = probe.connection_sample(invalid_limit)
        assert sample is None and result["status"] == "schema_changed"
        assert_private_markers_absent(result)
    for invalid_sample in (
        connection_payload(True, 0, 1, 1), connection_payload(2, 0, 2, 1),
        connection_payload(2**64, 0, 2**64, 2**64),
    ):
        assert probe.connection_sample(invalid_sample)[0] is None
    leaking = probe.collect_connection_samples(
        SequenceClient([connection_payload(10, 9, 1, 8), connection_payload(15, 12, 3, 12)]),
        headers={},
        **sampling_kwargs(),
    )
    assert leaking["active_recovered"] is False
    assert leaking["peak_active_abnormal"] is True
    unbalanced = probe.collect_connection_samples(
        SequenceClient([connection_payload(10, 7, 1, 4), connection_payload(11, 10, 1, 4)]),
        headers={},
        **sampling_kwargs(),
    )
    assert unbalanced["all_samples_conserved"] is False
    counter_reset = probe.collect_connection_samples(
        SequenceClient([connection_payload(100, 99, 1, 8), connection_payload(2, 1, 1, 2)]),
        headers={},
        **sampling_kwargs(),
    )
    assert counter_reset["counter_reset_between_samples"] is True
    assert counter_reset["active_recovered"] is None
    assert counter_reset["completeness"] == "partial"
    short_interval = probe.collect_connection_samples(
        SequenceClient([connection_payload(10, 9, 1, 4), connection_payload(11, 10, 1, 4)]),
        headers={},
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert short_interval["status"] == "schema_changed"
    assert short_interval["interval_requirement_met"] is False
    partial_connections = probe.collect_connection_samples(
        SequenceClient(
            [
                connection_payload(10, 9, 1, 4),
                {"status": "network_restricted", "message": "synthetic timeout"},
            ]
        ),
        headers={},
        **sampling_kwargs(),
    )
    assert partial_connections["status"] == "network_restricted"
    assert partial_connections["completeness"] == "partial"
    unavailable_connections = probe.collect_connection_samples(
        SequenceClient(
            [
                {"status": "permission_denied", "message": "synthetic denied"},
                {"status": "permission_denied", "message": "synthetic denied"},
            ]
        ),
        headers={},
        **sampling_kwargs(),
    )
    assert unavailable_connections["completeness"] == "unavailable"

    metric_params = {
        "startTime": "2026-08-13T16:00:00Z",
        "endTime": "2026-08-14T16:00:00Z",
        "resolutionSeconds": 300,
        "resource": probe.RENDER_SERVICE_ID,
    }
    wrapper_latency = probe.collect_http_latency(
        LatencyClient("multi_wrapper_success"), metric_params=metric_params, headers={}
    )
    assert wrapper_latency["status"] == "ok"
    assert wrapper_latency["query_mode"] == "multi_quantile"
    fallback_latency = probe.collect_http_latency(
        LatencyClient("fallback_success"), metric_params=metric_params, headers={}
    )
    assert fallback_latency["status"] == "ok"
    assert fallback_latency["query_mode"] == "single_quantile_fallback"
    assert len(fallback_latency["series"]) == 3
    partial_latency = probe.collect_http_latency(
        LatencyClient("partial_success"), metric_params=metric_params, headers={}
    )
    assert partial_latency["status"] == "ok"
    assert partial_latency["coverage"] == "partial"
    assert len(partial_latency["failed_quantiles"]) == 2
    unavailable_latency = probe.collect_http_latency(
        LatencyClient("all_400"), metric_params=metric_params, headers={}
    )
    assert unavailable_latency["status"] == "http_error"
    assert unavailable_latency["http_status"] == 400
    plan_client = LatencyClient("plan_restricted")
    plan_latency = probe.collect_http_latency(plan_client, metric_params=metric_params, headers={})
    assert plan_latency["status"] == "permission_denied" and plan_latency["http_status"] == 400
    assert plan_latency["availability"] == "unavailable" and plan_latency["reason"] == "plan_restricted"
    assert len(plan_client.quantile_queries) == 1, "known plan restriction retried as invalid quantile"
    assert "series" not in plan_latency and "latest" not in plan_latency
    partial_plan = probe.collect_http_latency(LatencyClient("partial_plan"), metric_params=metric_params, headers={})
    assert partial_plan["status"] == "ok" and partial_plan["coverage"] == "partial"
    assert partial_plan["series"][0]["latest"] == 0.3
    assert len(partial_plan["failed_quantiles"]) == 2
    assert partial_plan["failed_quantiles"][0]["reason"] == "plan_restricted"
    class PlanReportClient(FakeClient):
        def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
            if urllib.parse.urlparse(url).path == "/v1/metrics/http-latency":
                return plan_latency
            return super().fetch(url, headers)
    plan_report = probe.collect_report(
        "2026-08-14", audit_token="synthetic-audit-secret", render_token="synthetic-render-secret",
        client=PlanReportClient(), **sampling_kwargs(),
    )
    assert plan_report["overall_status"] == "error", "legacy exit/error semantics must remain"
    assert plan_report["website_health"]["availability"] == "available"
    assert any(gap["reason"] == "plan_restricted" for gap in plan_report["evidence_completeness"]["gaps"])
    assert_private_markers_absent(plan_report)
    no_latency = probe.collect_http_latency(
        LatencyClient("no_data"), metric_params=metric_params, headers={}
    )
    assert no_latency["status"] == "no_data"
    changed_latency = probe.collect_http_latency(
        LatencyClient("schema_changed"), metric_params=metric_params, headers={}
    )
    assert changed_latency["status"] == "schema_changed"

    print_activity = probe.summarize_print_events(
        [
            {
                "timestamp": "2026-08-14T15:58:00Z",
                "kind": "batch_print",
                "outcome": "success",
                "duration_ms": 1500,
                "slow": True,
                "source": "structured_app_log",
            }
        ],
        0,
    )
    memory_result = ok(
        [
            {
                "labels": [{"field": "service", "value": probe.RENDER_SERVICE_ID}],
                "values": [
                    {"timestamp": "2026-08-14T15:55:00Z", "value": 100 * 1024 * 1024},
                    {"timestamp": "2026-08-14T16:00:00Z", "value": 200 * 1024 * 1024},
                ],
                "unit": "bytes",
            }
        ]
    )
    correlation = probe.correlate_print_activity(
        print_activity,
        memory_result,
        [{"type": "server_failed", "timestamp": "2026-08-15T00:03:00+08:00"}],
    )
    assert correlation["status"] == "ok"
    assert correlation["memory_spike_count"] == 1
    assert correlation["abnormal_restart_count"] == 1
    assert correlation["correlated_window_count"] == 2
    high_precision = probe.correlate_print_activity(
        probe.summarize_print_events([{
            "timestamp": "2026-08-14T15:59:59.999999Z", "kind": "batch_print", "outcome": "success",
            "duration_ms": 1500, "slow": True, "source": "structured_app_log",
        }], 0),
        ok([{
            "labels": {}, "unit": "bytes", "values": [
                {"timestamp": "2026-08-14T23:59:58.123456789+08:00", "value": 100 * 1024 * 1024},
                {"timestamp": "2026-08-15T00:00:01.123456789+08:00", "value": 200 * 1024 * 1024},
            ],
        }]),
        [{"type": "server_failed", "timestamp": "2026-08-15T00:00:00.123456789+08:00"}],
    )
    assert high_precision["status"] == "ok" and high_precision["evidence_complete"] is True
    assert high_precision["correlated_window_count"] == 2
    unavailable_memory = probe.correlate_print_activity(print_activity, ok([]), [])
    assert unavailable_memory["memory_evidence_complete"] is False
    assert unavailable_memory["memory_spike_count"] is None
    no_print_correlation = probe.correlate_print_activity(
        probe.summarize_print_events([], 0), memory_result, []
    )
    assert no_print_correlation["status"] == "no_data"
    insufficient_time = probe.summarize_print_events([], 1)
    assert insufficient_time["status"] == "schema_changed"
    malicious_event, malicious_missing = probe.print_event_from_log(
        "[audit-print] kind=batch_print outcome=success duration_ms=1 slow=0 "
        + "PRIVATE-RECIPIENT" * 1000,
        {"type": "app", "path": "/api/admin/labels/batch-print?recipient=PRIVATE-RECIPIENT"},
        "2026-08-14T00:00:00Z",
    )
    assert malicious_event is None
    assert malicious_missing is False
    safe_request_event, _ = probe.print_event_from_log(
        "PRIVATE-RECIPIENT PRIVATE-ADDRESS PRIVATE-RAW-PAYLOAD",
        {
            "type": "request",
            "path": "/api/admin/labels/batch-print?recipient=PRIVATE-RECIPIENT",
            "statusCode": "503",
            "responseTimeMs": "1250",
        },
        "2026-08-14T00:00:00Z",
    )
    assert safe_request_event == {
        "kind": "batch_print",
        "outcome": "failure",
        "duration_ms": 1250,
        "slow": True,
        "source": "render_request_log",
        "timestamp": "2026-08-14T00:00:00Z",
    }
    assert_private_markers_absent({"event": safe_request_event})
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
