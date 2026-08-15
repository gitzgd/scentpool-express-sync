#!/usr/bin/env python3
"""Atomically install the audited daily collector into the local Codex directory."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "scentpool_daily_audit_probe.py"
DESTINATION = Path.home() / ".codex" / "scentpool_daily_audit_probe.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="安装万物香铺只读每日盘点采集器")
    parser.add_argument("--check", action="store_true", help="仅检查本机版本是否与仓库一致")
    args = parser.parse_args()

    if not SOURCE.is_file():
        raise SystemExit("仓库内采集器不存在。")
    if args.check:
        destination_mode = stat.S_IMODE(DESTINATION.stat().st_mode) if DESTINATION.is_file() else None
        if (
            not DESTINATION.is_file()
            or digest(SOURCE) != digest(DESTINATION)
            or destination_mode != 0o700
        ):
            print("本机采集器需要更新。")
            return 1
        print("本机采集器已与仓库版本一致。")
        return 0

    DESTINATION.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=DESTINATION.parent,
            prefix=f".{DESTINATION.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            with SOURCE.open("rb") as source:
                shutil.copyfileobj(source, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o700)
        os.replace(temporary_name, DESTINATION)
        temporary_name = ""
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)

    if digest(SOURCE) != digest(DESTINATION):
        raise SystemExit("本机采集器安装后校验失败。")
    print(f"已安全更新：{DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
