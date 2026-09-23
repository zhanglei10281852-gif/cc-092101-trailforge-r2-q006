from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from tests.conftest import create_route, create_user
from trailforge.domain.enums import (
    ActivityStatus,
)
from trailforge.models.activities import Expedition
from trailforge.models.audit import AuditLog
from trailforge.models.offline import OfflineImportRecord
from trailforge.models.safety import EmergencyIncident, ItineraryCheckIn
from trailforge.offline.pack import ENTRY_CHECK_IN, ENTRY_INCIDENT, add_offline_entry
from trailforge.schemas.activities import ExpeditionCreate
from trailforge.schemas.gear import GearCatalogCreate, GearRequirementCreate
from trailforge.schemas.safety import CheckInScheduleCreate, RiskAssessmentCreate
from trailforge.services.activities import ExpeditionService
from trailforge.services.gear import GearService
from trailforge.services.safety import SafetyService

NOW = datetime.now(UTC)


def _seed(database) -> dict:
    with database.session() as session:
        leader_id = create_user(session, email="api-leader@example.com", name="API Leader")
        route_id = create_route(session, actor_id=leader_id, name="API Ridge")
        live = ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=leader_id,
                route_id=route_id,
                name="API Expedition",
                meeting_location="Trailhead",
                meeting_at=NOW - timedelta(hours=1),
                start_at=NOW - timedelta(minutes=30),
                end_at=NOW + timedelta(hours=6),
                registration_deadline=NOW - timedelta(days=1),
                capacity=5,
                minimum_fitness_level=1,
                risk_level="moderate",
            )
        )
        session.get(Expedition, live.id).status = ActivityStatus.IN_PROGRESS
        catalog = GearService(session).create_catalog(
            GearCatalogCreate(sku="API-KIT", name="Radio", category="comm"),
            actor_id=leader_id,
        )
        GearService(session).add_requirement(
            live.id,
            GearRequirementCreate(catalog_id=catalog.id, quantity_for_group=1, mandatory=True),
            actor_id=leader_id,
        )
        SafetyService(session).assess_risk(
            RiskAssessmentCreate(
                expedition_id=live.id,
                assessor_id=leader_id,
                category="weather",
                hazard="Afternoon thunderstorms",
                likelihood=2,
                impact=3,
                mitigation="Summit before noon",
            )
        )
        due_at = NOW - timedelta(minutes=20)
        scheduled = SafetyService(session).schedule_check_in(
            live.id,
            CheckInScheduleCreate(
                user_id=leader_id, check_in_type="routine", due_at=due_at
            ),
            actor_id=leader_id,
        )
        return {
            "leader_id": leader_id,
            "expedition_id": live.id,
            "catalog_id": catalog.id,
            "due_at": due_at,
            "check_in_id": scheduled.id,
        }


def test_export_endpoint_returns_sealed_pack(client, database) -> None:
    world = _seed(database)
    response = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    )
    assert response.status_code == 200
    pack = response.json()
    assert pack["manifest"]["format"] == "trailforge-action-pack"
    assert pack["manifest"]["expedition_id"] == world["expedition_id"]
    assert len(pack["checksum"]["manifest_sha256"]) == 64
    assert "payload" in pack and "safety_plan" in pack["payload"]
    assert "attachment" in response.headers["content-disposition"]


def test_import_endpoint_round_trip_and_idempotent_replay(client, database) -> None:
    world = _seed(database)
    pack = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    ).json()
    add_offline_entry(
        pack,
        ENTRY_CHECK_IN,
        {
            "expedition_id": world["expedition_id"],
            "user_id": world["leader_id"],
            "check_in_type": "routine",
            "due_at": world["due_at"],
            "checked_in_at": NOW - timedelta(minutes=18),
            "note": "On schedule",
            "is_safe": True,
        },
    )
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        {
            "expedition_id": world["expedition_id"],
            "reported_by": world["leader_id"],
            "client_ref": "inc-api-roundtrip-001",
            "incident_type": "delay",
            "risk_level": "low",
            "occurred_at": NOW - timedelta(minutes=6),
            "description": "Group rested longer than planned",
            "actions_taken": "",
        },
    )

    first = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"]},
        json=pack,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "applied"
    assert len(body["applied"]) == 2

    second = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"]},
        json=pack,
    )
    assert second.status_code == 200
    replay = second.json()
    assert replay["status"] == "no_op"
    assert len(replay["already_applied"]) == 2

    with database.session() as session:
        slot = session.get(ItineraryCheckIn, world["check_in_id"])
        assert slot.checked_in_at is not None and slot.is_safe is True
        assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 1


