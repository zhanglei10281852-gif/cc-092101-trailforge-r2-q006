from __future__ import annotations

from sqlalchemy import select

from trailforge.models.actionpack import (
    ActionPackConflict,
    ActionPackEntryReceipt,
    ActionPackImport,
)
from trailforge.repositories.base import BaseRepository


class ActionPackImportRepository(BaseRepository[ActionPackImport]):
    model = ActionPackImport

    def get_by_digest(self, pack_digest: str) -> ActionPackImport | None:
        return self.session.scalar(
            select(ActionPackImport).where(ActionPackImport.pack_digest == pack_digest)
        )

    def latest_for_pack(self, pack_id: str) -> ActionPackImport | None:
        return self.session.scalar(
            select(ActionPackImport)
            .where(ActionPackImport.pack_id == pack_id)
            .order_by(ActionPackImport.imported_at.desc(), ActionPackImport.id.desc())
            .limit(1)
        )

    def seen_entry_uids(self, pack_id: str) -> dict[str, ActionPackEntryReceipt]:
        rows = self.session.scalars(
            select(ActionPackEntryReceipt)
            .join(ActionPackImport, ActionPackEntryReceipt.import_id == ActionPackImport.id)
            .where(ActionPackImport.pack_id == pack_id)
        )
        return {row.entry_uid: row for row in rows}


class ActionPackConflictRepository(BaseRepository[ActionPackConflict]):
    model = ActionPackConflict


class ActionPackReceiptRepository(BaseRepository[ActionPackEntryReceipt]):
    model = ActionPackEntryReceipt
