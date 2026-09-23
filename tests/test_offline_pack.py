from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.conftest import create_route, create_user
from trailforge.database.migrations import initialize_database
from trailforge.database.session import Database
from trailforge.domain.enums import (
    ActivityStatus,
    EmergencyStatus,
    RegistrationStatus,
    TeamRole,
)
from trailforge.errors import ActionPackConflictError, PackageIntegrityError, PackValidationError
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.models.audit import AuditLog
from trailforge.models.gear import ActivityGearCheck
from trailforge.models.offline import OfflineImportRecord
from trailforge.models.safety import EmergencyIncident, ItineraryCheckIn
from trailforge.offline.pack import (
    ENTRY_CHECK_IN,
    ENTRY_GEAR_CHECK,
    ENTRY_INCIDENT,
    ActionPackIntegrityError,
    ActionPackValidationError,
    add_offline_entry,
    load_pack,
    save_pack,
)
from trailforge.schemas.gear import (
    GearCatalogCreate,
    GearRequirementCreate,
)
from trailforge.schemas.safety import (
    CheckInScheduleCreate,
    RiskAssessmentCreate,
)
from trailforge.schemas.users import EmergencyContactCreate, HealthRestrictionCreate
from trailforge.services.activities import ExpeditionService
from trailforge.services.gear import GearService
from trailforge.services.offline import OfflinePackService
from trailforge.services.safety import SafetyService
from trailforge.services.users import UserService

UTC_NOW = datetime.now(UTC)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def world(session):
    """构建一个正在进行中的活动：领队 + 一名队员、路线、装备、签到计划与风险评估。"""

    from trailforge.schemas.activities import ExpeditionCreate

    leader_id = create_user(session, email="leader@example.com", name="Lin Leader")
    member_id = create_user(session, email="member@example.com", name="Mo Member")
    # 敏感资料：健康说明全文绝不能出现在行动包里。
    UserService(session).add_restriction(
        leader_id,
        HealthRestrictionCreate(
            name="SECRET-CONDITION-XYZ",
            description="SENSITIVE-HEALTH-DETAIL-MUST-NOT-LEAVE",
            severity=3,
            activity_guidance="carry medication",
        ),
        actor_id=leader_id,
    )
    UserService(session).add_contact(
        leader_id,
        EmergencyContactCreate(
            name="Emergency Home",
            relationship_label="family",
            phone="+10000000001",
            priority=1,
        ),
        actor_id=leader_id,
    )
    route_id = create_route(session, actor_id=leader_id, name="Offline Ridge")

    start = UTC_NOW - timedelta(minutes=30)
    live = ExpeditionService(session).create(
        ExpeditionCreate(
            organizer_id=leader_id,
            route_id=route_id,
            name="Live Expedition",
            meeting_location="North trailhead",
            meeting_at=UTC_NOW - timedelta(hours=1),
            start_at=start,
            end_at=UTC_NOW + timedelta(hours=8),
            registration_deadline=UTC_NOW - timedelta(days=1),
            capacity=8,
            minimum_fitness_level=1,
            risk_level="moderate",
        )
    )
    expedition_id = live.id
    # 活动已经出发（报名期已过），直接在模型层落实状态与队员名单。
    live_entity = session.get(Expedition, expedition_id)
    live_entity.status = ActivityStatus.IN_PROGRESS
    session.add(
        ExpeditionRegistration(
            expedition_id=expedition_id,
            user_id=member_id,
            role=TeamRole.MEMBER,
            status=RegistrationStatus.CONFIRMED,
            registered_at=UTC_NOW - timedelta(days=2),
        )
    )

    catalog = GearService(session).create_catalog(
        GearCatalogCreate(
            sku="PACK-001",
            name="First-aid kit",
            category="medical",
            safety_critical=True,
        ),
        actor_id=leader_id,
    )
    GearService(session).add_requirement(
        expedition_id,
        GearRequirementCreate(catalog_id=catalog.id, quantity_per_person=1, mandatory=True),
        actor_id=leader_id,
    )
    SafetyService(session).assess_risk(
        RiskAssessmentCreate(
            expedition_id=expedition_id,
            assessor_id=leader_id,
            category="terrain",
            hazard="Loose scree on the ridge",
            likelihood=3,
            impact=4,
            mitigation="Keep spacing, use helmet",
        )
    )
    # 一个待完成的签到计划（离线端将补录完成）。
    due_at = UTC_NOW - timedelta(minutes=20)
    scheduled = SafetyService(session).schedule_check_in(
        expedition_id,
        CheckInScheduleCreate(
            user_id=leader_id, check_in_type="routine", due_at=due_at
        ),
        actor_id=leader_id,
    )

    session.flush()
    return {
        "leader_id": leader_id,
        "member_id": member_id,
        "route_id": route_id,
        "expedition_id": expedition_id,
        "catalog_id": catalog.id,
        "check_in_due_at": due_at,
        "check_in_id": scheduled.id,
    }


