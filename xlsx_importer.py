"""Tiny XLSX reader for Wanwu product imports.

The app intentionally avoids third-party runtime dependencies. This module reads
the first worksheet in a normal .xlsx file using only zipfile + ElementTree.
"""

from __future__ import annotations

import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple
from xml.etree import ElementTree as ET


PRODUCT_NAME = "名称（必填）"
PRODUCT_CATEGORY = "分类（必填）"
PRODUCT_BARCODE = "条码"
PRODUCT_PRICE = "销售价（必填）"
PRODUCT_STATUS = "商品状态"
PRODUCT_SPEC = "规格"


class XlsxImportError(Exception):
    """Raised when the product workbook cannot be read safely."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children_by_name(node: ET.Element, name: str) -> List[ET.Element]:
    return [child for child in list(node) if _local_name(child.tag) == name]


def _first_child(node: ET.Element, name: str) -> ET.Element | None:
    for child in list(node):
        if _local_name(child.tag) == name:
            return child
    return None


def _column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return value - 1


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: List[str] = []
    for si in _children_by_name(root, "si"):
        parts: List[str] = []
        for node in si.iter():
            if _local_name(node.tag) == "t" and node.text:
                parts.append(node.text)
        strings.append("".join(parts))
    return strings


def _first_worksheet_name(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    worksheet_names = sorted(
        name
        for name in names
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
    )
    if not worksheet_names:
        raise XlsxImportError("Excel 文件中没有找到 worksheet。")
    return worksheet_names[0]


def _cell_value(cell: ET.Element, shared_strings: List[str]) -> str:
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        text_parts = []
        for node in cell.iter():
            if _local_name(node.tag) == "t" and node.text:
                text_parts.append(node.text)
        return "".join(text_parts).strip()

    value_node = _first_child(cell, "v")
    if value_node is None or value_node.text is None:
        return ""

    raw = value_node.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "是" if raw == "1" else "否"
    return raw


def _read_rows(path: str) -> List[List[str]]:
    try:
        with zipfile.ZipFile(path) as zf:
            shared_strings = _load_shared_strings(zf)
            worksheet = _first_worksheet_name(zf)
            root = ET.fromstring(zf.read(worksheet))
    except zipfile.BadZipFile as exc:
        raise XlsxImportError("这不是一个有效的 .xlsx 文件。") from exc
    except FileNotFoundError as exc:
        raise XlsxImportError(f"找不到商品文件：{path}") from exc

    sheet_data = None
    for node in root.iter():
        if _local_name(node.tag) == "sheetData":
            sheet_data = node
            break
    if sheet_data is None:
        raise XlsxImportError("Excel 文件中没有 sheetData。")

    rows: List[List[str]] = []
    for row_node in _children_by_name(sheet_data, "row"):
        row_values: Dict[int, str] = {}
        max_index = -1
        for cell in _children_by_name(row_node, "c"):
            ref = cell.attrib.get("r", "")
            if not ref:
                continue
            index = _column_index(ref)
            row_values[index] = _cell_value(cell, shared_strings)
            max_index = max(max_index, index)
        if max_index >= 0:
            rows.append([row_values.get(i, "") for i in range(max_index + 1)])
    return rows


def _money_to_text(value: str) -> str:
    value = value.strip()
    if not value:
        return "0.00"
    try:
        return f"{Decimal(value):.2f}"
    except InvalidOperation:
        return value


def read_products(path: str) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    rows = _read_rows(path)
    if not rows:
        raise XlsxImportError("商品表为空。")

    header_index = None
    headers: List[str] = []
    for idx, row in enumerate(rows):
        compact = [value.strip() for value in row]
        if PRODUCT_NAME in compact and PRODUCT_CATEGORY in compact:
            header_index = idx
            headers = compact
            break
    if header_index is None:
        raise XlsxImportError("没有找到商品表头：名称（必填）/分类（必填）。")

    products: List[Dict[str, str]] = []
    skipped = 0
    seen_barcodes = set()

    for row in rows[header_index + 1 :]:
        values = {headers[i]: row[i].strip() if i < len(row) else "" for i in range(len(headers))}
        name = values.get(PRODUCT_NAME, "").strip()
        category = values.get(PRODUCT_CATEGORY, "").strip()
        barcode = values.get(PRODUCT_BARCODE, "").strip()
        if not name or not category or not barcode:
            skipped += 1
            continue
        if barcode in seen_barcodes:
            skipped += 1
            continue
        seen_barcodes.add(barcode)
        products.append(
            {
                "barcode": barcode,
                "name": name,
                "category": category,
                "price": _money_to_text(values.get(PRODUCT_PRICE, "")),
                "status": values.get(PRODUCT_STATUS, "启用").strip() or "启用",
                "spec": values.get(PRODUCT_SPEC, "").strip(),
            }
        )

    return products, {"rows": len(rows), "products": len(products), "skipped": skipped}
