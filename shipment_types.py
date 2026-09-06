"""Fixed business taxonomy; no provider-specific fulfillment logic."""
SHIPMENT_TYPES = {
    "legacy": ("历史未分类", "历史", "legacy"),
    "standard": ("门店订单", "普通", "ordinary"),
    "resend": ("售后补发", "补发", "aftersales"),
    "exchange": ("换货寄出", "换货", "aftersales"),
    "influencer": ("博主合作", "博主", "cooperation"),
    "sample": ("合作样品", "样品", "cooperation"),
}
SPECIAL_TYPES = {"resend", "exchange", "influencer", "sample"}
GROUP_LABELS = {"ordinary": "普通发货", "aftersales": "售后发货", "cooperation": "合作寄送", "legacy": "历史未分类"}


def type_info(value):
    label, short, group = SHIPMENT_TYPES.get(value, SHIPMENT_TYPES["legacy"])
    return {"shipment_type": value if value in SHIPMENT_TYPES else "legacy",
            "shipment_type_label": label, "shipment_type_short": short,
            "shipment_group": group}


def migrate(conn):
    """Additive only: historical business fields and timestamps are never rewritten."""
    columns = {
        "stores": {"kind": "TEXT NOT NULL DEFAULT 'store' CHECK(kind IN ('store','team'))"},
        "shipments": {
            "shipment_type": "TEXT NOT NULL DEFAULT 'legacy' CHECK(shipment_type IN ('legacy','standard','resend','exchange','influencer','sample'))",
            "internal_note": "TEXT NOT NULL DEFAULT ''",
            "cooperation_subject": "TEXT NOT NULL DEFAULT ''",
            "original_shipment_id": "INTEGER REFERENCES shipments(id)",
            "related_return_id": "INTEGER REFERENCES return_orders(id)",
        },
        "shipment_items": {
            "item_kind": "TEXT NOT NULL DEFAULT 'product' CHECK(item_kind IN ('product','material'))",
            "material_spec": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, additions in columns.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_type_created ON shipments(shipment_type, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_original ON shipments(original_shipment_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_return ON shipments(related_return_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS shipment_submissions (
        store_id INTEGER NOT NULL REFERENCES stores(id),
        request_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        shipment_id INTEGER REFERENCES shipments(id) ON DELETE SET NULL,
        PRIMARY KEY(store_id, request_key)
    )""")


def category_counts(rows):
    result = {key: 0 for key in SHIPMENT_TYPES}
    for row in rows:
        result[row.get("shipment_type", "legacy")] += 1
    return result
