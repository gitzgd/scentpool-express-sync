from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import sqlite3
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote

from pypdf import PdfReader, PdfWriter

import server
import label_pdf
import shipping
import tracking
import daily_audit_probe_test
import daily_audit_test
from database import AppError, DEFAULT_PRODUCT_FILE, Database


def request(opener, base, method, path, payload=None):
    status, body, _headers = request_full(opener, base, method, path, payload)
    return status, body


def request_full(opener, base, method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=5) as response:
            raw = response.read()
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return response.status, json.loads(raw.decode("utf-8")), response.headers
            return response.status, raw, response.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = raw.decode("utf-8", errors="replace")
        return exc.code, body, exc.headers


def request_multipart(opener, base, method, path, field_name, file_path):
    boundary = "----scentpool-smoke-boundary"
    file_bytes = Path(file_path).read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{field_name}"; filename="products.xlsx"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with opener.open(req, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")), response.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = raw.decode("utf-8", errors="replace")
        return exc.code, body, exc.headers


def request_form(opener, base, method, path, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def blank_label_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=216, height=369)
    writer.write(output)
    return output.getvalue()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SCENTPOOL_TRACKING_PROVIDER"] = "kuaidi100"
        os.environ["SCENTPOOL_KUAIDI100_CUSTOMER"] = " test-customer "
        os.environ["SCENTPOOL_KUAIDI100_KEY"] = " test-key "
        os.environ["SCENTPOOL_KUAIDI100_LABEL_SECRET"] = " test-label-secret "
        os.environ["SCENTPOOL_KUAIDI100_LABEL_ENABLED"] = "1"
        os.environ["SCENTPOOL_PUBLIC_BASE_URL"] = "https://example.test"
        label_config = shipping.label_config_public()
        assert label_config["ready"] is True
        assert label_config["missing"] == []
        del os.environ["SCENTPOOL_KUAIDI100_LABEL_SECRET"]
        label_config = shipping.label_config_public()
        assert label_config["configured"] is False
        assert label_config["missing"] == ["SCENTPOOL_KUAIDI100_LABEL_SECRET"]
        os.environ["SCENTPOOL_KUAIDI100_LABEL_SECRET"] = " test-label-secret "
        assert tracking.KUAIDI100_ENDPOINT == "https://poll.kuaidi100.com/poll/query.do"
        original_urlopen = tracking.urllib.request.urlopen
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "status": "200",
                        "state": "0",
                        "ischeck": "0",
                        "data": [{"ftime": "2026-07-07 12:00:00", "context": "快件运输中"}],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["payload"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return FakeResponse()

        try:
            tracking.urllib.request.urlopen = fake_urlopen
            tracking.Kuaidi100Client("test-customer", "test-key").query({"express_company": "圆通", "tracking_no": "YT123456"})
        finally:
            tracking.urllib.request.urlopen = original_urlopen
        params = urllib.parse.parse_qs(captured["payload"])
        request_data = json.loads(params["param"][0])
        expected_sign = hashlib.md5(f"{params['param'][0]}test-keytest-customer".encode("utf-8")).hexdigest().upper()
        assert captured["url"] == "https://poll.kuaidi100.com/poll/query.do"
        assert params["customer"][0] == "test-customer"
        assert params["sign"][0] == expected_sign
        assert request_data == {"com": "yuantong", "num": "YT123456", "resultv2": "1", "show": "0", "order": "desc"}

        def fake_forbidden_urlopen(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

        try:
            tracking.urllib.request.urlopen = fake_forbidden_urlopen
            forbidden = tracking.Kuaidi100Client("test-customer", "test-key").query(
                {"express_company": "圆通", "tracking_no": "YT-SYSTEM-403"}
            )
            assert forbidden["system_error"] is True
            assert forbidden["error_scope"] == "tracking_provider"
            assert forbidden["http_status"] == 403
            assert forbidden["provider_reached"] is False
            assert "不是单个快递单号错误" in forbidden["error"]
            assert forbidden["tracking_status"] == "查询失败"
        finally:
            tracking.urllib.request.urlopen = original_urlopen

        try:
            tracking.urllib.request.urlopen = fake_urlopen
            tracking.Kuaidi100Client("test-customer", "test-key").query(
                {
                    "express_company": "申通快递",
                    "express_company_code": "shentong",
                    "tracking_no": "STO123456",
                    "sender_phone": "13800138000",
                }
            )
        finally:
            tracking.urllib.request.urlopen = original_urlopen
        params = urllib.parse.parse_qs(captured["payload"])
        request_data = json.loads(params["param"][0])
        assert request_data == {
            "com": "shentong",
            "num": "STO123456",
            "resultv2": "1",
            "show": "0",
            "order": "desc",
        }, request_data

        try:
            tracking.Kuaidi100Client("test-customer", "test-key").query(
                {
                    "express_company": "伪造快递",
                    "express_company_code": "../shentong",
                    "tracking_no": "INVALID-COMPANY-CODE",
                }
            )
            raise AssertionError("invalid company code should fail before querying")
        except AppError as exc:
            assert "公司编码" in exc.message

        class FakeDetectResponse(FakeResponse):
            def read(self):
                return json.dumps(
                    [{"comCode": "shentong", "name": "申通快递", "lengthPre": 15}],
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_detect_urlopen(request, timeout=0):
            captured["detect_url"] = request.full_url
            captured["detect_timeout"] = timeout
            return FakeDetectResponse()

        try:
            tracking.urllib.request.urlopen = fake_detect_urlopen
            detected = tracking.Kuaidi100Client("test-customer", "test-key").detect_company("STO-SMOKE-001")
        finally:
            tracking.urllib.request.urlopen = original_urlopen
        detect_url = urllib.parse.urlparse(captured["detect_url"])
        detect_params = urllib.parse.parse_qs(detect_url.query)
        assert f"{detect_url.scheme}://{detect_url.netloc}{detect_url.path}" == tracking.KUAIDI100_AUTODETECT_ENDPOINT
        assert detect_params == {"num": ["STO-SMOKE-001"], "key": ["test-key"]}
        assert captured["detect_timeout"] == 10
        assert detected["express_company"] == "申通快递"
        assert detected["company_code"] == "shentong"

        class FakeDetectErrorResponse(FakeResponse):
            def read(self):
                return json.dumps(
                    {"returnCode": "201", "message": "单号格式错误", "result": False},
                    ensure_ascii=False,
                ).encode("utf-8")

        try:
            tracking.urllib.request.urlopen = lambda _request, timeout=0: FakeDetectErrorResponse()
            tracking.Kuaidi100Client("test-customer", "test-key").detect_company("INVALID-SMOKE")
            raise AssertionError("invalid tracking number should fail auto-detection")
        except AppError as exc:
            assert "核对单号" in exc.message
        finally:
            tracking.urllib.request.urlopen = original_urlopen

        try:
            tracking.urllib.request.urlopen = fake_forbidden_urlopen
            tracking.Kuaidi100Client("test-customer", "test-key").detect_company("STO-SYSTEM-403")
            raise AssertionError("provider HTTP 403 should be a system-level auto-detection failure")
        except AppError as exc:
            assert exc.status == 503
            assert exc.details["tracking_service_error"] is True
            assert exc.details["http_status"] == 403
        finally:
            tracking.urllib.request.urlopen = original_urlopen

        pending = tracking.normalize_kuaidi100_response(
            {"status": "500", "message": "查询无结果，请隔段时间再查", "state": "0", "data": []},
            '{"status":"500"}',
        )
        assert pending["tracking_status"] == "等待揽收"
        assert pending["error"] == ""
        assert pending["is_signed"] is False
        pending = tracking.normalize_kuaidi100_response(
            {"status": "200", "message": "ok", "state": "0", "data": []},
            '{"status":"200"}',
        )
        assert pending["tracking_status"] == "等待揽收"
        body_forbidden = tracking.normalize_kuaidi100_response(
            {"status": "403", "message": "Forbidden", "state": "", "data": []},
            '{"status":"403"}',
        )
        assert body_forbidden["system_error"] is True
        assert body_forbidden["http_status"] == 403

        shipping_captured = {}

        class FakeLabelResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "success": True,
                        "code": 200,
                        "message": "success",
                        "data": {
                            "taskId": "TASK-SMOKE", "kuaidinum": "YT-SMOKE-BOOKING-001",
                            "label": "https://example.test/label/smoke.pdf", "kdComOrderNum": "KD-SMOKE",
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_label_urlopen(request, timeout=0):
            shipping_captured["url"] = request.full_url
            shipping_captured["payload"] = request.data.decode("utf-8")
            shipping_captured["timeout"] = timeout
            form = urllib.parse.parse_qs(shipping_captured["payload"])
            method = form.get("method", [""])[0]
            if method in {"cancel", "printOld"}:
                class SimpleSuccess(FakeLabelResponse):
                    def read(self):
                        return json.dumps({"success": True, "code": 200, "message": "success"}).encode("utf-8")
                return SimpleSuccess()
            return FakeLabelResponse()

        label_shipment = {
            "express_company": "圆通",
            "recipient_name": "测试收件人",
            "phone": "13800138000",
            "address": "上海市测试路1号",
            "booking_salt": "salt-smoke",
            "booking_request_id": "SP20260710S01N1",
            "remark": "烟测",
            "items": [
                {"product_category": "睡眠喷雾", "product_name": "（喷雾）基诺山雨林与苔藓", "quantity": 2},
                {"product_category": "香包", "product_name": "（香包）曼听墨玫瑰", "quantity": 1},
            ],
        }
        label_settings = {
            "sender_name": "总部",
            "sender_mobile": "13900139000",
            "sender_address": "云南省昆明市测试路1号",
            "cargo_name": "香氛商品",
            "partnerId": "CAINIAO-ID",
            "partnerKey": "CAINIAO-KEY",
            "net": "cainiao",
            "tbNet": "测试网点,001",
            "third_template_url": "https://cloudprint.cainiao.com/template/standard/850338",
            "third_custom_template_url": "https://cloudprint.cainiao.com/template/customArea/77205369",
            "pay_type": "MONTHLY",
            "print_mode": "PDF",
        }
        original_shipping_urlopen = shipping.urllib.request.urlopen
        try:
            shipping.urllib.request.urlopen = fake_label_urlopen
            order_result = shipping.Kuaidi100LabelClient("test-key", "test-label-secret").create_label(
                label_shipment,
                label_settings,
            )
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert order_result["success"] is True

        class RecoveredLabelResponse(FakeLabelResponse):
            def read(self):
                return json.dumps(
                    {
                        "success": False,
                        "code": 30011,
                        "message": "重新获取面单成功",
                        "data": {
                            "taskId": "TASK-RECOVERED",
                            "kuaidinum": "YT-RECOVERED-001",
                            "label": "https://example.test/label/recovered.pdf",
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        try:
            shipping.urllib.request.urlopen = lambda _request, timeout=0: RecoveredLabelResponse()
            recovered_order = shipping.Kuaidi100LabelClient(
                "test-key", "test-label-secret"
            ).create_label(label_shipment, label_settings)
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert recovered_order["success"] is True
        assert recovered_order["tracking_no"] == "YT-RECOVERED-001"
        assert recovered_order["task_id"] == "TASK-RECOVERED"

        class IncompleteRecoveredLabelResponse(FakeLabelResponse):
            def read(self):
                return json.dumps(
                    {
                        "success": False,
                        "code": 30011,
                        "message": "重新获取面单成功但缺少任务号",
                        "data": {"kuaidinum": "YT-INCOMPLETE-001"},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        try:
            shipping.urllib.request.urlopen = lambda _request, timeout=0: IncompleteRecoveredLabelResponse()
            incomplete_order = shipping.Kuaidi100LabelClient(
                "test-key", "test-label-secret"
            ).create_label(label_shipment, label_settings)
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert incomplete_order["success"] is False

        try:
            def fail_label_urlopen(_request, timeout=0):
                raise urllib.error.URLError("synthetic network failure")

            shipping.urllib.request.urlopen = fail_label_urlopen
            transient_result = shipping.Kuaidi100LabelClient(
                "test-key", "test-label-secret"
            )._post("https://api.kuaidi100.com/label/order", "order", {})
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert transient_result["success"] is False
        assert transient_result["retryable"] is True
        assert transient_result["error"] == "暂时无法连接快递100，请检查网络后重试。"
        order_form = urllib.parse.parse_qs(shipping_captured["payload"])
        order_param = order_form["param"][0]
        expected_order_sign = hashlib.md5(
            f"{order_param}{order_form['t'][0]}test-keytest-label-secret".encode("utf-8")
        ).hexdigest().upper()
        assert shipping_captured["url"] == "https://api.kuaidi100.com/label/order"
        assert order_form["method"][0] == "order"
        assert order_form["sign"][0] == expected_order_sign
        assert json.loads(order_param)["orderId"] == "SP20260710S01N1"
        assert json.loads(order_param)["reorder"] is False
        assert json.loads(order_param)["cargo"] == "【睡眠喷雾】基诺山雨林与苔藓*2\n【香包】曼听墨玫瑰*1"
        assert json.loads(order_param)["remark"] == "【睡眠喷雾】基诺山雨林与苔藓*2\n【香包】曼听墨玫瑰*1\n备注：烟测"
        assert json.loads(order_param)["thirdTemplateURL"] == "https://cloudprint.cainiao.com/template/standard/850338"
        assert json.loads(order_param)["thirdCustomTemplateUrl"] == "https://cloudprint.cainiao.com/template/customArea/77205369"
        assert json.loads(order_param)["customParam"] == {
            "itemSummary": "【睡眠喷雾】基诺山雨林与苔藓*2\n【香包】曼听墨玫瑰*1\n备注：烟测",
            "cargo": "【睡眠喷雾】基诺山雨林与苔藓*2\n【香包】曼听墨玫瑰*1",
            "remark": "【睡眠喷雾】基诺山雨林与苔藓*2\n【香包】曼听墨玫瑰*1\n备注：烟测",
        }
        assert "tempId" not in json.loads(order_param)
        assert order_result["cancel_param"] == {
            "partnerId": "CAINIAO-ID",
            "partnerKey": "CAINIAO-KEY",
            "net": "cainiao",
            "kuaidicom": "yuantong",
            "kuaidinum": "YT-SMOKE-BOOKING-001",
            "orderId": "KD-SMOKE",
        }
        original_shipping_urlopen = shipping.urllib.request.urlopen
        try:
            shipping.urllib.request.urlopen = fake_label_urlopen
            cancel_result = shipping.Kuaidi100LabelClient("test-key", "test-label-secret").cancel_label(
                {
                    "express_company": "圆通",
                    "tracking_no": "YT-SMOKE-BOOKING-001",
                    "label_carrier_order_no": "KD-SMOKE",
                    "label_cancel_param": order_result["cancel_param"],
                },
                {
                    "partnerId": "CHANGED-ID",
                    "partnerKey": "CHANGED-KEY",
                    "net": "cainiao",
                    "tbNet": "不应进入取消参数",
                },
            )
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert cancel_result["success"] is True
        cancel_form = urllib.parse.parse_qs(shipping_captured["payload"])
        cancel_param = json.loads(cancel_form["param"][0])
        assert cancel_form["method"][0] == "cancel"
        assert cancel_param["partnerId"] == "CAINIAO-ID"
        assert cancel_param["partnerKey"] == "CAINIAO-KEY"
        assert cancel_param["kuaidicom"] == "yuantong"
        assert cancel_param["kuaidinum"] == "YT-SMOKE-BOOKING-001"
        assert cancel_param["orderId"] == "KD-SMOKE"
        assert "tbNet" not in cancel_param

        class CancelFailureResponse(FakeLabelResponse):
            def read(self):
                return json.dumps(
                    {"success": False, "code": 30005, "message": "该面单暂不支持取消"},
                    ensure_ascii=False,
                ).encode("utf-8")

        original_shipping_urlopen = shipping.urllib.request.urlopen
        try:
            shipping.urllib.request.urlopen = lambda _request, timeout=0: CancelFailureResponse()
            failed_cancel = shipping.Kuaidi100LabelClient("test-key", "test-label-secret").cancel_label(
                {
                    "express_company": "圆通",
                    "tracking_no": "YT-SMOKE-BOOKING-001",
                    "label_carrier_order_no": "KD-SMOKE",
                    "label_cancel_param": order_result["cancel_param"],
                },
                {},
            )
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert failed_cancel["success"] is False
        assert "30005" in failed_cancel["error"]
        assert "普通圆通" in failed_cancel["error"]
        long_summary = shipping.build_label_item_summary(
            [
                {"product_category": "睡眠喷雾", "product_name": "（喷雾）基诺山雨林与苔藓", "quantity": 2},
                {"product_category": "睡眠喷雾", "product_name": "（喷雾）打洛边境青柠与罗勒", "quantity": 3},
                {"product_category": "香包", "product_name": "（香包）橘河红柚佛手柑", "quantity": 4},
                {"product_category": "香包", "product_name": "（香包）基诺山雨林与苔藓", "quantity": 5},
            ],
            50,
        )
        assert len(long_summary) <= 50
        assert "【睡眠喷雾】基诺山雨林*2" in long_summary
        assert "打洛边境青柠*3" in long_summary
        assert "【香包】橘河红柚佛手柑*4" in long_summary
        assert "基诺山雨林*5" in long_summary
        assert "\n【香包】" in long_summary
        assert "另" not in long_summary
        auth_credentials = shipping.parse_auth_callback(
            json.dumps(
                {
                    "result": True,
                    "returnCode": "200",
                    "message": json.dumps(
                        {"parterId": "CAINIAO-ID", "partnerKey": "CAINIAO-KEY", "net": "cainiao"},
                        ensure_ascii=False,
                    ),
                },
                ensure_ascii=False,
            )
        )
        assert auth_credentials["partnerId"] == "CAINIAO-ID"
        assert auth_credentials["net"] == "cainiao"

        legacy_path = Path(tmp) / "legacy.db"
        with sqlite3.connect(legacy_path) as legacy_conn:
            legacy_conn.executescript(
                """
                CREATE TABLE stores (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL, store_id INTEGER, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL, store_name_snapshot TEXT NOT NULL,
                    created_by INTEGER NOT NULL, recipient_name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
                    store_order_no TEXT NOT NULL, remark TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '待处理',
                    express_company TEXT NOT NULL DEFAULT '', tracking_no TEXT NOT NULL DEFAULT '',
                    shipping_note TEXT NOT NULL DEFAULT '', shipped_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (store_id, store_order_no)
                );
                CREATE TABLE shipment_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL, product_barcode TEXT NOT NULL,
                    product_name TEXT NOT NULL, product_category TEXT NOT NULL, unit_price TEXT NOT NULL DEFAULT '0.00',
                    quantity INTEGER NOT NULL
                );
                CREATE TABLE return_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL, store_name_snapshot TEXT NOT NULL,
                    created_by INTEGER NOT NULL, express_company TEXT NOT NULL DEFAULT '圆通', tracking_no TEXT NOT NULL,
                    sender_phone TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '待查询',
                    tracking_provider TEXT NOT NULL DEFAULT '', tracking_status TEXT NOT NULL DEFAULT '',
                    tracking_state_code TEXT NOT NULL DEFAULT '', tracking_last_event TEXT NOT NULL DEFAULT '',
                    tracking_last_checked_at TEXT NOT NULL DEFAULT '', tracking_signed_at TEXT NOT NULL DEFAULT '',
                    tracking_error TEXT NOT NULL DEFAULT '', tracking_raw TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (store_id, tracking_no)
                );
                INSERT INTO stores VALUES (1, '迁移门店', 1, '2026-07-09T10:00:00+08:00', '2026-07-09T10:00:00+08:00');
                INSERT INTO users VALUES (1, 'legacy', 'unused', 'staff', 1, 1, '2026-07-09T10:00:00+08:00', '2026-07-09T10:00:00+08:00');
                INSERT INTO shipments VALUES (1, 1, '迁移门店', 1, '旧收件人', '13800138000', '旧地址', '1001', '', '待处理', '', '', '', '', '2026-07-09T10:00:00+08:00', '2026-07-09T10:00:00+08:00');
                INSERT INTO shipment_items VALUES (1, 1, 'LEGACY-PRODUCT', '旧商品', '旧分类', '1.00', 1);
                INSERT INTO return_orders VALUES (
                    1, 1, '迁移门店', 1, '圆通', 'LEGACY-RETURN-001', '', '', '已签收', 'kuaidi100', '已签收',
                    '3', '旧退货已签收', '2026-07-09T10:00:00+08:00', '2026-07-09T10:00:00+08:00', '', '{}',
                    '2026-07-09T10:00:00+08:00', '2026-07-09T10:00:00+08:00'
                );
                """
            )
        legacy_db = Database(str(legacy_path))
        legacy_db.initialize(DEFAULT_PRODUCT_FILE)
        with legacy_db.connect() as legacy_conn:
            migrated = legacy_conn.execute("SELECT * FROM shipments WHERE id = 1").fetchone()
            assert migrated["order_date"] == "2026-07-09"
            assert migrated["business_id"] == "20260709-S01-1001"
            assert legacy_conn.execute("SELECT COUNT(*) FROM shipment_items WHERE shipment_id = 1").fetchone()[0] == 1
            migrated_return = legacy_conn.execute("SELECT * FROM return_orders WHERE id = 1").fetchone()
            assert migrated_return["express_company_source"] == "manual"
            assert migrated_return["express_company_code"] == "yuantong"
            assert legacy_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_alert_acknowledgements'"
            ).fetchone()
            assert legacy_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'operational_incidents'"
            ).fetchone()
            assert legacy_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_event_meta'"
            ).fetchone()
            assert legacy_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_state_events'"
            ).fetchone()
            assert legacy_conn.execute("SELECT COUNT(*) FROM audit_state_events").fetchone()[0] == 5
            audit_columns = {
                str(row["name"]) for row in legacy_conn.execute("PRAGMA table_info(audit_state_events)").fetchall()
            }
            assert not {
                "business_id", "store_order_no", "recipient_name", "phone", "address",
                "tracking_no", "message", "raw",
            }.intersection(audit_columns)
        assert list((Path(tmp) / "backups").glob("legacy-before-order-date-*.db"))
        legacy_product = legacy_db.list_products()[0]
        created_next_day = legacy_db.create_shipment(
            {"id": 1, "role": "staff", "store_id": 1},
            {
                "recipient_name": "新收件人", "phone": "13800138000", "address": "新地址", "store_order_no": "1001",
                "items": [{"barcode": legacy_product["barcode"], "quantity": 1}],
            },
        )
        assert created_next_day["business_id"].endswith("-S01-1001")
        try:
            legacy_db.create_shipment(
                {"id": 1, "role": "staff", "store_id": 1},
                {
                    "recipient_name": "重复", "phone": "13800138000", "address": "新地址", "store_order_no": "1001",
                    "items": [{"barcode": legacy_product["barcode"], "quantity": 1}],
                },
            )
            raise AssertionError("same-day duplicate should fail")
        except AppError as exc:
            assert exc.status == 409
            assert exc.details["business_id"] == created_next_day["business_id"]
        for index in range(51):
            legacy_db.create_shipment(
                {"id": 1, "role": "staff", "store_id": 1},
                {
                    "recipient_name": f"批量收件人{index}", "phone": "13800138000", "address": "批量测试地址",
                    "store_order_no": f"BATCH-{index + 1:04d}",
                    "items": [{"barcode": legacy_product["barcode"], "quantity": 1}],
                },
            )
        batch_preview = legacy_db.preview_shipping_batch({"id": 99, "role": "admin"}, {"q": "BATCH-"})
        assert len(batch_preview["eligible"]) == 51
        large_batch = legacy_db.create_shipping_batch(
            {"id": 1, "role": "admin"},
            [{"id": row["id"], "express_company": "圆通"} for row in batch_preview["eligible"]],
            {"q": "BATCH-"},
        )
        assert large_batch["batch"]["total_count"] == 51
        failed_job = legacy_db.claim_next_shipping_job()
        assert failed_job is not None
        legacy_db.complete_shipping_job(
            failed_job["batch_item_id"],
            {"success": False, "error": "模拟失败", "raw": "{}"},
        )
        failure_alerts = legacy_db.task_alerts()
        assert failure_alerts["counts"]["面单下单失败"] == 1
        assert failure_alerts["items"][0]["message"] == "模拟失败"
        assert failure_alerts["items"][0]["category"] == "电子面单"
        assert failure_alerts["items"][0]["reason"] == "其他下单问题"
        assert failure_alerts["items"][0]["auto_retry"] is False
        assert "recipient_name" not in failure_alerts["items"][0]
        assert "phone" not in failure_alerts["items"][0]
        assert "address" not in failure_alerts["items"][0]
        original_alert = failure_alerts["items"][0]

        with legacy_db.connect() as legacy_conn:
            legacy_conn.execute(
                """
                UPDATE shipments
                SET tracking_status = '查询失败', tracking_error = '单号或快递公司不匹配',
                    tracking_last_checked_at = '2031-01-01T00:00:00+08:00',
                    booking_updated_at = CASE
                        WHEN booking_status IN ('排队中', '提交中') THEN '2031-01-01T00:00:00+08:00'
                        ELSE booking_updated_at
                    END
                WHERE store_order_no LIKE 'BATCH-%'
                """
            )
        skewed_alerts = legacy_db.task_alerts(limit=10)
        assert skewed_alerts["category_counts"]["电子面单"] == 1, skewed_alerts["category_counts"]
        assert all(item["category"] == "物流查询" for item in skewed_alerts["items"])
        assert len(skewed_alerts["category_items"]["电子面单"]) == 1
        assert skewed_alerts["category_items"]["电子面单"][0]["alert_key"] == original_alert["alert_key"]

        with legacy_db.connect() as legacy_conn:
            legacy_conn.execute(
                """
                UPDATE shipments
                SET tracking_error = '快递100请求失败：HTTP Error 403: Forbidden'
                WHERE store_order_no LIKE 'BATCH-%'
                """
            )
        grouped_alerts = legacy_db.task_alerts(limit=10)
        grouped_system_alerts = [item for item in grouped_alerts["items"] if item.get("system_scope")]
        assert len(grouped_system_alerts) == 1
        assert grouped_system_alerts[0]["type"] == "物流服务异常"
        assert grouped_system_alerts[0]["affected_count"] == 51
        assert grouped_alerts["category_counts"]["物流查询"] == 1
        assert grouped_alerts["counts"]["total"] == 2

        legacy_db.record_operational_incident(
            "tracking:kuaidi100",
            category="物流查询",
            incident_type="物流服务异常",
            status="系统检查中",
            message="快递100接口暂时拒绝访问（HTTP 403）。",
            affected_count=51,
        )
        legacy_db.clear_operational_incident("tracking:kuaidi100")
        with legacy_db.connect() as legacy_conn:
            still_failed = legacy_conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE store_order_no LIKE 'BATCH-%' AND tracking_status = '查询失败'"
            ).fetchone()[0]
            assert still_failed == 51
            legacy_conn.execute(
                """
                UPDATE shipments
                SET tracking_status = '', tracking_error = '', tracking_last_checked_at = ''
                WHERE store_order_no LIKE 'BATCH-%'
                """
            )
        assert legacy_db.task_alerts()["counts"]["total"] == 1

        acknowledged_alerts = legacy_db.resolve_task_alert(
            original_alert["alert_key"], original_alert["fingerprint"], 1
        )
        assert acknowledged_alerts["counts"]["total"] == 0
        with legacy_db.connect() as legacy_conn:
            legacy_conn.execute(
                "UPDATE shipments SET booking_updated_at = ? WHERE id = ?",
                ("2030-01-01T00:00:00+08:00", original_alert["record_id"]),
            )
        repeated_alerts = legacy_db.task_alerts()
        assert repeated_alerts["counts"]["total"] == 1
        assert repeated_alerts["items"][0]["fingerprint"] != original_alert["fingerprint"]
        retried_batch = legacy_db.retry_shipping_batch(large_batch["batch"]["id"])
        assert retried_batch["counts"]["排队中"] == 51
        stale_job = legacy_db.claim_next_shipping_job()
        assert stale_job is not None
        with legacy_db.connect() as legacy_conn:
            legacy_conn.execute(
                "UPDATE shipping_batch_items SET updated_at = '2000-01-01T00:00:00+08:00' WHERE id = ?",
                (stale_job["batch_item_id"],),
            )
        recovered = legacy_db.reset_stale_shipping_jobs()
        assert recovered == {"requeued": 1, "failed": 0}
        stale_job = legacy_db.claim_next_shipping_job()
        assert stale_job is not None
        with legacy_db.connect() as legacy_conn:
            legacy_conn.execute(
                "UPDATE shipping_batch_items SET updated_at = '2000-01-01T00:00:00+08:00' WHERE id = ?",
                (stale_job["batch_item_id"],),
            )
        recovered = legacy_db.reset_stale_shipping_jobs()
        assert recovered == {"requeued": 0, "failed": 1}
        timeout_alerts = legacy_db.task_alerts()
        assert timeout_alerts["counts"]["面单下单失败"] == 1
        assert "停止自动重试" in timeout_alerts["items"][0]["message"]

        db_path = str(Path(tmp) / "test.db")
        server.DB = Database(db_path)
        server.PRODUCT_FILE_PATH = str(Path(tmp) / "products.xlsx")
        server.SESSION_SECURE = False
        server.ALLOW_DB_RESTORE = False
        server.DB.initialize(DEFAULT_PRODUCT_FILE)
        server.DB.save_label_authorization(
            {"partnerId": "CAINIAO-ID", "partnerKey": "CAINIAO-KEY", "net": "cainiao"}
        )

        assert server.MAX_BATCH_PRINT_ORDERS == 50
        assert label_pdf.DEFAULT_MEMORY_LIMIT_MB == 192
        assert label_pdf.MAX_MEMORY_LIMIT_MB == 224
        original_connect = server.DB.connect
        original_idle_delays = server.SHIPPING_IDLE_DELAYS_SECONDS
        connection_calls = {"count": 0}

        def counted_worker_connect():
            connection_calls["count"] += 1
            return original_connect()

        server.DB.connect = counted_worker_connect
        server.SHIPPING_IDLE_DELAYS_SECONDS = (0.01, 0.01, 0.01, 0.01)
        server.SHIPPING_QUEUE_EVENT.clear()
        worker_stop = threading.Event()
        worker_thread = threading.Thread(target=server.shipping_worker, args=(worker_stop,), daemon=True)
        worker_thread.start()
        time.sleep(0.08)
        worker_stop.set()
        server.notify_shipping_worker()
        worker_thread.join(timeout=1)
        server.DB.connect = original_connect
        server.SHIPPING_IDLE_DELAYS_SECONDS = original_idle_delays
        server.SHIPPING_QUEUE_EVENT.clear()
        assert not worker_thread.is_alive()
        assert connection_calls["count"] == 1, connection_calls

        httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", 0), server.Handler, 4)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        time.sleep(0.1)

        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        staff = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

        status, body = request(admin, base, "GET", "/api/health")
        assert status == 200, body
        assert body["ok"] is True
        assert body["database"] is True

        status, body = request(admin, base, "GET", "/login")
        assert status == 200
        login_html = body.decode("utf-8")
        assert "scentpool2026" not in login_html
        assert "示例门店" not in login_html

        status, body = request(admin, base, "POST", "/api/login", {"username": "admin", "password": "scentpool2026"})
        assert status == 200, body
        assert body["user"]["role"] == "admin"

        status, body = request(admin, base, "GET", "/api/admin/system/diagnostics")
        assert status == 200, body
        assert body["storage"]["journal_mode"] == "wal"
        assert body["storage"]["table_counts"]["products"] == 52
        assert body["process"]["request_thread_limit"] == server.MAX_REQUEST_THREADS

        status, body = request(admin, base, "GET", "/api/admin/task-alerts")
        assert status == 200, body
        assert body["counts"]["total"] == 0
        assert body["items"] == []
        assert body["refresh"] == {
            "alerts_seconds": 60,
            "shipment_tracking_minutes": 360,
            "return_tracking_minutes": 720,
        }

        status, body = request(admin, base, "GET", "/api/admin/tracking/config")
        assert status == 200, body
        assert body["tracking"]["provider"] == "kuaidi100"
        assert body["tracking"]["configured"] is True
        assert body["tracking"]["customer"] == "test...omer"
        assert body["tracking"]["endpoint"] == "https://poll.kuaidi100.com/poll/query.do"
        assert body["tracking"]["autodetect_endpoint"] == "https://www.kuaidi100.com/autonumber/auto"
        assert body["shipping"]["endpoint"] == "https://api.kuaidi100.com/label/order"

        status, body = request(admin, base, "GET", "/api/admin/shipping-settings")
        assert status == 200, body
        assert body["settings"]["partner_authorized"] is True
        assert "partner_key" not in body["settings"]
        assert body["settings"]["partner_key_masked"]
        assert body["settings"]["carrier_settings"]["圆通"]["thirdTemplateURL"] == (
            "https://cloudprint.cainiao.com/template/standard/850338"
        )
        assert body["settings"]["carrier_settings"]["圆通"]["thirdCustomTemplateUrl"] == (
            "https://cloudprint.cainiao.com/template/customArea/77205369"
        )
        assert body["settings"]["carrier_settings"]["京东"]["thirdTemplateURL"] == ""
        assert body["settings"]["carrier_settings"]["顺丰"]["thirdTemplateURL"] == ""

        status, body = request(admin, base, "GET", "/api/products?all=1")
        assert status == 200, body
        assert len(body["products"]) == 52, len(body["products"])
        product = body["products"][0]
        product_2 = next(item for item in body["products"] if item["category"] != product["category"])

        manual_product = {
            "barcode": "SMOKE-PRODUCT-001",
            "category": "烟测分类",
            "name": "烟测商品",
            "spec": "1盒",
            "price": "9.90",
            "status": "启用",
        }
        status, body = request(admin, base, "POST", "/api/products", manual_product)
        assert status == 201, body
        assert body["product"]["barcode"] == "SMOKE-PRODUCT-001"
        assert len(body["products"]) == 53
        status, body = request(admin, base, "GET", "/api/products")
        assert status == 200, body
        assert any(item["barcode"] == "SMOKE-PRODUCT-001" for item in body["categories"]["烟测分类"])
        status, body = request(admin, base, "DELETE", f"/api/products/{quote('SMOKE-PRODUCT-001')}")
        assert status == 200, body
        assert body["product"]["deleted"] is True
        assert len(body["products"]) == 52
        status, body = request(admin, base, "GET", "/api/products")
        assert status == 200, body
        assert "烟测分类" not in body["categories"]

        status, body, headers = request_full(admin, base, "GET", "/api/admin/backup.db")
        assert status == 200
        assert body.startswith(b"SQLite format 3"), body[:32]
        assert "scentpool-backup" in headers.get("Content-Disposition", "")

        status, body, _headers = request_multipart(admin, base, "POST", "/api/products/import", "product_file", DEFAULT_PRODUCT_FILE)
        assert status == 200, body
        assert body["result"]["imported"] == 52
        assert Path(server.PRODUCT_FILE_PATH).exists()
        status, body = request(admin, base, "GET", "/api/products?all=1")
        assert status == 200, body
        product = body["products"][0]
        product_2 = next(item for item in body["products"] if item["category"] != product["category"])

        status, body = request(staff, base, "POST", "/api/login", {"username": "store01", "password": "scentpool2026"})
        assert status == 200, body
        assert body["user"]["role"] == "staff"

        status, body = request(staff, base, "GET", "/api/admin/tracking/config")
        assert status == 403, body
        status, body = request(staff, base, "GET", "/api/admin/task-alerts")
        assert status == 403, body
        status, body = request(
            staff,
            base,
            "POST",
            "/api/admin/task-alerts/resolve",
            {"alert_key": "shipment:1:tracking", "fingerprint": "0" * 64},
        )
        assert status == 403, body

        status, body = request(staff, base, "GET", "/shipments")
        assert status == 200
        assert "万物香铺".encode("utf-8") in body

        shipment_payload = {
            "recipient_name": "测试收件人",
            "phone": "13800138000",
            "address": "上海市测试路 1 号",
            "store_order_no": "ORDER-SMOKE-001",
            "remark": "烟测",
            "items": [
                {"barcode": product["barcode"], "quantity": 2},
                {"barcode": product_2["barcode"], "quantity": 1},
            ],
        }
        status, body = request(staff, base, "POST", "/api/shipments", shipment_payload)
        assert status == 201, body
        shipment_id = body["shipment"]["id"]
        assert body["shipment"]["items"][0]["quantity"] == 2

        with server.DB.connect() as conn:
            conn.execute(
                """
                UPDATE shipments
                SET tracking_status = '查询失败', tracking_error = '查询无结果，请隔段时间再查',
                    tracking_last_checked_at = '2026-08-06T15:00:00+08:00'
                WHERE id = ?
                """,
                (shipment_id,),
            )
        status, body = request(admin, base, "GET", "/api/admin/task-alerts")
        assert status == 200, body
        assert body["counts"]["total"] == 1
        assert body["category_counts"]["物流查询"] == 1
        tracking_alert = body["items"][0]
        assert tracking_alert["reason"] == "暂未查到物流"
        assert tracking_alert["auto_retry"] is True
        status, body = request(
            admin,
            base,
            "POST",
            "/api/admin/task-alerts/resolve",
            {"alert_key": tracking_alert["alert_key"], "fingerprint": "0" * 64},
        )
        assert status == 409, body
        status, body = request(
            admin,
            base,
            "POST",
            "/api/admin/task-alerts/resolve",
            {"alert_key": tracking_alert["alert_key"], "fingerprint": tracking_alert["fingerprint"]},
        )
        assert status == 200, body
        assert body["counts"]["total"] == 0
        with server.DB.connect() as conn:
            conn.execute(
                "UPDATE shipments SET tracking_last_checked_at = ? WHERE id = ?",
                ("2026-08-06T15:01:00+08:00", shipment_id),
            )
        status, body = request(admin, base, "GET", "/api/admin/task-alerts")
        assert status == 200, body
        assert body["counts"]["total"] == 1
        with server.DB.connect() as conn:
            conn.execute(
                "UPDATE shipments SET tracking_status = '', tracking_error = '', tracking_last_checked_at = '' WHERE id = ?",
                (shipment_id,),
            )
        status, body = request(admin, base, "GET", "/api/admin/task-alerts")
        assert status == 200, body
        assert body["counts"]["total"] == 0

        status, body = request(
            staff,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}/items",
            {"items": [{"barcode": product["barcode"], "quantity": 1}, {"barcode": product_2["barcode"], "quantity": 3}]},
        )
        assert status == 200, body
        assert len(body["shipment"]["items"]) == 2
        assert body["shipment"]["items"][1]["product_barcode"] == product_2["barcode"]
        assert body["shipment"]["items"][1]["quantity"] == 3

        status, body = request(
            staff,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}/remark",
            {"remark": "门店修改后的备注"},
        )
        assert status == 200, body
        assert body["shipment"]["remark"] == "门店修改后的备注"

        delete_payload = {
            **shipment_payload,
            "store_order_no": "ORDER-SMOKE-DELETE",
            "remark": "待删除",
        }
        status, body = request(staff, base, "POST", "/api/shipments", delete_payload)
        assert status == 201, body
        delete_shipment_id = body["shipment"]["id"]
        status, body = request(staff, base, "DELETE", f"/api/shipments/{delete_shipment_id}")
        assert status == 200, body
        assert body["shipment"]["deleted"] is True
        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-DELETE")
        assert status == 200, body
        assert len(body["shipments"]) == 0

        status, body = request(staff, base, "POST", "/api/shipments", shipment_payload)
        assert status == 409, body

        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert "booking_raw" not in body["shipments"][0]
        assert "booking_salt" not in body["shipments"][0]
        assert "tracking_raw" not in body["shipments"][0]

        status, body = request(admin, base, "GET", "/api/shipments/summary?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert body["counts"]["total"] == 1
        assert body["counts"]["待处理"] == 1

        status, body = request(
            admin,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}/items",
            {"items": [{"barcode": product["barcode"], "quantity": 2}, {"barcode": product_2["barcode"], "quantity": 1}]},
        )
        assert status == 200, body
        assert len(body["shipment"]["items"]) == 2
        assert body["shipment"]["items"][0]["quantity"] == 2

        status, body = request(
            admin,
            base,
            "PUT",
            "/api/admin/shipping-settings",
            {
                "sender_name": "总部",
                "sender_mobile": "13900139000",
                "sender_address": "云南省昆明市测试路1号",
                "sender_company": "万物香铺",
                "default_company": "圆通",
                "cargo_name": "香氛商品",
                "pay_type": "MONTHLY",
                "print_mode": "PDF",
                "carrier_settings": {
                    "圆通": {"tbNet": "测试网点,001", "expType": "标准快递"},
                    "京东": {"tbNet": "", "expType": "标准快递"},
                    "顺丰": {"tbNet": "", "expType": "顺丰标快"},
                },
            },
        )
        assert status == 200, body
        assert body["settings"]["sender_name"] == "总部"
        assert body["settings"]["carrier_settings"]["圆通"]["thirdTemplateURL"] == (
            "https://cloudprint.cainiao.com/template/standard/850338"
        )
        assert body["settings"]["carrier_settings"]["圆通"]["thirdCustomTemplateUrl"] == (
            "https://cloudprint.cainiao.com/template/customArea/77205369"
        )

        status, body = request(
            admin,
            base,
            "POST",
            "/api/admin/shipping-batches/preview",
            {"filters": {"q": "ORDER-SMOKE-001", "status": "待处理"}},
        )
        assert status == 200, body
        assert len(body["preview"]["eligible"]) == 1
        assert body["preview"]["eligible"][0]["id"] == shipment_id

        status, body = request(
            admin,
            base,
            "POST",
            "/api/admin/shipping-batches",
            {
                "filters": {"q": "ORDER-SMOKE-001", "status": "待处理"},
                "shipments": [{"id": shipment_id, "express_company": "圆通"}],
            },
        )
        assert status == 202, body
        batch_id = body["batch"]["id"]

        def fake_batch_tracking(_shipment):
            return {
                "provider": "kuaidi100", "tracking_status": "查询失败", "state_code": "",
                "last_event": "", "checked_at": "2026-07-10T12:05:00+08:00", "signed_at": "",
                "error": "查询无结果", "raw": "{}", "is_signed": False,
            }

        server.query_tracking = fake_batch_tracking

        original_shipping_urlopen = shipping.urllib.request.urlopen
        try:
            shipping.urllib.request.urlopen = fake_label_urlopen
            assert server.process_next_shipping_job() is True
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        status, body = request(admin, base, "GET", f"/api/admin/shipping-batches/{batch_id}")
        assert status == 200, body
        assert body["counts"]["成功"] == 1
        assert body["items"][0]["booking_status"] == "已出单"
        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert body["shipments"][0]["tracking_status"] == "等待揽收"
        assert body["shipments"][0]["label_print_status"] == "待打印"
        assert "tracking_raw" not in body["shipments"][0]
        status, detail_body = request(
            admin,
            base,
            "GET",
            f"/api/shipments?id={shipment_id}&include_tracking_raw=1",
        )
        assert status == 200, detail_body
        assert detail_body["shipments"][0]["tracking_raw"] == "{}"

        breaker_user = server.DB.authenticate("store01", "scentpool2026")
        assert breaker_user is not None
        breaker_shipment = server.DB.create_shipment(
            breaker_user,
            {
                "recipient_name": "熔断测试",
                "phone": "13800138000",
                "address": "系统故障不会写入订单",
                "store_order_no": "TRACKING-BREAKER-002",
                "items": [{"barcode": product["barcode"], "quantity": 1}],
            },
        )
        with server.DB.connect() as conn:
            conn.execute(
                """
                UPDATE shipments
                SET status = '已发货', express_company = '圆通', tracking_no = 'YT-BREAKER-002',
                    tracking_status = '等待揽收', tracking_error = '', tracking_last_checked_at = ''
                WHERE id = ?
                """,
                (breaker_shipment["id"],),
            )
        protected_ids = [shipment_id, breaker_shipment["id"]]
        with server.DB.connect() as conn:
            protected_before = {
                int(row["id"]): dict(row)
                for row in conn.execute(
                    "SELECT id, tracking_status, tracking_error, tracking_last_checked_at FROM shipments WHERE id IN (?, ?)",
                    protected_ids,
                ).fetchall()
            }
        breaker_calls = {"count": 0}

        def fake_provider_incident(_shipment):
            breaker_calls["count"] += 1
            return tracking.tracking_service_error_result(http_status=403)

        server.query_tracking = fake_provider_incident
        breaker_result = server.sync_tracking_batch(force=True, limit=0)
        assert breaker_result["provider_incident"] is True
        assert breaker_result["attempted"] == 1
        assert breaker_result["checked"] == 0
        assert breaker_calls["count"] == 1
        with server.DB.connect() as conn:
            protected_after = {
                int(row["id"]): dict(row)
                for row in conn.execute(
                    "SELECT id, tracking_status, tracking_error, tracking_last_checked_at FROM shipments WHERE id IN (?, ?)",
                    protected_ids,
                ).fetchall()
            }
        assert protected_after == protected_before
        breaker_alerts = server.DB.task_alerts()
        provider_alerts = [item for item in breaker_alerts["items"] if item.get("system_scope")]
        assert len(provider_alerts) == 1
        assert provider_alerts[0]["affected_count"] >= 2
        server.clear_tracking_service_incident()
        with server.DB.connect() as conn:
            conn.execute("UPDATE shipments SET status = '已取消' WHERE id = ?", (breaker_shipment["id"],))

        compressed_request = urllib.request.Request(
            base + "/api/shipments",
            method="GET",
            headers={"Accept-Encoding": "gzip"},
        )
        with admin.open(compressed_request, timeout=5) as response:
            compressed_body = response.read()
            assert response.headers.get("Content-Encoding") == "gzip"
            assert response.headers.get("Server-Timing", "").startswith("app;dur=")
            compressed_json = json.loads(gzip.decompress(compressed_body).decode("utf-8"))
            assert compressed_json["shipments"]

        original_download_label_pdf = label_pdf.download_label_pdf
        original_build_batch_label_pdf_file = server.build_batch_label_pdf_file
        original_current_process_rss_mb = server.current_process_rss_mb
        try:
            def fake_download_label_pdf(_url, _business_id, target, *, max_bytes):
                payload = blank_label_pdf()
                assert len(payload) <= max_bytes
                target.write_bytes(payload)
                return len(payload)

            def fake_build_batch_label_pdf_file(_shipments, work_dir):
                target = work_dir / "labels.pdf"
                target.write_bytes(blank_label_pdf())
                return target

            label_pdf.download_label_pdf = fake_download_label_pdf
            server.build_batch_label_pdf_file = fake_build_batch_label_pdf_file

            status, body = request(
                admin,
                base,
                "POST",
                "/api/admin/labels/batch-print",
                {"shipment_ids": [shipment_id] * 51},
            )
            assert status == 413, body
            assert "最多合并 50" in body["error"]

            assert server.LABEL_BATCH_PRINT_LOCK.acquire(blocking=False)
            try:
                status, body = request(
                    admin,
                    base,
                    "POST",
                    "/api/admin/labels/batch-print",
                    {"shipment_ids": [shipment_id]},
                )
            finally:
                server.LABEL_BATCH_PRINT_LOCK.release()
            assert status == 409, body
            assert "正在生成" in body["error"]

            server.current_process_rss_mb = lambda: float(server.BATCH_PRINT_PARENT_RSS_LIMIT_MB + 1)
            status, body = request(
                admin,
                base,
                "POST",
                "/api/admin/labels/batch-print",
                {"shipment_ids": [shipment_id]},
            )
            assert status == 503, body
            assert "订单" in body["error"]
            status, print_guard_body = request(admin, base, "GET", "/api/shipments?q=YT-SMOKE-BOOKING-001")
            assert status == 200, print_guard_body
            assert print_guard_body["shipments"][0]["label_print_status"] == "待打印"
            server.current_process_rss_mb = lambda: None

            with tempfile.TemporaryDirectory() as merge_dir:
                merged_path = Path(merge_dir) / "merged.pdf"
                label_pdf.merge_label_pdfs(
                    [
                        {"id": 1, "business_id": "PDF-1", "label_urls": ["https://api.kuaidi100.com/label/1"]},
                        {"id": 2, "business_id": "PDF-2", "label_urls": ["https://api.kuaidi100.com/label/2"]},
                    ],
                    merged_path,
                    max_label_bytes=1024 * 1024,
                    max_total_bytes=2 * 1024 * 1024,
                )
                assert len(PdfReader(str(merged_path)).pages) == 2
            status, merged_pdf, headers = request_full(
                admin,
                base,
                "POST",
                "/api/admin/labels/batch-print",
                {"shipment_ids": [shipment_id]},
            )
        finally:
            label_pdf.download_label_pdf = original_download_label_pdf
            server.build_batch_label_pdf_file = original_build_batch_label_pdf_file
            server.current_process_rss_mb = original_current_process_rss_mb
        assert status == 200
        assert merged_pdf.startswith(b"%PDF")
        assert len(PdfReader(io.BytesIO(merged_pdf)).pages) == 1
        assert "inline" in headers.get("Content-Disposition", "")
        status, body = request(admin, base, "GET", "/api/shipments?q=YT-SMOKE-BOOKING-001")
        assert status == 200, body
        assert body["shipments"][0]["label_print_status"] == "打印成功"
        status, body = request(
            admin,
            base,
            "POST",
            "/api/admin/labels/batch-print",
            {"shipment_ids": [shipment_id]},
        )
        assert status == 409, body

        status, body = request(
            staff,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}/remark",
            {"remark": "发货后不能修改"},
        )
        assert status == 409, body

        callback_param = json.dumps(
            {"status": "200", "message": "打印成功"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        callback_salt = server.DB.booking_salt_for_task("TASK-SMOKE")
        callback_sign = hashlib.md5(f"{callback_param}{callback_salt}".encode("utf-8")).hexdigest()

        status, body = request_form(
            admin,
            base,
            "POST",
            "/api/integrations/kuaidi100/label-print-callback",
            {"taskId": "TASK-SMOKE", "param": callback_param, "sign": callback_sign},
        )
        assert status == 200, body
        status, duplicate_body = request_form(
            admin,
            base,
            "POST",
            "/api/integrations/kuaidi100/label-print-callback",
            {"taskId": "TASK-SMOKE", "param": callback_param, "sign": callback_sign},
        )
        assert status == 200, duplicate_body
        status, invalid_body = request_form(
            admin,
            base,
            "POST",
            "/api/integrations/kuaidi100/label-print-callback",
            {"taskId": "TASK-SMOKE", "param": callback_param, "sign": "invalid"},
        )
        assert status == 403, invalid_body
        status, body = request(admin, base, "GET", "/api/shipments?q=YT-SMOKE-BOOKING-001")
        assert status == 200, body
        assert body["shipments"][0]["status"] == "已发货"
        assert body["shipments"][0]["label_print_status"] == "打印成功"
        assert body["shipments"][0]["label_url"].endswith("smoke.pdf")
        first_booking_request_id = body["shipments"][0]["booking_request_id"]
        status, body = request(admin, base, "POST", f"/api/shipments/{shipment_id}/label/printed", {})
        assert status == 200, body
        assert body["shipment"]["label_print_status"] == "打印成功"

        status, export_body, _headers = request_full(
            admin,
            base,
            "GET",
            "/api/export/cainiao.xlsx?q=YT-SMOKE-BOOKING-001",
        )
        assert status == 404
        assert export_body["error"] == "接口不存在。"

        try:
            shipping.urllib.request.urlopen = fake_label_urlopen
            status, body = request(admin, base, "POST", f"/api/shipments/{shipment_id}/label/cancel", {})
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert status == 200, body
        assert body["shipment"]["status"] == "待处理"
        assert body["shipment"]["booking_status"] == "已取消"
        assert body["shipment"]["tracking_no"] == ""
        assert body["shipment"]["express_company"] == "圆通"

        status, rebook_body = request(
            admin,
            base,
            "POST",
            "/api/admin/shipping-batches",
            {
                "filters": {"q": "ORDER-SMOKE-001", "status": "待处理"},
                "shipments": [{"id": shipment_id, "express_company": "圆通"}],
            },
        )
        assert status == 202, rebook_body
        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        second_booking_request_id = body["shipments"][0]["booking_request_id"]
        assert second_booking_request_id != first_booking_request_id
        assert len(second_booking_request_id) <= 32
        original_shipping_urlopen = shipping.urllib.request.urlopen
        try:
            shipping.urllib.request.urlopen = fake_label_urlopen
            assert server.process_next_shipping_job() is True
            status, body = request(admin, base, "POST", f"/api/shipments/{shipment_id}/label/cancel", {})
        finally:
            shipping.urllib.request.urlopen = original_shipping_urlopen
        assert status == 200, body
        assert body["shipment"]["booking_status"] == "已取消"
        assert body["shipment"]["tracking_no"] == ""

        def fake_in_transit(_shipment):
            return {
                "provider": "kuaidi100",
                "tracking_status": "运输中",
                "state_code": "0",
                "last_event": "2026-07-07 12:00:00 快件运输中",
                "checked_at": "2026-07-07T12:05:00+08:00",
                "signed_at": "",
                "error": "",
                "raw": "{}",
                "is_signed": False,
            }

        server.query_tracking = fake_in_transit
        status, body = request(
            admin,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}",
            {
                "status": "待处理",
                "express_company": "顺丰",
                "tracking_no": "SF123456",
                "shipping_note": "已交接",
            },
        )
        assert status == 200, body
        assert body["shipment"]["status"] == "已发货"
        assert body["shipment"]["tracking_no"] == "SF123456"
        assert body["shipment"]["express_company"] == "顺丰"
        assert body["shipment"]["tracking_status"] == "运输中"

        status, body = request(
            staff,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}/items",
            {"items": [{"barcode": product["barcode"], "quantity": 1}]},
        )
        assert status == 409, body

        status, body = request(staff, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert body["shipments"][0]["status"] == "已发货"
        assert body["shipments"][0]["express_company"] == "顺丰"
        assert body["shipments"][0]["tracking_no"] == "SF123456"
        created_date = body["shipments"][0]["created_at"][:10]
        store_code = f"S{int(body['shipments'][0]['store_id']):02d}"

        def fake_query_tracking(_shipment):
            return {
                "provider": "kuaidi100",
                "tracking_status": "已签收",
                "state_code": "3",
                "last_event": f"{created_date} 12:00:00 已签收",
                "checked_at": f"{created_date}T12:05:00+08:00",
                "signed_at": f"{created_date} 12:00:00",
                "error": "",
                "raw": "{}",
                "is_signed": True,
            }

        server.query_tracking = fake_query_tracking
        status, body = request(admin, base, "POST", "/api/admin/tracking/sync", {"force": True, "limit": 5})
        assert status == 200, body
        assert body["result"]["signed"] == 1
        assert body["result"]["remaining"] == 0
        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert body["shipments"][0]["status"] == "已签收"
        assert body["shipments"][0]["tracking_status"] == "已签收"
        assert body["shipments"][0]["tracking_signed_at"]

        status, body = request(staff, base, "GET", "/returns/new")
        assert status == 200
        status, body = request(staff, base, "GET", "/returns")
        assert status == 200

        def fake_detect_return_company(tracking_no):
            assert tracking_no == "STO-SMOKE-001"
            return {"express_company": "申通快递", "company_code": "shentong", "provider_name": "申通快递"}

        def fake_return_in_transit(return_order):
            assert return_order["express_company"] == "申通快递"
            assert return_order["express_company_code"] == "shentong"
            return {
                "provider": "kuaidi100",
                "tracking_status": "运输中",
                "state_code": "0",
                "last_event": "2026-07-07 12:00:00 退货快件运输中",
                "checked_at": "2026-07-07T12:05:00+08:00",
                "signed_at": "",
                "error": "",
                "raw": "{}",
                "is_signed": False,
            }

        server.detect_tracking_company = fake_detect_return_company
        server.query_tracking = fake_return_in_transit
        return_payload = {
            "express_company": "圆通",
            "tracking_no": "STO-SMOKE-001",
            "sender_phone": "13800138000",
            "remark": "退货烟测",
            "items": [{"barcode": product["barcode"], "quantity": 1}],
        }
        status, body = request(staff, base, "POST", "/api/returns", return_payload)
        assert status == 201, body
        return_id = body["return_order"]["id"]
        assert body["return_order"]["status"] == "运输中"
        assert body["return_order"]["express_company"] == "申通快递"
        assert body["return_order"]["express_company_code"] == "shentong"
        assert body["return_order"]["express_company_source"] == "kuaidi100"
        assert body["return_order"]["items"][0]["product_barcode"] == product["barcode"]

        status, body = request(staff, base, "POST", "/api/returns", return_payload)
        assert status == 409, body

        status, body = request(staff, base, "GET", "/api/returns?q=STO-SMOKE-001")
        assert status == 200, body
        assert len(body["returns"]) == 1
        assert body["returns"][0]["tracking_no"] == "STO-SMOKE-001"

        status, body = request(admin, base, "GET", "/api/returns?q=STO-SMOKE-001")
        assert status == 200, body
        assert len(body["returns"]) == 1

        with server.DB.connect() as conn:
            conn.execute(
                """
                UPDATE return_orders
                SET express_company = '圆通', express_company_code = 'yuantong',
                    express_company_source = 'manual', tracking_status = '等待揽收', tracking_error = '',
                    tracking_last_checked_at = '2000-01-01T00:00:00+08:00'
                WHERE id = ?
                """,
                (return_id,),
            )
        server.detect_tracking_company = fake_detect_return_company
        server.query_tracking = fake_query_tracking
        status, body = request(admin, base, "POST", f"/api/returns/{return_id}/tracking/refresh", {})
        assert status == 200, body
        assert body["return_order"]["status"] == "已签收"
        assert body["return_order"]["tracking_status"] == "已签收"
        assert body["return_order"]["express_company"] == "申通快递"
        assert body["return_order"]["express_company_code"] == "shentong"
        assert body["return_order"]["express_company_source"] == "kuaidi100"

        def fake_detection_failure(_tracking_no):
            raise AppError("快递100无法识别这个单号，请核对单号是否完整、准确。")

        server.detect_tracking_company = fake_detection_failure
        failed_return_payload = {
            "tracking_no": "UNKNOWN-SMOKE-002",
            "sender_phone": "13800138000",
            "remark": "识别失败烟测",
            "items": [{"barcode": product["barcode"], "quantity": 1}],
        }
        status, body = request(staff, base, "POST", "/api/returns", failed_return_payload)
        assert status == 201, body
        assert body["return_order"]["status"] == "异常"
        assert body["return_order"]["tracking_status"] == "查询失败"
        assert body["return_order"]["express_company"] == ""
        assert body["return_order"]["express_company_source"] == "auto_pending"
        assert "快递公司自动识别失败" in body["return_order"]["tracking_error"]
        assert "核对单号" in body["return_order"]["tracking_error"]

        test_staff_user = server.DB.authenticate("store01", "scentpool2026")
        assert test_staff_user is not None
        fallback_return = server.DB.create_return_order(
            test_staff_user,
            {
                "tracking_no": "LEGACY-FALLBACK-SMOKE-003",
                "items": [{"barcode": product["barcode"], "quantity": 1}],
            },
        )
        with server.DB.connect() as conn:
            conn.execute(
                """
                UPDATE return_orders
                SET express_company = '圆通', express_company_source = 'manual', tracking_status = '等待揽收'
                WHERE id = ?
                """,
                (fallback_return["id"],),
            )
        fallback_return = server.DB.get_return_order(
            fallback_return["id"],
            test_staff_user,
        )

        def fake_fallback_query(return_order):
            assert return_order["express_company"] == "圆通"
            return {
                "provider": "kuaidi100",
                "tracking_status": "运输中",
                "state_code": "0",
                "last_event": "2026-07-07 12:00:00 旧公司兜底查询成功",
                "checked_at": "2026-07-07T12:05:00+08:00",
                "signed_at": "",
                "error": "",
                "raw": "{}",
                "is_signed": False,
            }

        server.detect_tracking_company = fake_detection_failure
        server.query_tracking = fake_fallback_query
        fallback_result = server.refresh_tracking_for_return(fallback_return)
        assert fallback_result["status"] == "运输中"
        fallback_updated = server.DB.get_return_order(
            fallback_return["id"],
            test_staff_user,
        )
        assert fallback_updated["express_company"] == "圆通"
        assert fallback_updated["express_company_source"] == "manual"

        unexpected_return = server.DB.create_return_order(
            test_staff_user,
            {
                "tracking_no": "UNEXPECTED-SMOKE-004",
                "items": [{"barcode": product["barcode"], "quantity": 1}],
            },
        )
        server.detect_tracking_company = lambda _tracking_no: (_ for _ in ()).throw(RuntimeError("synthetic provider fault"))
        unexpected_result = server.refresh_tracking_for_return(unexpected_return)
        assert unexpected_result["tracking_status"] == "查询失败"
        unexpected_updated = server.DB.get_return_order(
            unexpected_return["id"],
            test_staff_user,
        )
        assert "系统暂时无法完成快递公司识别" in unexpected_updated["tracking_error"]

        status, body = request(
            staff,
            base,
            "GET",
            f"/api/shipments?date_from={created_date}&date_to={created_date}&q=SF123456",
        )
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert body["shipments"][0]["tracking_no"] == "SF123456"

        business_id = f"{created_date.replace('-', '')}-{store_code}-ORDER-SMOKE-001"
        status, body = request(admin, base, "GET", f"/api/shipments?q={quote(business_id)}")
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert body["shipments"][0]["store_order_no"] == "ORDER-SMOKE-001"

        expected_csv_name = f"{quote('示例门店_' + created_date + '.csv')}"
        status, body, headers = request_full(admin, base, "GET", f"/api/export/shipments.csv?date_from={created_date}&date_to={created_date}&q=ORDER-SMOKE-001")
        assert status == 200
        assert expected_csv_name in headers.get("Content-Disposition", "")
        assert "ORDER-SMOKE-001".encode("utf-8") in body
        csv_text = body.decode("utf-8-sig")
        csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert len(csv_rows) == 1
        detail = csv_rows[0]["商品明细"]
        assert "\n" in detail, detail
        assert f"{product['category']}：" in detail, detail
        assert f"{product_2['category']}：" in detail, detail

        expected_xlsx_name = f"{quote('示例门店_' + created_date + '.xlsx')}"
        status, body, headers = request_full(admin, base, "GET", f"/api/export/shipments.xlsx?date_from={created_date}&date_to={created_date}&q=ORDER-SMOKE-001")
        assert status == 200
        assert expected_xlsx_name in headers.get("Content-Disposition", "")
        with zipfile.ZipFile(io.BytesIO(body)) as xlsx:
            names = set(xlsx.namelist())
            assert "xl/worksheets/sheet1.xml" in names
            assert "xl/styles.xml" in names
            sheet_xml = xlsx.read("xl/worksheets/sheet1.xml").decode("utf-8")
            styles_xml = xlsx.read("xl/styles.xml").decode("utf-8")
            assert 'width="52"' in sheet_xml, sheet_xml[:500]
            assert "ORDER-SMOKE-001" in sheet_xml
            assert "wrapText" in styles_xml

        status, body = request(staff, base, "DELETE", f"/api/shipments/{shipment_id}")
        assert status == 409, body

        status, body = request(admin, base, "DELETE", f"/api/shipments/{shipment_id}")
        assert status == 409, body

        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1

        httpd.shutdown()
        thread.join(timeout=2)

        prod_db = Database(str(Path(tmp) / "prod.db"))
        prod_db.initialize(str(Path(tmp) / "missing.xlsx"), production=True, admin_password="strong-pass-123")
        assert prod_db.database_summary()["users"] == 1
        assert prod_db.database_summary()["stores"] == 0
        assert not prod_db.default_credentials_active()

        try:
            Database(str(Path(tmp) / "blocked.db")).initialize(
                str(Path(tmp) / "missing.xlsx"), production=True, admin_password=""
            )
            raise AssertionError("production init without admin password should fail")
        except AppError:
            pass

        daily_audit_test.main()
        daily_audit_probe_test.main()
        print("smoke test passed")


if __name__ == "__main__":
    main()
