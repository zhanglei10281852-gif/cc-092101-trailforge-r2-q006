from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trailforge.offline.pack import (
    ActionPackIntegrityError,
    ActionPackValidationError,
    add_offline_entry,
    load_pack,
    save_pack,
)


def export_pack(
    database: Any, expedition_id: int, *, actor_id: int, output: Path
) -> dict[str, Any]:
    from trailforge.services.offline import OfflinePackService

    with database.session() as session:
        pack, summary = OfflinePackService(session).export_pack(
            expedition_id, actor_id=actor_id
        )
    save_pack(pack, output)
    return summary.model_dump(mode="json")


def import_pack_file(
    database: Any,
    path: Path,
    *,
    actor_id: int,
    on_conflict: str,
) -> dict[str, Any]:
    from trailforge.errors import TrailForgeError
    from trailforge.services.offline import OfflinePackService

    try:
        pack = load_pack(path)
    except (ActionPackIntegrityError, ActionPackValidationError) as exc:
        # 拒绝记录在独立事务中留痕（不含正文）。
        with database.session() as audit_session:
            OfflinePackService(audit_session).log_rejection(
                pack=None,
                reason="action_pack_integrity",
                actor_id=actor_id,
                detail={"file": str(path), "error": str(exc)},
            )
        raise SystemExit(f"action pack rejected: {exc}") from exc

    def operation(session: Any) -> dict[str, Any]:
        result = OfflinePackService(session).import_pack(
            pack, actor_id=actor_id, on_conflict=on_conflict
        )
        return result.model_dump(mode="json")

    try:
        return database.run_write(operation)
    except TrailForgeError as exc:
        # 业务事务已回滚；用独立事务留下不含正文的拒绝审计记录。
        with database.session() as audit_session:
            OfflinePackService(audit_session).log_rejection(
                pack=pack, reason=exc.code, actor_id=actor_id, detail=exc.context
            )
        detail = getattr(exc, "result", None) or exc.context
        raise SystemExit(
            f"action pack rejected ({exc.code}): "
            f"{json.dumps(detail, ensure_ascii=False)}"
        ) from exc


def add_entry_file(pack_path: Path, entry_type: str, data_path: Path) -> dict[str, Any]:
    """离线端操作：从 JSON 文件读取条目数据，追加进行动包并重封。

    此命令不连接数据库，可在无网络的另一台电脑上对同一个包反复执行。
    """

    pack = load_pack(pack_path)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    entry = add_offline_entry(pack, entry_type, data)
    save_pack(pack, pack_path)
    return {"entry_id": entry["entry_id"], "type": entry["type"], "pack": str(pack_path)}


def inspect_pack(path: Path) -> dict[str, Any]:
    pack = load_pack(path)
    manifest = pack["manifest"]
    payload = pack["payload"]
    entries = manifest["entries"]
    return {
        "format": manifest["format"],
        "format_version": manifest["format_version"],
        "pack_id": manifest["pack_id"],
        "expedition_id": manifest["expedition_id"],
        "created_at": manifest["created_at"],
        "manifest_sha256": pack["checksum"]["manifest_sha256"],
        "entry_count": len(entries),
        "entries_by_type": {
            entry_type: sum(1 for entry in entries if entry["type"] == entry_type)
            for entry_type in sorted({entry["type"] for entry in entries})
        },
        "payload_sections": sorted(payload.keys()),
    }