def _check_in_entry(world, *, when: datetime | None = None, is_safe: bool = True) -> dict:
    return {
        "type": ENTRY_CHECK_IN,
        "data": {
            "expedition_id": world["expedition_id"],
            "user_id": world["leader_id"],
            "check_in_type": "routine",
            "due_at": world["check_in_due_at"],
            "checked_in_at": when or (UTC_NOW - timedelta(minutes=18)),
            "latitude": 30.1,
            "longitude": 120.1,
            "note": "Arrived at the ridge junction",
            "is_safe": is_safe,
        },
    }


def _incident_entry(
    world,
    *,
    ref: str,
    when: datetime,
    reported_by: int | None = None,
    incident_type: str = "injury",
    description: str = "Twisted ankle on loose rock",
    risk_level: str = "high",
) -> dict:
    return {
        "type": ENTRY_INCIDENT,
        "data": {
            "expedition_id": world["expedition_id"],
            "reported_by": reported_by or world["leader_id"],
            "client_ref": ref,
            "incident_type": incident_type,
            "risk_level": risk_level,
            "occurred_at": when,
            "latitude": 30.12,
            "longitude": 120.12,
            "description": description,
            "actions_taken": "Splinted and rested",
        },
    }


def _gear_entry(world, *, user_id: int | None = None, catalog_id: int | None = None) -> dict:
    return {
        "type": ENTRY_GEAR_CHECK,
        "data": {
            "expedition_id": world["expedition_id"],
            "user_id": user_id or world["leader_id"],
            "catalog_id": catalog_id or world["catalog_id"],
            "quantity": 1,
            "status": "verified",
            "verified_by": world["leader_id"],
            "notes": "Kit complete",
        },
    }


def _export(session, world, *, actor_id: int | None = None):
    return OfflinePackService(session).export_pack(
        world["expedition_id"], actor_id=actor_id or world["leader_id"]
    )


# --------------------------------------------------------------------- export


def test_export_contains_required_sections_and_baseline_cursor(session, world) -> None:
    pack, response = _export(session, world)
    assert response.entry_count == 0
    payload = pack["payload"]
    assert payload["expedition"]["id"] == world["expedition_id"]
    assert payload["expedition"]["version"] == 1
    assert payload["route"]["segments"][0]["name"] == "Main ridge"
    assert {m["user_id"] for m in payload["members"]} == {
        world["leader_id"],
        world["member_id"],
    }
    assert payload["members"][0]["emergency_contacts"][0]["phone"] == "+10000000001"
    assert payload["gear"]["catalog"][0]["sku"] == "PACK-001"
    assert payload["gear"]["requirements"][0]["mandatory"] is True
    assert payload["safety_plan"]["risk_assessments"][0]["hazard"].startswith("Loose scree")
    assert payload["safety_plan"]["check_ins"][0]["checked_in_at"] is None
    cursor = pack["manifest"]["baseline_cursor"]
    assert cursor["expedition"]["version"] == 1
    assert len(cursor["payload_sha256"]) == 64
    assert pack["checksum"]["algorithm"] == "sha256"


