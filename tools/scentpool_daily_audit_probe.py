#!/usr/bin/env python3
"""Fixed, read-only daily audit collector for Scentpool production.

The executable loads both credentials from macOS Keychain. Responses contain only
aggregate counts, fixed categories, and service-level metrics; raw logs are never
printed or persisted.
"""

from __future__ import annotations

import json
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
from typing import Any, Dict, Iterable, Optional
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
                    context=ssl.create_default_context(),
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


def metric_summary(result: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
    if result.get("status") != "ok":
        return without_data(result)
    series = result.get("data")
    if not isinstance(series, list):
        return schema_error(result, "指标响应不再是时间序列数组。")
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
                )
            return {
                **without_data(last_result),
                "total_logs": total_logs,
                "categories": counts,
                "request_logs": request_logs,
                "http_5xx_request_logs": http_5xx,
                "pagination_complete": True,
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
        "date", "timezone", "metrics", "by_store", "exceptions", "historical_end_of_day",
        "failures", "completeness", "long_waiting", "recent_7_day_average", "data_quality", "basis",
    )
    if data.get("date") is None or data.get("timezone") is None:
        return schema_error(result, "日报响应缺少日期或时区。")
    return {**without_data(result), "result": {key: data.get(key) for key in allowed}}


def collect_report(
    date_text: str,
    *,
    audit_token: str,
    render_token: str,
    client: Optional[JsonClient] = None,
) -> Dict[str, Any]:
    requested_date([date_text])
    http = client or JsonClient()
    start_time, end_time = day_window(date_text)
    audit_headers = {"Authorization": f"Bearer {audit_token}"}
    render_headers = {"Authorization": f"Bearer {render_token}"}

    health = app_summary(http.fetch(f"{BASE_URL}/api/health"))
    daily_url = f"{BASE_URL}/api/admin/system/daily-audit?{urllib.parse.urlencode({'date': date_text})}"
    daily = app_summary(http.fetch(daily_url, audit_headers), daily=True)
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

        logs = collect_logs(
            http,
            owner_id=owner_id,
            start_time=start_time,
            end_time=end_time,
            headers=render_headers,
        )
        metric_params: Dict[str, Any] = {
            "startTime": start_time,
            "endTime": end_time,
            "resolutionSeconds": 300,
            "resource": RENDER_SERVICE_ID,
        }
        memory = metric_summary(
            http.fetch(render_url("/metrics/memory", metric_params), render_headers), mode="resource"
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
        latency_params = {**metric_params, "quantile": [0.5, 0.9, 0.99]}
        http_latency = metric_summary(
            http.fetch(render_url("/metrics/http-latency", latency_params), render_headers), mode="series"
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

    sections = [
        health, daily, render_service, deploys, events, logs, memory, memory_limit,
        disk_usage, disk_capacity, http_requests, http_latency,
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
        "render_service": render_service,
        "render_deploys": deploys,
        "render_events": events,
        "render_logs": logs,
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
