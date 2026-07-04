from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from database import AppError, now_text


KDNIAO_ENDPOINT = "https://api.kdniao.com/api/dist"
KDNIAO_REQUEST_TYPE_TRACK = "8002"
KDNIAO_DATA_TYPE = "2"
SIGNED_STATE = "3"
PROBLEM_STATE = "4"
EXPRESS_COMPANY_CODES = {
    "圆通": "YTO",
    "顺丰": "SF",
    "京东": "JD",
}


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def tracking_interval_minutes() -> int:
    raw = os.environ.get("SCENTPOOL_TRACKING_INTERVAL_MINUTES", "1440").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 1440
    return max(30, value)


def tracking_stale_before() -> str:
    return (datetime.now().astimezone() - timedelta(minutes=tracking_interval_minutes())).isoformat(timespec="seconds")


def tracking_auto_enabled() -> bool:
    return env_flag("SCENTPOOL_TRACKING_AUTO")


def configured_provider() -> str:
    return os.environ.get("SCENTPOOL_TRACKING_PROVIDER", "kdniao").strip().lower() or "kdniao"


def kdniao_request_type() -> str:
    return os.environ.get("SCENTPOOL_KDNIAO_REQUEST_TYPE", KDNIAO_REQUEST_TYPE_TRACK).strip() or KDNIAO_REQUEST_TYPE_TRACK


def mask_secret(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}...{value[-4:]}"


def secret_diagnostics(raw_value: Optional[str], *, expose_trimmed_value: bool = False) -> Dict[str, Any]:
    raw = "" if raw_value is None else str(raw_value)
    trimmed = raw.strip()
    info: Dict[str, Any] = {
        "present": bool(raw_value),
        "masked": mask_secret(trimmed),
        "raw_length": len(raw),
        "trimmed_length": len(trimmed),
        "leading_or_trailing_whitespace": raw != trimmed,
        "contains_whitespace": any(ch.isspace() for ch in raw),
        "contains_non_ascii": any(ord(ch) > 127 for ch in raw),
        "sha256": hashlib.sha256(trimmed.encode("utf-8")).hexdigest() if trimmed else "",
    }
    if expose_trimmed_value:
        info["trimmed_value"] = trimmed
    return info


def app_key_format_info(value: str) -> Dict[str, Any]:
    trimmed = value.strip()
    uuid_like = (
        len(trimmed) == 36
        and trimmed[8:9] == "-"
        and trimmed[13:14] == "-"
        and trimmed[18:19] == "-"
        and trimmed[23:24] == "-"
        and all(ch.isdigit() or ch.lower() in "abcdef-" for ch in trimmed)
    )
    return {"looks_uuid": uuid_like}


