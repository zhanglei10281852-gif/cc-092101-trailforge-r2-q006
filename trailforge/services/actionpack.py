from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    AuditAction,
    EmergencyStatus,
    RegistrationStatus,
)
from trailforge.errors import (
    ActionPackIntegrityError,
    ActionPackValidationError,
    NotFoundError,
)
from trailforge.models.actionpack import (
    ENTRY_APPLIED,
    ENTRY_CONFLICT,
    ENTRY_DUPLICATE,
    OUTCOME_COMMITTED,
    OUTCOME_CONFLICTS,
    OUTCOME_REJECTED,
    ActionPackAuditRecord,
    ActionPackConflict,
    ActionPackEntryReceipt,
    ActionPackImport,
)
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.models.gear import ActivityGearCheck, ActivityGearRequirement, GearCatalog
from trailforge.models.routes import (
    RiskTag,
    RoutePoint,
    RouteSegment,
    TrailRoute,
    route_risk_tags,
)
from trailforge.models.safety import (
    EmergencyIncident,
    ItineraryCheckIn,
    RiskAssessment,
)
from trailforge.models.users import User
from trailforge.schemas.actionpack import (
    PACK_FORMAT,
    PACK_VERSION,
    ExportSummary,
    ImportReport,
    OfflineCheckIn,
    OfflineGearCheck,
    OfflineIncident,
    OfflineIncidentUpdate,
    PackConflict,
)
from trailforge.services.actionpack_codec import (
    decode_pack_bytes,
    encode_pack_bytes,
    open_pack,
    seal_pack,
)
from trailforge.services.base import ServiceBase

FUTURE_SKEW = timedelta(seconds=60)

ENTRY_MODELS: dict[str, type] = {
    "check_in": OfflineCheckIn,
    "incident": OfflineIncident,
    "incident_update": OfflineIncidentUpdate,
    "gear_check": OfflineGearCheck,
}

# Allowed emergency status transitions for imported timeline updates.
INCIDENT_TRANSITIONS: dict[EmergencyStatus, set[EmergencyStatus]] = {
    EmergencyStatus.OPEN: {
        EmergencyStatus.MONITORING,
        EmergencyStatus.RESOLVED,
        EmergencyStatus.FALSE_ALARM,
    },
    EmergencyStatus.MONITORING: {
        EmergencyStatus.OPEN,
        EmergencyStatus.RESOLVED,
        EmergencyStatus.FALSE_ALARM,
    },
    EmergencyStatus.RESOLVED: set(),
    EmergencyStatus.FALSE_ALARM: set(),
}