def test_export_excludes_health_text_email_and_unrelated_profiles(session, world, tmp_path) -> None:
    pack, _ = _export(session, world)
    save_pack(pack, tmp_path / "pack.json")
    raw = (tmp_path / "pack.json").read_bytes()
    # 健康说明全文与疾病名称不得出现在包中
    assert b"SENSITIVE-HEALTH-DETAIL-MUST-NOT-LEAVE" not in raw
    assert b"SECRET-CONDITION-XYZ" not in raw
    # 邮箱与出生日期不是“必要信息”，不得导出
    assert b"leader@example.com" not in raw
    assert b"member@example.com" not in raw
    # 另一台电脑/其他用户的资料也不得出现
    other_id = create_user(session, email="stranger@example.com", name="Stranger")
    assert b"stranger@example.com" not in raw
    assert str(other_id).encode() not in _member_ids_blob(pack)


def _member_ids_blob(pack) -> bytes:
    return json.dumps(pack["payload"]["members"]).encode()


def test_pack_file_is_stable_utf8_json(session, world, tmp_path) -> None:
    pack, _ = _export(session, world)
    target = tmp_path / "pack.json"
    save_pack(pack, target)
    raw = target.read_bytes()
    raw.decode("utf-8")  # 必须是合法 UTF-8
    assert raw.endswith(b"\n")
    # 重新解析得到的包与内存中的包具有相同的 manifest 哈希
    reloaded = load_pack(target)
    assert reloaded["checksum"]["manifest_sha256"] == pack["checksum"]["manifest_sha256"]


# ------------------------------------------------------------- tamper detection


def test_modified_payload_is_rejected_before_any_write(session, world) -> None:
    pack, _ = _export(session, world)
    pack["payload"]["expedition"]["name"] = "Tampered name"
    with pytest.raises(ActionPackIntegrityError, match="payload checksum"):
        load_pack(pack)


def test_modified_entry_is_rejected(session, world) -> None:
    from trailforge.offline.pack import canonical_bytes, sha256_hex

    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_GEAR_CHECK, _gear_entry(world)["data"])

    # 攻击 1：直接改条目，不重算任何哈希 -> manifest 校验先失败
    direct = copy.deepcopy(pack)
    direct["manifest"]["entries"][0]["data"]["quantity"] = 42
    with pytest.raises(ActionPackIntegrityError, match="manifest checksum mismatch"):
        load_pack(direct)

    # 攻击 2：重封 manifest 但保留旧的条目哈希 -> 逐条哈希失败
    resealed = copy.deepcopy(pack)
    resealed["manifest"]["entries"][0]["data"]["quantity"] = 42
    resealed["checksum"]["manifest_sha256"] = sha256_hex(
        canonical_bytes(resealed["manifest"])
    )
    with pytest.raises(ActionPackIntegrityError, match="entry checksum mismatch"):
        load_pack(resealed)


def test_manifest_tamper_after_legit_append_is_detected(session, world, tmp_path) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_INCIDENT, _incident_entry(
        world, ref="inc-aaaaaaaaaaaa1", when=UTC_NOW - timedelta(minutes=10)
    )["data"])
    # 离线端正常写盘后文件可校验通过
    save_pack(pack, tmp_path / "legit.json")
    legit = load_pack(tmp_path / "legit.json")

    # 篡改 pack_id 后直接改 JSON 而不重算 -> manifest 校验失败
    legit["manifest"]["pack_id"] = "pack-forged"
    save_raw = json.dumps(legit, ensure_ascii=False).encode()
    (tmp_path / "forged.json").write_bytes(save_raw)
    with pytest.raises(ActionPackIntegrityError, match="manifest checksum mismatch"):
        load_pack(tmp_path / "forged.json")


def test_broken_json_and_wrong_activity_are_rejected(session, world, tmp_path) -> None:
    (tmp_path / "broken.json").write_bytes(b"{not json")
    with pytest.raises(ActionPackIntegrityError):
        load_pack(tmp_path / "broken.json")

    pack, _ = _export(session, world)
    # 条目的来源活动与包不一致
    foreign = _gear_entry(world)["data"]
    foreign["expedition_id"] = world["expedition_id"] + 999
    with pytest.raises(Exception, match="expedition_id"):
        add_offline_entry(pack, ENTRY_GEAR_CHECK, foreign)


