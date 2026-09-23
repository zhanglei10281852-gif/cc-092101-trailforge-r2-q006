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
from trailforge.services.actionpack import ActionPackService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trailforge", description="TrailForge maintenance CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="create the SQLite schema and apply migrations")
    subparsers.add_parser("migration-status", help="show applied and pending migrations")
    subparsers.add_parser("check-db", help="run SQLite integrity and foreign-key checks")
    reset = subparsers.add_parser("reset-db", help="delete and recreate the local SQLite database")
    reset.add_argument("--confirm", action="store_true", help="confirm destructive local reset")
    export = subparsers.add_parser(
        "export-pack", help="export a portable offline action pack for one expedition"
    )
    export.add_argument("--expedition-id", type=int, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--actor-id", type=int, required=True)
    import_pack = subparsers.add_parser(
        "import-pack", help="validate and import an offline action pack"
    )
    import_pack.add_argument("--input", type=Path, required=True)
    import_pack.add_argument("--actor-id", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
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
        initialize_database(database)
        with database.session() as session:
            raw, summary = ActionPackService(
                session, secret=settings.action_pack_secret or None
            ).export_pack(args.expedition_id, actor_id=args.actor_id)
        args.output.write_bytes(raw)
        print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False))
        return 0
    if args.command == "import-pack":
        initialize_database(database)
        raw = args.input.read_bytes()
        with database.session() as session:
            report = ActionPackService(
                session, secret=settings.action_pack_secret or None
            ).import_pack(raw, actor_id=args.actor_id)
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