class ActionPackService(ServiceBase):
    """Export a portable offline pack and re-import leader entries atomically."""

    def __init__(self, session: Session, *, secret: str | None = None) -> None:
        super().__init__(session)
        self.secret = secret

    # ------------------------------------------------------------------ export

    def export_pack(
        self,
        expedition_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> tuple[bytes, ExportSummary]:
        current = now or utc_now()
        expedition = self.session.get(Expedition, expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        route = self.session.get(TrailRoute, expedition.route_id)
        if route is None:  # pragma: no cover - route is FK-RESTRICT protected
            raise NotFoundError(f"TrailRoute {expedition.route_id} was not found")
        pack_id = str(uuid.uuid4())
        manifest = {
            "format": PACK_FORMAT,
            "version": PACK_VERSION,
            "pack_id": pack_id,
            "expedition_id": expedition_id,
            "exported_at": current.isoformat(),
            "generated_by": actor_id,
            "baseline": {
                "generated_at": current.isoformat(),
                "expedition": {
                    "id": expedition.id,
                    "version": expedition.version,
                    "updated_at": expedition.updated_at.isoformat(),
                },
                "route": {
                    "id": route.id,
                    "version": route.version,
                    "updated_at": route.updated_at.isoformat(),
                },
            },
            "snapshot": {
                "expedition": self._expedition_snapshot(expedition),
                "route": self._route_snapshot(route),
                "members": self._member_snapshots(expedition_id),
                "gear": self._gear_snapshots(expedition_id),
                "safety_plan": self._safety_snapshots(expedition_id),
            },
            "offline": {
                "check_ins": [],
                "incidents": [],
                "incident_updates": [],
                "gear_checks": [],
            },
        }
        envelope = seal_pack(manifest, secret=self.secret)
        digest = envelope["digest"]
        summary = ExportSummary(
            pack_id=pack_id,
            expedition_id=expedition_id,
            exported_at=current,
            entry_sections={
                "members": len(manifest["snapshot"]["members"]),
                "gear_catalog": len(manifest["snapshot"]["gear"]["catalog"]),
                "gear_requirements": len(manifest["snapshot"]["gear"]["requirements"]),
                "check_in_slots": len(manifest["snapshot"]["safety_plan"]["check_in_slots"]),
                "risk_assessments": len(
                    manifest["snapshot"]["safety_plan"]["risk_assessments"]
                ),
                "open_incidents": len(manifest["snapshot"]["safety_plan"]["open_incidents"]),
            },
            baseline_sections=["expedition", "route"],
            digest=digest,
        )
        self.session.add(
            ActionPackAuditRecord(
                actor_id=actor_id,
                stage="export",
                action=AuditAction.ACTION_PACK_EXPORTED,
                expedition_id=expedition_id,
                pack_id=pack_id,
                pack_digest=digest,
                outcome="exported",
                summary={
                    "entry_sections": summary.entry_sections,
                    "baseline_sections": summary.baseline_sections,
                    "route_revision": route.version,
                    "activity_revision": expedition.version,
                },
            )
        )
        self.session.flush()
        return encode_pack_bytes(envelope), summary

    @staticmethod
    def _expedition_snapshot(expedition: Expedition) -> dict[str, Any]:
        # Free-text description/cancellation_reason are intentionally excluded:
        # the field leader needs logistics facts, not potentially sensitive notes.
        return {
            "id": expedition.id,
            "organizer_id": expedition.organizer_id,
            "route_id": expedition.route_id,
            "name": expedition.name,
            "meeting_location": expedition.meeting_location,
            "meeting_at": expedition.meeting_at.isoformat(),
            "start_at": expedition.start_at.isoformat(),
            "end_at": expedition.end_at.isoformat(),
            "registration_deadline": expedition.registration_deadline.isoformat(),
            "capacity": expedition.capacity,
            "minimum_fitness_level": expedition.minimum_fitness_level,
            "status": str(expedition.status),
            "risk_level": str(expedition.risk_level),
            "version": expedition.version,
            "updated_at": expedition.updated_at.isoformat(),
        }

    def _route_snapshot(self, route: TrailRoute) -> dict[str, Any]:
        segments = self.session.scalars(
            select(RouteSegment)
            .where(RouteSegment.route_id == route.id)
            .order_by(RouteSegment.sequence)
        )
        points = self.session.scalars(
            select(RoutePoint)
            .where(RoutePoint.route_id == route.id)
            .order_by(RoutePoint.sequence)
        )
        tag_rows = self.session.execute(
            select(RiskTag)
            .join(route_risk_tags, route_risk_tags.c.risk_tag_id == RiskTag.id)
            .where(route_risk_tags.c.route_id == route.id)
            .order_by(RiskTag.code)
        ).scalars()
        return {
            "id": route.id,
            "name": route.name,
            "region": route.region,
            "distance_km": route.distance_km,
            "elevation_gain_m": route.elevation_gain_m,
            "elevation_loss_m": route.elevation_loss_m,
            "min_altitude_m": route.min_altitude_m,
            "max_altitude_m": route.max_altitude_m,
            "estimated_duration_minutes": route.estimated_duration_minutes,
            "difficulty": str(route.difficulty),
            "is_loop": route.is_loop,
            "is_published": route.is_published,
            "version": route.version,
            "updated_at": route.updated_at.isoformat(),
            "segments": [
                {
                    "sequence": segment.sequence,
                    "name": segment.name,
                    "distance_km": segment.distance_km,
                    "elevation_gain_m": segment.elevation_gain_m,
                    "estimated_duration_minutes": segment.estimated_duration_minutes,
                    "difficulty": str(segment.difficulty),
                    "start_latitude": segment.start_latitude,
                    "start_longitude": segment.start_longitude,
                    "end_latitude": segment.end_latitude,
                    "end_longitude": segment.end_longitude,
                }
                for segment in segments
            ],
            "points": [
                {
                    "sequence": point.sequence,
                    "name": point.name,
                    "point_type": str(point.point_type),
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "altitude_m": point.altitude_m,
                    "distance_from_start_km": point.distance_from_start_km,
                }
                for point in points
            ],
            "risk_tags": [
                {"code": tag.code, "name": tag.name, "level": str(tag.level)}
                for tag in tag_rows
            ],
        }

    def _member_snapshots(self, expedition_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(ExpeditionRegistration, User)
            .join(User, User.id == ExpeditionRegistration.user_id)
            .where(ExpeditionRegistration.expedition_id == expedition_id)
            .order_by(ExpeditionRegistration.id)
        ).all()
        members: list[dict[str, Any]] = []
        for registration, user in rows:
            # Essentials for field coordination only: no email, birth date, sport
            # profile, emergency contacts or health restriction text.
            members.append(
                {
                    "user_id": user.id,
                    "display_name": user.display_name,
                    "phone": user.phone,
                    "role": str(registration.role),
                    "status": str(registration.status),
                }
            )
        return members

    def _gear_snapshots(self, expedition_id: int) -> dict[str, Any]:
        requirements = list(
            self.session.scalars(
                select(ActivityGearRequirement)
                .where(ActivityGearRequirement.expedition_id == expedition_id)
                .order_by(ActivityGearRequirement.id)
            )
        )
        catalog_ids = sorted({item.catalog_id for item in requirements})
        catalogs = []
        if catalog_ids:
            catalogs = list(
                self.session.scalars(
                    select(GearCatalog)
                    .where(GearCatalog.id.in_(catalog_ids))
                    .order_by(GearCatalog.id)
                )
            )
        return {
            "catalog": [
                {
                    "id": item.id,
                    "sku": item.sku,
                    "name": item.name,
                    "category": item.category,
                    "safety_critical": item.safety_critical,
                }
                for item in catalogs
            ],
            "requirements": [
                {
                    "catalog_id": item.catalog_id,
                    "quantity_per_person": item.quantity_per_person,
                    "quantity_for_group": item.quantity_for_group,
                    "mandatory": item.mandatory,
                }
                for item in requirements
            ],
        }

    def _safety_snapshots(self, expedition_id: int) -> dict[str, Any]:
        slots = list(
            self.session.scalars(
                select(ItineraryCheckIn)
                .where(ItineraryCheckIn.expedition_id == expedition_id)
                .order_by(ItineraryCheckIn.due_at, ItineraryCheckIn.id)
            )
        )
        assessments = list(
            self.session.scalars(
                select(RiskAssessment)
                .where(RiskAssessment.expedition_id == expedition_id)
                .order_by(RiskAssessment.id)
            )
        )
        incidents = list(
            self.session.scalars(
                select(EmergencyIncident)
                .where(
                    EmergencyIncident.expedition_id == expedition_id,
                    EmergencyIncident.status.in_(
                        [EmergencyStatus.OPEN, EmergencyStatus.MONITORING]
                    ),
                )
                .order_by(EmergencyIncident.occurred_at, EmergencyIncident.id)
            )
        )
        return {
            "check_in_slots": [
                {
                    "id": slot.id,
                    "user_id": slot.user_id,
                    "check_in_type": str(slot.check_in_type),
                    "due_at": slot.due_at.isoformat(),
                    "checked_in_at": (
                        slot.checked_in_at.isoformat() if slot.checked_in_at else None
                    ),
                }
                for slot in slots
            ],
            "risk_assessments": [
                {
                    "id": item.id,
                    "assessor_id": item.assessor_id,
                    "category": item.category,
                    "hazard": item.hazard,
                    "likelihood": item.likelihood,
                    "impact": item.impact,
                    "score": item.score,
                    "risk_level": str(item.risk_level),
                    "mitigation": item.mitigation,
                }
                for item in assessments
            ],
            "open_incidents": [
                {
                    "id": item.id,
                    "version": item.version,
                    "incident_type": str(item.incident_type),
                    "risk_level": str(item.risk_level),
                    "status": str(item.status),
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in incidents
            ],
        }

    # ------------------------------------------------------------------ import

    def import_pack(
        self,
        raw: bytes,
        *,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> ImportReport:
        current = now or utc_now()
        try:
            envelope = decode_pack_bytes(raw)
            manifest = open_pack(envelope, secret=self.secret)
        except ActionPackIntegrityError as exc:
            self._rejection_audit(actor_id=actor_id, reason=exc.message, digest=None)
            raise

        expedition_id = manifest.get("expedition_id")
        pack_id = manifest.get("pack_id")
        digest = envelope.get("digest") if isinstance(envelope, dict) else None
        try:
            self._validate_manifest_header(manifest)
            entries = self._parse_entries(manifest)
        except ActionPackValidationError as exc:
            self._rejection_audit(
                actor_id=actor_id,
                expedition_id=expedition_id if isinstance(expedition_id, int) else None,
                pack_id=pack_id if isinstance(pack_id, str) else None,
                digest=digest,
                reason=exc.message,
                summary={"errors": exc.context.get("errors", [])},
            )
            raise

        expedition = self.session.get(Expedition, expedition_id)
        if expedition is None:
            error = ActionPackValidationError(
                "source expedition does not exist at headquarters",
                context={"expedition_id": expedition_id},
            )
            self._rejection_audit(
                actor_id=actor_id,
                expedition_id=expedition_id if isinstance(expedition_id, int) else None,
                pack_id=pack_id if isinstance(pack_id, str) else None,
                digest=digest,
                reason=error.message,
            )
            raise error

        existing = self.session.scalar(
            select(ActionPackImport).where(ActionPackImport.pack_digest == digest)
        )
        if existing is not None:
            # Identical pack bytes: short circuit before any write, including
            # without producing a new audit row.
            return self._report_for_existing(existing)

        baseline_conflicts = self._check_baseline(manifest, expedition)
        plan = self._build_plan(
            manifest=manifest,
            entries=entries,
            expedition=expedition,
            current=current,
        )
        conflicts = baseline_conflicts + plan["conflicts"]
        if conflicts:
            return self._persist_conflicts(
                manifest=manifest,
                digest=digest,
                expedition=expedition,
                entries=entries,
                conflicts=conflicts,
                actor_id=actor_id,
            )
        return self._apply_plan(
            manifest=manifest,
            digest=digest,
            expedition=expedition,
            plan=plan,
            actor_id=actor_id,
        )

    def _check_baseline(
        self, manifest: dict[str, Any], expedition: Expedition
    ) -> list[PackConflict]:
        """The baseline cursor pins the activity and route revisions seen offline.

        A newer revision at headquarters is a pack-level conflict because the
        field entries were recorded against a different plan.
        """
        baseline = manifest.get("baseline")
        if not isinstance(baseline, dict):
            raise ActionPackValidationError("manifest is missing the baseline cursor")
        conflicts: list[PackConflict] = []
        activity_base = baseline.get("expedition") or {}
        if int(activity_base.get("version", 0)) != expedition.version:
            conflicts.append(
                PackConflict(
                    entry_type="pack",
                    conflict_type="baseline_revision_conflict",
                    entity_type="expedition",
                    local_id=expedition.id,
                    detail={
                        "baseline_version": activity_base.get("version"),
                        "current_version": expedition.version,
                    },
                )
            )
        route = self.session.get(TrailRoute, expedition.route_id)
        route_base = baseline.get("route") or {}
        if route is not None and int(route_base.get("version", 0)) != route.version:
            conflicts.append(
                PackConflict(
                    entry_type="pack",
                    conflict_type="baseline_revision_conflict",
                    entity_type="trail_route",
                    local_id=route.id,
                    detail={
                        "baseline_version": route_base.get("version"),
                        "current_version": route.version,
                    },
                )
            )
        return conflicts

    # ---------------------------------------------------------- entry parsing

    @staticmethod
    def _validate_manifest_header(manifest: dict[str, Any]) -> None:
        expedition_id = manifest.get("expedition_id")
        pack_id = manifest.get("pack_id")
        exported_at = manifest.get("exported_at")
        if not isinstance(expedition_id, int) or isinstance(expedition_id, bool):
            raise ActionPackValidationError(
                "manifest expedition_id must be an integer",
                context={"expedition_id": expedition_id},
            )
        if not isinstance(pack_id, str) or not pack_id:
            raise ActionPackValidationError("manifest pack_id must be a non-empty string")
        if not isinstance(exported_at, str):
            raise ActionPackValidationError("manifest exported_at must be an ISO-8601 string")
        try:
            datetime.fromisoformat(exported_at)
        except ValueError as exc:
            raise ActionPackValidationError(
                "manifest exported_at is not a valid ISO-8601 timestamp"
            ) from exc
        for section in ("baseline", "snapshot", "offline"):
            if not isinstance(manifest.get(section), dict):
                raise ActionPackValidationError(
                    "manifest is missing a required section", context={"section": section}
                )

    def _parse_entries(self, manifest: dict[str, Any]) -> list[Any]:
        offline = manifest.get("offline")
        if not isinstance(offline, dict):
            raise ActionPackValidationError(
                "manifest is missing the offline section",
            )
        collected: list[tuple[int, str, Any]] = []
        errors: list[dict[str, Any]] = []
        for section, model in ENTRY_MODELS.items():
            plural = f"{section}s" if section != "gear_check" else "gear_checks"
            raw_items = offline.get(plural)
            if raw_items is None:
                raise ActionPackValidationError(
                    "offline section is incomplete", context={"missing": plural}
                )
            if not isinstance(raw_items, list):
                raise ActionPackValidationError(
                    "offline section must contain arrays", context={"section": plural}
                )
            for index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    errors.append(
                        {"section": plural, "index": index, "error": "entry must be an object"}
                    )
                    continue
                try:
                    parsed = model.model_validate(raw_item)
                except Exception as exc:  # pydantic ValidationError
                    errors.append(
                        {
                            "section": plural,
                            "index": index,
                            "uid": raw_item.get("uid"),
                            "error": str(exc).splitlines()[0][:300],
                        }
                    )
                    continue
                collected.append((index, section, parsed))
        if errors:
            raise ActionPackValidationError(
                "one or more offline entries failed schema validation",
                context={"errors": errors},
            )
        uids = [item[2].uid for item in collected]
        if len(set(uids)) != len(uids):
            raise ActionPackValidationError("duplicate entry uid inside the action pack")
        return [item[2] for item in collected]

    # ------------------------------------------------------------- validation

    def _build_plan(
        self,
        *,
        manifest: dict[str, Any],
        entries: list[Any],
        expedition: Expedition,
        current: datetime,
    ) -> dict[str, Any]:
        conflicts: list[PackConflict] = []
        participant_ids = self._participant_ids(expedition.id)
        applied_uids = set(
            self.session.scalars(
                select(ActionPackEntryReceipt.entry_uid)
                .join(ActionPackImport, ActionPackEntryReceipt.import_id == ActionPackImport.id)
                .where(
                    ActionPackImport.expedition_id == expedition.id,
                    ActionPackEntryReceipt.status == ENTRY_APPLIED,
                )
            )
        )
        slot_rows = {
            slot.id: slot
            for slot in self.session.scalars(
                select(ItineraryCheckIn).where(
                    ItineraryCheckIn.expedition_id == expedition.id
                )
            )
        }
        catalog_rows = {
            item.id: item
            for item in self.session.scalars(select(GearCatalog))
        }
        incident_rows = {
            item.id: item
            for item in self.session.scalars(
                select(EmergencyIncident).where(
                    EmergencyIncident.expedition_id == expedition.id
                )
            )
        }
        existing_checks = {
            (row.user_id, row.catalog_id): row
            for row in self.session.scalars(
                select(ActivityGearCheck).where(
                    ActivityGearCheck.expedition_id == expedition.id
                )
            )
        }
        baseline_versions = {
            int(item["id"]): int(item["version"])
            for item in manifest.get("snapshot", {})
            .get("safety_plan", {})
            .get("open_incidents", [])
        }
        offline_incidents = {
            entry.uid: entry for entry in entries if isinstance(entry, OfflineIncident)
        }
        pack_gear_keys: set[tuple[int, int]] = set()
        window_low = min(expedition.meeting_at, expedition.start_at)
        window_high = expedition.end_at
        # Entries are logged on the device during the activity and backfilled on
        # return, so the upper bound is the later of the activity end and the
        # import wall-clock, plus a small clock-skew allowance.
        recording_upper = max(current, window_high) + FUTURE_SKEW

        ordered = sorted(entries, key=self._entry_sort_key)
        duplicates: list[dict[str, Any]] = []

        for entry in ordered:
            uid = entry.uid
            if uid in applied_uids:
                # Already applied by an earlier pack: non-blocking duplicate.
                # Blocking conflicts still abort the whole pack; duplicates are
                # skipped and reported separately.
                duplicates.append(
                    {
                        "entry_uid": uid,
                        "entry_type": entry.entry_type,
                    }
                )
                continue
            if entry.recorded_at > recording_upper:
                conflicts.append(
                    self._conflict(
                        entry,
                        "time_order_error",
                        detail={
                            "reason": "recorded_at is in the future",
                            "recorded_at": entry.recorded_at.isoformat(),
                        },
                    )
                )
                continue
            if isinstance(entry, OfflineCheckIn):
                self._validate_check_in(
                    entry,
                    conflicts=conflicts,
                    slot_rows=slot_rows,
                    window_low=window_low,
                    window_high=window_high,
                )
            elif isinstance(entry, OfflineIncident):
                self._validate_incident(
                    entry,
                    conflicts=conflicts,
                    participant_ids=participant_ids,
                    window_low=window_low,
                    latest_now=recording_upper,
                )
            elif isinstance(entry, OfflineIncidentUpdate):
                self._validate_incident_update(
                    entry,
                    conflicts=conflicts,
                    participant_ids=participant_ids,
                    offline_incidents=offline_incidents,
                    incident_rows=incident_rows,
                    baseline_versions=baseline_versions,
                )
            elif isinstance(entry, OfflineGearCheck):
                key = (entry.user_id, entry.catalog_id)
                if key in pack_gear_keys:
                    conflicts.append(
                        self._conflict(
                            entry,
                            "duplicate_in_pack",
                            entity_type="activity_gear_check",
                            detail={"user_id": entry.user_id, "catalog_id": entry.catalog_id},
                        )
                    )
                    continue
                pack_gear_keys.add(key)
                self._validate_gear_check(
                    entry,
                    conflicts=conflicts,
                    participant_ids=participant_ids,
                    catalog_rows=catalog_rows,
                    existing_checks=existing_checks,
                    window_low=window_low,
                )
        duplicate_uids = {item["entry_uid"] for item in duplicates}
        remaining = [entry for entry in ordered if entry.uid not in duplicate_uids]
        return {"ordered": remaining, "duplicates": duplicates, "conflicts": conflicts}

    def _validate_check_in(
        self,
        entry: OfflineCheckIn,
        *,
        conflicts: list[PackConflict],
        slot_rows: dict[int, ItineraryCheckIn],
        window_low: datetime,
        window_high: datetime,
    ) -> None:
        slot = slot_rows.get(entry.slot_id)
        if slot is None:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="itinerary_check_in",
                    detail={"reason": "check-in slot not found", "slot_id": entry.slot_id},
                )
            )
            return
        if slot.checked_in_at is not None:
            conflicts.append(
                self._conflict(
                    entry,
                    "already_recorded",
                    entity_type="itinerary_check_in",
                    local_id=slot.id,
                    detail={
                        "reason": "headquarters already holds a check-in for this slot",
                        "checked_in_at": slot.checked_in_at.isoformat(),
                    },
                )
            )
            return
        if entry.checked_in_at < window_low or entry.checked_in_at > window_high:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    entity_type="itinerary_check_in",
                    local_id=slot.id,
                    detail={
                        "reason": "checked_in_at is outside the expedition window",
                        "window_start": window_low.isoformat(),
                        "window_end": window_high.isoformat(),
                    },
                )
            )

    def _validate_incident(
        self,
        entry: OfflineIncident,
        *,
        conflicts: list[PackConflict],
        participant_ids: set[int],
        window_low: datetime,
        latest_now: datetime,
    ) -> None:
        if entry.reported_by not in participant_ids:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="user",
                    local_id=entry.reported_by,
                    detail={"reason": "reporter is not a participant of this expedition"},
                )
            )
        if entry.occurred_at < window_low or entry.occurred_at > latest_now:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    detail={
                        "reason": "occurred_at is outside the expedition window",
                        "window_start": window_low.isoformat(),
                    },
                )
            )
        if entry.recorded_at < entry.occurred_at:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    detail={"reason": "recorded_at precedes occurred_at"},
                )
            )

    def _validate_incident_update(
        self,
        entry: OfflineIncidentUpdate,
        *,
        conflicts: list[PackConflict],
        participant_ids: set[int],
        offline_incidents: dict[str, OfflineIncident],
        incident_rows: dict[int, EmergencyIncident],
        baseline_versions: dict[int, int],
    ) -> None:
        target, target_local_id = self._resolve_incident_ref(
            entry.incident_ref,
            offline_incidents=offline_incidents,
            incident_rows=incident_rows,
        )
        if target is None:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="emergency_incident",
                    detail={
                        "reason": "incident reference cannot be resolved",
                        "incident_ref": entry.incident_ref,
                    },
                )
            )
            return
        if isinstance(target, EmergencyIncident):
            current_status = EmergencyStatus(target.status)
            anchor_time = target.occurred_at
            if entry.status not in INCIDENT_TRANSITIONS.get(current_status, set()):
                conflicts.append(
                    self._conflict(
                        entry,
                        "invalid_state",
                        entity_type="emergency_incident",
                        local_id=target_local_id,
                        detail={
                            "reason": "status transition is not allowed",
                            "current_status": str(current_status),
                            "requested_status": str(entry.status),
                        },
                    )
                )
            baseline_version = baseline_versions.get(target_local_id)
            if baseline_version is not None and target.version != baseline_version:
                conflicts.append(
                    self._conflict(
                        entry,
                        "entity_version_conflict",
                        entity_type="emergency_incident",
                        local_id=target_local_id,
                        detail={
                            "reason": "headquarters holds a newer revision of this incident",
                            "baseline_version": baseline_version,
                            "current_version": target.version,
                        },
                    )
                )
        else:
            anchor_time = target.occurred_at
            if entry.status not in {
                EmergencyStatus.OPEN,
                EmergencyStatus.MONITORING,
                EmergencyStatus.RESOLVED,
                EmergencyStatus.FALSE_ALARM,
            }:  # pragma: no cover - guarded by pydantic enum
                conflicts.append(
                    self._conflict(entry, "invalid_state", detail={"reason": "unknown status"})
                )
        if entry.resolved_at is not None and entry.resolved_at < anchor_time:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    entity_type="emergency_incident",
                    local_id=target_local_id,
                    detail={"reason": "resolved_at precedes the incident occurrence"},
                )
            )
        if entry.recorded_at < anchor_time:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    entity_type="emergency_incident",
                    local_id=target_local_id,
                    detail={"reason": "update recorded before the incident occurred"},
                )
            )

    def _validate_gear_check(
        self,
        entry: OfflineGearCheck,
        *,
        conflicts: list[PackConflict],
        participant_ids: set[int],
        catalog_rows: dict[int, GearCatalog],
        existing_checks: dict[tuple[int, int], ActivityGearCheck],
        window_low: datetime,
    ) -> None:
        if entry.user_id not in participant_ids:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="user",
                    local_id=entry.user_id,
                    detail={"reason": "user is not a participant of this expedition"},
                )
            )
        if entry.verified_by is not None and entry.verified_by not in participant_ids:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="user",
                    local_id=entry.verified_by,
                    detail={"reason": "verifier is not a participant of this expedition"},
                )
            )
        if entry.catalog_id not in catalog_rows:
            conflicts.append(
                self._conflict(
                    entry,
                    "reference_error",
                    entity_type="gear_catalog",
                    local_id=entry.catalog_id,
                    detail={"reason": "gear catalog item not found"},
                )
            )
            return
        if entry.recorded_at < window_low:
            conflicts.append(
                self._conflict(
                    entry,
                    "time_order_error",
                    detail={"reason": "gear check recorded before expedition assembly"},
                )
            )
        existing = existing_checks.get((entry.user_id, entry.catalog_id))
        if existing is not None:
            conflicts.append(
                self._conflict(
                    entry,
                    "already_recorded",
                    entity_type="activity_gear_check",
                    local_id=existing.id,
                    detail={
                        "reason": "headquarters already holds a gear check for this user/item",
                        "current_status": str(existing.status),
                    },
                )
            )

    @staticmethod
    def _resolve_incident_ref(
        ref: str,
        *,
        offline_incidents: dict[str, OfflineIncident],
        incident_rows: dict[int, EmergencyIncident],
    ) -> tuple[OfflineIncident | EmergencyIncident | None, int | None]:
        if ref in offline_incidents:
            return offline_incidents[ref], None
        if ref.isdigit():
            local_id = int(ref)
            incident = incident_rows.get(local_id)
            if incident is not None:
                return incident, local_id
        return None, None

    @staticmethod
    def _entry_sort_key(entry: Any) -> tuple[int, str]:
        type_order = {
            "incident": 0,
            "check_in": 1,
            "gear_check": 1,
            "incident_update": 2,
        }
        return type_order.get(entry.entry_type, 9), entry.recorded_at.isoformat()

    @staticmethod
    def _conflict(
        entry: Any,
        conflict_type: str,
        *,
        entity_type: str = "",
        local_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> PackConflict:
        return PackConflict(
            entry_uid=entry.uid,
            entry_type=entry.entry_type,
            conflict_type=conflict_type,
            entity_type=entity_type,
            local_id=local_id,
            detail=detail or {},
        )

    def _participant_ids(self, expedition_id: int) -> set[int]:
        rows = self.session.scalars(
            select(ExpeditionRegistration.user_id).where(
                ExpeditionRegistration.expedition_id == expedition_id,
                ExpeditionRegistration.status.in_(
                    [RegistrationStatus.CONFIRMED, RegistrationStatus.PENDING]
                ),
            )
        )
        return set(rows)

    # --------------------------------------------------------------- applying

    def _apply_plan(
        self,
        *,
        manifest: dict[str, Any],
        digest: str,
        expedition: Expedition,
        plan: dict[str, Any],
        actor_id: int | None,
    ) -> ImportReport:
        import_record = ActionPackImport(
            pack_id=manifest["pack_id"],
            pack_digest=digest,
            expedition_id=expedition.id,
            outcome=OUTCOME_COMMITTED,
            exported_at=datetime.fromisoformat(manifest["exported_at"]),
            actor_id=actor_id,
            pack_version=manifest["version"],
        )
        self.session.add(import_record)
        self.session.flush()

        applied: list[dict[str, Any]] = []
        offline_incident_uids: dict[str, int] = {}
        for entry in plan["ordered"]:
            if isinstance(entry, OfflineCheckIn):
                resource_id = self._apply_check_in(entry, expedition.id, actor_id)
                resource_type = "itinerary_check_in"
            elif isinstance(entry, OfflineIncident):
                resource_id = self._apply_incident(entry, expedition.id, actor_id)
                offline_incident_uids[entry.uid] = resource_id
                resource_type = "emergency_incident"
            elif isinstance(entry, OfflineIncidentUpdate):
                resource_id = self._apply_incident_update(
                    entry, expedition.id, offline_incident_uids, actor_id
                )
                resource_type = "emergency_incident"
            else:
                resource_id = self._apply_gear_check(entry, expedition.id, actor_id)
                resource_type = "activity_gear_check"
            self.session.add(
                ActionPackEntryReceipt(
                    import_id=import_record.id,
                    entry_uid=entry.uid,
                    entry_type=entry.entry_type,
                    status=ENTRY_APPLIED,
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
            )
            applied.append(
                {
                    "entry_uid": entry.uid,
                    "entry_type": entry.entry_type,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                }
            )

        # Duplicates already carry an "applied" receipt from an earlier import
        # (entry_uid is globally unique), so no second receipt is inserted.

        import_record.entry_count = len(plan["ordered"]) + len(plan["duplicates"])
        import_record.applied_count = len(applied)
        self.session.add(
            ActionPackAuditRecord(
                actor_id=actor_id,
                stage="import",
                action=AuditAction.ACTION_PACK_IMPORTED,
                expedition_id=expedition.id,
                pack_id=manifest["pack_id"],
                pack_digest=digest,
                outcome=OUTCOME_COMMITTED,
                summary={
                    "entries_total": import_record.entry_count,
                    "applied": len(applied),
                    "duplicates": len(plan["duplicates"]),
                },
            )
        )
        self.session.flush()
        return ImportReport(
            pack_id=manifest["pack_id"],
            pack_digest=digest,
            expedition_id=expedition.id,
            outcome=OUTCOME_COMMITTED,
            entries_total=import_record.entry_count,
            applied=applied,
            duplicates=plan["duplicates"],
        )

    def _apply_check_in(
        self, entry: OfflineCheckIn, expedition_id: int, actor_id: int | None
    ) -> int:
        slot = self.session.get(ItineraryCheckIn, entry.slot_id)
        delta_seconds = (entry.checked_in_at - slot.due_at).total_seconds()
        slot.checked_in_at = entry.checked_in_at
        slot.latitude = entry.latitude
        slot.longitude = entry.longitude
        slot.note = entry.note
        slot.is_safe = entry.is_safe
        slot.late_minutes = max(int(delta_seconds // 60), 0)
        self.session.flush()
        self.audit(
            actor_id=actor_id or slot.user_id,
            entity_type="itinerary_check_in",
            entity_id=slot.id,
            action=AuditAction.CHECKED_IN,
            after={
                "checked_in_at": slot.checked_in_at.isoformat(),
                "is_safe": slot.is_safe,
                "late_minutes": slot.late_minutes,
                "source": "action_pack",
                "entry_uid": entry.uid,
            },
            correlation_id=entry.uid,
        )
        return slot.id

    def _apply_incident(
        self, entry: OfflineIncident, expedition_id: int, actor_id: int | None
    ) -> int:
        incident = EmergencyIncident(
            expedition_id=expedition_id,
            reported_by=entry.reported_by,
            incident_type=entry.incident_type,
            risk_level=entry.risk_level,
            status=EmergencyStatus.OPEN,
            occurred_at=entry.occurred_at,
            latitude=entry.latitude,
            longitude=entry.longitude,
            description=entry.description,
            actions_taken=entry.actions_taken,
        )
        self.session.add(incident)
        self.session.flush()
        self.audit(
            actor_id=actor_id or entry.reported_by,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.EMERGENCY_RECORDED,
            after={
                "id": incident.id,
                "status": str(incident.status),
                "occurred_at": incident.occurred_at.isoformat(),
                "source": "action_pack",
                "entry_uid": entry.uid,
            },
            correlation_id=entry.uid,
        )
        return incident.id

    def _apply_incident_update(
        self,
        entry: OfflineIncidentUpdate,
        expedition_id: int,
        offline_incident_uids: dict[str, int],
        actor_id: int | None,
    ) -> int:
        if entry.incident_ref in offline_incident_uids:
            incident_id = offline_incident_uids[entry.incident_ref]
        else:
            incident_id = int(entry.incident_ref)
        incident = self.session.get(EmergencyIncident, incident_id)
        incident.status = entry.status
        if entry.actions_taken is not None:
            incident.actions_taken = entry.actions_taken
        if entry.resolution is not None:
            incident.resolution = entry.resolution
        incident.resolved_at = entry.resolved_at
        incident.version += 1
        self.session.flush()
        self.audit(
            actor_id=actor_id or incident.reported_by,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.STATUS_CHANGED,
            after={
                "id": incident.id,
                "status": str(incident.status),
                "resolved_at": (
                    incident.resolved_at.isoformat() if incident.resolved_at else None
                ),
                "source": "action_pack",
                "entry_uid": entry.uid,
            },
            correlation_id=entry.uid,
        )
        return incident.id

    def _apply_gear_check(
        self, entry: OfflineGearCheck, expedition_id: int, actor_id: int | None
    ) -> int:
        check = ActivityGearCheck(
            expedition_id=expedition_id,
            user_id=entry.user_id,
            catalog_id=entry.catalog_id,
            quantity=entry.quantity,
            status=entry.status,
            verified_by=entry.verified_by,
            verified_at=entry.recorded_at if str(entry.status) == "verified" else None,
            notes=entry.notes,
        )
        self.session.add(check)
        self.session.flush()
        self.audit(
            actor_id=actor_id or entry.verified_by or entry.user_id,
            entity_type="activity_gear_check",
            entity_id=check.id,
            action=AuditAction.CREATED,
            after={
                "id": check.id,
                "user_id": check.user_id,
                "catalog_id": check.catalog_id,
                "status": str(check.status),
                "quantity": check.quantity,
                "source": "action_pack",
                "entry_uid": entry.uid,
            },
            correlation_id=entry.uid,
        )
        return check.id

    # -------------------------------------------------------------- reconciled

    def _persist_conflicts(
        self,
        *,
        manifest: dict[str, Any],
        digest: str,
        expedition: Expedition,
        entries: list[Any],
        conflicts: list[PackConflict],
        actor_id: int | None,
    ) -> ImportReport:
        # Atomic policy in action: no business row is created. Only the import
        # ledger, structured conflicts and an audit row are committed.
        import_record = ActionPackImport(
            pack_id=manifest["pack_id"],
            pack_digest=digest,
            expedition_id=expedition.id,
            outcome=OUTCOME_CONFLICTS,
            exported_at=datetime.fromisoformat(manifest["exported_at"]),
            actor_id=actor_id,
            entry_count=len(entries),
            conflict_count=len(conflicts),
            pack_version=manifest["version"],
        )
        self.session.add(import_record)
        self.session.flush()
        status_by_uid = {conflict.entry_uid: ENTRY_CONFLICT for conflict in conflicts}
        for entry in entries:
            self.session.add(
                ActionPackEntryReceipt(
                    import_id=import_record.id,
                    entry_uid=entry.uid,
                    entry_type=entry.entry_type,
                    status=status_by_uid.get(entry.uid, "skipped"),
                )
            )
        for conflict in conflicts:
            self.session.add(
                ActionPackConflict(
                    import_id=import_record.id,
                    entry_uid=conflict.entry_uid,
                    entry_type=conflict.entry_type,
                    conflict_type=conflict.conflict_type,
                    entity_type=conflict.entity_type,
                    local_id=conflict.local_id,
                    detail=conflict.detail,
                )
            )
        self.session.add(
            ActionPackAuditRecord(
                actor_id=actor_id,
                stage="import",
                action=AuditAction.ACTION_PACK_REJECTED,
                expedition_id=expedition.id,
                pack_id=manifest["pack_id"],
                pack_digest=digest,
                outcome=OUTCOME_CONFLICTS,
                summary={
                    "entries_total": len(entries),
                    "conflicts": len(conflicts),
                    "conflict_types": sorted({c.conflict_type for c in conflicts}),
                },
            )
        )
        self.session.flush()
        return ImportReport(
            pack_id=manifest["pack_id"],
            pack_digest=digest,
            expedition_id=expedition.id,
            outcome=OUTCOME_CONFLICTS,
            entries_total=len(entries),
            conflicts=conflicts,
        )

    def _report_for_existing(self, existing: ActionPackImport) -> ImportReport:
        receipts = list(
            self.session.scalars(
                select(ActionPackEntryReceipt).where(
                    ActionPackEntryReceipt.import_id == existing.id
                )
            )
        )
        conflicts = list(
            self.session.scalars(
                select(ActionPackConflict).where(
                    ActionPackConflict.import_id == existing.id
                )
            )
        )
        return ImportReport(
            pack_id=existing.pack_id,
            pack_digest=existing.pack_digest,
            expedition_id=existing.expedition_id,
            # The original outcome is preserved so callers learn what happened
            # the first time; "replayed" marks the current attempt as a no-op.
            outcome=existing.outcome,
            replayed=True,
            entries_total=existing.entry_count,
            applied=[
                {
                    "entry_uid": row.entry_uid,
                    "entry_type": row.entry_type,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                }
                for row in receipts
                if row.status == ENTRY_APPLIED
            ],
            duplicates=[
                {"entry_uid": row.entry_uid, "entry_type": row.entry_type}
                for row in receipts
                if row.status == ENTRY_DUPLICATE
            ],
            conflicts=[
                PackConflict(
                    entry_uid=row.entry_uid,
                    entry_type=row.entry_type,
                    conflict_type=row.conflict_type,
                    entity_type=row.entity_type,
                    local_id=row.local_id,
                    detail=row.detail,
                )
                for row in conflicts
            ],
        )

    # ------------------------------------------------------------------ audit

    def _rejection_audit(
        self,
        *,
        actor_id: int | None,
        reason: str,
        digest: str | None,
        expedition_id: int | None = None,
        pack_id: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Record a rejection that survives the business-transaction rollback.

        Rejections are detected before any business entity is added to the
        session, so it is safe to commit the audit row directly. Committing on
        the same connection (rather than opening a second session) avoids a
        second writer contending for the SQLite file lock.
        """
        resolved_actor = actor_id
        if resolved_actor is not None and self.session.get(User, resolved_actor) is None:
            # The actor cannot satisfy the foreign key (e.g. forged input).
            resolved_actor = None
        self.session.add(
            ActionPackAuditRecord(
                actor_id=resolved_actor,
                stage="import",
                action=AuditAction.ACTION_PACK_REJECTED,
                expedition_id=expedition_id or 0,
                pack_id=pack_id,
                pack_digest=digest,
                outcome=OUTCOME_REJECTED,
                summary={"reason": reason[:300], **(summary or {})},
            )
        )
        self.session.commit()