def test_entry_from_another_expedition_rejected_at_import(session, world) -> None:
    pack, _ = _export(session, world)
    entry = _incident_entry(
        world, ref="inc-bbbbbbbbbbbb2", when=UTC_NOW - timedelta(minutes=10)
    )
    entry["data"]["expedition_id"] = 999999
    # 手工构造跨活动条目并按规范重封，仍然必须在导入前被 verify 拒绝
    from trailforge.offline.pack import canonical_bytes, sha256_hex

    forged = copy.deepcopy(pack)
    forged["manifest"]["entries"].append(
        {
            "entry_id": "deadbeefdeadbeef",
            "type": ENTRY_INCIDENT,
            "sha256": sha256_hex(
                canonical_bytes({"type": ENTRY_INCIDENT, "data": entry["data"]})
            ),
            "data": entry["data"],
        }
    )
    forged["checksum"]["manifest_sha256"] = sha256_hex(
        canonical_bytes(forged["manifest"])
    )
    with pytest.raises(PackageIntegrityError, match="expedition_id"):
        OfflinePackService(session).import_pack(forged, actor_id=world["leader_id"])


# ----------------------------------------------------------------- happy roundtrip


def test_roundtrip_applies_all_three_entry_types(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_CHECK_IN, _check_in_entry(world)["data"])
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world, ref="inc-1111111111111", when=UTC_NOW - timedelta(minutes=10)
        )["data"],
    )
    add_offline_entry(pack, ENTRY_GEAR_CHECK, _gear_entry(world)["data"])

    result = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert result.status == "applied"
    assert result.committed is True
    assert len(result.applied) == 3
    assert result.total_entries == 3

    slot = session.get(ItineraryCheckIn, world["check_in_id"])
    assert slot.checked_in_at is not None
    assert slot.is_safe is True
    assert slot.late_minutes == 2

    incident = session.scalar(
        select(EmergencyIncident).where(EmergencyIncident.client_ref == "inc-1111111111111")
    )
    assert incident is not None
    assert incident.status == EmergencyStatus.OPEN
    assert incident.version == 1

    check = session.scalar(
        select(ActivityGearCheck).where(
            ActivityGearCheck.expedition_id == world["expedition_id"],
            ActivityGearCheck.user_id == world["leader_id"],
            ActivityGearCheck.catalog_id == world["catalog_id"],
        )
    )
    assert check is not None and check.status == "verified" and check.verified_at is not None

    record = session.scalar(select(OfflineImportRecord))
    assert record is not None
    assert record.pack_id == pack["manifest"]["pack_id"]
    assert record.applied_count == 3
    assert len(record.entry_fingerprints) == 3


def test_reimporting_same_pack_is_a_no_op(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_CHECK_IN, _check_in_entry(world)["data"])

    first = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert first.status == "applied"
    incidents_before = session.scalar(select(func.count()).select_from(EmergencyIncident))
    records_before = session.scalar(select(func.count()).select_from(OfflineImportRecord))
    audits_before = session.scalar(select(func.count()).select_from(AuditLog))

    second = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert second.status == "no_op"
    assert second.applied == []
    assert len(second.already_applied) == 1
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == records_before
    # 重复导入仍写一条审计，但业务实体数不变
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == incidents_before
    assert session.scalar(select(func.count()).select_from(AuditLog)) == audits_before + 1


def test_same_pack_id_with_different_content_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_CHECK_IN, _check_in_entry(world)["data"])
    OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])

    # 复用 pack_id 但换 payload，重新密封整条哈希链
    forged = copy.deepcopy(pack)
    forged["payload"]["expedition"]["name"] = "Another name"
    from trailforge.offline.pack import canonical_bytes, sha256_hex

    forged["manifest"]["baseline_cursor"]["payload_sha256"] = sha256_hex(
        canonical_bytes(forged["payload"])
    )
    forged["checksum"]["manifest_sha256"] = sha256_hex(
        canonical_bytes(forged["manifest"])
    )
    with pytest.raises(PackageIntegrityError, match="different content"):
        OfflinePackService(session).import_pack(forged, actor_id=world["leader_id"])


