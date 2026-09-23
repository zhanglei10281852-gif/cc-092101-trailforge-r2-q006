from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import AuditAction
from trailforge.errors import (
    ActionPackConflictError,
    NotFoundError,
    PackageIntegrityError,
    PackValidationError,
)
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.models.gear import ActivityGearCheck, GearCatalog
from trailforge.models.offline import OfflineImportRecord
from trailforge.models.safety import EmergencyIncident, ItineraryCheckIn, RiskAssessment
from trailforge.offline.pack import (
    ENTRY_CHECK_IN,
    ENTRY_INCIDENT,
    ActionPackIntegrityError,
    ActionPackValidationError,
    PackEntry,
    build_pack,
    canonical_bytes,
    new_pack_id,
    sha256_hex,
    verify_pack,
)
from trailforge.repositories.activities import ACTIVE_REGISTRATION_STATUSES, ExpeditionRepository
from trailforge.repositories.gear import GearRepository
from trailforge.repositories.routes import RouteRepository
from trailforge.repositories.safety import SafetyRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.offline import (
    AppliedEntry,
    ConflictItem,
    ImportResult,
    PackExportResponse,
)
from trailforge.services.base import ServiceBase

# 允许的离线机时钟偏差；超出该值的“未来”时间戳将被拒绝。
_CLOCK_SKEW = timedelta(minutes=5)

_CONTACT_FIELDS = ("name", "relationship_label", "phone", "priority")
_CATALOG_FIELDS = (
    "id",
    "sku",
    "name",
    "category",
    "default_weight_grams",
    "safety_critical",
    "inspection_interval_days",
)


