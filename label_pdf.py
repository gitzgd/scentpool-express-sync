from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


LABEL_PDF_HOST_SUFFIXES = ("kuaidi100.com", "cainiao.com", "aliyuncs.com")
DOWNLOAD_CHUNK_BYTES = 64 * 1024
DEFAULT_MEMORY_LIMIT_MB = 192
MAX_MEMORY_LIMIT_MB = 224


class LabelPdfError(Exception):
    pass


def apply_process_memory_limit() -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource

        configured = int(os.environ.get("SCENTPOOL_LABEL_PROCESS_MEMORY_MB", str(DEFAULT_MEMORY_LIMIT_MB)))
        limit_bytes = max(128, min(configured, MAX_MEMORY_LIMIT_MB)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, OSError, ValueError):
        return


def trusted_label_pdf_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith(f".{suffix}") for suffix in LABEL_PDF_HOST_SUFFIXES
    )


def download_label_pdf(
    url: str,
    business_id: str,
    target: Path,
    *,
    max_bytes: int,
) -> int:
    if not trusted_label_pdf_url(url):
        raise LabelPdfError(f"订单 {business_id} 的面单地址不受信任。")
    request = urllib.request.Request(url, headers={"User-Agent": "ScentpoolExpress/1.0"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as handle:
            final_url = response.geturl()
            if not trusted_label_pdf_url(final_url):
                raise LabelPdfError(f"订单 {business_id} 的面单跳转地址不受信任。")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise LabelPdfError(f"订单 {business_id} 的面单文件超过限制。")
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise LabelPdfError(f"订单 {business_id} 的面单文件超过限制。")
                handle.write(chunk)
    except LabelPdfError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LabelPdfError(f"订单 {business_id} 的面单下载失败：{exc}") from exc

    try:
        with target.open("rb") as handle:
            signature = handle.read(1024).lstrip()
    except OSError as exc:
        raise LabelPdfError(f"订单 {business_id} 的面单文件无法读取。") from exc
    if not signature.startswith(b"%PDF"):
        raise LabelPdfError(f"订单 {business_id} 返回的面单不是 PDF。")
    return total


def merge_label_pdfs(
    shipments: list[dict[str, Any]],
    output_path: Path,
    *,
    max_label_bytes: int,
    max_total_bytes: int,
) -> dict[str, int]:
    writer = PdfWriter()
    total_bytes = 0
    page_count = 0
    with tempfile.TemporaryDirectory(prefix="scentpool-label-source-") as source_dir:
        source_root = Path(source_dir)
        source_index = 0
        for shipment in shipments:
            business_id = str(shipment.get("business_id") or shipment.get("id") or "未知")
            label_urls = shipment.get("label_urls")
            if not isinstance(label_urls, list) or not label_urls:
                raise LabelPdfError(f"订单 {business_id} 没有可打印的面单。")
            for label_url in label_urls:
                source_index += 1
                source_path = source_root / f"label-{source_index}.pdf"
                total_bytes += download_label_pdf(
                    str(label_url), business_id, source_path, max_bytes=max_label_bytes
                )
                if total_bytes > max_total_bytes:
                    raise LabelPdfError("本次合并的面单文件超过限制，请减少勾选数量后重试。")
                try:
                    reader = PdfReader(str(source_path), strict=False)
                    for page in reader.pages:
                        writer.add_page(page)
                        page_count += 1
                except (PdfReadError, ValueError, OSError) as exc:
                    raise LabelPdfError(f"订单 {business_id} 的面单 PDF 无法解析。") from exc

        if page_count == 0:
            raise LabelPdfError("没有可合并的面单页面。")
        try:
            with output_path.open("wb") as output:
                writer.write(output)
        except OSError as exc:
            raise LabelPdfError("合并后的面单文件无法写入。") from exc
    return {"source_bytes": total_bytes, "pages": page_count, "output_bytes": output_path.stat().st_size}


def run_job(job_path: Path, output_path: Path) -> dict[str, int]:
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LabelPdfError("批量面单任务文件无效。") from exc
    shipments = job.get("shipments")
    if not isinstance(shipments, list):
        raise LabelPdfError("批量面单任务缺少订单数据。")
    return merge_label_pdfs(
        shipments,
        output_path,
        max_label_bytes=int(job.get("max_label_bytes") or 10 * 1024 * 1024),
        max_total_bytes=int(job.get("max_total_bytes") or 60 * 1024 * 1024),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="在隔离进程中合并电子面单 PDF")
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    apply_process_memory_limit()
    try:
        result = run_job(Path(args.job), Path(args.output))
    except (LabelPdfError, MemoryError) as exc:
        if isinstance(exc, MemoryError):
            exc = LabelPdfError("本次面单数量或文件体积过大，请减少勾选数量后重试。")
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