# ------------------------------------------------------- conflicts and atomicity


def test_hq_submitted_check_in_creates_conflict_and_abort_writes_nothing(session, world) -> None:
    pack, _ = _export(session, world)
    # 总部在领队离线期间先录入了一次签到（内容不同）
    SafetyService(session).submit_check_in(
        world["check_in_id"],
        _submit_payload(world, when=UTC_NOW - timedelta(minutes=19), note="HQ record", safe=True),
    )
    # 另加一条本来可以应用的事件：abort 时必须一起回滚
    add_offline_entry(pack, ENTRY_CHECK_IN, _check_in_entry(world)["data"])
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world, ref="inc-2222222222222", when=UTC_NOW - timedelta(minutes=5)
        )["data"],
    )
    incidents_before = session.scalar(select(func.count()).select_from(EmergencyIncident))
    with pytest.raises(ActionPackConflictError) as exc_info:
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    report = exc_info.value.result
    assert report["current_expedition_version"] == 1
    conflict = report["conflicts"][0]
    assert conflict["reason"] == "check_in_already_submitted"
    assert conflict["existing"]["check_in_id"] == world["check_in_id"]
    # 原子性：总部签到未被覆盖，新事件没有写入，也没有导入记录
    slot = session.get(ItineraryCheckIn, world["check_in_id"])
    assert slot.note == "HQ record"
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == incidents_before
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 0


def test_skip_conflicts_commits_the_rest_atomically(session, world) -> None:
    pack, _ = _export(session, world)
    SafetyService(session).submit_check_in(
        world["check_in_id"],
        _submit_payload(world, when=UTC_NOW - timedelta(minutes=19), note="HQ record", safe=True),
    )
    add_offline_entry(pack, ENTRY_CHECK_IN, _check_in_entry(world)["data"])
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world, ref="inc-3333333333333", when=UTC_NOW - timedelta(minutes=5)
        )["data"],
    )
    result = OfflinePackService(session).import_pack(
        pack, actor_id=world["leader_id"], on_conflict="skip_conflicts"
    )
    assert result.status == "applied_with_skips"
    assert len(result.applied) == 1
    assert result.applied[0].resource_type == "emergency_incident"
    assert result.skipped == [c.entry_id for c in result.conflicts]
    # 总部数据保持不变
    assert session.get(ItineraryCheckIn, world["check_in_id"]).note == "HQ record"
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 1


def test_hq_new_incident_version_with_same_client_ref_is_never_overwritten(session, world) -> None:
    pack, _ = _export(session, world)
    when = UTC_NOW - timedelta(minutes=30)
    # 总部已有同 client_ref 但内容不同的新版本事件
    session.add(
        EmergencyIncident(
            expedition_id=world["expedition_id"],
            reported_by=world["member_id"],
            client_ref="inc-shared-ref-0001",
            incident_type="weather",
            risk_level="critical",
            occurred_at=when,
            description="HQ: sudden storm",
        )
    )
    session.flush()
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world,
            ref="inc-shared-ref-0001",
            when=when,
            description="Offline: twisted ankle",
            incident_type="injury",
        )["data"],
    )
    with pytest.raises(ActionPackConflictError) as exc_info:
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    conflict = exc_info.value.result["conflicts"][0]
    assert conflict["reason"] == "duplicate_client_ref_content"
    remaining = session.scalars(
        select(EmergencyIncident).where(
            EmergencyIncident.client_ref == "inc-shared-ref-0001"
        )
    ).all()
    assert len(remaining) == 1
    assert remaining[0].description == "HQ: sudden storm"