class KdniaoClient:
    def __init__(self, business_id: str, app_key: str, endpoint: str = KDNIAO_ENDPOINT):
        self.business_id = business_id.strip()
        self.app_key = app_key.strip()
        self.endpoint = endpoint.strip() or KDNIAO_ENDPOINT

    @classmethod
    def from_env(cls) -> "KdniaoClient":
        return cls(
            os.environ.get("SCENTPOOL_KDNIAO_EBUSINESS_ID", ""),
            os.environ.get("SCENTPOOL_KDNIAO_APP_KEY", ""),
            os.environ.get("SCENTPOOL_KDNIAO_ENDPOINT", KDNIAO_ENDPOINT),
        )

    def is_configured(self) -> bool:
        return bool(self.business_id and self.app_key)

    def query(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_configured():
            raise AppError("快递鸟接口未配置，请在 Render 环境变量中设置 SCENTPOOL_KDNIAO_EBUSINESS_ID 和 SCENTPOOL_KDNIAO_APP_KEY。", 503)

        express_company = str(shipment.get("express_company") or "").strip()
        shipper_code = EXPRESS_COMPANY_CODES.get(express_company)
        if not shipper_code:
            raise AppError(f"暂不支持这个快递公司：{express_company}")

        tracking_no = str(shipment.get("tracking_no") or "").strip()
        if not tracking_no:
            raise AppError("快递单号为空。")

        request_data: Dict[str, Any] = {
            "LogisticCode": tracking_no,
        }
        if shipper_code == "SF":
            phone_tail = phone_last_four(str(shipment.get("phone") or shipment.get("sender_phone") or ""))
            if phone_tail:
                request_data["CustomerName"] = phone_tail

        request_json = json.dumps(request_data, ensure_ascii=False, separators=(",", ":"))
        payload = urllib.parse.urlencode(
            {
                "RequestData": request_json,
                "EBusinessID": self.business_id,
                "RequestType": kdniao_request_type(),
                "DataSign": self.data_sign(request_json),
                "DataType": KDNIAO_DATA_TYPE,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            return tracking_error_result(f"快递鸟请求失败：{exc}", provider="kdniao")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return tracking_error_result("快递鸟返回内容不是有效 JSON。", provider="kdniao", raw=raw)
        return normalize_kdniao_response(data, raw)

    def data_sign(self, request_json: str) -> str:
        digest = hashlib.md5(f"{request_json}{self.app_key}".encode("utf-8")).digest()
        return base64.b64encode(digest).decode("utf-8")


def phone_last_four(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


def normalize_kdniao_response(data: Dict[str, Any], raw: str) -> Dict[str, Any]:
    checked_at = now_text()
    success = bool(data.get("Success"))
    state = str(data.get("State") or "")
    traces = data.get("Traces") or []
    if not isinstance(traces, list):
        traces = []
    last_trace = latest_trace(traces)
    last_event = trace_text(last_trace)
    if not success:
        return tracking_error_result(str(data.get("Reason") or data.get("ErrorMessage") or "快递鸟查询失败。"), provider="kdniao", raw=raw, state=state, last_event=last_event)

    tracking_status = state_label(state, last_event)
    signed_at = ""
    if state == SIGNED_STATE:
        signed_at = str(trace_value(last_trace, "AcceptTime") or checked_at) if last_trace else checked_at
    return {
        "provider": "kdniao",
        "tracking_status": tracking_status,
        "state_code": state,
        "last_event": last_event,
        "checked_at": checked_at,
        "signed_at": signed_at,
        "error": "",
        "raw": raw[:5000],
        "is_signed": state == SIGNED_STATE,
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
    return trace_list[-1]


def trace_text(trace: Dict[str, Any]) -> str:
    if not trace:
        return ""
    time_text = str(trace_value(trace, "AcceptTime") or "").strip()
    station = str(trace_value(trace, "AcceptStation") or "").strip()
    return " ".join(part for part in [time_text, station] if part)


def trace_value(trace: Dict[str, Any], key: str) -> Any:
    return trace.get(key) or trace.get(key[:1].lower() + key[1:]) or trace.get(key.lower())


def state_label(state: str, last_event: str) -> str:
    if state == "0":
        return "无轨迹"
    if state == "1":
        return "已揽收"
    if state == "2":
        return "运输中"
    if state == SIGNED_STATE:
        return "已签收"
    if state == PROBLEM_STATE:
        return "问题件"
    if state == "5":
        return "转寄"
    return "运输中" if last_event else "待查询"


def tracking_client() -> KdniaoClient:
    provider = configured_provider()
    if provider != "kdniao":
        raise AppError(f"暂不支持物流服务商：{provider}")
    return KdniaoClient.from_env()


def query_tracking(shipment: Dict[str, Any]) -> Dict[str, Any]:
    return tracking_client().query(shipment)


def tracking_config_public() -> Dict[str, Any]:
    client = KdniaoClient.from_env()
    return {
        "provider": configured_provider(),
        "configured": client.is_configured(),
        "business_id": mask_secret(client.business_id),
        "endpoint": client.endpoint,
        "request_type": kdniao_request_type(),
        "auto": tracking_auto_enabled(),
        "interval_minutes": tracking_interval_minutes(),
    }


def tracking_env_diagnostics(*, reveal_secrets: bool = False) -> Dict[str, Any]:
    business_id_raw = os.environ.get("SCENTPOOL_KDNIAO_EBUSINESS_ID")
    app_key_raw = os.environ.get("SCENTPOOL_KDNIAO_APP_KEY")
    business_id = "" if business_id_raw is None else business_id_raw.strip()
    app_key = "" if app_key_raw is None else app_key_raw.strip()
    result = {
        "provider": configured_provider(),
        "endpoint": os.environ.get("SCENTPOOL_KDNIAO_ENDPOINT", KDNIAO_ENDPOINT).strip() or KDNIAO_ENDPOINT,
        "request_type": kdniao_request_type(),
        "data_type": KDNIAO_DATA_TYPE,
        "secrets_revealed": reveal_secrets,
        "business_id": {
            **secret_diagnostics(business_id_raw, expose_trimmed_value=True),
            "digits_only": business_id.isdigit() if business_id else False,
        },
        "app_key": {
            **secret_diagnostics(app_key_raw, expose_trimmed_value=reveal_secrets),
            **app_key_format_info(app_key),
        },
    }
    if reveal_secrets:
        result["business_id"]["raw_value"] = "" if business_id_raw is None else business_id_raw
        result["app_key"]["raw_value"] = "" if app_key_raw is None else app_key_raw
    return result
