#!/usr/bin/env python3
"""Fixed, read-only daily audit collector for Scentpool production.

The executable loads both credentials from macOS Keychain. Responses contain only
aggregate counts, fixed categories, and service-level metrics; raw logs are never
printed or persisted.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo


APP_TZ = ZoneInfo("Asia/Shanghai")
BASE_URL = "https://scentpool-express-sync-ec7c.onrender.com"
RENDER_API_URL = "https://api.render.com/v1"
RENDER_SERVICE_ID = "srv-d913padckfvc73eom3f0"
RENDER_SERVICE_NAME = "scentpool-express-sync-ec7c"
KEYCHAIN_ACCOUNT = "codex-daily-audit"
AUDIT_KEYCHAIN_SERVICE = "scentpool-audit-token"
RENDER_KEYCHAIN_SERVICE = "scentpool-render-api"
MAX_CURSOR_PAGES = 50
MAX_LOG_PAGES = 200
CONNECTION_SAMPLE_INTERVAL_SECONDS = 30
EXPECTED_CONNECTION_PEAK_UPPER_BOUND = 9
CORRELATION_WINDOW_MINUTES = 10
LATENCY_QUANTILES = (0.5, 0.9, 0.99)
AUDIT_DIAGNOSTICS_PATH = "/api/admin/system/audit-diagnostics"
MACOS_SYSTEM_CA_FILE = "/etc/ssl/cert.pem"

RENDER_QUERY_WHITELIST = {
    f"/services/{RENDER_SERVICE_ID}": set(),
    f"/services/{RENDER_SERVICE_ID}/deploys": {"limit", "cursor", "createdAfter", "createdBefore"},
    f"/services/{RENDER_SERVICE_ID}/events": {"limit", "cursor", "startTime", "endTime"},
    "/logs": {"ownerId", "startTime", "endTime", "direction", "resource", "limit"},
    "/metrics/memory": {"startTime", "endTime", "resolutionSeconds", "resource"},
    "/metrics/memory-limit": {"startTime", "endTime", "resolutionSeconds", "resource"},
    "/metrics/disk-usage": {"startTime", "endTime", "resolutionSeconds", "resource"},
    "/metrics/disk-capacity": {"startTime", "endTime", "resolutionSeconds", "resource"},
    "/metrics/http-requests": {
        "startTime", "endTime", "resolutionSeconds", "resource", "aggregateBy"
    },
    "/metrics/http-latency": {
        "startTime", "endTime", "resolutionSeconds", "resource", "quantile"
    },
}

LOG_PATTERNS = {
    "oom": re.compile(r"(?:ran out of memory|out of memory|\boom\b|memory limit|killed process)", re.I),
    "exception_stack": re.compile(r"(?:traceback \(most recent call last\)|unhandled exception|exception:)", re.I),
    "database_locked": re.compile(r"database is locked", re.I),
    "timeout": re.compile(r"(?:timed out|timeout)", re.I),
    "slow_request": re.compile(r"\[slow-request\]", re.I),
}
AUDIT_PRINT_PATTERN = re.compile(
    r"^\[audit-print\] kind=(batch_print|merge) outcome=(success|failure) "
    r"duration_ms=([0-9]+) slow=([01])$"
)
LEGACY_MERGE_PATTERN = re.compile(
    r"^\[labels\] merged orders=[0-9]+ pages=[0-9]+ source_bytes=[0-9]+ output_bytes=[0-9]+$"
)
SAFE_EVENT_TYPES = {
    "build_started", "build_ended", "deploy_started", "deploy_ended", "server_available",
    "server_failed", "server_hardware_failure", "server_restarted", "service_resumed",
    "service_suspended", "maintenance_started", "maintenance_ended",
    "zero_downtime_redeploy_started", "zero_downtime_redeploy_ended",
}
SAFE_DEPLOY_STATUSES = {
    "created", "queued", "build_in_progress", "update_in_progress", "live", "deactivated",
    "build_failed", "update_failed", "canceled", "pre_deploy_in_progress",
    "pre_deploy_failed",
}
FORBIDDEN_AUDIT_KEYS = {
    "recipient", "recipient_name", "phone", "address", "business_id", "store_order_no",
    "order_no", "order_number", "tracking_no", "tracking_number", "express_no", "raw",
    "booking_raw", "tracking_raw", "raw_payload", "token", "secret",
    "cookie", "session", "authorization", "database_path", "error_message",
}


def status_result(status: str, *, message: str, **values: Any) -> Dict[str, Any]:
    return {"status": status, "message": message, **values}


def requested_date(argv: Optional[list[str]] = None) -> str:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) > 1:
        raise ValueError("date must be YYYY-MM-DD")
    value = arguments[0] if arguments else (datetime.now(APP_TZ) - timedelta(days=1)).date().isoformat()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def day_window(date_text: str) -> tuple[str, str]:
    start = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=APP_TZ)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        end.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def expanded_window(start_time: str, end_time: str, minutes: int) -> tuple[str, str]:
    start = parsed_timestamp(start_time)
    end = parsed_timestamp(end_time)
    if start is None or end is None:
        raise ValueError("window timestamps must be timezone-aware")
    delta = timedelta(minutes=minutes)
    return (
        (start - delta).isoformat(timespec="seconds").replace("+00:00", "Z"),
        (end + delta).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def keychain_secret(service: str) -> tuple[Optional[str], Dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        value = completed.stdout.strip()
        if not value:
            return None, status_result(
                "process_error", message="钥匙串项目存在但没有可用内容。", error_type="empty_credential"
            )
        return value, status_result("ok", message="凭据已从钥匙串读入内存。")
    except (subprocess.SubprocessError, OSError) as exc:
        return None, status_result(
            "process_error",
            message="无法从钥匙串读取所需凭据。",
            error_type=type(exc).__name__,
        )


def https_context() -> ssl.SSLContext:
    if sys.platform == "darwin" and os.path.isfile(MACOS_SYSTEM_CA_FILE):
        return ssl.create_default_context(cafile=MACOS_SYSTEM_CA_FILE)
    return ssl.create_default_context()


def render_url(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    allowed = RENDER_QUERY_WHITELIST.get(path)
    if allowed is None:
        raise ValueError("render path is not in the read-only GET whitelist")
    supplied = set((params or {}).keys())
    if not supplied.issubset(allowed):
        raise ValueError("render query is not in the read-only GET whitelist")
    query = urllib.parse.urlencode(params or {}, doseq=True)
    return f"{RENDER_API_URL}{path}" + (f"?{query}" if query else "")


def validate_get_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("collector only permits HTTPS GET requests")
    if parsed.netloc == urllib.parse.urlparse(BASE_URL).netloc:
        if parsed.path == "/api/health" and not parsed.query:
            return
        if parsed.path == AUDIT_DIAGNOSTICS_PATH and not parsed.query:
            return
        if parsed.path == "/api/admin/system/daily-audit":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(params) == {"date"} and len(params["date"]) == 1:
                requested_date([params["date"][0]])
                return
        raise ValueError("application URL is not in the read-only GET whitelist")
    if parsed.netloc != urllib.parse.urlparse(RENDER_API_URL).netloc:
        raise ValueError("collector host is not in the read-only GET whitelist")
    api_prefix = urllib.parse.urlparse(RENDER_API_URL).path.rstrip("/")
    path = parsed.path[len(api_prefix):] if parsed.path.startswith(f"{api_prefix}/") else ""
    allowed = RENDER_QUERY_WHITELIST.get(path)
    if allowed is None:
        raise ValueError("render URL is not in the read-only GET whitelist")
    if not set(urllib.parse.parse_qs(parsed.query, keep_blank_values=True)).issubset(allowed):
        raise ValueError("render query is not in the read-only GET whitelist")


class JsonClient:
    def __init__(self, *, attempts: int = 3, timeout_seconds: int = 20) -> None:
        self.attempts = max(1, attempts)
        self.timeout_seconds = max(1, timeout_seconds)

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        validate_get_url(url)
        last_result: Dict[str, Any] = {}
        for attempt in range(self.attempts):
            started = time.monotonic()
            request = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "scentpool-daily-audit/2",
                    **(headers or {}),
                },
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                    context=https_context(),
                ) as response:
                    raw = response.read()
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return status_result(
                            "schema_changed",
                            message="接口返回的 JSON 结构无法识别。",
                            http_status=int(response.status),
                            elapsed_ms=round((time.monotonic() - started) * 1000),
                        )
                    return status_result(
                        "ok",
                        message="只读采集成功。",
                        http_status=int(response.status),
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        data=data,
                    )
            except urllib.error.HTTPError as exc:
                status = "permission_denied" if exc.code in (401, 403) else "http_error"
                return status_result(
                    status,
                    message="接口拒绝了只读请求。" if status == "permission_denied" else "接口返回 HTTP 错误。",
                    http_status=int(exc.code),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                restricted = isinstance(reason, (socket.gaierror, PermissionError)) or isinstance(
                    exc, (TimeoutError, socket.timeout)
                )
                last_result = status_result(
                    "network_restricted" if restricted else "process_error",
                    message=(
                        "当前网络环境限制或超时，无法完成只读请求。"
                        if restricted
                        else "采集进程无法完成网络请求。"
                    ),
                    error_type=type(reason).__name__,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                if attempt + 1 < self.attempts:
                    time.sleep(1 + attempt)
        return last_result or status_result("process_error", message="采集进程没有生成结果。")


def without_data(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in result.items() if key != "data"}


def schema_error(result: Dict[str, Any], message: str) -> Dict[str, Any]:
    base = without_data(result)
    base.update(status="schema_changed", message=message)
    return base


def no_data(result: Dict[str, Any], message: str, **values: Any) -> Dict[str, Any]:
    base = without_data(result)
    base.update(status="no_data", message=message, **values)
    return base


def audit_payload_is_private(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if (
                normalized in FORBIDDEN_AUDIT_KEYS
                or normalized.endswith(("_raw", "_phone", "_address", "_id"))
                or (
                    normalized.endswith("_name")
                    and normalized not in {"store_name", "missing_store_name"}
                )
            ):
                return True
            if audit_payload_is_private(child):
                return True
    elif isinstance(value, list):
        return any(audit_payload_is_private(child) for child in value)
    return False


def label_map(labels: Any) -> Optional[Dict[str, str]]:
    if isinstance(labels, dict):
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
            return None
        return dict(labels)
    if not isinstance(labels, list):
        return None
    result: Dict[str, str] = {}
    for label in labels:
        if not isinstance(label, dict):
            return None
        name = label.get("name", label.get("field"))
        value = label.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        result[name] = value
    return result


def metric_series_payload(payload: Any) -> Optional[list[Any]]:
    """Accept the documented array plus bounded wrapper variants seen across metric APIs."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    direct = payload.get("data")
    if isinstance(direct, list):
        return direct
    direct = payload.get("series")
    if isinstance(direct, list):
        return direct
    if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("series"), list):
        return payload["data"]["series"]
    return None


