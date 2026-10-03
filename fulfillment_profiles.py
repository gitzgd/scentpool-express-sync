"""Named dispatch origins and private, immutable booking configuration snapshots.

No real addresses, branch numbers or credentials belong in this module.
Profiles share the existing Cainiao authorization; jobs retain the exact values
chosen at submission, even if a profile or authorization is changed afterwards.
"""
import hashlib
import json

PROFILE_COMPANIES = {"kunming": "中通", "banna": "圆通"}
PROFILE_NAMES = {"kunming": "昆明中台发货", "banna": "版纳门店发货"}
PUBLIC_FIELDS = ("id", "name", "express_company", "sender_name", "sender_mobile",
                 "sender_address", "sender_company", "tbNet", "exp_type",
                 "third_template_url", "third_custom_template_url", "pay_type", "revision")
SNAPSHOT_FIELDS = ("sender_name", "sender_mobile", "sender_address", "sender_company",
                   "cargo_name", "pay_type", "print_mode", "printer_siid", "template_id",
                   "paper_width", "paper_height", "need_desensitization", "need_logo")
AUTH_FIELDS = {"partnerId": "partner_id", "partnerKey": "partner_key",
               "partnerSecret": "partner_secret", "partnerName": "partner_name",
               "net": "partner_net", "code": "partner_code", "checkMan": "partner_check_man"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fulfillment_profiles (
        id TEXT PRIMARY KEY, config_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)""")
    for table, name, definition in (
        ("shipping_settings", "default_profile_id", "TEXT NOT NULL DEFAULT ''"),
        ("shipping_batch_items", "settings_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("shipping_batches", "fulfillment_name", "TEXT NOT NULL DEFAULT ''"),
        ("shipments", "fulfillment_name", "TEXT NOT NULL DEFAULT ''"),
    ):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def profiles(conn):
    result = []
    for row in conn.execute("SELECT * FROM fulfillment_profiles ORDER BY id"):
        data = json.loads(row["config_json"])
        data.update(id=row["id"], revision=row["revision"])
        result.append(data)
    return result


def public_profile(profile):
    return {key: profile.get(key, "") for key in PUBLIC_FIELDS}


def account_hash(settings):
    return digest([settings.get("partner_id", ""), settings.get("partner_net", "")])


def selected(conn, settings, profile_id=None):
    from database import AppError
    target = settings.get("default_profile_id", "") if profile_id is None else profile_id
    if not target:
        if settings.get("default_profile_id"):
            raise AppError("请选择发货方案，不可绕过默认方案使用旧寄件配置。", 409)
        return None
    profile = next((p for p in profiles(conn) if p["id"] == target), None)
    if not profile:
        raise AppError("发货方案不存在，请刷新后重新选择。", 409)
    if profile.get("authorization_hash") != account_hash(settings):
        raise AppError("菜鸟授权账号已变化，请刷新网点并重新确认该发货方案。", 409)
    return profile


def snapshot(settings, company, profile=None):
    carrier = settings.get("carrier_settings", {}).get(company, {})
    result = {key: settings.get(key, "") for key in SNAPSHOT_FIELDS}
    result.update({key: settings.get(source, "") for key, source in AUTH_FIELDS.items()})
    result.update(tbNet=carrier.get("tbNet", ""), exp_type=carrier.get("expType", "标准快递"),
                  third_template_url=carrier.get("thirdTemplateURL", ""),
                  third_custom_template_url=carrier.get("thirdCustomTemplateUrl", ""),
                  express_company=company, profile_id="", fulfillment_name="原总部配置")
    if profile:
        for key in ("sender_name", "sender_mobile", "sender_address", "sender_company", "tbNet",
                    "exp_type", "third_template_url", "third_custom_template_url", "pay_type"):
            result[key] = profile[key]
        result.update(profile_id=profile["id"], fulfillment_name=profile["name"], print_mode="PDF", printer_siid="")
    return result


def ensure_branch(settings, profile):
    from database import AppError
    option = next((b for b in settings.get("branch_options", [])
                   if b.get("company") == profile["express_company"] and b.get("tbNet") == profile["tbNet"]), None)
    if option is None:
        raise AppError("所选网点不在当前菜鸟授权中，请刷新网点并核对发货方案。", 409)
    return option


def save(conn, settings, payload):
    from database import AppError, re_phone_ok
    profile_id = str(payload.get("id") or "")
    if profile_id not in PROFILE_COMPANIES:
        raise AppError("请选择昆明中台或版纳门店方案。")
    if not settings.get("partner_authorized"):
        raise AppError("请先完成菜鸟授权并刷新网点。", 409)
    old = conn.execute("SELECT revision FROM fulfillment_profiles WHERE id=?", (profile_id,)).fetchone()
    if old and payload.get("revision") != old["revision"]:
        raise AppError("方案已被其他操作更新，请刷新后重新编辑。", 409)
    result = {key: str(payload.get(key) or "").strip() for key in PUBLIC_FIELDS if key not in {"revision"}}
    result.update(id=profile_id, express_company=PROFILE_COMPANIES[profile_id],
                  name=result["name"] or PROFILE_NAMES[profile_id],
                  exp_type=result["exp_type"] or "标准快递", pay_type=result["pay_type"] or "MONTHLY")
    if not result["sender_name"] or not result["sender_address"] or not re_phone_ok(result["sender_mobile"]):
        raise AppError("请填写寄件人、有效联系电话和完整寄件地址。")
    for key, value in result.items():
        if len(value) > (500 if key in {"sender_address", "third_template_url", "third_custom_template_url", "tbNet"} else 100):
            raise AppError("方案内容过长，请缩短后保存。")
    if result["pay_type"] not in {"MONTHLY", "SHIPPER"}:
        raise AppError("请选择月结或寄方付。")
    for key in ("third_template_url", "third_custom_template_url"):
        if result[key] and not result[key].startswith("https://cloudprint.cainiao.com/template/"):
            raise AppError("请填写有效的菜鸟面单模板地址。")
    if result["third_custom_template_url"] and not result["third_template_url"]:
        raise AppError("自定义货品区必须同时配置基础面单模板。")
    ensure_branch(settings, result)
    result["authorization_hash"] = account_hash(settings)
    conn.execute("""INSERT INTO fulfillment_profiles(id,config_json,revision) VALUES(?,?,1)
        ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,revision=revision+1""",
        (profile_id, encoded(result)))
    if payload.get("make_default") is True:
        conn.execute("UPDATE shipping_settings SET default_profile_id=? WHERE id=1", (profile_id,))


def guard_legacy_jobs(conn):
    from database import AppError
    if conn.execute("""SELECT 1 FROM shipping_batch_items WHERE status IN ('排队中','提交中')
                       AND settings_snapshot_json='{}' LIMIT 1""").fetchone():
        raise AppError("旧版打单任务尚未完成，暂不能切换配置。请等待任务结束后重试。", 409)