class OfflinePackService(ServiceBase):
    """导出单个活动的离线行动包，并把返程后的补录原子地导回。"""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.expeditions = ExpeditionRepository(session)
        self.routes = RouteRepository(session)
        self.users = UserRepository(session)
        self.gear = GearRepository(session)
        self.safety = SafetyRepository(session)

    # ------------------------------------------------------------------ export

    def export_pack(
        self, expedition_id: int, *, actor_id: int, now: datetime | None = None
    ) -> tuple[dict[str, Any], PackExportResponse]:
        exported_at = now or utc_now()
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        route = self.routes.get_detail(expedition.route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {expedition.route_id} was not found")

        payload = self._build_payload(expedition, route)
        cursor = self._build_cursor(expedition, route, exported_at)
        pack = build_pack(
            pack_id=new_pack_id(),
            expedition_id=expedition.id,
            created_by=actor_id,
            created_at=exported_at,
            baseline_cursor=cursor,
            payload=payload,
        )
        manifest_hash = pack["checksum"]["manifest_sha256"]
        payload_hash = pack["manifest"]["baseline_cursor"]["payload_sha256"]
        summary = {
            "members": len(payload["members"]),
            "route_segments": len(payload["route"]["segments"]),
            "route_points": len(payload["route"]["points"]),
            "gear_catalog_items": len(payload["gear"]["catalog"]),
            "gear_requirements": len(payload["gear"]["requirements"]),
            "risk_assessments": len(payload["safety_plan"]["risk_assessments"]),
            "scheduled_check_ins": len(payload["safety_plan"]["check_ins"]),
            "incidents": len(payload["safety_plan"]["incidents"]),
            "gear_checks": len(payload["gear"]["checks"]),
        }
        # 审计只记录标识、哈希和计数，不含成员电话、事件正文等敏感内容。
        self.audit(
            actor_id=actor_id,
            entity_type="offline_action_pack",
            entity_id=expedition.id,
            action=AuditAction.PACK_EXPORTED,
            after={
                "pack_id": pack["manifest"]["pack_id"],
                "manifest_sha256": manifest_hash,
                "payload_sha256": payload_hash,
                "counts": summary,
            },
            context={"phase": "export"},
            correlation_id=pack["manifest"]["pack_id"],
        )
        response = PackExportResponse(
            pack_id=pack["manifest"]["pack_id"],
            expedition_id=expedition.id,
            manifest_sha256=manifest_hash,
            created_at=exported_at,
            entry_count=0,
            payload_summary=summary,
        )
        return pack, response

    def _build_payload(self, expedition: Expedition, route: Any) -> dict[str, Any]:
        members: list[dict[str, Any]] = []
        for registration in expedition.registrations:
            if str(registration.status) not in {
                status.value for status in ACTIVE_REGISTRATION_STATUSES
            }:
                continue
            user = self.users.get(registration.user_id)
            if user is None:
                continue
            member = {
                "user_id": user.id,
                "display_name": user.display_name,
                "phone": user.phone,
                "timezone": user.timezone,
                "role": str(registration.role),
                "status": str(registration.status),
                "emergency_contacts": [
                    {field: getattr(contact, field) for field in _CONTACT_FIELDS}
                    for contact in user.emergency_contacts
                ],
            }
            members.append(member)

        requirements = [
            {
                "catalog_id": item.catalog_id,
                "quantity_per_person": item.quantity_per_person,
                "quantity_for_group": item.quantity_for_group,
                "mandatory": item.mandatory,
            }
            for item in self.gear.requirements(expedition.id)
        ]
        catalog_ids = {item["catalog_id"] for item in requirements}
        catalog: list[dict[str, Any]] = []
        for catalog_id in sorted(catalog_ids):
            item = self.gear.get_catalog(catalog_id)
            if item is not None:
                catalog.append({field: getattr(item, field) for field in _CATALOG_FIELDS})

        assessments = self.session.scalars(
            select(RiskAssessment)
            .where(RiskAssessment.expedition_id == expedition.id)
            .order_by(RiskAssessment.id)
        )
        incidents = self.safety.incidents(expedition.id)
        check_ins = self.safety.check_ins(expedition.id)
        checks = self.gear.gear_checks(expedition.id)

        return {
            "expedition": {
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
            },
            "route": {
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
                    for segment in sorted(route.segments, key=lambda item: item.sequence)
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
                    for point in sorted(route.points, key=lambda item: item.sequence)
                ],
                "risk_tags": [
                    {
                        "code": tag.code,
                        "name": tag.name,
                        "level": str(tag.level),
                        "mitigation": tag.mitigation,
                    }
                    for tag in sorted(route.risk_tags, key=lambda item: item.code)
                ],
            },
            "members": sorted(members, key=lambda item: item["user_id"]),
            "gear": {
                "catalog": catalog,
                "requirements": requirements,
                "checks": [
                    {
                        "user_id": check.user_id,
                        "catalog_id": check.catalog_id,
                        "quantity": check.quantity,
                        "status": str(check.status),
                        "verified_by": check.verified_by,
                    }
                    for check in checks
                ],
            },
            "safety_plan": {
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
                        "residual_risk": item.residual_risk,
                    }
                    for item in assessments
                ],
                "check_ins": [
                    {
                        "id": item.id,
                        "user_id": item.user_id,
                        "check_in_type": str(item.check_in_type),
                        "due_at": item.due_at.isoformat(),
                        "checked_in_at": (
                            item.checked_in_at.isoformat() if item.checked_in_at else None
                        ),
                        "latitude": item.latitude,
                        "longitude": item.longitude,
                        "is_safe": item.is_safe,
                    }
                    for item in check_ins
                ],
                "incidents": [
                    {
                        "id": item.id,
                        "reported_by": item.reported_by,
                        "client_ref": item.client_ref,
                        "incident_type": str(item.incident_type),
                        "risk_level": str(item.risk_level),
                        "status": str(item.status),
                        "occurred_at": item.occurred_at.isoformat(),
                        "latitude": item.latitude,
                        "longitude": item.longitude,
                        "description": item.description,
                        "actions_taken": item.actions_taken,
                        "resolution": item.resolution,
                        "version": item.version,
                    }
                    for item in incidents
                ],
            },
        }

    def _build_cursor(
        self, expedition: Expedition, route: Any, exported_at: datetime
    ) -> dict[str, Any]:
        check_ins = self.safety.check_ins(expedition.id)
        incidents = self.safety.incidents(expedition.id)
        checks = self.gear.gear_checks(expedition.id)
        return {
            "exported_at": exported_at.isoformat(),
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
            "check_ins": [
                {
                    "id": item.id,
                    "checked_in_at": (
                        item.checked_in_at.isoformat() if item.checked_in_at else None
                    ),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in check_ins
            ],
            "incidents": [
                {"id": item.id, "version": item.version, "updated_at": item.updated_at.isoformat()}
                for item in incidents
            ],
            "gear_checks": [
                {
                    "user_id": item.user_id,
                    "catalog_id": item.catalog_id,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in checks
            ],
        }

    # ------------------------------------------------------------------ import

    def import_pack(
        self,
        pack: dict[str, Any],
        *,
        actor_id: int,
        on_conflict: str = "abort",
        now: datetime | None = None,
    ) -> ImportResult:
        """导回行动包。

        原子策略（明确且可测试）：

        * ``abort``（默认）：只要存在任何冲突条目，整个导入不写任何业务数据，
          抛出 :class:`ActionPackConflictError` 并携带结构化冲突清单。
        * ``skip_conflicts``：冲突条目跳过，其余条目在**同一个事务**内提交。
          即使所有条目都冲突，也会在同一事务登记包去重记录（不含正文），
          使该包以后重放为 no-op。
        * 已应用过的条目（按内容指纹）永远跳过，因此同包重复导入无业务副作用。
        """

        current = now or utc_now()
        if on_conflict not in {"abort", "skip_conflicts"}:
            raise PackValidationError(
                "on_conflict must be 'abort' or 'skip_conflicts'",
                context={"on_conflict": on_conflict},
            )
        try:
            verify_pack(pack)
        except ActionPackIntegrityError as exc:
            raise PackageIntegrityError(str(exc)) from exc
        except ActionPackValidationError as exc:
            raise PackValidationError(str(exc), context={"path": exc.path}) from exc

        manifest = pack["manifest"]
        payload = pack["payload"]
        pack_id = str(manifest["pack_id"])
        expedition_id = int(manifest["expedition_id"])
        manifest_hash = pack["checksum"]["manifest_sha256"]
        if int(payload["expedition"]["id"]) != expedition_id:
            raise PackageIntegrityError(
                "payload expedition id does not match the manifest",
                context={"pack_id": pack_id},
            )
        expedition = self.expeditions.get(expedition_id)
        if expedition is None:
            raise NotFoundError(
                f"Expedition {expedition_id} was not found",
                context={"pack_id": pack_id},
            )

        record = self.session.scalar(
            select(OfflineImportRecord).where(OfflineImportRecord.pack_id == pack_id)
        )
        if record is not None and record.manifest_hash != manifest_hash:
            # 同一个 pack_id 却是不同内容：包被重新伪造过，拒绝写入。
            raise PackageIntegrityError(
                "pack_id was already imported with different content",
                context={"pack_id": pack_id},
            )
        already_done = set(record.entry_fingerprints) if record is not None else set()

        plan = self._plan_entries(
            manifest["entries"],
            expedition=expedition,
            already_done=already_done,
            current=current,
        )

        total = len(manifest["entries"])
        conflicts = plan["conflicts"]
        if conflicts and on_conflict == "abort":
            self._raise_conflict(
                pack_id=pack_id,
                expedition_id=expedition.id,
                expedition=expedition,
                manifest=manifest,
                conflicts=conflicts,
                total=total,
            )

        applied: list[AppliedEntry] = []
        try:
            for entry in plan["applicable"]:
                applied.append(self._apply_entry(entry, current))
            self.session.flush()
        except IntegrityError as exc:
            # 预检与写入之间的并发写入（另一导入或在线操作抢先占用了同一实体）。
            # 绝不能部分提交：抛出后整个事务回滚。
            raise PackageIntegrityError(
                "an entry conflicts with a concurrent write; import rolled back",
                context={"pack_id": pack_id},
            ) from exc

        skipped = [item.entry_id for item in conflicts]
        new_fingerprints = already_done | {item.fingerprint for item in applied}
        if record is None:
            # 首次导入：登记包指纹。纯重放（record 已存在）不再新增记录。
            # 同一 pack_id 但不同哈希的包此前已被拒绝，因此这里哈希必然一致。
            self._save_record(
                pack_id=pack_id,
                expedition_id=expedition.id,
                manifest=manifest,
                manifest_hash=manifest_hash,
                fingerprints=sorted(new_fingerprints),
                applied_count=len(applied),
                duplicate_count=len(plan["already_applied"]),
                actor_id=actor_id,
            )

        if applied:
            status = "applied_with_skips" if conflicts else "applied"
        elif conflicts:
            # skip_conflicts 模式下没有任何条目可应用，但导入记录已提交。
            status = "applied_with_skips"
        else:
            status = "no_op"
        result = ImportResult(
            status=status,
            pack_id=pack_id,
            expedition_id=expedition.id,
            manifest_sha256=manifest_hash,
            applied=applied,
            already_applied=plan["already_applied"],
            conflicts=conflicts,
            skipped=skipped,
            total_entries=total,
            committed=True,
        )
        self.audit(
            actor_id=actor_id,
            entity_type="offline_action_pack",
            entity_id=expedition.id,
            action=AuditAction.PACK_IMPORTED,
            after={
                "pack_id": pack_id,
                "manifest_sha256": manifest_hash,
                "status": status,
                "applied": [item.model_dump() for item in applied],
                "applied_count": len(applied),
                "already_applied_count": len(plan["already_applied"]),
                "conflict_count": len(conflicts),
            },
            context={"phase": "import", "on_conflict": on_conflict},
            correlation_id=pack_id,
        )
        return result

    def log_rejection(
        self,
        *,
        pack: dict[str, Any] | None,
        reason: str,
        actor_id: int | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """在独立事务中记录一次被拒绝的导入（损坏、篡改、校验失败、冲突中止）。

        只记录 pack_id、哈希、原因和计数，绝不记录条目正文。
        """

        manifest = (pack or {}).get("manifest") if isinstance(pack, dict) else None
        checksum = (pack or {}).get("checksum") if isinstance(pack, dict) else None
        pack_id = manifest.get("pack_id") if isinstance(manifest, dict) else None
        expedition_id = manifest.get("expedition_id") if isinstance(manifest, dict) else None
        entry_count = len(manifest.get("entries", [])) if isinstance(manifest, dict) else 0
        manifest_hash = checksum.get("manifest_sha256") if isinstance(checksum, dict) else None
        self.audit(
            actor_id=actor_id,
            entity_type="offline_action_pack",
            entity_id=int(expedition_id) if isinstance(expedition_id, int) else 0,
            action=AuditAction.PACK_REJECTED,
            after={
                "pack_id": pack_id,
                "manifest_sha256": manifest_hash,
                "entry_count": entry_count,
                "reason": reason,
            },
            context={"phase": "import", "rejected": True, "detail": self._sanitize(detail or {})},
            correlation_id=pack_id,
        )

    # ------------------------------------------------------------- planning

    def _plan_entries(
        self,
        raw_entries: list[dict[str, Any]],
        *,
        expedition: Expedition,
        already_done: set[str],
        current: datetime,
    ) -> dict[str, Any]:
        entries = [PackEntry(item) for item in raw_entries]
        # 自然键一律使用解析后的 datetime，避免 ISO 文本格式差异。
        slots = {
            (item.user_id, str(item.check_in_type), item.due_at): item
            for item in self.safety.check_ins(expedition.id)
        }
        incidents_by_ref = {
            item.client_ref: item
            for item in self.safety.incidents(expedition.id)
            if item.client_ref is not None
        }
        checks = {
            (item.user_id, item.catalog_id): item for item in self.gear.gear_checks(expedition.id)
        }
        member_ids = {
            row[0]
            for row in self.session.execute(
                select(ExpeditionRegistration.user_id).where(
                    ExpeditionRegistration.expedition_id == expedition.id,
                    ExpeditionRegistration.status.in_(
                        [status.value for status in ACTIVE_REGISTRATION_STATUSES]
                    ),
                )
            ).all()
        }
        catalog_ids = {row[0] for row in self.session.execute(select(GearCatalog.id)).all()}

        # 包内自然键追踪：同一实体在一个包里只能出现一次（内容相同会被摘要层
        # 以重复指纹拦截，内容不同则是自相矛盾的包，直接拒绝）。
        seen_check_in_keys: set[tuple[Any, ...]] = set()
        seen_gear_keys: set[tuple[int, int]] = set()
        seen_incident_refs: set[str] = set()

        applicable: list[PackEntry] = []
        already_applied: list[str] = []
        conflicts: list[ConflictItem] = []
        incident_timeline: datetime | None = None

        for entry in entries:
            data = entry.data
            if entry.fingerprint in already_done:
                already_applied.append(entry.entry_id)
                continue
            if entry.type == ENTRY_CHECK_IN:
                natural_key = (
                    data["user_id"],
                    data["check_in_type"],
                    _parse_dt(data["due_at"]),
                )
                if natural_key in seen_check_in_keys:
                    raise PackValidationError(
                        "the same check-in slot appears twice in the pack",
                        context={"entry_id": entry.entry_id},
                    )
                seen_check_in_keys.add(natural_key)
                decision = self._plan_check_in(
                    entry, expedition=expedition, slots=slots, member_ids=member_ids
                )
            elif entry.type == ENTRY_INCIDENT:
                if data["client_ref"] in seen_incident_refs:
                    raise PackValidationError(
                        "the same incident client_ref appears twice in the pack",
                        context={"entry_id": entry.entry_id},
                    )
                seen_incident_refs.add(data["client_ref"])
                decision, occurred_at = self._plan_incident(
                    entry,
                    expedition=expedition,
                    incidents_by_ref=incidents_by_ref,
                    member_ids=member_ids,
                    timeline_last=incident_timeline,
                    current=current,
                )
                # 时间顺序按包内条目顺序对所有事件条目校验，无论是否最终应用。
                incident_timeline = occurred_at
            else:
                natural_key = (data["user_id"], data["catalog_id"])
                if natural_key in seen_gear_keys:
                    raise PackValidationError(
                        "the same (user, gear item) check appears twice in the pack",
                        context={"entry_id": entry.entry_id},
                    )
                seen_gear_keys.add(natural_key)
                decision = self._plan_gear_check(
                    entry,
                    member_ids=member_ids,
                    catalog_ids=catalog_ids,
                    checks=checks,
                )

            if decision is None:
                applicable.append(entry)
            elif decision == "already_applied":
                already_applied.append(entry.entry_id)
            else:
                conflicts.append(decision)

        return {
            "applicable": applicable,
            "already_applied": already_applied,
            "conflicts": conflicts,
        }

    def _plan_check_in(
        self,
        entry: PackEntry,
        *,
        expedition: Expedition,
        slots: dict[tuple[Any, ...], ItineraryCheckIn],
        member_ids: set[int],
    ) -> ConflictItem | str | None:
        data = entry.data
        if data["user_id"] not in member_ids:
            raise PackValidationError(
                "check-in references a user who is not an active participant",
                context={"entry_id": entry.entry_id, "user_id": data["user_id"]},
            )
        due_at = _parse_dt(data["due_at"])
        slot = slots.get((data["user_id"], data["check_in_type"], due_at))
        if slot is None:
            raise PackValidationError(
                "check-in references a schedule slot that does not exist at headquarters",
                context={
                    "entry_id": entry.entry_id,
                    "user_id": data["user_id"],
                    "check_in_type": data["check_in_type"],
                    "due_at": data["due_at"],
                },
            )
        checked_in_at = _parse_dt(data["checked_in_at"])
        if checked_in_at < expedition.meeting_at:
            raise PackValidationError(
                "checked_in_at is earlier than the expedition meeting time",
                context={"entry_id": entry.entry_id},
            )
        if slot.checked_in_at is None:
            return None
        if _check_in_matches(slot, data):
            return "already_applied"
        return ConflictItem(
            entry_id=entry.entry_id,
            entry_type=entry.type,
            reason="check_in_already_submitted",
            local_fingerprint=entry.fingerprint,
            existing={
                "check_in_id": slot.id,
                "checked_in_at": slot.checked_in_at.isoformat(),
                "is_safe": slot.is_safe,
            },
        )

    def _plan_incident(
        self,
        entry: PackEntry,
        *,
        expedition: Expedition,
        incidents_by_ref: dict[str, EmergencyIncident],
        member_ids: set[int],
        timeline_last: datetime | None,
        current: datetime,
    ) -> tuple[ConflictItem | str | None, datetime]:
        data = entry.data
        if data["reported_by"] not in member_ids:
            raise PackValidationError(
                "incident reported_by is not an active participant of this expedition",
                context={"entry_id": entry.entry_id, "reported_by": data["reported_by"]},
            )
        occurred_at = _parse_dt(data["occurred_at"])
        if occurred_at < expedition.meeting_at:
            raise PackValidationError(
                "incident occurred_at is earlier than the expedition meeting time",
                context={"entry_id": entry.entry_id},
            )
        if occurred_at > current + _CLOCK_SKEW:
            raise PackValidationError(
                "incident occurred_at is in the future beyond clock skew",
                context={"entry_id": entry.entry_id, "occurred_at": data["occurred_at"]},
            )
        if timeline_last is not None and occurred_at < timeline_last:
            raise PackValidationError(
                "incident timeline is out of order: occurred_at precedes an earlier entry",
                context={"entry_id": entry.entry_id, "occurred_at": data["occurred_at"]},
            )
        existing = incidents_by_ref.get(data["client_ref"])
        if existing is None:
            return None, occurred_at
        if _incident_matches(existing, data):
            return "already_applied", occurred_at
        conflict = ConflictItem(
            entry_id=entry.entry_id,
            entry_type=entry.type,
            reason="duplicate_client_ref_content",
            local_fingerprint=entry.fingerprint,
            existing={
                "incident_id": existing.id,
                "version": existing.version,
                "incident_type": str(existing.incident_type),
                "risk_level": str(existing.risk_level),
                "occurred_at": existing.occurred_at.isoformat(),
                "status": str(existing.status),
            },
        )
        return conflict, occurred_at

    def _plan_gear_check(
        self,
        entry: PackEntry,
        *,
        member_ids: set[int],
        catalog_ids: set[int],
        checks: dict[tuple[int, int], ActivityGearCheck],
    ) -> ConflictItem | str | None:
        data = entry.data
        if data["user_id"] not in member_ids:
            raise PackValidationError(
                "gear check references a user who is not an active participant",
                context={"entry_id": entry.entry_id, "user_id": data["user_id"]},
            )
        if data["verified_by"] is not None and data["verified_by"] not in member_ids:
            raise PackValidationError(
                "gear check verified_by is not a participant",
                context={"entry_id": entry.entry_id, "verified_by": data["verified_by"]},
            )
        if data["catalog_id"] not in catalog_ids:
            raise PackValidationError(
                "gear check references a catalog item that does not exist",
                context={"entry_id": entry.entry_id, "catalog_id": data["catalog_id"]},
            )
        existing = checks.get((data["user_id"], data["catalog_id"]))
        if existing is None:
            return None
        if _gear_check_matches(existing, data):
            return "already_applied"
        return ConflictItem(
            entry_id=entry.entry_id,
            entry_type=entry.type,
            reason="entity_version_changed",
            local_fingerprint=entry.fingerprint,
            existing={
                "gear_check_id": existing.id,
                "quantity": existing.quantity,
                "status": str(existing.status),
                "verified_by": existing.verified_by,
            },
        )

    # ------------------------------------------------------------- applying

    def _apply_entry(self, entry: PackEntry, current: datetime) -> AppliedEntry:
        if entry.type == ENTRY_CHECK_IN:
            resource_type, resource_id = self._apply_check_in(entry)
        elif entry.type == ENTRY_INCIDENT:
            resource_type, resource_id = self._apply_incident(entry)
        else:
            resource_type, resource_id = self._apply_gear_check(entry, current)
        return AppliedEntry(
            entry_id=entry.entry_id,
            entry_type=entry.type,
            resource_type=resource_type,
            resource_id=resource_id,
            fingerprint=entry.fingerprint,
        )

    def _apply_check_in(self, entry: PackEntry) -> tuple[str, int]:
        data = entry.data
        checked_in_at = _parse_dt(data["checked_in_at"])
        due_at = _parse_dt(data["due_at"])
        # 条件更新：仅当槽位仍未签到时写入。SQLite 写串行化保证并发的第二个
        # 导入得到 0 行 -> 整包回滚，绝不会覆盖已经写入的签到（含离线/在线互斥）。
        result = self.session.execute(
            update(ItineraryCheckIn)
            .where(
                ItineraryCheckIn.expedition_id == data["expedition_id"],
                ItineraryCheckIn.user_id == data["user_id"],
                ItineraryCheckIn.check_in_type == data["check_in_type"],
                ItineraryCheckIn.due_at == due_at,
                ItineraryCheckIn.checked_in_at.is_(None),
            )
            .values(
                checked_in_at=checked_in_at,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                note=data.get("note", ""),
                is_safe=data["is_safe"],
                late_minutes=max(int((checked_in_at - due_at).total_seconds() // 60), 0),
            )
        )
        if result.rowcount != 1:
            raise PackageIntegrityError(
                "check-in slot changed between validation and apply",
                context={"entry_id": entry.entry_id},
            )
        slot_id = self.session.scalar(
            select(ItineraryCheckIn.id).where(
                ItineraryCheckIn.expedition_id == data["expedition_id"],
                ItineraryCheckIn.user_id == data["user_id"],
                ItineraryCheckIn.check_in_type == data["check_in_type"],
                ItineraryCheckIn.due_at == due_at,
            )
        )
        return "itinerary_check_in", int(slot_id)

    def _apply_incident(self, entry: PackEntry) -> tuple[str, int]:
        data = entry.data
        incident = EmergencyIncident(
            expedition_id=data["expedition_id"],
            reported_by=data["reported_by"],
            client_ref=data["client_ref"],
            incident_type=data["incident_type"],
            risk_level=data["risk_level"],
            occurred_at=_parse_dt(data["occurred_at"]),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            description=data["description"],
            actions_taken=data.get("actions_taken", ""),
        )
        self.session.add(incident)
        self.session.flush()
        return "emergency_incident", incident.id

    def _apply_gear_check(self, entry: PackEntry, current: datetime) -> tuple[str, int]:
        data = entry.data
        check = ActivityGearCheck(
            expedition_id=data["expedition_id"],
            user_id=data["user_id"],
            catalog_id=data["catalog_id"],
            quantity=data["quantity"],
            status=data["status"],
            verified_by=data["verified_by"],
            notes=data.get("notes", ""),
            verified_at=current if data["status"] == "verified" else None,
        )
        self.session.add(check)
        self.session.flush()
        return "activity_gear_check", check.id

    def _save_record(
        self,
        *,
        pack_id: str,
        expedition_id: int,
        manifest: dict[str, Any],
        manifest_hash: str,
        fingerprints: list[str],
        applied_count: int,
        duplicate_count: int,
        actor_id: int,
    ) -> None:
        cursor = manifest["baseline_cursor"]
        record = OfflineImportRecord(
            pack_id=pack_id,
            expedition_id=expedition_id,
            manifest_hash=manifest_hash,
            baseline_hash=sha256_hex(canonical_bytes(cursor)),
            applied_count=applied_count,
            duplicate_count=duplicate_count,
            entry_fingerprints=fingerprints,
            imported_by=actor_id,
        )
        self.session.add(record)
        self.session.flush()

    def _raise_conflict(
        self,
        *,
        pack_id: str,
        expedition_id: int,
        expedition: Expedition,
        manifest: dict[str, Any],
        conflicts: list[ConflictItem],
        total: int,
    ) -> None:
        baseline_version = int(manifest["baseline_cursor"]["expedition"]["version"])
        result = {
            "pack_id": pack_id,
            "expedition_id": expedition_id,
            "baseline_expedition_version": baseline_version,
            "current_expedition_version": expedition.version,
            "total_entries": total,
            "conflicts": [item.model_dump(mode="json") for item in conflicts],
        }
        raise ActionPackConflictError(
            "action pack import aborted because one or more entries conflict",
            result=result,
        )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _check_in_matches(slot: ItineraryCheckIn, data: dict[str, Any]) -> bool:
    return (
        slot.checked_in_at is not None
        and slot.checked_in_at == _parse_dt(data["checked_in_at"])
        and slot.is_safe == data["is_safe"]
        and (slot.note or "") == (data.get("note") or "")
        and _opt_float(slot.latitude) == _opt_float(data.get("latitude"))
        and _opt_float(slot.longitude) == _opt_float(data.get("longitude"))
    )


def _incident_matches(incident: EmergencyIncident, data: dict[str, Any]) -> bool:
    return (
        incident.reported_by == data["reported_by"]
        and str(incident.incident_type) == data["incident_type"]
        and str(incident.risk_level) == data["risk_level"]
        and incident.occurred_at == _parse_dt(data["occurred_at"])
        and incident.description == data["description"]
        and (incident.actions_taken or "") == (data.get("actions_taken") or "")
        and _opt_float(incident.latitude) == _opt_float(data.get("latitude"))
        and _opt_float(incident.longitude) == _opt_float(data.get("longitude"))
    )


def _gear_check_matches(check: ActivityGearCheck, data: dict[str, Any]) -> bool:
    return (
        check.quantity == data["quantity"]
        and str(check.status) == data["status"]
        and check.verified_by == data["verified_by"]
        and (check.notes or "") == (data.get("notes") or "")
    )


def _opt_float(value: Any) -> float | None:
    return float(value) if value is not None else None
