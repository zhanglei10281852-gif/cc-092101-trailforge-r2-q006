from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import ChecklistStatus, EmergencyStatus, EmergencyType, RiskLevel
from trailforge.schemas.common import require_aware

PACK_FORMAT = "trailforge.action_pack"
PACK_VERSION = "1.0"
DIGEST_ALGORITHM = "SHA256"

EntryTypeName = Literal["check_in", "incident", "incident_update", "gear_check"]


def _coordinate_pair(latitude: float | None, longitude: float | None) -> None:
    if (latitude is None) != (longitude is None):
        raise ValueError("latitude and longitude must be provided together")


class OfflineCheckIn(BaseModel):
    """Completion of a scheduled check-in slot while offline."""

    entry_type: Literal["check_in"] = "check_in"
    uid: str = Field(min_length=8, max_length=36)
    slot_id: int = Field(gt=0)
    checked_in_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str = Field(default="", max_length=4000)
    is_safe: bool
    recorded_at: datetime

    @field_validator("checked_in_at", "recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> OfflineCheckIn:
        _coordinate_pair(self.latitude, self.longitude)
        return self


class OfflineIncident(BaseModel):
    """A new emergency incident recorded on the timeline while offline."""

    entry_type: Literal["incident"] = "incident"
    uid: str = Field(min_length=8, max_length=36)
    reported_by: int = Field(gt=0)
    incident_type: EmergencyType
    risk_level: RiskLevel
    occurred_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str = Field(min_length=1, max_length=10000)
    actions_taken: str = Field(default="", max_length=10000)
    recorded_at: datetime

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> OfflineIncident:
        _coordinate_pair(self.latitude, self.longitude)
        return self


class OfflineIncidentUpdate(BaseModel):
    """Timeline follow-up for an incident (created offline or pre-existing)."""

    entry_type: Literal["incident_update"] = "incident_update"
    uid: str = Field(min_length=8, max_length=36)
    incident_ref: str = Field(min_length=8, max_length=36)
    status: EmergencyStatus
    actions_taken: str | None = Field(default=None, max_length=10000)
    resolution: str | None = Field(default=None, max_length=10000)
    resolved_at: datetime | None = None
    recorded_at: datetime

    @field_validator("resolved_at", "recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def validate_closed(self) -> OfflineIncidentUpdate:
        closed = {EmergencyStatus.RESOLVED, EmergencyStatus.FALSE_ALARM}
        if self.status in closed and not (self.resolution or "").strip():
            raise ValueError("closed incidents require a resolution")
        if self.status in closed and self.resolved_at is None:
            raise ValueError("closed incidents require resolved_at")
        return self


class OfflineGearCheck(BaseModel):
    """Gear checklist verification performed while offline."""

    entry_type: Literal["gear_check"] = "gear_check"
    uid: str = Field(min_length=8, max_length=36)
    user_id: int = Field(gt=0)
    catalog_id: int = Field(gt=0)
    quantity: int = Field(ge=0)
    status: ChecklistStatus
    verified_by: int | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=4000)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def verifier_required(self) -> OfflineGearCheck:
        if self.status.value == "verified" and self.verified_by is None:
            raise ValueError("verified status requires verified_by")
        return self


class PackConflict(BaseModel):
    entry_uid: str | None = None
    entry_type: EntryTypeName | str = ""
    conflict_type: str
    entity_type: str = ""
    local_id: int | None = None
    detail: dict = Field(default_factory=dict)


class ImportReport(BaseModel):
    pack_id: str
    pack_digest: str
    expedition_id: int
    outcome: Literal["committed", "no_op", "rejected", "conflicts"]
    replayed: bool = False
    entries_total: int
    applied: list[dict] = Field(default_factory=list)
    duplicates: list[dict] = Field(default_factory=list)
    conflicts: list[PackConflict] = Field(default_factory=list)
    rejection_reason: str | None = None


class ExportSummary(BaseModel):
    pack_id: str
    expedition_id: int
    exported_at: datetime
    entry_sections: dict[str, int]
    baseline_sections: list[str]
    digest: str
