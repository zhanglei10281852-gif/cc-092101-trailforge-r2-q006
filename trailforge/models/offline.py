from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.database.base import Base, UTCDateTime, utc_now
from trailforge.models.mixins import IntegerPrimaryKeyMixin


class OfflineImportRecord(IntegerPrimaryKeyMixin, Base):
    """Durable record of a successfully applied offline action pack.

    The unique fingerprint makes re-importing the same pack a no-op; the
    summary stores only non-sensitive bookkeeping data (counts, hashes).
    """

    __tablename__ = "offline_import_records"
    __table_args__ = (
        UniqueConstraint("pack_id", name="uq_offline_import_pack"),
        Index("ix_offline_import_expedition", "expedition_id"),
    )

    pack_id: Mapped[str] = mapped_column(String(80), nullable=False)
    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), nullable=False
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entry_fingerprints: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    imported_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
