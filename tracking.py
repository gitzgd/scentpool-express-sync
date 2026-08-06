from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable

from database import AppError, now_text


KUAIDI100_ENDPOINT = "https://poll.kuaidi100.com/poll/query.do"
KUAIDI100_AUTODETECT_ENDPOINT = "https://www.kuaidi100.com/autonumber/auto"
SIGNED_STATE = "3"
PROBLEM_STATE = "2"
PENDING_TRACE_MESSAGES = ("查询无结果", "暂无轨迹", "暂无物流", "未查询到物流", "没有物流信息")
SYSTEM_HTTP_STATUSES = {401, 403, 408, 429, 500, 502, 503, 504}
SYSTEM_RESPONSE_STATUSES = {str(value) for value in SYSTEM_HTTP_STATUSES}
EXPRESS_COMPANY_CODES = {
    "圆通": "yuantong",
    "顺丰": "shunfeng",
    "京东": "jd",
}
EXPRESS_CODE_COMPANIES = {code: company for company, code in EXPRESS_COMPANY_CODES.items()}
COMPANY_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PHONE_REQUIRED_COMPANY_CODES = {"shunfeng", "shunfengkuaiyun", "zhongtong"}


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


def normalize_company_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    return code if COMPANY_CODE_PATTERN.fullmatch(code) else ""


