from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.database.base import Base, UTCDateTime, utc_now
from trailforge.domain.enums import AuditAction
from trailforge.models.mixins import IntegerPrimaryKeyMixin

# Import outcomes stored on ActionPackImport.
OUTCOME_COMMITTED = "committed"
OUTCOME_NOOP = "no_op"
OUTCOME_REJECTED = "rejected"
OUTCOME_CONFLICTS = "conflicts"

# Per-entry classification stored on ActionPackEntryReceipt.
ENTRY_APPLIED = "applied"
ENTRY_DUPLICATE = "duplicate"
ENTRY_CONFLICT = "conflict"
ENTRY_REFERENCE = "reference_error"
ENTRY_ORDER = "time_order_error"
ENTRY_INVALID = "invalid"


class ActionPackImport(IntegerPrimaryKeyMixin, Base):
    """One processing attempt of an action pack, keyed by its content digest."""

    __tablename__ = "action_pack_imports"
    __table_args__ = (
        UniqueConstraint("pack_digest", name="uq_action_pack_digest"),
        Index("ix_action_pack_imports_expedition", "expedition_id"),
    )

    pack_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pack_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    exported_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pack_version: Mapped[str] = mapped_column(String(12), default="1.0", nullable=False)


class ActionPackEntryReceipt(IntegerPrimaryKeyMixin, Base):
    """Per-entry ledger so a repeated import can never create a second entity."""

    __tablename__ = "action_pack_entry_receipts"
    __table_args__ = (
        # Only one *applied* receipt may exist for an entry uid, so a pack that
        # previously conflicted can be fixed and re-imported with the same uid.
        Index(
            "uq_action_pack_entry_uid_applied",
            "entry_uid",
            unique=True,
            sqlite_where=text("status = 'applied'"),
        ),
        Index("ix_action_pack_entry_import", "import_id"),
    )

    import_id: Mapped[int] = mapped_column(
        ForeignKey("action_pack_imports.id", ondelete="CASCADE"), nullable=False
    )
    entry_uid: Mapped[str] = mapped_column(String(36), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer)


class ActionPackConflict(IntegerPrimaryKeyMixin, Base):
    """Structured conflict record kept for auditability of rejected imports."""

    __tablename__ = "action_pack_conflicts"
    __table_args__ = (Index("ix_action_pack_conflict_import", "import_id"),)

    import_id: Mapped[int] = mapped_column(
        ForeignKey("action_pack_imports.id", ondelete="CASCADE"), nullable=False
    )
    entry_uid: Mapped[str | None] = mapped_column(String(36))
    entry_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    local_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ActionPackAuditRecord(IntegerPrimaryKeyMixin, Base):
    """Sensitive-free audit trail for export and import attempts."""

    __tablename__ = "action_pack_audit"
    __table_args__ = (
        Index("ix_action_pack_audit_expedition", "expedition_id"),
        Index("ix_action_pack_audit_time", "occurred_at"),
    )

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # export | import
    action: Mapped[AuditAction] = mapped_column(String(40), nullable=False)
    expedition_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pack_id: Mapped[str | None] = mapped_column(String(36))
    pack_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