def test_conflict_abort_returns_structured_list_and_audits_rejection(client, database) -> None:
    world = _seed(database)
    # 总部先在线录入同一签到槽位
    with database.session() as session:
        SafetyService(session).submit_check_in(
            world["check_in_id"],
            _submit(world, NOW - timedelta(minutes=19), "HQ online note", "hq-key-0001"),
        )

    pack = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    ).json()
    add_offline_entry(
        pack,
        ENTRY_CHECK_IN,
        {
            "expedition_id": world["expedition_id"],
            "user_id": world["leader_id"],
            "check_in_type": "routine",
            "due_at": world["due_at"],
            "checked_in_at": NOW - timedelta(minutes=18),
            "note": "Offline note",
            "is_safe": True,
        },
    )
    response = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"]},
        json=pack,
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "action_pack_conflict"
    report = detail["context"]
    assert report["expedition_id"] == world["expedition_id"]
    assert report["conflicts"][0]["reason"] == "check_in_already_submitted"

    with database.session() as session:
        # 总部数据没有被覆盖
        assert session.get(ItineraryCheckIn, world["check_in_id"]).note == "HQ online note"
        # 冲突中止也留下不含正文的拒绝审计
        rejected = session.scalars(
            select(AuditLog).where(AuditLog.action == "pack_rejected")
        ).all()
        assert len(rejected) == 1
        assert rejected[0].after_state["reason"] == "action_pack_conflict"
        assert "Offline note" not in json.dumps(rejected[0].after_state)


def test_tampered_pack_is_rejected_with_422_and_no_write(client, database) -> None:
    world = _seed(database)
    pack = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    ).json()
    pack["payload"]["expedition"]["name"] = "Tampered"

    response = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"]},
        json=pack,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "action_pack_integrity"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(OfflineImportRecord)) == 0
        rejected = session.scalars(
            select(AuditLog).where(AuditLog.action == "pack_rejected")
        ).all()
        assert len(rejected) == 1
        assert rejected[0].after_state["reason"] == "action_pack_integrity"


def test_skip_conflicts_query_parameter_applies_remaining(client, database) -> None:
    world = _seed(database)
    with database.session() as session:
        SafetyService(session).submit_check_in(
            world["check_in_id"],
            _submit(world, NOW - timedelta(minutes=19), "HQ", "hq-key-0002"),
        )
    pack = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    ).json()
    add_offline_entry(
        pack,
        ENTRY_CHECK_IN,
        {
            "expedition_id": world["expedition_id"],
            "user_id": world["leader_id"],
            "check_in_type": "routine",
            "due_at": world["due_at"],
            "checked_in_at": NOW - timedelta(minutes=18),
            "note": "Offline",
            "is_safe": True,
        },
    )
    add_offline_entry(
        pack,
        ENTRY_INCIDENT,
        {
            "expedition_id": world["expedition_id"],
            "reported_by": world["leader_id"],
            "client_ref": "inc-api-skip-0000001",
            "incident_type": "equipment",
            "risk_level": "moderate",
            "occurred_at": NOW - timedelta(minutes=4),
            "description": "Radio battery low",
            "actions_taken": "Switched to spare battery",
        },
    )
    response = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"], "on_conflict": "skip_conflicts"},
        json=pack,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied_with_skips"
    assert len(body["applied"]) == 1
    assert body["applied"][0]["resource_type"] == "emergency_incident"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == 1


def test_invalid_on_conflict_query_is_422(client, database) -> None:
    world = _seed(database)
    pack = client.post(
        f"/api/v1/offline/expeditions/{world['expedition_id']}/action-pack/export",
        params={"actor_id": world["leader_id"]},
    ).json()
    response = client.post(
        "/api/v1/offline/action-packs/import",
        params={"actor_id": world["leader_id"], "on_conflict": "merge"},
        json=pack,
    )
    assert response.status_code == 422


def _submit(world, when, note, key):
    from trailforge.schemas.safety import CheckInSubmit

    return CheckInSubmit(
        checked_in_at=when,
        note=note,
        is_safe=True,
        idempotency_key=key,
    )
