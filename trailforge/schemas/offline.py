from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import CheckInType, ChecklistStatus, EmergencyType, RiskLevel
from trailforge.offline.pack import (
    ENTRY_CHECK_IN,
    ENTRY_GEAR_CHECK,
    ENTRY_INCIDENT,
)
from trailforge.schemas.common import clean_text, require_aware


class OfflineCheckInEntry(BaseModel):
    """离线端补录的一次签到完成（引用导出时已存在的签到计划）。"""

    expedition_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    check_in_type: CheckInType
    due_at: datetime
    checked_in_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str = Field(default="", max_length=4000)
    is_safe: bool

    @field_validator("due_at", "checked_in_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> OfflineCheckInEntry:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class OfflineIncidentEntry(BaseModel):
    expedition_id: int = Field(gt=0)
    reported_by: int = Field(gt=0)
    client_ref: str = Field(min_length=8, max_length=80)
    incident_type: EmergencyType
    risk_level: RiskLevel
    occurred_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str = Field(min_length=1, max_length=10000)
    actions_taken: str = Field(default="", max_length=10000)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return clean_text(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> OfflineIncidentEntry:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class OfflineGearCheckEntry(BaseModel):
    expedition_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    catalog_id: int = Field(gt=0)
    quantity: int = Field(ge=0, le=100000)
    status: ChecklistStatus
    verified_by: int | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def verifier_required(self) -> OfflineGearCheckEntry:
        if self.status == ChecklistStatus.VERIFIED and self.verified_by is None:
            raise ValueError("verified status requires verified_by")
        return self


OfflineEntryPayload = OfflineCheckInEntry | OfflineIncidentEntry | OfflineGearCheckEntry

ENTRY_TYPE_TO_MODEL: dict[str, type[OfflineEntryPayload]] = {
    ENTRY_CHECK_IN: OfflineCheckInEntry,
    ENTRY_INCIDENT: OfflineIncidentEntry,
    ENTRY_GEAR_CHECK: OfflineGearCheckEntry,
}


class ConflictItem(BaseModel):
    entry_id: str
    entry_type: str
    reason: Literal[
        "entity_version_changed",
        "check_in_already_submitted",
        "duplicate_client_ref_content",
    ]
    local_fingerprint: str
    existing: dict[str, Any] = Field(default_factory=dict)


class AppliedEntry(BaseModel):
    entry_id: str
    entry_type: str
    resource_type: str
    resource_id: int
    fingerprint: str


class ImportConflictReport(BaseModel):
    pack_id: str
    expedition_id: int
    baseline_expedition_version: int
    current_expedition_version: int
    conflicts: list[ConflictItem]


class ImportResult(BaseModel):
    status: Literal["applied", "no_op", "aborted_on_conflict", "applied_with_skips"]
    pack_id: str
    expedition_id: int
    manifest_sha256: str
    applied: list[AppliedEntry] = Field(default_factory=list)
    already_applied: list[str] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    total_entries: int = 0
    committed: bool


class PackExportResponse(BaseModel):
    pack_id: str
    expedition_id: int
    manifest_sha256: str
    created_at: datetime
    entry_count: int
    payload_summary: dict[str, Any]