def metric_summary(result: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
    if result.get("status") != "ok":
        return without_data(result)
    series = metric_series_payload(result.get("data"))
    if series is None:
        return schema_error(result, "指标响应不再是支持的时间序列结构。")
    normalized: list[Dict[str, Any]] = []
    for item in series:
        if not isinstance(item, dict) or label_map(item.get("labels")) is None:
            return schema_error(result, "指标时间序列标签结构发生变化。")
        values = item.get("values")
        unit = item.get("unit", "")
        if not isinstance(values, list) or not isinstance(unit, str):
            return schema_error(result, "指标时间序列数值结构发生变化。")
        points: list[tuple[str, float]] = []
        for point in values:
            if not isinstance(point, dict) or not isinstance(point.get("timestamp"), str):
                return schema_error(result, "指标数据点结构发生变化。")
            value = point.get("value")
            if not isinstance(value, (int, float)):
                return schema_error(result, "指标数据点不是数值。")
            points.append((str(point["timestamp"]), float(value)))
        if points:
            normalized.append(
                {
                    "labels": label_map(item["labels"]),
                    "unit": unit,
                    "latest": points[-1][1],
                    "maximum": max(value for _timestamp, value in points),
                    "sum": sum(value for _timestamp, value in points),
                    "points": len(points),
                }
            )
    if not normalized:
        return no_data(result, "指标接口成功，但目标时间段没有数据。", series=[])
    if mode == "resource":
        maximum = max(item["maximum"] for item in normalized)
        latest = sum(item["latest"] for item in normalized)
        units = sorted({item["unit"] for item in normalized})
        return {**without_data(result), "latest": latest, "maximum": maximum, "units": units}
    if mode == "requests":
        by_status: Dict[str, float] = {}
        for item in normalized:
            status_code = str(item["labels"].get("statusCode") or "all")
            if status_code != "all" and not re.fullmatch(r"[1-5][0-9]{2}", status_code):
                status_code = "unknown"
            by_status[status_code] = by_status.get(status_code, 0.0) + float(item["sum"])
        return {
            **without_data(result),
            "total": sum(by_status.values()),
            "http_5xx": sum(
                value for status_code, value in by_status.items() if status_code.startswith("5")
            ),
            "by_status_code": dict(sorted(by_status.items())),
        }
    return {
        **without_data(result),
        "series": [
            {
                "labels": {
                    key: value
                    for key, value in item["labels"].items()
                    if key in {"quantile"}
                },
                "unit": item["unit"],
                "latest": item["latest"],
                "maximum": item["maximum"],
            }
            for item in normalized
        ],
    }


def collect_http_latency(
    client: JsonClient,
    *,
    metric_params: Dict[str, Any],
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """Prefer the documented multi-quantile query and degrade to isolated quantiles on 400."""
    multi_params = {**metric_params, "quantile": list(LATENCY_QUANTILES)}
    multi_raw = client.fetch(render_url("/metrics/http-latency", multi_params), headers)
    multi = metric_summary(multi_raw, mode="series")
    if multi.get("status") != "http_error" or multi.get("http_status") != 400:
        return {**multi, "query_mode": "multi_quantile"}

    successful: list[Dict[str, Any]] = []
    failures: list[Dict[str, Any]] = []
    no_data_quantiles: list[float] = []
    for quantile in LATENCY_QUANTILES:
        raw = client.fetch(
            render_url("/metrics/http-latency", {**metric_params, "quantile": quantile}),
            headers,
        )
        summary = metric_summary(raw, mode="series")
        status = str(summary.get("status") or "process_error")
        if status == "ok":
            for series in summary.get("series", []):
                normalized = dict(series)
                labels = dict(normalized.get("labels") or {})
                labels.setdefault("quantile", str(quantile))
                normalized["labels"] = labels
                successful.append(normalized)
        elif status == "no_data":
            no_data_quantiles.append(quantile)
        else:
            failures.append(
                {
                    "quantile": quantile,
                    "status": status,
                    **({"http_status": summary["http_status"]} if "http_status" in summary else {}),
                }
            )

    if successful:
        return status_result(
            "ok",
            message="延迟指标已使用单分位受控降级采集。",
            query_mode="single_quantile_fallback",
            coverage="complete" if not failures and not no_data_quantiles else "partial",
            series=successful,
            no_data_quantiles=no_data_quantiles,
            failed_quantiles=failures,
        )
    if no_data_quantiles and not failures:
        return status_result(
            "no_data",
            message="延迟指标接口可用，但所选时间段或当前套餐没有数据。",
            query_mode="single_quantile_fallback",
            no_data_quantiles=no_data_quantiles,
        )
    if failures and all(item["status"] == "schema_changed" for item in failures):
        return status_result(
            "schema_changed",
            message="延迟指标的多分位与单分位响应结构均无法识别。",
            query_mode="single_quantile_fallback",
            failed_quantiles=failures,
        )
    representative = failures[0] if failures else {"status": "http_error", "http_status": 400}
    return status_result(
        str(representative["status"]),
        message="延迟指标的多分位与单分位请求均不可用。",
        query_mode="single_quantile_fallback",
        **({"http_status": representative["http_status"]} if "http_status" in representative else {}),
        failed_quantiles=failures,
        no_data_quantiles=no_data_quantiles,
    )


def collect_cursor_pages(
    client: JsonClient,
    path: str,
    params: Dict[str, Any],
    *,
    wrapper_key: str,
    headers: Dict[str, str],
) -> tuple[Dict[str, Any], list[Dict[str, Any]], bool]:
    rows: list[Dict[str, Any]] = []
    current = dict(params)
    last_result: Dict[str, Any] = {}
    for _page in range(MAX_CURSOR_PAGES):
        last_result = client.fetch(render_url(path, current), headers)
        if last_result.get("status") != "ok":
            return without_data(last_result), [], False
        page = last_result.get("data")
        if not isinstance(page, list):
            return schema_error(last_result, "分页接口不再返回数组。"), [], False
        if not page:
            return without_data(last_result), rows, True
        cursor = None
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get(wrapper_key), dict):
                return schema_error(last_result, "分页条目结构发生变化。"), [], False
            rows.append(dict(item[wrapper_key]))
            cursor = item.get("cursor")
        if len(page) < int(current.get("limit", 100)):
            return without_data(last_result), rows, True
        if not isinstance(cursor, str) or not cursor:
            return schema_error(last_result, "分页游标缺失，无法确认数据完整性。"), [], False
        current["cursor"] = cursor
    return status_result(
        "schema_changed", message="分页超过安全上限，结果可能不完整。", page_limit=MAX_CURSOR_PAGES
    ), rows, False


def parsed_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def safe_timestamp(value: Any) -> Optional[str]:
    parsed = parsed_timestamp(value)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z") if parsed else None


def fixed_print_request_path(labels: Dict[str, str]) -> bool:
    for key in ("path", "requestPath", "route"):
        value = labels.get(key)
        if isinstance(value, str) and len(value) <= 512:
            parsed = urllib.parse.urlparse(value)
            if parsed.path == "/api/admin/labels/batch-print":
                return True
    return False


def print_event_from_log(message: str, labels: Dict[str, str], timestamp: Any) -> tuple[Optional[Dict[str, Any]], bool]:
    match = AUDIT_PRINT_PATTERN.fullmatch(message)
    candidate: Optional[Dict[str, Any]] = None
    if match:
        duration_ms = int(match.group(3))
        if duration_ms <= 24 * 60 * 60 * 1000:
            candidate = {
                "kind": match.group(1),
                "outcome": match.group(2),
                "duration_ms": duration_ms,
                "slow": match.group(4) == "1",
                "source": "structured_app_log",
            }
    elif LEGACY_MERGE_PATTERN.fullmatch(message):
        candidate = {
            "kind": "merge",
            "outcome": "success",
            "duration_ms": None,
            "slow": None,
            "source": "legacy_merge_log",
        }
    elif labels.get("type") == "request" and fixed_print_request_path(labels):
        status_code = str(labels.get("statusCode") or "")
        outcome = "unknown"
        if re.fullmatch(r"[1-5][0-9]{2}", status_code):
            outcome = "success" if int(status_code) < 400 else "failure"
        duration_ms: Optional[int] = None
        for key in ("durationMs", "responseTimeMs", "responseTime"):
            raw = str(labels.get(key) or "")
            if re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,3})?", raw):
                duration_ms = round(float(raw))
                break
        candidate = {
            "kind": "batch_print",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "slow": duration_ms >= 1000 if duration_ms is not None else None,
            "source": "render_request_log",
        }
    if candidate is None:
        return None, False
    normalized_timestamp = safe_timestamp(timestamp)
    if normalized_timestamp is None:
        return None, True
    candidate["timestamp"] = normalized_timestamp
    return candidate, False


