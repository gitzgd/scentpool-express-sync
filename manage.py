from __future__ import annotations

import argparse
import getpass
import json
from datetime import datetime
from pathlib import Path

from database import AppError, Database


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "scentpool.db"


def prompt_password(username: str) -> str:
    password = getpass.getpass(f"请输入 {username} 的新密码：")
    confirm = getpass.getpass("请再次输入：")
    if password != confirm:
        raise AppError("两次输入的密码不一致。")
    return password


def command_summary(args: argparse.Namespace) -> None:
    db = Database(str(args.db))
    summary = db.database_summary()
    print("数据库概览：")
    for key, value in summary.items():
        print(f"- {key}: {value}")
    print(f"默认账号密码仍可登录：{'是' if db.default_credentials_active() else '否'}")


def command_set_password(args: argparse.Namespace) -> None:
    password = args.password or prompt_password(args.username)
    db = Database(str(args.db))
    db.set_user_password(args.username, password)
    print(f"已更新账号密码：{args.username}")


def command_export_production(args: argparse.Namespace) -> None:
    db = Database(str(args.db))
    if db.default_credentials_active():
        raise AppError("默认账号密码仍可登录。请先为 admin 和门店账号重置密码，再导出生产数据库。")

    output = Path(args.output)
    if output.exists() and not args.overwrite:
        raise AppError(f"输出文件已存在：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    db.backup_to(output)
    print(f"已生成生产数据库：{output}")


def command_backup(args: argparse.Namespace) -> None:
    db = Database(str(args.db))
    output = args.output
    if output is None:
        output = Path(args.db).parent / "backups" / f"scentpool-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    if output.exists() and not args.overwrite:
        raise AppError(f"输出文件已存在：{output}")
    db.backup_to(output)
    print(f"已生成并校验数据库备份：{output}")


def command_diagnostics(args: argparse.Namespace) -> None:
    db = Database(str(args.db))
    print(json.dumps(db.storage_diagnostics(), ensure_ascii=False, indent=2))


def command_repair_shipment_times(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser().resolve()
    db = Database(str(db_path))
    if not args.apply:
        try:
            preview = db.preview_shipment_time_repairs()
        except AppError:
            raise
        except Exception as exc:
            raise AppError("数据库不可读或尚未具备时间修复 Schema。", 500) from exc
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    if not Path(args.db).is_absolute():
        raise AppError("apply 模式必须使用绝对数据库路径。")
    if str(args.confirm_db_path or "") != str(db_path):
        raise AppError("数据库路径二次确认不匹配，未执行修复。")
    if args.confirm != "APPLY_SHIPMENT_TIME_REPAIR":
        raise AppError("缺少固定二次确认短语，未执行修复。")
    if not args.preview_fingerprint:
        raise AppError("apply 模式必须输入 dry-run 预览指纹。")
    if args.max_rows is None or args.max_rows <= 0:
        raise AppError("apply 模式必须设置大于 0 的 --max-rows。")
    if args.backup_output is None:
        raise AppError("apply 模式必须指定 --backup-output。")
    backup_path = args.backup_output.expanduser().resolve()
    if backup_path == db_path:
        raise AppError("备份路径不能与当前数据库相同。")
    if backup_path.exists():
        raise AppError("备份文件已存在；为避免覆盖，请使用新的路径。")
    try:
        current_preview = db.preview_shipment_time_repairs()
    except AppError:
        raise
    except Exception as exc:
        raise AppError("数据库不可读或尚未具备时间修复 Schema。", 500) from exc
    if args.preview_fingerprint != current_preview["preview_fingerprint"]:
        raise AppError("数据库或修复目标与预览不一致，请重新执行 dry-run。", 409)
    try:
        db.backup_to(backup_path)
    except Exception as exc:
        raise AppError("在线备份或完整性检查失败，未执行修复。", 500) from exc
    result = db.apply_shipment_time_repairs(
        preview_fingerprint=args.preview_fingerprint,
        max_rows=args.max_rows,
        include_estimated=args.include_estimated,
    )
    result["backup_created"] = True
    result["rollback"] = "restore_verified_pre_apply_backup"
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="万物香铺快递同步管理工具")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("summary", help="查看数据库数量和默认密码状态")
    summary.set_defaults(func=command_summary)

    set_password = subparsers.add_parser("set-password", help="重置指定账号密码")
    set_password.add_argument("username", help="账号，例如 admin 或 store01")
    set_password.add_argument("--password", default="", help="新密码；不传则安全输入")
    set_password.set_defaults(func=command_set_password)

    export_production = subparsers.add_parser("export-production", help="导出可上传到云端的生产数据库")
    export_production.add_argument("--output", type=Path, required=True, help="输出 .db 文件路径")
    export_production.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出文件")
    export_production.set_defaults(func=command_export_production)

    backup = subparsers.add_parser("backup", help="使用 SQLite 在线备份并执行完整性校验")
    backup.add_argument("--output", type=Path, help="输出 .db 文件；不传则写入数据库旁的 backups 目录")
    backup.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出文件")
    backup.set_defaults(func=command_backup)

    diagnostics = subparsers.add_parser("diagnostics", help="查看数据库文件、WAL、表记录和原始报文占用")
    diagnostics.set_defaults(func=command_diagnostics)

    repair_times = subparsers.add_parser(
        "repair-shipment-times",
        help="预览或受控修复发货/签收时间；默认 dry-run 且只输出聚合",
    )
    repair_times.add_argument("--apply", action="store_true", help="显式启用写入模式")
    repair_times.add_argument("--preview-fingerprint", default="", help="最近一次 dry-run 指纹")
    repair_times.add_argument("--max-rows", type=int, help="允许修改的最大记录数")
    repair_times.add_argument("--backup-output", type=Path, help="写入前在线备份的新文件路径")
    repair_times.add_argument("--confirm-db-path", default="", help="再次输入数据库绝对路径")
    repair_times.add_argument("--confirm", default="", help="固定确认短语")
    repair_times.add_argument(
        "--include-estimated",
        action="store_true",
        help="额外授权写入估算时间；未设置时只应用强证据精确修复",
    )
    repair_times.set_defaults(func=command_repair_shipment_times)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except AppError as exc:
        parser.exit(1, f"错误：{exc.message}\n")


if __name__ == "__main__":
    main()
