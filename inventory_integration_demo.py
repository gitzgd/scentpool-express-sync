"""Loopback-only integration fixture. Temporary DB, synthetic labels, no external IO.

This runner is NOT imported by production. It does not implement a production
carrier: its small worker only completes the existing queue with synthetic data.
The token is random, local-only, in a 0600 file inside a 0700 temporary directory.
"""
import argparse
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from database import Database
from fulfillment_profiles_test import configure, order, book
from special_shipments_test import seed


def external_io_guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
        raise RuntimeError("Local integration fixture blocks external IO")


def sample_payload(reference="00000000-0000-4000-8000-000000000101"):
    return {"reference": reference, "purpose": "store_restock",
            "owner": {"kind": "store", "external_id": "demo-store", "name": "合成门店甲"},
            "recipient": {"name": "合成收件人", "phone": "13800000000", "address": "本地联调地址，禁止真实寄送"},
            "items": [{"barcode": "DEMO-PRODUCT", "quantity": 2}], "note": "仅本地合成联调"}


def complete_one(db):
    job = db.claim_next_shipping_job()
    if not job:
        return False
    db.complete_shipping_job(job["batch_item_id"], {
        "success": True, "tracking_no": f"LOCAL-DEMO-{job['id']}-{job['batch_item_id']}",
        "task_id": f"DEMO-TASK-{job['batch_item_id']}", "carrier_order_no": f"DEMO-ORDER-{job['batch_item_id']}",
        "label_url": "https://api.kuaidi100.com/synthetic-never-fetch.pdf",
        "print_status": "待打印", "print_type": "PDF", "raw": "{}"}, expected_attempt=job["booking_attempt"])
    return True


def synthetic_pdf(_rows, directory):
    from pypdf import PdfWriter
    target = Path(directory) / "synthetic-label.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=216, height=369)
    writer.add_metadata({"/Title": "SYNTHETIC ONLY - DO NOT SHIP"})
    with target.open("wb") as stream:
        writer.write(stream)
    target.chmod(0o600)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8891)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    # Guard applies before fixture initialization, imports and serving. No real
    # worker, tracking scheduler, browser login or production DB option exists.
    sys.addaudithook(external_io_guard)
    os.environ["SCENTPOOL_TRACKING_AUTO"] = "0"
    os.environ["SCENTPOOL_KUAIDI100_LABEL_ENABLED"] = "0"
    os.environ["SCENTPOOL_SESSION_SECURE"] = "0"
    os.environ["SCENTPOOL_INVENTORY_TOKEN"] = secrets.token_hex(32)
    import server
    with tempfile.TemporaryDirectory(prefix="inventory-integration-demo-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        db = Database(str(root / "synthetic.db"))
        users = seed(db)
        configure(db, users)
        examples = []
        for index, profile in enumerate(("banna", "kunming", "kunming_sf"), 1):
            row = order(db, users, f"INVENTORY-E2E-{index}")
            book(db, users, row, profile)
            complete_one(db)
            examples.append({"id": row["id"], "profile_id": profile})
        # One explicit reference-only fixture. No historical production import.
        with db.connect() as conn:
            conn.execute("INSERT INTO inventory_historical VALUES(?)", (examples[0]["id"],))
        server.DB = db
        server.build_batch_label_pdf_file = synthetic_pdf
        class Handler(server.Handler):
            def log_message(self, *_args):
                pass
            def route_api(self, path, query):
                if not urlsplit(self.path).path.startswith("/api/inventory/v1/"):
                    self.error_json("Local fixture exposes only the inventory API", 404)
                    return
                super().route_api(path, query)
        base = f"http://127.0.0.1:{args.port}/api/inventory/v1"
        token_file = root / "token"
        token_file.write_text(os.environ["SCENTPOOL_INVENTORY_TOKEN"])
        token_file.chmod(0o600)
        sample = root / "sample-dispatch.json"
        sample.write_text(json.dumps(sample_payload(), ensure_ascii=False, indent=2))
        sample.chmod(0o600)
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"base_url": base, "token_file": str(token_file),
            "sample_payload_file": str(sample), "database": str(root / "synthetic.db"),
            "synthetic_shipped_samples": examples, "production_ready": False,
            "behavior": "book queues real local DB job, synthetic worker succeeds; PDF is blank synthetic only"}, indent=2))
        manifest.chmod(0o600)
        stop = threading.Event()
        def worker():
            while not stop.wait(0.25):
                try:
                    complete_one(db)
                except Exception:
                    print("Synthetic worker failed; inspect fixture before continuing", flush=True)
                    stop.set()
        httpd = server.FixedThreadPoolHTTPServer(("127.0.0.1", args.port), Handler, 4)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        print(json.dumps({"base_url": base, "manifest": str(manifest), "synthetic_only": True}), flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            thread.join(timeout=5)
            httpd.server_close()


if __name__ == "__main__":
    main()
