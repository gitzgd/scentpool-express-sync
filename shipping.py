from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from database import AppError
from tracking import EXPRESS_COMPANY_CODES, mask_secret


KUAIDI100_LABEL_ENDPOINT = "https://api.kuaidi100.com/label/order"
KUAIDI100_AUTH_ENDPOINT = "https://poll.kuaidi100.com/printapi/authThird.do"
KUAIDI100_THIRD_INFO_ENDPOINT = "https://poll.kuaidi100.com/eorderapi.do"
THIRD_PARTY_NETS = {"taobao", "cainiao", "jdalpha", "pinduoduoWx", "douyin", "kuaishou", "weipinhui", "xiaohongshu"}
LABEL_CARGO_MAX_CHARS = 50
LABEL_REMARK_MAX_CHARS = 100
LABEL_ITEM_KEYWORD_MAX_CHARS = 8


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def label_enabled() -> bool:
    return env_flag("SCENTPOOL_KUAIDI100_LABEL_ENABLED")


def public_url(path: str) -> str:
    base = os.environ.get("SCENTPOOL_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


def print_callback_url() -> str:
    return public_url("/api/integrations/kuaidi100/label-print-callback")


def auth_callback_url(state: str) -> str:
    return public_url(f"/api/integrations/kuaidi100/label-auth-callback?state={urllib.parse.quote(state)}")


def compact_label_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return f"{text[:max_chars - 1]}…"


def clean_label_product_name(value: Any, category: Any) -> str:
    name = " ".join(str(value or "").split()).strip()
    category_text = " ".join(str(category or "商品").split()).strip() or "商品"
    while True:
        match = re.match(r"^[（(]\s*([^）)]+)\s*[）)]\s*", name)
        if not match:
            break
        prefix = match.group(1).strip()
        if prefix in category_text or category_text in prefix:
            name = name[match.end():].strip()
            continue
        break
    line_incense = re.fullmatch(r"(?:灵气)?线香[（(](.+)[）)]", name)
    if category_text == "线香" and line_incense:
        name = line_incense.group(1).strip()
    return name or category_text


def abbreviate_label_product_name(name: str, category: str, max_chars: int = LABEL_ITEM_KEYWORD_MAX_CHARS) -> str:
    keyword = re.split(r"\s*(?:与|和|及|&|＋|\+|/|、)\s*", name, maxsplit=1)[0].strip()
    suffixes = [category, "睡眠喷雾", "喷雾", "香氛蜡烛", "蜡烛", "手串", "项链", "香包", "线香", "系列"]
    for suffix in suffixes:
        if suffix and keyword.endswith(suffix) and len(keyword) > len(suffix):
            keyword = keyword[:-len(suffix)].strip()
            break
    return (keyword or name)[:max_chars]


def format_grouped_label_items(products: list[tuple[str, str, int]], keyword_chars: int | None = None) -> str:
    grouped: Dict[str, list[str]] = {}
    for category, name, quantity in products:
        display_name = name if keyword_chars is None else abbreviate_label_product_name(name, category, keyword_chars)
        grouped.setdefault(category, []).append(f"{display_name}*{quantity}")
    return "\n".join(f"【{category}】{'、'.join(entries)}" for category, entries in grouped.items())


def build_label_item_summary(items: Any, max_chars: int) -> str:
    if not isinstance(items, list) or max_chars <= 0:
        return ""
    products = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        category = compact_label_text(item.get("product_category") or "商品", 12)
        name = clean_label_product_name(
            item.get("product_name") or item.get("product_barcode") or category,
            category,
        )
        products.append((category, name, quantity))
    if not products:
        return ""

    full_summary = format_grouped_label_items(products)
    if len(full_summary) <= max_chars:
        return full_summary

    shortest_summary = full_summary
    for keyword_chars in range(LABEL_ITEM_KEYWORD_MAX_CHARS, 0, -1):
        shortest_summary = format_grouped_label_items(products, keyword_chars)
        if len(shortest_summary) <= max_chars:
            return shortest_summary
    return shortest_summary


def build_label_remark(shipment: Dict[str, Any]) -> str:
    manual_remark = compact_label_text(shipment.get("remark"), LABEL_REMARK_MAX_CHARS)
    item_budget = 80 if manual_remark else LABEL_REMARK_MAX_CHARS
    item_summary = build_label_item_summary(shipment.get("items"), item_budget)
    if not item_summary:
        return manual_remark
    if not manual_remark:
        return item_summary
    prefix = f"{item_summary}；备注："
    remaining = LABEL_REMARK_MAX_CHARS - len(prefix)
    if remaining <= 0:
        return compact_label_text(item_summary, LABEL_REMARK_MAX_CHARS)
    return f"{prefix}{compact_label_text(manual_remark, remaining)}"


class Kuaidi100LabelClient:
    def __init__(
        self,
        key: str,
        secret: str,
        endpoint: str = KUAIDI100_LABEL_ENDPOINT,
        auth_endpoint: str = KUAIDI100_AUTH_ENDPOINT,
        third_info_endpoint: str = KUAIDI100_THIRD_INFO_ENDPOINT,
    ):
        self.key = key.strip()
        self.secret = secret.strip()
        self.endpoint = endpoint.strip() or KUAIDI100_LABEL_ENDPOINT
        self.auth_endpoint = auth_endpoint.strip() or KUAIDI100_AUTH_ENDPOINT
        self.third_info_endpoint = third_info_endpoint.strip() or KUAIDI100_THIRD_INFO_ENDPOINT

    @classmethod
    def from_env(cls) -> "Kuaidi100LabelClient":
        return cls(
            os.environ.get("SCENTPOOL_KUAIDI100_KEY", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_LABEL_SECRET", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_LABEL_ENDPOINT", KUAIDI100_LABEL_ENDPOINT),
            os.environ.get("SCENTPOOL_KUAIDI100_AUTH_ENDPOINT", KUAIDI100_AUTH_ENDPOINT),
            os.environ.get("SCENTPOOL_KUAIDI100_THIRD_INFO_ENDPOINT", KUAIDI100_THIRD_INFO_ENDPOINT),
        )

    def is_configured(self) -> bool:
        return bool(self.key and self.secret and public_url("/"))

    def _post(self, endpoint: str, method: str, param: Dict[str, Any], *, require_enabled: bool = True) -> Dict[str, Any]:
        if require_enabled and not label_enabled():
            raise AppError("电子面单尚未开启。请先完成测试并设置 SCENTPOOL_KUAIDI100_LABEL_ENABLED=1。", 503)
        if not self.is_configured():
            raise AppError("快递100电子面单未配置，请检查 KEY、LABEL_SECRET 和 PUBLIC_BASE_URL。", 503)
        param_text = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{param_text}{timestamp}{self.key}{self.secret}".encode("utf-8")).hexdigest().upper()
        body = {"key": self.key, "sign": sign, "t": timestamp, "param": param_text}
        if method:
            body["method"] = method
        payload = urllib.parse.urlencode(body).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"success": False, "error": f"快递100请求失败：{exc}", "raw": ""}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"success": False, "error": "快递100返回内容不是有效 JSON。", "raw": raw}
        return {"success": True, "data": data, "raw": raw, "param": param_text}

    @staticmethod
    def _account_param(settings: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: str(settings.get(key) or "").strip()
            for key in ("partnerId", "partnerKey", "partnerSecret", "partnerName", "net", "tbNet", "code", "checkMan")
            if str(settings.get(key) or "").strip()
        }

    def create_label(self, shipment: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        company = str(shipment.get("express_company") or "").strip()
        company_code = EXPRESS_COMPANY_CODES.get(company)
        if not company_code:
            return {"success": False, "error": f"暂不支持这个快递公司：{company}", "raw": ""}
        if not settings.get("partnerId") or not settings.get("partnerKey"):
            return {"success": False, "error": "请先在发货设置中完成菜鸟电子面单授权。", "raw": ""}

        print_mode = str(settings.get("print_mode") or "PDF")
        account_net = str(settings.get("net") or "")
        api_print_type = "CLOUD" if print_mode == "CLOUD" and account_net not in THIRD_PARTY_NETS else "IMAGE"
        item_cargo = build_label_item_summary(shipment.get("items"), LABEL_CARGO_MAX_CHARS)
        param: Dict[str, Any] = {
            **self._account_param(settings),
            "printType": api_print_type,
            "kuaidicom": company_code,
            "recMan": {
                "name": str(shipment.get("recipient_name") or ""),
                "mobile": str(shipment.get("phone") or ""),
                "printAddr": str(shipment.get("address") or ""),
            },
            "sendMan": {
                "name": str(settings.get("sender_name") or ""),
                "mobile": str(settings.get("sender_mobile") or ""),
                "printAddr": str(settings.get("sender_address") or ""),
                "company": str(settings.get("sender_company") or ""),
            },
            "cargo": item_cargo or str(settings.get("cargo_name") or "香氛商品"),
            "count": 1,
            "payType": str(settings.get("pay_type") or "MONTHLY"),
            "expType": str(settings.get("exp_type") or "标准快递"),
            "remark": build_label_remark(shipment),
            "orderId": str(shipment.get("booking_request_id") or shipment.get("business_id") or "")[:32],
            "reorder": False,
            "callBackUrl": print_callback_url(),
            "salt": str(shipment.get("booking_salt") or ""),
            "needSubscribe": False,
            "needDesensitization": bool(settings.get("need_desensitization")),
            "needLogo": bool(settings.get("need_logo")),
        }
        if settings.get("printer_siid"):
            param["siid"] = str(settings["printer_siid"])
        if settings.get("third_template_url") and account_net in THIRD_PARTY_NETS:
            param["thirdTemplateURL"] = str(settings["third_template_url"])
        elif settings.get("template_id") and account_net not in THIRD_PARTY_NETS:
            param["tempId"] = str(settings["template_id"])
        if api_print_type == "CLOUD":
            param["width"] = str(settings.get("paper_width") or "100")
            param["height"] = str(settings.get("paper_height") or "180")

        response = self._post(self.endpoint, "order", param)
        if not response.get("success"):
            return response
        payload = response["data"]
        if not bool(payload.get("success")) or str(payload.get("code")) not in {"200", "30011"}:
            return {
                "success": False,
                "error": str(payload.get("message") or payload.get("code") or "电子面单下单失败"),
                "raw": response["raw"],
            }
        result = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        tracking_no = str(result.get("kuaidinum") or "").strip()
        task_id = str(result.get("taskId") or "").strip()
        if not tracking_no or not task_id:
            return {"success": False, "error": "电子面单响应缺少快递单号或 taskId。", "raw": response["raw"]}
        return {
            "success": True,
            "task_id": task_id,
            "tracking_no": tracking_no,
            "label_url": str(result.get("label") or ""),
            "child_no": str(result.get("childNum") or ""),
            "return_no": str(result.get("returnNum") or ""),
            "carrier_order_no": str(result.get("kdComOrderNum") or ""),
            "print_type": api_print_type,
            "print_status": "打印中" if api_print_type == "CLOUD" else "待打印",
            "raw": response["raw"],
            "error": "",
        }

    def cancel_label(self, shipment: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        company_code = EXPRESS_COMPANY_CODES.get(str(shipment.get("express_company") or ""))
        param = {
            **self._account_param(settings),
            "kuaidicom": company_code or "",
            "kuaidinum": str(shipment.get("tracking_no") or ""),
            "orderId": str(shipment.get("label_carrier_order_no") or ""),
            "reason": "订单信息需要修改",
            "expType": str(settings.get("exp_type") or "标准快递"),
        }
        response = self._post(self.endpoint, "cancel", {key: value for key, value in param.items() if value})
        if not response.get("success"):
            return response
        payload = response["data"]
        if not bool(payload.get("success")):
            return {"success": False, "error": str(payload.get("message") or "电子面单取消失败"), "raw": response["raw"]}
        return {"success": True, "raw": response["raw"], "error": ""}

    def reprint(self, task_id: str, siid: str = "") -> Dict[str, Any]:
        param = {"taskId": task_id}
        if siid:
            param["siid"] = siid
        response = self._post(self.endpoint, "printOld", param)
        if not response.get("success"):
            return response
        payload = response["data"]
        if not bool(payload.get("success")):
            return {"success": False, "error": str(payload.get("message") or "电子面单复打失败"), "raw": response["raw"]}
        return {"success": True, "raw": response["raw"], "error": ""}

    def begin_cainiao_authorization(self, state: str, partner_id: str = "") -> Dict[str, Any]:
        param = {"net": "cainiao", "callBackUrl": auth_callback_url(state), "view": "web"}
        if partner_id:
            param["partnerId"] = partner_id
        response = self._post(self.auth_endpoint, "", param, require_enabled=False)
        if not response.get("success"):
            return response
        payload = response["data"]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = str(payload.get("returnCode") or "")
        if code == "201" and data.get("url"):
            return {"success": True, "authorized": False, "url": str(data["url"]), "raw": response["raw"]}
        if code == "200":
            return {"success": True, "authorized": True, "credentials": normalize_auth_credentials(data), "raw": response["raw"]}
        return {"success": False, "error": str(payload.get("message") or "无法创建菜鸟授权链接"), "raw": response["raw"]}

    def get_third_info(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        param = {
            "partnerId": str(credentials.get("partnerId") or ""),
            "partnerKey": str(credentials.get("partnerKey") or ""),
            "net": str(credentials.get("net") or "cainiao"),
        }
        response = self._post(self.third_info_endpoint, "getThirdInfo", param, require_enabled=False)
        if not response.get("success"):
            return response
        payload = response["data"]
        if not bool(payload.get("result")) or str(payload.get("status")) != "200":
            return {"success": False, "error": str(payload.get("message") or "查询面单余额失败"), "raw": response["raw"]}
        return {"success": True, "branches": payload.get("data") or [], "raw": response["raw"]}


def normalize_auth_credentials(data: Dict[str, Any]) -> Dict[str, str]:
    return {
        "partnerId": str(data.get("partnerId") or data.get("parterId") or ""),
        "partnerKey": str(data.get("partnerKey") or ""),
        "partnerSecret": str(data.get("partnerSecret") or ""),
        "partnerName": str(data.get("partnerName") or ""),
        "net": str(data.get("net") or "cainiao"),
        "code": str(data.get("code") or ""),
        "checkMan": str(data.get("checkMan") or ""),
    }


def parse_auth_callback(param_raw: str) -> Dict[str, Any]:
    try:
        outer = json.loads(param_raw)
    except json.JSONDecodeError as exc:
        raise AppError("菜鸟授权回调不是有效 JSON。") from exc
    if not bool(outer.get("result")) or str(outer.get("returnCode")) != "200":
        raise AppError(str(outer.get("message") or "菜鸟授权失败。"), 400)
    message = outer.get("message")
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except json.JSONDecodeError as exc:
            raise AppError("菜鸟授权凭证格式不正确。") from exc
    if not isinstance(message, dict):
        raise AppError("菜鸟授权回调缺少凭证。")
    credentials = normalize_auth_credentials(message)
    if not credentials["partnerId"] or not credentials["partnerKey"]:
        raise AppError("菜鸟授权回调缺少 partnerId 或 partnerKey。")
    return credentials


def label_config_public() -> Dict[str, Any]:
    client = Kuaidi100LabelClient.from_env()
    base_url = public_url("")
    missing = []
    if not client.key:
        missing.append("SCENTPOOL_KUAIDI100_KEY")
    if not client.secret:
        missing.append("SCENTPOOL_KUAIDI100_LABEL_SECRET")
    if not base_url:
        missing.append("SCENTPOOL_PUBLIC_BASE_URL")
    return {
        "enabled": label_enabled(),
        "configured": not missing,
        "ready": label_enabled() and not missing,
        "missing": missing,
        "key_configured": bool(client.key),
        "secret_configured": bool(client.secret),
        "public_base_url_configured": bool(base_url),
        "endpoint": client.endpoint,
        "key": mask_secret(client.key),
        "secret": mask_secret(client.secret),
        "public_base_url": base_url,
        "supports": {"cainiao_pdf": True, "kuaidi100_cloud": True, "reprint": True},
    }


def verify_callback_signature(param_raw: str, sign: str, salt: str) -> bool:
    expected = hashlib.md5(f"{param_raw}{salt}".encode("utf-8")).hexdigest()
    return bool(sign and salt) and expected.lower() == sign.strip().lower()
