from __future__ import annotations

import argparse
import json
from pathlib import Path

from trailforge.config import get_settings
from trailforge.database.migrations import (
    assert_database_integrity,
    initialize_database,
    migration_status,
)
from trailforge.database.session import Database
from trailforge.offline.pack import ENTRY_TYPES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trailforge", description="TrailForge maintenance CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="create the SQLite schema and apply migrations")
    subparsers.add_parser("migration-status", help="show applied and pending migrations")
    subparsers.add_parser("check-db", help="run SQLite integrity and foreign-key checks")
    reset = subparsers.add_parser("reset-db", help="delete and recreate the local SQLite database")
    reset.add_argument("--confirm", action="store_true", help="confirm destructive local reset")

    export_pack = subparsers.add_parser(
        "export-pack", help="export an offline action pack for one expedition"
    )
    export_pack.add_argument("expedition_id", type=int)
    export_pack.add_argument("--actor-id", type=int, required=True)
    export_pack.add_argument("--output", type=Path, required=True)

    import_pack = subparsers.add_parser(
        "import-pack", help="import an offline action pack back into the database"
    )
    import_pack.add_argument("pack_file", type=Path)
    import_pack.add_argument("--actor-id", type=int, required=True)
    import_pack.add_argument(
        "--on-conflict",
        choices=("abort", "skip_conflicts"),
        default="abort",
        help="abort (default): reject the whole pack on any conflict; "
        "skip_conflicts: commit non-conflicting entries only",
    )

    add_entry = subparsers.add_parser(
        "pack-add-entry",
        help="offline: append a check-in/incident/gear-check entry to a pack file",
    )
    add_entry.add_argument("pack_file", type=Path)
    add_entry.add_argument("--type", choices=sorted(ENTRY_TYPES), required=True)
    add_entry.add_argument(
        "--data",
        type=Path,
        required=True,
        help="path to a UTF-8 JSON file with the entry payload",
    )

    inspect_pack = subparsers.add_parser(
        "pack-inspect", help="offline: verify and summarize an action pack file"
    )
    inspect_pack.add_argument("pack_file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    # 纯离线文件命令不需要数据库连接，可在无网络的另一台电脑上运行。
    if args.command == "pack-add-entry":
        from trailforge.cli_offline import add_entry_file

        print(json.dumps(add_entry_file(args.pack_file, args.type, args.data), ensure_ascii=False))
        return 0
    if args.command == "pack-inspect":
        from trailforge.cli_offline import inspect_pack

        print(json.dumps(inspect_pack(args.pack_file), ensure_ascii=False))
        return 0

    settings = get_settings()
    database = Database(settings)
    if args.command == "init-db":
        applied = initialize_database(database)
        print(json.dumps({"database": str(database.path), "applied": applied}, ensure_ascii=False))
        return 0
    if args.command == "migration-status":
        print(json.dumps(migration_status(database), ensure_ascii=False))
        return 0
    if args.command == "check-db":
        result = assert_database_integrity(database)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["healthy"] else 1
    if args.command == "reset-db":
        if not args.confirm:
            parser = build_parser()
            parser.error("reset-db requires --confirm")
        path = database.path
        if path is None:
            raise SystemExit("reset-db is unavailable for in-memory SQLite")
        database.engine.dispose()
        allowed_suffixes = {".db", ".sqlite", ".sqlite3"}
        if path.suffix.lower() not in allowed_suffixes:
            raise SystemExit("refusing to reset a file without a SQLite extension")
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.exists():
                candidate.unlink()
        recreated = Database(settings)
        applied = initialize_database(recreated)
        print(json.dumps({"database": str(path), "applied": applied}, ensure_ascii=False))
        return 0
    if args.command == "export-pack":
        from trailforge.cli_offline import export_pack

        summary = export_pack(
            database,
            args.expedition_id,
            actor_id=args.actor_id,
            output=args.output,
        )
        print(json.dumps({"output": str(args.output), "summary": summary}, ensure_ascii=False))
        return 0
    if args.command == "import-pack":
        from trailforge.cli_offline import import_pack_file

        result = import_pack_file(
            database,
            args.pack_file,
            actor_id=args.actor_id,
            on_conflict=args.on_conflict,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"applied", "no_op", "applied_with_skips"} else 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
