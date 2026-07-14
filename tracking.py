from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable

from database import AppError, now_text


KUAIDI100_ENDPOINT = "https://poll.kuaidi100.com/poll/query.do"
SIGNED_STATE = "3"
PROBLEM_STATE = "2"
PENDING_TRACE_MESSAGES = ("查询无结果", "暂无轨迹", "暂无物流", "未查询到物流", "没有物流信息")
EXPRESS_COMPANY_CODES = {
    "圆通": "yuantong",
    "顺丰": "shunfeng",
    "京东": "jd",
}


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def tracking_interval_minutes() -> int:
    raw = os.environ.get("SCENTPOOL_TRACKING_INTERVAL_MINUTES", "360").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 360
    return max(30, value)


def tracking_stale_before() -> str:
    return (datetime.fromisoformat(now_text()) - timedelta(minutes=tracking_interval_minutes())).isoformat(timespec="seconds")


def manual_refresh_stale_before() -> str:
    return (datetime.fromisoformat(now_text()) - timedelta(minutes=30)).isoformat(timespec="seconds")


def tracking_auto_enabled() -> bool:
    return env_flag("SCENTPOOL_TRACKING_AUTO")


def configured_provider() -> str:
    return os.environ.get("SCENTPOOL_TRACKING_PROVIDER", "kuaidi100").strip().lower() or "kuaidi100"


def mask_secret(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}...{value[-4:]}"


class Kuaidi100Client:
    def __init__(self, customer: str, key: str, endpoint: str = KUAIDI100_ENDPOINT):
        self.customer = customer.strip()
        self.key = key.strip()
        self.endpoint = endpoint.strip() or KUAIDI100_ENDPOINT

    @classmethod
    def from_env(cls) -> "Kuaidi100Client":
        return cls(
            os.environ.get("SCENTPOOL_KUAIDI100_CUSTOMER", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_KEY", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_ENDPOINT", KUAIDI100_ENDPOINT),
        )

    def is_configured(self) -> bool:
        return bool(self.customer and self.key)

    def query(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_configured():
            raise AppError("快递100接口未配置，请在 Render 环境变量中设置 SCENTPOOL_KUAIDI100_CUSTOMER 和 SCENTPOOL_KUAIDI100_KEY。", 503)

        express_company = str(shipment.get("express_company") or "").strip()
        shipper_code = EXPRESS_COMPANY_CODES.get(express_company)
        if not shipper_code:
            raise AppError(f"暂不支持这个快递公司：{express_company}")

        tracking_no = str(shipment.get("tracking_no") or "").strip()
        if not tracking_no:
            raise AppError("快递单号为空。")

        request_data: Dict[str, Any] = {
            "com": shipper_code,
            "num": tracking_no,
            "resultv2": "1",
            "show": "0",
            "order": "desc",
        }
        if shipper_code == "shunfeng":
            phone = str(shipment.get("phone") or shipment.get("sender_phone") or "").strip()
            if phone:
                request_data["phone"] = phone

        param = json.dumps(request_data, ensure_ascii=False, separators=(",", ":"))
        form = {
            "customer": self.customer,
            "sign": self.sign(param),
            "param": param,
        }
        poll_token = str(shipment.get("booking_poll_token") or "").strip()
        if poll_token:
            form["pollToken"] = poll_token
        payload = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            return tracking_error_result(f"快递100请求失败：{exc}", provider="kuaidi100")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return tracking_error_result("快递100返回内容不是有效 JSON。", provider="kuaidi100", raw=raw)
        return normalize_kuaidi100_response(data, raw)

    def sign(self, param: str) -> str:
        return hashlib.md5(f"{param}{self.key}{self.customer}".encode("utf-8")).hexdigest().upper()


def normalize_kuaidi100_response(data: Dict[str, Any], raw: str) -> Dict[str, Any]:
    checked_at = now_text()
    status = str(data.get("status") or "")
    state = str(data.get("state") or "")
    traces = data.get("data") or []
    if not isinstance(traces, list):
        traces = []
    last_trace = latest_trace(traces)
    last_event = trace_text(last_trace)
    is_signed = state == SIGNED_STATE or str(data.get("ischeck") or "") == "1"

    if status != "200":
        message = str(data.get("message") or data.get("result") or data.get("returnCode") or "快递100查询失败。")
        if status == "500" and any(text in message for text in PENDING_TRACE_MESSAGES):
            return tracking_pending_result(raw=raw, state=state)
        return tracking_error_result(message, provider="kuaidi100", raw=raw, state=state, last_event=last_event)

    if not traces and state in {"", "0"}:
        return tracking_pending_result(raw=raw, state=state)

    return {
        "provider": "kuaidi100",
        "tracking_status": state_label(state, last_event),
        "state_code": state,
        "last_event": last_event,
        "checked_at": checked_at,
        "signed_at": str(trace_value(last_trace, "ftime") or trace_value(last_trace, "time") or checked_at) if is_signed else "",
        "error": "",
        "raw": raw[:5000],
        "is_signed": is_signed,
    }


def tracking_pending_result(*, raw: str = "", state: str = "") -> Dict[str, Any]:
    return {
        "provider": "kuaidi100",
        "tracking_status": "等待揽收",
        "state_code": state,
        "last_event": "",
        "checked_at": now_text(),
        "signed_at": "",
        "error": "",
        "raw": raw[:5000],
        "is_signed": False,
    }


def tracking_error_result(message: str, *, provider: str, raw: str = "", state: str = "", last_event: str = "") -> Dict[str, Any]:
    return {
        "provider": provider,
        "tracking_status": "查询失败",
        "state_code": state,
        "last_event": last_event,
        "checked_at": now_text(),
        "signed_at": "",
        "error": message,
        "raw": raw[:5000],
        "is_signed": False,
    }


def latest_trace(traces: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    trace_list = [trace for trace in traces if isinstance(trace, dict)]
    if not trace_list:
        return {}
    return trace_list[0]


def trace_text(trace: Dict[str, Any]) -> str:
    if not trace:
        return ""
    time_text = str(trace_value(trace, "ftime") or trace_value(trace, "time") or "").strip()
    station = str(trace_value(trace, "context") or "").strip()
    return " ".join(part for part in [time_text, station] if part)


def trace_value(trace: Dict[str, Any], key: str) -> Any:
    return trace.get(key) or trace.get(key[:1].lower() + key[1:]) or trace.get(key.lower())


def state_label(state: str, last_event: str) -> str:
    if state == "0":
        return "运输中"
    if state == "1":
        return "已揽收"
    if state == PROBLEM_STATE:
        return "问题件"
    if state == SIGNED_STATE:
        return "已签收"
    if state == "4":
        return "退签"
    if state == "5":
        return "派件中"
    if state == "6":
        return "退回"
    if state == "7":
        return "转投"
    if state == "8":
        return "清关"
    if state == "14":
        return "拒签"
    return "运输中" if last_event else "待查询"


def tracking_client() -> Kuaidi100Client:
    provider = configured_provider()
    if provider != "kuaidi100":
        raise AppError(f"暂不支持物流服务商：{provider}")
    return Kuaidi100Client.from_env()


def query_tracking(shipment: Dict[str, Any]) -> Dict[str, Any]:
    return tracking_client().query(shipment)


def tracking_config_public() -> Dict[str, Any]:
    client = Kuaidi100Client.from_env()
    return {
        "provider": configured_provider(),
        "configured": client.is_configured(),
        "customer": mask_secret(client.customer),
        "endpoint": client.endpoint,
        "auto": tracking_auto_enabled(),
        "interval_minutes": tracking_interval_minutes(),
    }