def test_identical_hq_incident_is_recognised_as_already_applied(session, world) -> None:
    pack, _ = _export(session, world)
    when = UTC_NOW - timedelta(minutes=12)
    data = _incident_entry(world, ref="inc-same-same-000001", when=when)["data"]
    add_offline_entry(pack, ENTRY_INCIDENT, data)
    # 另一份包（同样的指纹）先导入；此处用重复导入模拟“总部已有同一实体”
    result1 = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert result1.status == "applied"
    result2 = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert result2.status == "no_op"


def test_fresh_pack_matching_hq_entities_is_no_op_without_duplicates(session, world) -> None:
    """全新 pack_id 的包，但其条目与总部已有同 client_ref 事件内容完全一致：
    必须识别为 already_applied，零业务写入、零重复行。"""

    when = UTC_NOW - timedelta(minutes=12)
    data = _incident_entry(world, ref="inc-existing-hq-00001", when=when)["data"]
    # 总部先有同 client_ref、同内容的事件
    first_pack, _ = _export(session, world)
    add_offline_entry(first_pack, ENTRY_INCIDENT, data)
    OfflinePackService(session).import_pack(first_pack, actor_id=world["leader_id"])
    incidents_after_first = session.scalar(
        select(func.count()).select_from(EmergencyIncident)
    )
    records_after_first = session.scalar(
        select(func.count()).select_from(OfflineImportRecord)
    )

    # 之后导出的另一个包（不同 pack_id）包含同一事件条目
    second_pack, _ = _export(session, world)
    add_offline_entry(second_pack, ENTRY_INCIDENT, data)
    result = OfflinePackService(session).import_pack(
        second_pack, actor_id=world["leader_id"]
    )
    assert result.status == "no_op"
    assert result.applied == []
    assert len(result.already_applied) == 1
    assert len(result.conflicts) == 0
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == (
        incidents_after_first
    )
    # 即使没有新条目应用，也为这个新 pack_id 登记去重记录
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == (
        records_after_first + 1
    )


# ------------------------------------------------------- referential / time rules


def test_unknown_participant_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    entry = _gear_entry(world, user_id=987654)
    # add_offline_entry 不查库（离线端无法查）；引用完整性在导回时拦截
    add_offline_entry(pack, ENTRY_GEAR_CHECK, entry["data"])
    with pytest.raises(PackValidationError, match="active participant"):
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 0


def test_missing_schedule_slot_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    data = _check_in_entry(world)["data"]
    data["check_in_type"] = "waypoint"  # 总部不存在该计划槽位
    add_offline_entry(pack, ENTRY_CHECK_IN, data)
    with pytest.raises(PackValidationError, match="schedule slot"):
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])


def test_unknown_catalog_item_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_GEAR_CHECK, _gear_entry(world, catalog_id=424242)["data"])
    with pytest.raises(PackValidationError, match="catalog"):
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])


def test_incident_timeline_must_be_in_order(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world, ref="inc-order-later-0001", when=UTC_NOW - timedelta(minutes=5)
        )["data"],
    )
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world, ref="inc-order-earlier-001", when=UTC_NOW - timedelta(minutes=20)
        )["data"],
    )
    with pytest.raises(PackValidationError, match="out of order"):
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == 0


def test_check_in_earlier_than_meeting_is_rejected_offline(session, world) -> None:
    pack, _ = _export(session, world)
    data = _check_in_entry(world)["data"]
    data["checked_in_at"] = UTC_NOW - timedelta(days=3)
    data["due_at"] = UTC_NOW - timedelta(days=3)
    with pytest.raises(ActionPackValidationError, match="meeting time"):
        add_offline_entry(pack, ENTRY_CHECK_IN, data)


def test_duplicate_natural_key_inside_one_pack_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    add_offline_entry(pack, ENTRY_GEAR_CHECK, _gear_entry(world)["data"])
    second = _gear_entry(world)["data"]
    second["quantity"] = 2  # 同一 (user, catalog) 不同内容
    add_offline_entry(pack, ENTRY_GEAR_CHECK, second)
    with pytest.raises(PackValidationError, match="appears twice"):
        OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])


