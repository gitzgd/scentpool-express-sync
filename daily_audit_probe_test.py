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
