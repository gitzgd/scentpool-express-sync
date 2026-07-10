from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from database import AppError
from tracking import EXPRESS_COMPANY_CODES, mask_secret


KUAIDI100_ORDER_ENDPOINT = "https://order.kuaidi100.com/order/corderapi.do"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def order_enabled() -> bool:
    return env_flag("SCENTPOOL_KUAIDI100_ORDER_ENABLED")


def callback_url() -> str:
    base = os.environ.get("SCENTPOOL_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return f"{base}/api/integrations/kuaidi100/order-callback" if base else ""


class Kuaidi100OrderClient:
    def __init__(self, key: str, secret: str, endpoint: str = KUAIDI100_ORDER_ENDPOINT):
        self.key = key.strip()
        self.secret = secret.strip()
        self.endpoint = endpoint.strip() or KUAIDI100_ORDER_ENDPOINT

    @classmethod
    def from_env(cls) -> "Kuaidi100OrderClient":
        return cls(
            os.environ.get("SCENTPOOL_KUAIDI100_KEY", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_ORDER_SECRET", ""),
            os.environ.get("SCENTPOOL_KUAIDI100_ORDER_ENDPOINT", KUAIDI100_ORDER_ENDPOINT),
        )

    def is_configured(self) -> bool:
        return bool(self.key and self.secret and callback_url())

    def _request(self, method: str, param: Dict[str, Any]) -> Dict[str, Any]:
        if not order_enabled():
            raise AppError("快递一键下单尚未开启。请先完成测试并设置 SCENTPOOL_KUAIDI100_ORDER_ENABLED=1。", 503)
        if not self.is_configured():
            raise AppError("快递100下单未配置，请检查 KEY、ORDER_SECRET 和 PUBLIC_BASE_URL。", 503)
        param_text = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{param_text}{timestamp}{self.key}{self.secret}".encode("utf-8")).hexdigest().upper()
        payload = urllib.parse.urlencode(
            {"method": method, "key": self.key, "sign": sign, "t": timestamp, "param": param_text}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"success": False, "error": f"快递100下单请求失败：{exc}", "raw": ""}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"success": False, "error": "快递100下单返回内容不是有效 JSON。", "raw": raw}
        if not bool(data.get("result")):
            message = str(data.get("message") or data.get("returnCode") or "快递100下单失败")
            return {"success": False, "error": message, "raw": raw}
        result = data.get("data") if isinstance(data.get("data"), dict) else {}
        if method == "cancel":
            return {"success": True, "raw": raw, "error": ""}
        task_id = str(result.get("taskId") or "")
        order_id = str(result.get("orderId") or result.get("orderId ") or "")
        if not task_id or not order_id:
            return {"success": False, "error": "快递100下单成功响应缺少 taskId 或 orderId。", "raw": raw}
        return {
            "success": True,
            "task_id": task_id,
            "order_id": order_id,
            "tracking_no": str(result.get("kuaidinum") or ""),
            "poll_token": str(result.get("pollToken") or ""),
            "raw": raw,
            "error": "",
        }

    def create_order(self, shipment: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        company = str(shipment.get("express_company") or "").strip()
        company_code = EXPRESS_COMPANY_CODES.get(company)
        if not company_code:
            return {"success": False, "error": f"暂不支持这个快递公司：{company}", "raw": ""}
        param: Dict[str, Any] = {
            "kuaidicom": company_code,
            "recManName": str(shipment.get("recipient_name") or ""),
            "recManMobile": str(shipment.get("phone") or ""),
            "recManPrintAddr": str(shipment.get("address") or ""),
            "sendManName": str(settings.get("sender_name") or ""),
            "sendManMobile": str(settings.get("sender_mobile") or ""),
            "sendManPrintAddr": str(settings.get("sender_address") or ""),
            "callBackUrl": callback_url(),
            "cargo": str(settings.get("cargo_name") or "香氛商品"),
            "payment": "SHIPPER",
            "dayType": str(shipment.get("pickup_day") or "今天"),
            "pickupStartTime": str(shipment.get("pickup_start_time") or ""),
            "pickupEndTime": str(shipment.get("pickup_end_time") or ""),
            "remark": str(shipment.get("remark") or "")[:100],
            "salt": str(shipment.get("booking_salt") or ""),
            "thirdOrderId": str(shipment.get("booking_request_id") or "")[:32],
            "op": "0",
            "resultv2": "0",
        }
        if company == "顺丰":
            param["serviceType"] = "顺丰标快"
        return self._request("cOrder", param)

    def cancel_order(self, task_id: str, order_id: str, reason: str = "订单信息需要修改") -> Dict[str, Any]:
        return self._request(
            "cancel",
            {"taskId": task_id, "orderId": order_id, "cancelMsg": reason[:30]},
        )


def order_config_public() -> Dict[str, Any]:
    client = Kuaidi100OrderClient.from_env()
    return {
        "enabled": order_enabled(),
        "configured": client.is_configured(),
        "endpoint": client.endpoint,
        "key": mask_secret(client.key),
        "secret": mask_secret(client.secret),
        "callback_url": callback_url(),
    }


def verify_callback_signature(param_raw: str, sign: str, salt: str) -> bool:
    expected = hashlib.md5(f"{param_raw}{salt}".encode("utf-8")).hexdigest()
    return bool(sign) and expected.lower() == sign.strip().lower()