# ----------------------------------------------------------------- restart / bulk
def test_concurrent_imports_of_same_check_in_slot_leave_exactly_one_winner(settings) -> None:
    """两个不同的行动包并发补录同一签到槽位：恰好一个成功，另一个整包回滚。"""

    from concurrent.futures import ThreadPoolExecutor

    from trailforge.schemas.activities import ExpeditionCreate

    database = Database(settings)
    initialize_database(database)
    with database.session() as session:
        leader_id = create_user(session, email="c-leader@example.com", name="C Leader")
        route_id = create_route(session, actor_id=leader_id, name="C Ridge")
        now = datetime.now(UTC)
        expedition = ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=leader_id,
                route_id=route_id,
                name="Concurrent Expedition",
                meeting_location="Trailhead",
                meeting_at=now - timedelta(hours=1),
                start_at=now - timedelta(minutes=30),
                end_at=now + timedelta(hours=5),
                registration_deadline=now - timedelta(days=1),
                capacity=4,
                minimum_fitness_level=1,
                risk_level="low",
            )
        )
        session.get(Expedition, expedition.id).status = ActivityStatus.IN_PROGRESS
        due_at = now - timedelta(minutes=20)
        scheduled = SafetyService(session).schedule_check_in(
            expedition.id,
            CheckInScheduleCreate(
                user_id=leader_id, check_in_type="routine", due_at=due_at
            ),
            actor_id=leader_id,
        )
        # 导出两个不同 pack_id 的包，包含同一槽位的补录
        packs = []
        for _ in range(2):
            pack, _ = OfflinePackService(session).export_pack(
                expedition.id, actor_id=leader_id
            )
            add_offline_entry(
                pack,
                ENTRY_CHECK_IN,
                {
                    "expedition_id": expedition.id,
                    "user_id": leader_id,
                    "check_in_type": "routine",
                    "due_at": due_at,
                    "checked_in_at": now - timedelta(minutes=18),
                    "note": "Offline",
                    "is_safe": True,
                },
            )
            packs.append(pack)
        check_in_id = scheduled.id

    outcomes: list[str] = []

    def run_import(pack) -> str:
        try:
            with database.session() as thread_session:
                OfflinePackService(thread_session).import_pack(
                    pack, actor_id=1
                )
            return "applied"
        except PackageIntegrityError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_import, pack) for pack in packs]
        outcomes = [future.result() for future in futures]

    assert sorted(outcomes) == ["applied", "rejected"]
    with database.session() as session:
        slot = session.get(ItineraryCheckIn, check_in_id)
        assert slot.checked_in_at is not None
        assert session.scalar(
            select(func.count()).select_from(OfflineImportRecord)
        ) == 1
    database.engine.dispose()


def test_import_survives_engine_restart_and_reimports_as_no_op(settings) -> None:
    # 自建文件库验证“返程后重启电脑”场景
    database = Database(settings)
    initialize_database(database)
    with database.session() as session:
        leader_id = create_user(session, email="r-leader@example.com", name="Restart Leader")
        route_id = create_route(session, actor_id=leader_id, name="Restart Ridge")
        from trailforge.schemas.activities import ExpeditionCreate

        now = datetime.now(UTC)
        expedition = ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=leader_id,
                route_id=route_id,
                name="Restart Expedition",
                meeting_location="Trailhead",
                meeting_at=now - timedelta(hours=1),
                start_at=now - timedelta(minutes=30),
                end_at=now + timedelta(hours=5),
                registration_deadline=now - timedelta(days=1),
                capacity=4,
                minimum_fitness_level=1,
                risk_level="low",
            )
        )
        pack, _ = OfflinePackService(session).export_pack(
            expedition.id, actor_id=leader_id
        )
        add_offline_entry(
            pack,
            ENTRY_INCIDENT,
            {
                "expedition_id": expedition.id,
                "reported_by": leader_id,
                "client_ref": "inc-restart-00000001",
                "incident_type": "delay",
                "risk_level": "low",
                "occurred_at": now - timedelta(minutes=10),
                "description": "Slow group pace",
                "actions_taken": "",
            },
        )
        pack_bytes = json.dumps(pack, ensure_ascii=False).encode()
        OfflinePackService(session).import_pack(pack, actor_id=leader_id)
    database.engine.dispose()

    # 模拟关机后重新打开同一数据库文件，再次导入同一个包
    restarted = Database(settings)
    initialize_database(restarted)
    with restarted.session() as session:
        reopened = load_pack(json.loads(pack_bytes.decode()))
        result = OfflinePackService(session).import_pack(reopened, actor_id=1)
        assert result.status == "no_op"
        assert len(result.already_applied) == 1
    restarted.engine.dispose()