def summarize_print_events(events: list[Dict[str, Any]], missing_timestamps: int) -> Dict[str, Any]:
    structured_requests = [
        event for event in events
        if event["kind"] == "batch_print" and event["source"] == "structured_app_log"
    ]
    request_events = structured_requests or [
        event for event in events
        if event["kind"] == "batch_print" and event["source"] == "render_request_log"
    ]
    structured_merges = [
        event for event in events
        if event["kind"] == "merge" and event["source"] == "structured_app_log"
    ]
    merge_events = structured_merges or [event for event in events if event["kind"] == "merge"]
    selected = sorted(request_events + merge_events, key=lambda item: item["timestamp"])
    counts = {
        "requests": len(request_events),
        "merges": len(merge_events),
        "success": sum(event["outcome"] == "success" for event in selected),
        "failure": sum(event["outcome"] == "failure" for event in selected),
        "unknown_outcome": sum(event["outcome"] == "unknown" for event in selected),
        "slow": sum(event["slow"] is True for event in selected),
    }
    if not selected and not missing_timestamps:
        return status_result(
            "no_data",
            message="目标时间段没有批量打印或合并时间证据。",
            evidence_complete=True,
            missing_timestamps=0,
            counts=counts,
            events=[],
        )
    return status_result(
        "ok" if selected else "schema_changed",
        message=(
            "已生成脱敏的批量打印与合并时间证据。"
            if selected
            else "发现打印证据但时间字段无法识别。"
        ),
        evidence_complete=missing_timestamps == 0,
        missing_timestamps=missing_timestamps,
        counts=counts,
        events=selected,
    )