class Kuaidi100Client:
    def __init__(
        self,
        customer: str,
        key: str,
        endpoint: str = KUAIDI100_ENDPOINT,
        autodetect_endpoint: str = KUAIDI100_AUTODETECT_ENDPOINT,
    ):
        self.customer = customer.strip()
        self.key = key.strip()
        self.endpoint = endpoint.strip() or KUAIDI100_ENDPOINT
        self.autodetect_endpoint = autodetect_endpoint.strip() or KUAIDI100_AUTODETECT_ENDPOINT

    @classmethod
    def from_env(cls) -> "Kuaidi100Client":
        return cls(
            os.environ.get("SCENTPOOL_KUAIDI100_CUSTOMER", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_KEY", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_ENDPOINT", KUAIDI100_ENDPOINT),
            os.environ.get("SCENTPOOL_KUAIDI100_AUTODETECT_ENDPOINT", KUAIDI100_AUTODETECT_ENDPOINT),
        )

    def is_configured(self) -> bool:
        return bool(self.customer and self.key)

    def detect_company(self, tracking_no: str) -> Dict[str, str]:
        if not self.key:
            raise tracking_service_app_error(
                message="快递100智能识别未配置，请管理员检查接口密钥。"
            )

        tracking_no = str(tracking_no or "").strip()
        if not tracking_no:
            raise AppError("请输入退货快递单号。")

        query = urllib.parse.urlencode({"num": tracking_no, "key": self.key})
        request = urllib.request.Request(f"{self.autodetect_endpoint}?{query}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if int(exc.code or 0) in SYSTEM_HTTP_STATUSES:
                raise tracking_service_app_error(int(exc.code or 0)) from exc
            raise AppError("快递100智能识别请求未成功，请稍后重试。", 503) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise tracking_service_app_error() from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise tracking_service_app_error(
                message="快递100智能识别返回异常，系统已暂停本轮查询。"
            ) from exc

        if isinstance(data, dict):
            code = str(data.get("returnCode") or "").strip()
            message = str(data.get("message") or "").strip()
            if code in {"601", "701"}:
                raise tracking_service_app_error(
                    message="快递100账号尚未开通智能单号识别，请管理员检查接口权限。"
                )
            if code == "201":
                raise AppError("快递100无法识别这个单号，请核对单号是否完整、准确。")
            detail = message[:160] if message else "没有返回候选快递公司"
            raise AppError(f"快递100暂时无法识别快递公司：{detail}。请核对单号后重试。")

        if not isinstance(data, list) or not data:
            raise AppError("快递100暂时无法识别该单号对应的快递公司，请核对单号是否完整；确认无误后稍后重试。")

        candidate = data[0] if isinstance(data[0], dict) else {}
        company_code = normalize_company_code(candidate.get("comCode"))
        provider_name = str(candidate.get("name") or company_code or "未知快递").strip()[:80]
        if not company_code:
            raise AppError("快递100没有返回有效的快递公司编码，请核对单号后稍后重试。")
        company = EXPRESS_CODE_COMPANIES.get(company_code) or provider_name
        return {
            "express_company": company,
            "company_code": company_code,
            "provider_name": provider_name,
        }

    def query(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_configured():
            raise tracking_service_app_error(
                message="快递100接口配置不完整，系统已暂停批量查询，请管理员检查生产环境配置。"
            )

        express_company = str(shipment.get("express_company") or "").strip()
        shipper_code = normalize_company_code(shipment.get("express_company_code"))
        if not shipper_code:
            shipper_code = EXPRESS_COMPANY_CODES.get(express_company, "")
        if not shipper_code:
            company_label = express_company or "未知快递公司"
            raise AppError(f"缺少“{company_label}”对应的快递100公司编码，请重新识别快递公司后再查询。")

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
        phone = str(shipment.get("phone") or shipment.get("sender_phone") or "").strip()
        if phone and shipper_code in PHONE_REQUIRED_COMPANY_CODES:
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
        except urllib.error.HTTPError as exc:
            if int(exc.code or 0) in SYSTEM_HTTP_STATUSES:
                return tracking_service_error_result(http_status=int(exc.code or 0))
            return tracking_error_result("快递100查询请求未成功，请稍后重试。", provider="kuaidi100")
        except (urllib.error.URLError, TimeoutError) as exc:
            return tracking_service_error_result()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return tracking_service_error_result("快递100返回内容异常，系统已暂停本轮批量查询。")
        if not isinstance(data, dict):
            return tracking_service_error_result("快递100返回格式异常，系统已暂停本轮批量查询。")
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
        if status in SYSTEM_RESPONSE_STATUSES:
            detail = message.strip()[:120]
            return tracking_service_error_result(
                f"快递100接口返回系统错误（状态 {status}）{f'：{detail}' if detail else ''}。"
                "系统已停止本轮批量查询并将在下一轮自动重试。",
                http_status=int(status),
            )
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


def tracking_service_message(http_status: int = 0) -> str:
    if http_status == 403:
        return (
            "快递100接口暂时拒绝访问（HTTP 403）。这不是单个快递单号错误；"
            "系统已停止本轮批量查询，请管理员检查接口权限、账号状态或访问限制。"
        )
    if http_status == 429:
        return "快递100接口当前请求过多，系统已停止本轮批量查询并将在下一轮自动重试。"
    if http_status:
        return f"快递100接口暂时不可用（HTTP {http_status}），系统已停止本轮批量查询并将在下一轮自动重试。"
    return "快递100接口暂时无法连接，系统已停止本轮批量查询并将在下一轮自动重试。"


def tracking_service_error_result(message: str = "", *, http_status: int = 0) -> Dict[str, Any]:
    result = tracking_error_result(
        message or tracking_service_message(http_status),
        provider="kuaidi100",
    )
    result.update(
        {
            "system_error": True,
            "error_scope": "tracking_provider",
            "http_status": int(http_status or 0),
            "provider_reached": False,
        }
    )
    return result


def tracking_service_app_error(http_status: int = 0, *, message: str = "") -> AppError:
    return AppError(
        message or tracking_service_message(http_status),
        503,
        {
            "tracking_service_error": True,
            "provider": "kuaidi100",
            "http_status": int(http_status or 0),
        },
    )


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


def detect_tracking_company(tracking_no: str) -> Dict[str, str]:
    return tracking_client().detect_company(tracking_no)


def tracking_config_public() -> Dict[str, Any]:
    client = Kuaidi100Client.from_env()
    return {
        "provider": configured_provider(),
        "configured": client.is_configured(),
        "customer": mask_secret(client.customer),
        "endpoint": client.endpoint,
        "autodetect_endpoint": client.autodetect_endpoint,
        "auto": tracking_auto_enabled(),
        "interval_minutes": tracking_interval_minutes(),
    }
