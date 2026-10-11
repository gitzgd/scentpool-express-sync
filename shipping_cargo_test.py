"""Synthetic request-contract tests; never call a label provider."""
import copy
import json
from unittest.mock import patch

import shipping


def capture(shipment, settings, payload=None):
    client = shipping.Kuaidi100LabelClient("SYNTHETIC", "SYNTHETIC")
    payload = payload or {"success": True, "code": 200, "data": {
        "kuaidinum": "SYNTHETIC-NO", "taskId": "SYNTHETIC-TASK"}}
    with patch.object(client, "_post", return_value={
        "success": True, "data": payload, "raw": json.dumps(payload)}) as post:
        result = client.create_label(shipment, settings)
    post.assert_called_once()
    assert post.call_args.args[:2] == (shipping.KUAIDI100_LABEL_ENDPOINT, "order")
    return post.call_args.args[2], result


def examples():
    base = {"express_company": "顺丰", "booking_request_id": "SYNTHETIC-RETRY-ID",
            "booking_salt": "SYNTHETIC-SALT", "remark": "",
            "internal_purpose": "INTERNAL-MUST-NOT-PRINT", "collaboration_project": "PRIVATE-PROJECT"}
    return [
        {**base, "items": [{"product_category": "睡眠喷雾", "product_name": "（喷雾）合成雪松喷雾", "quantity": 1}]},
        {**base, "remark": "加保护包装", "items": [
            {"product_category": "香包", "product_name": "合成花香", "quantity": 2},
            {"product_category": "临时物料", "product_name": "拍摄背景板", "quantity": 1}]},
        {**base, "express_company": "中通", "items": [
            {"product_category": "线香", "product_name": "合成木香", "quantity": 3}]},
    ]


SETTINGS = {"partnerId": "SYNTHETIC-ID", "partnerKey": "SYNTHETIC-KEY", "net": "cainiao",
            "third_template_url": "https://example.test/standard/SYNTHETIC",
            "third_custom_template_url": "https://example.test/custom/SYNTHETIC",
            "code": "SYNTHETIC-MONTHLY", "cargo_name": "香氛商品"}


def main():
    for shipment in examples():
        original = copy.deepcopy(shipment)
        param, result = capture(shipment, SETTINGS)
        assert result["success"]
        assert shipment == original
        assert param["customParam"]["itemSummary"] == shipping.build_label_remark(shipment)
        assert param["orderId"] == shipment["booking_request_id"] and param["reorder"] is False
        assert param["salt"] == shipment["booking_salt"]
        assert param["code"] == SETTINGS["code"]
        assert param["thirdTemplateURL"] == SETTINGS["third_template_url"]
        assert param["thirdCustomTemplateUrl"] == SETTINGS["third_custom_template_url"]
        assert "INTERNAL-MUST-NOT-PRINT" not in json.dumps(param)
        assert "PRIVATE-PROJECT" not in json.dumps(param)
        if shipment["express_company"] == "顺丰":
            assert 0 < len(param["cargo"]) <= 20
            assert param["cargo"] != param["remark"]
            assert param["customParam"]["cargo"] == param["cargo"]
        else:
            assert param["cargo"] == shipping.build_label_item_summary(shipment["items"], 50)

    single = examples()[0]
    assert shipping.build_label_item_summary(single["items"], 50) == shipping.build_label_remark(single)
    param, _ = capture(single, SETTINGS)
    assert param["cargo"] == "睡眠喷雾"
    # Unrelated carriers / direct SF are byte-for-byte compatible in these fields.
    for company, net in [("圆通", "cainiao"), ("中通", "taobao"), ("顺丰", "direct")]:
        row = {**single, "express_company": company}
        param, _ = capture(row, {**SETTINGS, "net": net})
        assert param["cargo"] == shipping.build_label_item_summary(row["items"], 50)
        assert param["remark"] == shipping.build_label_remark(row)
    for net in ("cainiao", "taobao"):
        for items in (None, [], [None], [{"product_name": "纸盒", "product_category": "商品"}],
                      [{"product_category": "合成长分类" * 20, "product_name": "合成名称" * 100}]):
            row = {**single, "items": items, "remark": "香氛商品"}
            param, _ = capture(row, {**SETTINGS, "net": net})
            assert 0 < len(param["cargo"]) <= 20 and param["cargo"] != param["remark"]
            assert param["customParam"]["itemSummary"] == shipping.build_label_remark(row)
    # Existing failure and recovery behaviour must not be converted into fake success.
    for payload, succeeds in [
        ({"success": False, "code": 30005, "message": "itemName和goods_description取值不能相同"}, False),
        ({"success": False, "code": 30011, "data": {"kuaidinum": "SYNTHETIC", "taskId": "SYNTHETIC"}}, True),
        ({"success": False, "code": 30011, "data": {"kuaidinum": "SYNTHETIC"}}, False),
    ]:
        _, result = capture(single, SETTINGS, payload)
        assert result["success"] is succeeds
    print("shipping cargo contract tests passed (provider acceptance not simulated)")


if __name__ == "__main__":
    main()
