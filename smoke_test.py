from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import server
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

        status, body = request(admin, base, "GET", "/api/products?all=1")
        assert status == 200, body
        assert len(body["products"]) == 52, len(body["products"])
        product = body["products"][0]
        product_2 = next(item for item in body["products"] if item["category"] != product["category"])

        status, body, headers = request_full(admin, base, "GET", "/api/admin/backup.db")
        assert status == 200
        assert body.startswith(b"SQLite format 3"), body[:32]
        assert "scentpool-backup" in headers.get("Content-Disposition", "")

        status, body, _headers = request_multipart(admin, base, "POST", "/api/products/import", "product_file", DEFAULT_PRODUCT_FILE)
        assert status == 200, body
        assert body["result"]["imported"] == 52
        assert Path(server.PRODUCT_FILE_PATH).exists()

        status, body = request(staff, base, "POST", "/api/login", {"username": "store01", "password": "scentpool2026"})
        assert status == 200, body
        assert body["user"]["role"] == "staff"

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

        status, body = request(staff, base, "POST", "/api/shipments", shipment_payload)
        assert status == 409, body

        status, body = request(admin, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1

        status, body = request(
            admin,
            base,
            "PATCH",
            f"/api/shipments/{shipment_id}",
            {
                "status": "已发货",
                "express_company": "顺丰",
                "tracking_no": "SF123456",
                "shipping_note": "已交接",
            },
        )
        assert status == 200, body
        assert body["shipment"]["status"] == "已发货"
        assert body["shipment"]["tracking_no"] == "SF123456"
        assert body["shipment"]["express_company"] == "顺丰"

        status, body = request(staff, base, "GET", "/api/shipments?q=ORDER-SMOKE-001")
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert body["shipments"][0]["status"] == "已发货"
        assert body["shipments"][0]["express_company"] == "顺丰"
        assert body["shipments"][0]["tracking_no"] == "SF123456"
        created_date = body["shipments"][0]["created_at"][:10]

        status, body = request(
            staff,
            base,
            "GET",
            f"/api/shipments?date_from={created_date}&date_to={created_date}&q=SF123456",
        )
        assert status == 200, body
        assert len(body["shipments"]) == 1
        assert body["shipments"][0]["tracking_no"] == "SF123456"

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
