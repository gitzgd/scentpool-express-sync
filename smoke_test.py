from __future__ import annotations

import csv
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
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import server
import tracking
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SCENTPOOL_TRACKING_PROVIDER"] = "kuaidi100"
        os.environ["SCENTPOOL_KUAIDI100_CUSTOMER"] = " test-customer "
        os.environ["SCENTPOOL_KUAIDI100_KEY"] = " test-key "
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

        db_path = str(Path(tmp) / "test.db")
        server.DB = Database(db_path)
        server.PRODUCT_FILE_PATH = str(Path(tmp) / "products.xlsx")
        server.SESSION_SECURE = False
        server.ALLOW_DB_RESTORE = False
        server.DB.initialize(DEFAULT_PRODUCT_FILE)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        time.sleep(0.1)

        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        staff = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

        status, body = request(admin, base, "GET", "/api/health")
        assert status == 200, body
        assert body["ok"] is True
        assert body["products"] == 52

        status, body = request(admin, base, "GET", "/login")
        assert status == 200
        login_html = body.decode("utf-8")
        assert "scentpool2026" not in login_html
        assert "示例门店" not in login_html

        status, body = request(admin, base, "POST", "/api/login", {"username": "admin", "password": "scentpool2026"})
        assert status == 200, body
        assert body["user"]["role"] == "admin"

        status, body = request(admin, base, "GET", "/api/admin/tracking/config")
        assert status == 200, body
        assert body["tracking"]["provider"] == "kuaidi100"
        assert body["tracking"]["configured"] is True
        assert body["tracking"]["customer"] == "test...omer"
        assert body["tracking"]["endpoint"] == "https://poll.kuaidi100.com/poll/query.do"

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

        status, body = request(staff, base, "POST", "/api/shipments", shipment_payload)
        assert status == 409, body

        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1

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
        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert body["shipments"][0]["status"] == "已签收"
        assert body["shipments"][0]["tracking_status"] == "已签收"
        assert body["shipments"][0]["tracking_signed_at"]

        status, body = request(staff, base, "GET", "/returns/new")
        assert status == 200
        status, body = request(staff, base, "GET", "/returns")
        assert status == 200

        return_payload = {
            "express_company": "圆通",
            "tracking_no": "YT-SMOKE-001",
            "sender_phone": "13800138000",
            "remark": "退货烟测",
            "items": [{"barcode": product["barcode"], "quantity": 1}],
        }
        status, body = request(staff, base, "POST", "/api/returns", return_payload)
        assert status == 201, body
        return_id = body["return_order"]["id"]
        assert body["return_order"]["status"] == "待查询"
        assert body["return_order"]["items"][0]["product_barcode"] == product["barcode"]

        status, body = request(staff, base, "POST", "/api/returns", return_payload)
        assert status == 409, body

        status, body = request(staff, base, "GET", "/api/returns?q=YT-SMOKE-001")
        assert status == 200, body
        assert len(body["returns"]) == 1
        assert body["returns"][0]["tracking_no"] == "YT-SMOKE-001"

        status, body = request(admin, base, "GET", "/api/returns?q=YT-SMOKE-001")
        assert status == 200, body
        assert len(body["returns"]) == 1

        status, body = request(admin, base, "POST", f"/api/returns/{return_id}/tracking/refresh", {})
        assert status == 200, body
        assert body["return_order"]["status"] == "已签收"
        assert body["return_order"]["tracking_status"] == "已签收"

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
        assert status == 403, body

        status, body = request(admin, base, "DELETE", f"/api/shipments/{shipment_id}")
        assert status == 200, body
        assert body["shipment"]["deleted"] is True

        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 0

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

        print("smoke test passed")


if __name__ == "__main__":
    main()