def test_large_batch_imports_atomically(session, world) -> None:
    pack, _ = _export(session, world)
    total = 300
    for index in range(total):
        add_offline_entry(
            pack,
            ENTRY_INCIDENT,
            _incident_entry(
                world,
                ref=f"inc-bulk-{index:010d}",
                when=UTC_NOW - timedelta(minutes=15) + timedelta(seconds=index),
                description=f"Observation {index}",
                risk_level="low",
                incident_type="other",
            )["data"],
        )
    assert len(pack["manifest"]["entries"]) == total
    result = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert result.status == "applied"
    assert len(result.applied) == total
    assert (
        session.scalar(select(func.count()).select_from(EmergencyIncident)) == total
    )

    # 大批量包重放仍须零写入
    again = OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    assert again.status == "no_op"
    assert len(again.already_applied) == total
    assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 1


# ------------------------------------------------------------------------ audit


def test_export_and_import_audit_logs_contain_no_sensitive_body(session, world) -> None:
    pack, _ = _export(session, world)
    export_hash = pack["checksum"]["manifest_sha256"]
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        _incident_entry(
            world,
            ref="inc-audit-000000001",
            when=UTC_NOW - timedelta(minutes=8),
            description="SECRET-INCIDENT-BODY-XYZ",
        )["data"],
    )
    OfflinePackService(session).import_pack(pack, actor_id=world["leader_id"])
    logs = session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "offline_action_pack")
    ).all()
    actions = {str(log.action) for log in logs}
    assert {"pack_exported", "pack_imported"} <= actions
    serialized = json.dumps(
        [
            {"before": log.before_state, "after": log.after_state, "context": log.context}
            for log in logs
        ],
        ensure_ascii=False,
    )
    assert "SECRET-INCIDENT-BODY-XYZ" not in serialized
    assert "SENSITIVE-HEALTH-DETAIL-MUST-NOT-LEAVE" not in serialized
    export_log = next(log for log in logs if str(log.action) == "pack_exported")
    assert export_log.after_state["manifest_sha256"] == export_hash


def test_rejection_is_audited_without_body_when_called(session, world) -> None:
    pack, _ = _export(session, world)
    pack["payload"]["expedition"]["name"] = "tampered"
    service = OfflinePackService(session)
    service.log_rejection(
        pack=pack, reason="action_pack_integrity", actor_id=world["leader_id"]
    )
    session.flush()
    log = session.scalar(
        select(AuditLog).where(AuditLog.action == "pack_rejected")
    )
    assert log is not None
    assert log.after_state["reason"] == "action_pack_integrity"
    assert "tampered" not in json.dumps(log.after_state)


# ------------------------------------------------------------------------- schema


def test_invalid_on_conflict_value_is_rejected(session, world) -> None:
    pack, _ = _export(session, world)
    with pytest.raises(PackValidationError, match="on_conflict"):
        OfflinePackService(session).import_pack(
            pack, actor_id=world["leader_id"], on_conflict="merge"
        )


# ----------------------------------------------------------------------- helpers


def _submit_payload(world, *, when: datetime, note: str, safe: bool):
    from trailforge.schemas.safety import CheckInSubmit

    return CheckInSubmit(
        checked_in_at=when,
        latitude=None,
        longitude=None,
        note=note,
        is_safe=safe,
        idempotency_key=f"submit-{world['check_in_id']}-{note}",
    )