def merge_print_activities(activities: list[Dict[str, Any]]) -> Dict[str, Any]:
    events: list[Dict[str, Any]] = []
    missing_timestamps = 0
    source_statuses: list[str] = []
    evidence_complete = True
    for activity in activities:
        source_statuses.append(str(activity.get("status") or "schema_changed"))
        if isinstance(activity.get("events"), list):
            events.extend(event for event in activity["events"] if isinstance(event, dict))
        missing_timestamps += int(activity.get("missing_timestamps") or 0)
        evidence_complete = evidence_complete and bool(activity.get("evidence_complete", False))
    deduplicated: list[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for event in events:
        identity = (
            event.get("timestamp"), event.get("kind"), event.get("outcome"),
            event.get("duration_ms"), event.get("source"),
        )
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(event)
    merged = summarize_print_events(deduplicated, missing_timestamps)
    merged["evidence_complete"] = evidence_complete and missing_timestamps == 0
    merged["source_window_statuses"] = source_statuses
    return merged


def unit_multiplier(unit: str) -> Optional[float]:
    normalized = unit.strip().lower()
    return {
        "b": 1.0,
        "byte": 1.0,
        "bytes": 1.0,
        "kb": 1000.0,
        "kib": 1024.0,
        "mb": 1000.0 * 1000.0,
        "mib": 1024.0 * 1024.0,
        "gb": 1000.0 * 1000.0 * 1000.0,
        "gib": 1024.0 * 1024.0 * 1024.0,
    }.get(normalized)


def memory_spike_evidence(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("status") != "ok":
        return without_data(result)
    series = metric_series_payload(result.get("data"))
    if series is None:
        return schema_error(result, "内存相关性指标结构无法识别。")
    spike_times: list[str] = []
    usable_series = 0
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            return schema_error(result, "内存相关性数据点结构发生变化。")
        multiplier = unit_multiplier(str(item.get("unit") or ""))
        if multiplier is None:
            continue
        points: list[tuple[datetime, float]] = []
        for point in item["values"]:
            if not isinstance(point, dict) or not isinstance(point.get("value"), (int, float)):
                return schema_error(result, "内存相关性数据点不是数值。")
            timestamp = parsed_timestamp(point.get("timestamp"))
            if timestamp is None:
                return schema_error(result, "内存相关性数据点时间无法识别。")
            points.append((timestamp, float(point["value"]) * multiplier))
        points.sort(key=lambda item: item[0])
        if points:
            usable_series += 1
        for previous, current in zip(points, points[1:]):
            delta = current[1] - previous[1]
            threshold = max(64 * 1024 * 1024, max(previous[1], 1.0) * 0.25)
            if delta >= threshold:
                spike_times.append(current[0].isoformat(timespec="seconds").replace("+00:00", "Z"))
    if not usable_series:
        return status_result(
            "no_data",
            message="内存指标没有可用于突升判定的数据或可识别单位。",
            timestamps=[],
        )
    return status_result(
        "ok" if spike_times else "no_data",
        message=("已识别内存突升时间。" if spike_times else "目标时间段未识别到内存突升。"),
        timestamps=sorted(set(spike_times)),
    )


def abnormal_restart_timestamps(event_rows: list[Dict[str, Any]]) -> tuple[list[str], int]:
    timestamps: list[str] = []
    missing = 0
    for row in event_rows:
        if str(row.get("type") or "") not in {
            "server_failed", "server_hardware_failure", "server_restarted"
        }:
            continue
        timestamp = safe_timestamp(row.get("timestamp", row.get("createdAt")))
        if timestamp is None:
            missing += 1
        else:
            timestamps.append(timestamp)
    return sorted(set(timestamps)), missing


def correlate_print_activity(
    print_activity: Dict[str, Any],
    memory_result: Dict[str, Any],
    event_rows: list[Dict[str, Any]],
) -> Dict[str, Any]:
    memory_spikes = memory_spike_evidence(memory_result)
    restart_times, missing_restart_times = abnormal_restart_timestamps(event_rows)
    print_events = print_activity.get("events") if isinstance(print_activity, dict) else None
    if not isinstance(print_events, list):
        print_events = []
    signals = [
        *(('memory_spike', timestamp) for timestamp in memory_spikes.get("timestamps", [])),
        *(('abnormal_restart', timestamp) for timestamp in restart_times),
    ]
    windows: list[Dict[str, Any]] = []
    for signal, timestamp in sorted(signals, key=lambda item: item[1]):
        signal_time = parsed_timestamp(timestamp)
        if signal_time is None:
            continue
        nearby = []
        for event in print_events:
            event_time = parsed_timestamp(event.get("timestamp")) if isinstance(event, dict) else None
            if event_time is not None and abs((event_time - signal_time).total_seconds()) <= CORRELATION_WINDOW_MINUTES * 60:
                nearby.append(
                    {
                        "timestamp": event["timestamp"],
                        "kind": event["kind"],
                        "outcome": event["outcome"],
                        "slow": event["slow"],
                    }
                )
        windows.append(
            {
                "signal": signal,
                "timestamp": timestamp,
                "print_event_count": len(nearby),
                "print_events": nearby,
            }
        )
    memory_evidence_complete = memory_spikes.get("status") in {"ok", "no_data"}
    evidence_complete = (
        bool(print_activity.get("evidence_complete", False))
        and not missing_restart_times
        and memory_evidence_complete
    )
    if print_activity.get("status") == "schema_changed" or memory_spikes.get("status") == "schema_changed":
        status = "schema_changed"
        message = "打印、内存或重启的时间证据结构不完整，不能完成相关性判定。"
    elif not print_events or not signals:
        status = "no_data"
        message = "打印事件或风险信号不足，未形成可判定的时间窗口相关性。"
    else:
        status = "ok"
        message = "已完成打印事件与内存突升/异常重启的时间窗口相关性判定。"
    return status_result(
        status,
        message=message,
        window_minutes=CORRELATION_WINDOW_MINUTES,
        evidence_complete=evidence_complete,
        memory_spike_status=memory_spikes.get("status"),
        memory_evidence_complete=memory_evidence_complete,
        memory_spike_count=len(memory_spikes.get("timestamps", [])),
        abnormal_restart_count=len(restart_times),
        missing_restart_timestamps=missing_restart_times,
        correlated_window_count=sum(window["print_event_count"] > 0 for window in windows),
        windows=windows,
    )


def collect_logs(
    client: JsonClient,
    *,
    owner_id: str,
    start_time: str,
    end_time: str,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "ownerId": owner_id,
        "startTime": start_time,
        "endTime": end_time,
        "direction": "forward",
        "resource": RENDER_SERVICE_ID,
        "limit": 100,
    }
    counts = {key: 0 for key in LOG_PATTERNS}
    request_logs = 0
    http_5xx = 0
    total_logs = 0
    print_events: list[Dict[str, Any]] = []
    missing_print_timestamps = 0
    last_result: Dict[str, Any] = {}
    for page_number in range(MAX_LOG_PAGES):
        last_result = client.fetch(render_url("/logs", params), headers)
        if last_result.get("status") != "ok":
            return without_data(last_result)
        payload = last_result.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("logs"), list):
            return schema_error(last_result, "日志响应结构发生变化。")
        for entry in payload["logs"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
                return schema_error(last_result, "日志条目结构发生变化。")
            labels = label_map(entry.get("labels"))
            if labels is None:
                return schema_error(last_result, "日志标签结构发生变化。")
            total_logs += 1
            message = str(entry["message"])
            for category, pattern in LOG_PATTERNS.items():
                if pattern.search(message):
                    counts[category] += 1
            if labels.get("type") == "request":
                request_logs += 1
                if str(labels.get("statusCode") or "").startswith("5"):
                    http_5xx += 1
            print_event, missing_timestamp = print_event_from_log(
                message, labels, entry.get("timestamp")
            )
            if print_event is not None:
                print_events.append(print_event)
            if missing_timestamp:
                missing_print_timestamps += 1
        if not payload.get("hasMore"):
            if total_logs == 0:
                return no_data(
                    last_result,
                    "日志接口成功，但目标时间段没有日志。",
                    total_logs=0,
                    categories=counts,
                    request_logs=0,
                    http_5xx_request_logs=0,
                    pagination_complete=True,
                    print_activity=summarize_print_events([], 0),
                )
            return {
                **without_data(last_result),
                "total_logs": total_logs,
                "categories": counts,
                "request_logs": request_logs,
                "http_5xx_request_logs": http_5xx,
                "pagination_complete": True,
                "print_activity": summarize_print_events(
                    print_events, missing_print_timestamps
                ),
            }
        next_start = payload.get("nextStartTime")
        next_end = payload.get("nextEndTime")
        if not isinstance(next_start, str) or not isinstance(next_end, str):
            return schema_error(last_result, "日志分页时间游标缺失。")
        if next_start == params["startTime"] and next_end == params["endTime"]:
            return schema_error(last_result, "日志分页时间游标没有前进。")
        params["startTime"] = next_start
        params["endTime"] = next_end
    return status_result(
        "schema_changed",
        message="日志分页超过安全上限，结果未静默截断。",
        total_logs=total_logs,
        categories=counts,
        request_logs=request_logs,
        http_5xx_request_logs=http_5xx,
        pagination_complete=False,
        print_activity=summarize_print_events(print_events, missing_print_timestamps),
        page_limit=MAX_LOG_PAGES,
    )


def app_summary(result: Dict[str, Any], *, daily: bool = False) -> Dict[str, Any]:
    if result.get("status") != "ok":
        return without_data(result)
    data = result.get("data")
    if not isinstance(data, dict):
        return schema_error(result, "应用接口响应结构发生变化。")
    if not daily:
        if "ok" not in data or "database" not in data:
            return schema_error(result, "健康检查响应缺少固定字段。")
        return {**without_data(result), "result": {"ok": bool(data["ok"]), "database": bool(data["database"])}}
    if audit_payload_is_private(data):
        return schema_error(result, "日报响应出现不允许的敏感字段，已停止转发。")
    allowed = (
        "date", "timezone", "metrics", "by_store", "shipment_classification", "exceptions", "historical_end_of_day",
        "failures", "completeness", "long_waiting", "recent_7_day_average", "data_quality", "basis",
    )
    if data.get("date") is None or data.get("timezone") is None:
        return schema_error(result, "日报响应缺少日期或时区。")
    return {**without_data(result), "result": {key: data.get(key) for key in allowed}}


def connection_sample(result: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    if result.get("status") != "ok":
        return None, without_data(result)
    data = result.get("data")
    if not isinstance(data, dict) or audit_payload_is_private(data):
        return None, schema_error(result, "连接诊断响应包含不允许字段或结构发生变化。")
    sampled_at = data.get("sampled_at")
    storage = data.get("storage")
    connections = storage.get("connections") if isinstance(storage, dict) else None
    expected = {"opened_total", "closed_total", "active", "peak_active"}
    if (
        not isinstance(sampled_at, str)
        or not isinstance(connections, dict)
        or set(connections) != expected
        or not all(isinstance(connections[key], int) and connections[key] >= 0 for key in expected)
    ):
        return None, schema_error(result, "连接诊断响应缺少固定计数或时间字段。")
    try:
        datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
    except ValueError:
        return None, schema_error(result, "连接诊断采样时间无法识别。")
    sample = {"sampled_at": sampled_at, **{key: int(connections[key]) for key in sorted(expected)}}
    sample["conserved"] = sample["opened_total"] - sample["closed_total"] == sample["active"]
    return sample, without_data(result)


def collect_connection_samples(
    client: JsonClient,
    *,
    headers: Dict[str, str],
    interval_seconds: int = CONNECTION_SAMPLE_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    interval_seconds = max(CONNECTION_SAMPLE_INTERVAL_SECONDS, int(interval_seconds))
    samples: list[Dict[str, Any]] = []
    attempts: list[Dict[str, Any]] = []
    first_started = monotonic()
    first, first_status = connection_sample(
        client.fetch(f"{BASE_URL}{AUDIT_DIAGNOSTICS_PATH}", headers)
    )
    attempts.append(first_status)
    if first is not None:
        samples.append(first)
    sleeper(interval_seconds)
    second_started = monotonic()
    second, second_status = connection_sample(
        client.fetch(f"{BASE_URL}{AUDIT_DIAGNOSTICS_PATH}", headers)
    )
    attempts.append(second_status)
    if second is not None:
        samples.append(second)
    measured_interval = max(0.0, second_started - first_started)
    interval_met = measured_interval >= CONNECTION_SAMPLE_INTERVAL_SECONDS

    if len(samples) != 2 or not interval_met:
        failure = next((attempt for attempt in attempts if attempt.get("status") != "ok"), None)
        status = str((failure or {}).get("status") or "schema_changed")
        return status_result(
            status,
            message="连接双采样证据不完整，未据此判定连接回落。",
            completeness="partial" if samples else "unavailable",
            sample_count=len(samples),
            required_interval_seconds=CONNECTION_SAMPLE_INTERVAL_SECONDS,
            measured_interval_seconds=round(measured_interval, 3),
            interval_requirement_met=interval_met,
            samples=samples,
            attempts=attempts,
        )

    first_sample, second_sample = samples
    counter_reset = any(
        second_sample[key] < first_sample[key]
        for key in ("opened_total", "closed_total", "peak_active")
    )
    peak_change = second_sample["peak_active"] - first_sample["peak_active"]
    peak_abnormal = (
        counter_reset
        or max(first_sample["peak_active"], second_sample["peak_active"])
        > EXPECTED_CONNECTION_PEAK_UPPER_BOUND
    )
    return status_result(
        "ok",
        message="连接诊断已完成至少 30 秒间隔的双采样。",
        completeness="complete",
        sample_count=2,
        required_interval_seconds=CONNECTION_SAMPLE_INTERVAL_SECONDS,
        measured_interval_seconds=round(measured_interval, 3),
        interval_requirement_met=True,
        samples=samples,
        all_samples_conserved=all(bool(sample["conserved"]) for sample in samples),
        active_recovered=(
            None if counter_reset else second_sample["active"] <= first_sample["active"]
        ),
        active_change=(
            None if counter_reset else second_sample["active"] - first_sample["active"]
        ),
        counter_reset_between_samples=counter_reset,
        peak_active_change=peak_change,
        peak_active_abnormal=peak_abnormal,
        expected_peak_active_upper_bound=EXPECTED_CONNECTION_PEAK_UPPER_BOUND,
    )


def collect_report(
    date_text: str,
    *,
    audit_token: str,
    render_token: str,
    client: Optional[JsonClient] = None,
    connection_interval_seconds: int = CONNECTION_SAMPLE_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    requested_date([date_text])
    http = client or JsonClient()
    start_time, end_time = day_window(date_text)
    correlation_start_time, correlation_end_time = expanded_window(
        start_time, end_time, CORRELATION_WINDOW_MINUTES
    )
    audit_headers = {"Authorization": f"Bearer {audit_token}"}
    render_headers = {"Authorization": f"Bearer {render_token}"}

    health = app_summary(http.fetch(f"{BASE_URL}/api/health"))
    daily_url = f"{BASE_URL}/api/admin/system/daily-audit?{urllib.parse.urlencode({'date': date_text})}"
    daily = app_summary(http.fetch(daily_url, audit_headers), daily=True)
    connection_diagnostics = collect_connection_samples(
        http,
        headers=audit_headers,
        interval_seconds=connection_interval_seconds,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    service_fetch = http.fetch(render_url(f"/services/{RENDER_SERVICE_ID}"), render_headers)
    service_data = service_fetch.get("data") if service_fetch.get("status") == "ok" else None
    if not isinstance(service_data, dict):
        render_service = (
            schema_error(service_fetch, "服务详情响应结构发生变化。")
            if service_fetch.get("status") == "ok"
            else without_data(service_fetch)
        )
        verified = False
        owner_id = ""
        details: Dict[str, Any] = {}
    else:
        details = service_data.get("serviceDetails") if isinstance(service_data.get("serviceDetails"), dict) else {}
        owner_id = str(service_data.get("ownerId") or "")
        target_url = str(details.get("url") or service_data.get("url") or "").rstrip("/")
        verified = (
            service_data.get("id") == RENDER_SERVICE_ID
            and service_data.get("name") == RENDER_SERVICE_NAME
            and target_url == BASE_URL
            and bool(owner_id)
        )
        if verified:
            render_service = {
                **without_data(service_fetch),
                "result": {
                    "id": RENDER_SERVICE_ID,
                    "name": RENDER_SERVICE_NAME,
                    "url": BASE_URL,
                    "disk_configured_capacity_gb": (
                        details.get("disk", {}).get("sizeGB")
                        if isinstance(details.get("disk"), dict)
                        else None
                    ),
                },
            }
        else:
            render_service = status_result(
                "target_mismatch",
                message="Render 服务身份、名称、URL 或所属工作区与固定生产目标不一致。",
            )

    if verified:
        deploy_status, deploy_rows, deploy_complete = collect_cursor_pages(
            http,
            f"/services/{RENDER_SERVICE_ID}/deploys",
            {"limit": 100, "createdAfter": start_time, "createdBefore": end_time},
            wrapper_key="deploy",
            headers=render_headers,
        )
        if deploy_status.get("status") == "ok" and not deploy_rows:
            deploys = no_data(deploy_status, "目标日没有部署记录。", count=0, pagination_complete=deploy_complete)
        elif deploy_status.get("status") == "ok":
            by_status: Dict[str, int] = {}
            for row in deploy_rows:
                value = str(row.get("status") or "unknown")
                if value not in SAFE_DEPLOY_STATUSES:
                    value = "unknown"
                by_status[value] = by_status.get(value, 0) + 1
            deploys = {
                **deploy_status,
                "count": len(deploy_rows),
                "by_status": dict(sorted(by_status.items())),
                "pagination_complete": deploy_complete,
            }
        else:
            deploys = deploy_status

        event_status, event_rows, event_complete = collect_cursor_pages(
            http,
            f"/services/{RENDER_SERVICE_ID}/events",
            {"limit": 100, "startTime": start_time, "endTime": end_time},
            wrapper_key="event",
            headers=render_headers,
        )
        if event_status.get("status") == "ok" and not event_rows:
            events = no_data(event_status, "目标日没有平台事件。", count=0, pagination_complete=event_complete)
        elif event_status.get("status") == "ok":
            by_type: Dict[str, int] = {}
            for row in event_rows:
                value = str(row.get("type") or "unknown")
                if value not in SAFE_EVENT_TYPES:
                    value = "other"
                by_type[value] = by_type.get(value, 0) + 1
            events = {
                **event_status,
                "count": len(event_rows),
                "by_type": dict(sorted(by_type.items())),
                "restarts": sum(by_type.get(key, 0) for key in ("server_restarted", "server_failed", "server_hardware_failure")),
                "pagination_complete": event_complete,
            }
        else:
            events = event_status

        correlation_event_status, correlation_event_rows, _correlation_event_complete = collect_cursor_pages(
            http,
            f"/services/{RENDER_SERVICE_ID}/events",
            {
                "limit": 100,
                "startTime": correlation_start_time,
                "endTime": correlation_end_time,
            },
            wrapper_key="event",
            headers=render_headers,
        )
        if correlation_event_status.get("status") != "ok":
            correlation_event_rows = event_rows

        logs = collect_logs(
            http,
            owner_id=owner_id,
            start_time=start_time,
            end_time=end_time,
            headers=render_headers,
        )
        boundary_print_activities: list[Dict[str, Any]] = []
        for boundary_start, boundary_end in (
            (correlation_start_time, start_time),
            (end_time, correlation_end_time),
        ):
            boundary_logs = collect_logs(
                http,
                owner_id=owner_id,
                start_time=boundary_start,
                end_time=boundary_end,
                headers=render_headers,
            )
            boundary_print_activities.append(
                boundary_logs.get("print_activity")
                if isinstance(boundary_logs.get("print_activity"), dict)
                else status_result(
                    str(boundary_logs.get("status") or "schema_changed"),
                    message="跨日边界日志不可用。",
                    evidence_complete=False,
                    missing_timestamps=0,
                    events=[],
                )
            )
        metric_params: Dict[str, Any] = {
            "startTime": start_time,
            "endTime": end_time,
            "resolutionSeconds": 300,
            "resource": RENDER_SERVICE_ID,
        }
        memory_raw = http.fetch(render_url("/metrics/memory", metric_params), render_headers)
        memory = metric_summary(memory_raw, mode="resource")
        correlation_memory_raw = http.fetch(
            render_url(
                "/metrics/memory",
                {
                    **metric_params,
                    "startTime": correlation_start_time,
                    "endTime": correlation_end_time,
                },
            ),
            render_headers,
        )
        memory_limit = metric_summary(
            http.fetch(render_url("/metrics/memory-limit", metric_params), render_headers), mode="resource"
        )
        disk_usage = metric_summary(
            http.fetch(render_url("/metrics/disk-usage", metric_params), render_headers), mode="resource"
        )
        disk_capacity = metric_summary(
            http.fetch(render_url("/metrics/disk-capacity", metric_params), render_headers), mode="resource"
        )
        request_params = {**metric_params, "aggregateBy": "statusCode"}
        http_requests = metric_summary(
            http.fetch(render_url("/metrics/http-requests", request_params), render_headers), mode="requests"
        )
        http_latency = collect_http_latency(
            http,
            metric_params=metric_params,
            headers=render_headers,
        )
        primary_print_activity = logs.get("print_activity") if isinstance(logs.get("print_activity"), dict) else status_result(
            str(logs.get("status") or "schema_changed"),
            message="日志通道不可用，无法提取打印时间证据。",
            evidence_complete=False,
            missing_timestamps=0,
            events=[],
        )
        print_activity = merge_print_activities(
            [primary_print_activity, *boundary_print_activities]
        )
        logs["print_activity"] = print_activity
        print_correlation = correlate_print_activity(
            print_activity, correlation_memory_raw, correlation_event_rows
        )
        print_correlation["expanded_window_start"] = correlation_start_time
        print_correlation["expanded_window_end"] = correlation_end_time
        print_correlation["event_window_status"] = correlation_event_status.get("status")
        print_correlation["evidence_complete"] = bool(
            print_correlation.get("evidence_complete")
            and correlation_event_status.get("status") == "ok"
        )
    else:
        blocked = status_result(
            "process_error", message="生产服务身份未通过固定校验，未继续读取深度通道。"
        )
        deploys = dict(blocked)
        events = dict(blocked)
        logs = dict(blocked)
        memory = dict(blocked)
        memory_limit = dict(blocked)
        disk_usage = dict(blocked)
        disk_capacity = dict(blocked)
        http_requests = dict(blocked)
        http_latency = dict(blocked)
        print_correlation = dict(blocked)

    sections = [
        health, daily, connection_diagnostics, render_service, deploys, events, logs, memory, memory_limit,
        disk_usage, disk_capacity, http_requests, http_latency, print_correlation,
    ]
    hard_failures = {"http_error", "permission_denied", "schema_changed", "process_error", "network_restricted", "target_mismatch"}
    if any(section.get("status") in hard_failures for section in sections):
        overall_status = "error"
    elif any(section.get("status") == "no_data" for section in sections):
        overall_status = "partial"
    else:
        overall_status = "ok"
    return {
        "collected_at": datetime.now(APP_TZ).isoformat(timespec="seconds"),
        "target_date": date_text,
        "target": {"service_id": RENDER_SERVICE_ID, "service_name": RENDER_SERVICE_NAME},
        "overall_status": overall_status,
        "health": health,
        "daily_audit": daily,
        "connection_diagnostics": connection_diagnostics,
        "render_service": render_service,
        "render_deploys": deploys,
        "render_events": events,
        "render_logs": logs,
        "print_risk_correlation": print_correlation,
        "render_metrics": {
            "memory_usage": memory,
            "memory_limit": memory_limit,
            "disk_usage": disk_usage,
            "disk_capacity": disk_capacity,
            "http_requests": http_requests,
            "http_latency": http_latency,
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    report: Dict[str, Any]
    try:
        date_text = requested_date(argv)
        audit_token, audit_status = keychain_secret(AUDIT_KEYCHAIN_SERVICE)
        render_token, render_status = keychain_secret(RENDER_KEYCHAIN_SERVICE)
        if not audit_token or not render_token:
            report = {
                "collected_at": datetime.now(APP_TZ).isoformat(timespec="seconds"),
                "target_date": date_text,
                "target": {"service_id": RENDER_SERVICE_ID, "service_name": RENDER_SERVICE_NAME},
                "overall_status": "error",
                "collector": status_result(
                    "process_error",
                    message="采集器无法读取全部钥匙串凭据，未发起网络请求。",
                    audit_credential=audit_status,
                    render_credential=render_status,
                ),
            }
        else:
            report = collect_report(
                date_text,
                audit_token=audit_token,
                render_token=render_token,
            )
    except Exception as exc:
        report = {
            "collected_at": datetime.now(APP_TZ).isoformat(timespec="seconds"),
            "overall_status": "error",
            "collector": status_result(
                "process_error",
                message="采集进程发生异常，但已生成脱敏错误结果。",
                error_type=type(exc).__name__,
            ),
        }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if report.get("overall_status") in {"ok", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
