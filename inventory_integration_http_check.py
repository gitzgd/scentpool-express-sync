"""Development HTTP acceptance against inventory_integration_demo.py only."""
import argparse
import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    base = manifest["base_url"]
    parsed = urlsplit(base)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    assert parsed.path == "/api/inventory/v1" and not parsed.username and not parsed.query
    token = Path(manifest["token_file"]).read_text()
    sample = json.loads(Path(manifest["sample_payload_file"]).read_text())
    sample["reference"] = str(uuid.uuid4())
    def request(path, body=None, method=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + token
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method or ("POST" if body is not None else "GET"))
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            payload = response.read()
            return response.status, (json.loads(payload) if "json" in response.headers.get("Content-Type", "") else payload)
    assert request("/catalog", auth=False)[0] == 401
    status, catalog = request("/catalog")
    assert status == 200 and len(catalog["profiles"]) == 3
    assert all(set(p) == {"id", "name", "revision", "ready"} for p in catalog["profiles"])
    assert request("/events?cursor=0&cursor=1")[0] == 400
    assert request("/events?cursor=0&limit=101")[0] == 400
    print("PASS auth, catalog whitelist and query bounds")
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _: request("/dispatches", sample), range(2)))
    assert all(status == 200 for status, _ in created), created
    sid = created[0][1]["id"]
    assert sid == created[1][1]["id"]
    changed = copy.deepcopy(sample)
    changed["items"][0]["quantity"] = 3
    assert request("/dispatches", changed)[0] == 409
    bad = copy.deepcopy(sample)
    bad["reference"] = str(uuid.uuid4())
    bad["items"][0]["quantity"] = True
    assert request("/dispatches", bad)[0] == 400
    print("PASS concurrent create replay, conflict and strict quantity")
    profile = next(p for p in catalog["profiles"] if p["id"] == "kunming")
    booking = {"profile_id": "kunming", "profile_revision": profile["revision"], "request_key": str(uuid.uuid4())}
    assert request(f"/dispatches/{sid}/book", {**booking, "profile_id": "banna"})[0] == 400
    assert request(f"/dispatches/{sid}/book", {**booking, "profile_revision": profile["revision"]+100})[0] == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        booked = list(pool.map(lambda _: request(f"/dispatches/{sid}/book", booking), range(2)))
    assert all(status == 200 for status, _ in booked), booked
    assert booked[0][1]["batch_id"] == booked[1][1]["batch_id"]
    for _ in range(30):
        status, dispatch = request(f"/dispatches/{sid}")
        if dispatch["booking_status"] == "已出单":
            break
        time.sleep(0.1)
    assert status == 200 and dispatch["label_available"] and dispatch["channel"] == "kunming", dispatch
    assert "label_url" not in dispatch and "address" not in dispatch
    assert request(f"/dispatches/{sid}/book", {**booking, "request_key": str(uuid.uuid4())})[0] == 409
    assert request("/dispatches/1")[0] == 404
    status, pdf = request(f"/dispatches/{sid}/label")
    assert status == 200 and pdf.startswith(b"%PDF")
    assert request(f"/dispatches/{sid}/label?url=https://example.com")[0] == 400
    print("PASS profile/revision gates, concurrent book replay, worker, ownership and synthetic PDF")
    cursor = 0
    checkpoint_query = ""
    seen = {}
    for _ in range(100):
        status, page = request(f"/events?cursor={cursor}&limit=2{checkpoint_query}")
        assert status == 200 and page["next_cursor"] >= cursor
        for event in page["events"]:
            assert event["cursor"] > cursor
            seen[event["shipment"]["id"]] = event["shipment"]
        cursor = page["next_cursor"]
        checkpoint_query = f"&stream_id={page['stream_id']}&checkpoint={page['next_checkpoint']}"
        if not page["has_more"]:
            break
    else:
        raise AssertionError("pagination failed to terminate")
    assert seen[sid]["source_kind"] == "inventory_store_restock"
    assert seen[sid]["items"][0]["quantity"] == 2
    forbidden = {"recipient_name", "phone", "address", "label_url", "note", "internal_note", "settings_snapshot_json", "booking_raw", "partner_key"}
    def safe(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value), forbidden.intersection(value)
            for item in value.values(): safe(item)
        elif isinstance(value, list):
            for item in value: safe(item)
    safe(seen)
    assert seen[1]["historical_reference"]
    assert seen[1]["business_id"] == "EXP-1"
    assert request(f"/events?cursor={cursor}{checkpoint_query}")[1]["events"] == []
    print("PASS durable paginated feed, historical reference and privacy whitelist")
    barcode = "LOCAL-CHECK-" + uuid.uuid4().hex
    product = {"name": "本地合成商品", "category": "合成", "spec": "测试规格", "status": "启用"}
    assert request("/products/"+barcode, product, "PUT")[0] == 200
    assert request("/products/"+barcode, product, "PUT")[0] == 200
    assert request("/products/"+barcode, {**product, "price": "99"}, "PUT")[0] == 400
    print("PASS product upsert replay and extra-field rejection")
    print("HTTP development checks passed; NOT full regression or real carrier acceptance")


if __name__ == "__main__":
    main()
