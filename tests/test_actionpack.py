from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.conftest import create_expedition, create_route, create_user
from trailforge.errors import ActionPackIntegrityError, ActionPackValidationError
from trailforge.models.actionpack import (
    ActionPackAuditRecord,
    ActionPackImport,
)
from trailforge.models.activities import Expedition
from trailforge.models.gear import (
    ActivityGearCheck,
)
from trailforge.models.safety import EmergencyIncident, ItineraryCheckIn
from trailforge.schemas.activities import ActivityStateChange, RegistrationCreate
from trailforge.schemas.gear import (
    GearCatalogCreate,
    GearRequirementCreate,
)
from trailforge.schemas.safety import CheckInScheduleCreate
from trailforge.services.actionpack import ActionPackService
from trailforge.services.actionpack_codec import (
    decode_pack_bytes,
    encode_pack_bytes,
    open_pack,
    seal_pack,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.gear import GearService
from trailforge.services.safety import SafetyService

UTC = UTC


# --------------------------------------------------------------------- helpers


def _setup_activity(session, *, members: int = 2, with_slot: bool = True) -> dict:
    organizer = create_user(session, email="leader@example.com", name="Leader")
    route_id = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
    activity_service = ExpeditionService(session)
    activity_service.change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    participant_ids = []
    for index in range(members):
        uid = create_user(
            session,
            email=f"hiker{index}@example.com",
            name=f"Hiker {index}",
        )
        activity_service.register(
            expedition_id,
            RegistrationCreate(
                user_id=uid, idempotency_key=f"register-{index}-{expedition_id}"
            ),
        )
        participant_ids.append(uid)
    gear_service = GearService(session)
    catalog = gear_service.create_catalog(
        GearCatalogCreate(
            sku="HELMET-01",
            name="Climbing helmet",
            category="safety",
            default_weight_grams=350,
            safety_critical=True,
        ),
        actor_id=organizer,
    )
    gear_service.add_requirement(
        expedition_id,
        GearRequirementCreate(catalog_id=catalog.id, quantity_per_person=1, mandatory=True),
        actor_id=organizer,
    )
    slot_id = None
    if with_slot:
        expedition = session.get(Expedition, expedition_id)
        slot = SafetyService(session).schedule_check_in(
            expedition_id,
            CheckInScheduleCreate(
                user_id=participant_ids[0],
                check_in_type="waypoint",
                due_at=expedition.start_at + timedelta(hours=2),
            ),
            actor_id=organizer,
        )
        slot_id = slot.id
    return {
        "organizer": organizer,
        "expedition_id": expedition_id,
        "participants": participant_ids,
        "catalog_id": catalog.id,
        "slot_id": slot_id,
    }


def _load_pack(raw: bytes, secret: str | None = None) -> dict:
    return open_pack(decode_pack_bytes(raw), secret=secret)


def _repack(manifest: dict, secret: str | None = None) -> bytes:
    return encode_pack_bytes(seal_pack(manifest, secret=secret))


def _add_entries(
    manifest: dict,
    *,
    check_ins: list[dict] | None = None,
    incidents: list[dict] | None = None,
    incident_updates: list[dict] | None = None,
    gear_checks: list[dict] | None = None,
) -> None:
    manifest["offline"]["check_ins"] = check_ins or []
    manifest["offline"]["incidents"] = incidents or []
    manifest["offline"]["incident_updates"] = incident_updates or []
    manifest["offline"]["gear_checks"] = gear_checks or []


def _check_in_entry(slot_id: int, uid: str, when: datetime, *, is_safe: bool = True) -> dict:
    return {
        "entry_type": "check_in",
        "uid": uid,
        "slot_id": slot_id,
        "checked_in_at": when.isoformat(),
        "note": "waypoint reached",
        "is_safe": is_safe,
        "recorded_at": when.isoformat(),
    }


def _incident_entry(reporter: int, uid: str, when: datetime, **overrides) -> dict:
    payload = {
        "entry_type": "incident",
        "uid": uid,
        "reported_by": reporter,
        "incident_type": "injury",
        "risk_level": "high",
        "occurred_at": when.isoformat(),
        "description": "Twisted ankle on descent",
        "actions_taken": "Rest and ice",
        "recorded_at": (when + timedelta(minutes=5)).isoformat(),
    }
    payload.update(overrides)
    return payload


def _gear_check_entry(user_id: int, catalog_id: int, uid: str, when: datetime) -> dict:
    return {
        "entry_type": "gear_check",
        "uid": uid,
        "user_id": user_id,
        "catalog_id": catalog_id,
        "quantity": 1,
        "status": "verified",
        "verified_by": user_id,
        "notes": "checked",
        "recorded_at": when.isoformat(),
    }


# --------------------------------------------------------------------- export


def test_export_contains_snapshot_and_baseline_but_no_sensitive_text(session) -> None:
    ctx = _setup_activity(session)
    raw, summary = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    assert manifest["format"] == "trailforge.action_pack"
    snap = manifest["snapshot"]
    assert snap["expedition"]["id"] == ctx["expedition_id"]
    assert "description" not in snap["expedition"]
    assert snap["route"]["segments"]
    assert snap["members"][0]["display_name"]
    member = snap["members"][0]
    assert "email" not in member
    assert "birth_date" not in member
    assert snap["gear"]["requirements"]
    assert snap["safety_plan"]["check_in_slots"]
    assert manifest["baseline"]["expedition"]["version"] >= 1
    # Health and free-text fields must never appear in the pack.
    blob = raw.decode("utf-8")
    assert "health" not in blob.lower()
    assert "emergency_contact" not in blob
    assert summary.digest


def test_export_writes_sanitized_audit_record(session) -> None:
    ctx = _setup_activity(session)
    ActionPackService(session).export_pack(ctx["expedition_id"], actor_id=ctx["organizer"])
    record = session.scalar(
        select(ActionPackAuditRecord).where(ActionPackAuditRecord.stage == "export")
    )
    assert record is not None
    assert record.pack_digest
    assert "description" not in json.dumps(record.summary, ensure_ascii=False)


# ----------------------------------------------------------------- round trip


def test_happy_roundtrip_applies_all_entries(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2, minutes=5)
    _add_entries(
        manifest,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-0001", when)],
        incidents=[_incident_entry(ctx["participants"][0], "incident-0001", when)],
        gear_checks=[
            _gear_check_entry(ctx["participants"][0], ctx["catalog_id"], "gear-0001", when)
        ],
    )
    report = ActionPackService(session).import_pack(_repack(manifest), actor_id=ctx["organizer"])
    assert report.outcome == "committed"
    assert len(report.applied) == 3

    slot = session.get(ItineraryCheckIn, ctx["slot_id"])
    assert slot.checked_in_at is not None
    assert slot.is_safe is True
    incident = session.scalar(
        select(EmergencyIncident).where(
            EmergencyIncident.expedition_id == ctx["expedition_id"]
        )
    )
    assert incident.status == "open"
    check = session.scalar(
        select(ActivityGearCheck).where(
            ActivityGearCheck.expedition_id == ctx["expedition_id"]
        )
    )
    assert check.status == "verified"
    audit = session.scalar(
        select(ActionPackAuditRecord).where(ActionPackAuditRecord.stage == "import")
    )
    assert audit.outcome == "committed"


def test_repeated_import_of_same_pack_has_no_side_effects(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=3)
    _add_entries(
        manifest,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-0001", when)],
    )
    pack = _repack(manifest)
    first = ActionPackService(session).import_pack(pack, actor_id=ctx["organizer"])
    assert first.outcome == "committed"
    second = ActionPackService(session).import_pack(pack, actor_id=ctx["organizer"])
    assert second.replayed is True
    assert second.outcome == "committed"  # original outcome preserved
    assert session.scalar(
        select(func.count()).select_from(ActionPackImport)
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(ActionPackAuditRecord)
        .where(ActionPackAuditRecord.stage == "import")
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(ItineraryCheckIn)
        .where(ItineraryCheckIn.checked_in_at.is_not(None))
    ) == 1


# ------------------------------------------------------------- tamper / damage


def test_tampered_manifest_is_rejected_before_any_write(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-0001", when)],
    )
    envelope = seal_pack(manifest)
    # Tamper with the sealed manifest but keep the original digest.
    envelope["manifest"]["offline"]["check_ins"][0]["note"] = "tampered"
    tampered = encode_pack_bytes(envelope)
    with pytest.raises(ActionPackIntegrityError, match="digest mismatch"):
        ActionPackService(session).import_pack(tampered, actor_id=ctx["organizer"])
    session.rollback()
    assert session.get(ItineraryCheckIn, ctx["slot_id"]).checked_in_at is None
    # The rejection itself is audited in a separate transaction.
    rejection = session.scalar(
        select(ActionPackAuditRecord).where(
            ActionPackAuditRecord.stage == "import",
            ActionPackAuditRecord.outcome == "rejected",
        )
    )
    assert rejection is not None
    assert rejection.summary["reason"]


def test_corrupted_bytes_are_rejected(session) -> None:
    with pytest.raises(ActionPackIntegrityError):
        ActionPackService(session).import_pack(b"{not json", actor_id=1)
    session.rollback()


def test_hmac_signature_is_verified_when_configured(session) -> None:
    ctx = _setup_activity(session)
    raw, _ = ActionPackService(session, secret="s3cret").export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    # Same bytes must verify with the configured secret.
    report_service = ActionPackService(session, secret="s3cret")
    manifest = _load_pack(raw, secret="s3cret")
    _add_entries(
        manifest,
        gear_checks=[
            _gear_check_entry(
                ctx["participants"][0],
                ctx["catalog_id"],
                "gear-0001",
                session.get(Expedition, ctx["expedition_id"]).start_at + timedelta(hours=1),
            )
        ],
    )
    report = report_service.import_pack(_repack(manifest, secret="s3cret"))
    assert report.outcome == "committed"
    # A pack signed with a different key must be refused.
    evil = _repack(_load_pack(raw, secret="s3cret"), secret="other")
    with pytest.raises(ActionPackIntegrityError, match="signature"):
        ActionPackService(session, secret="s3cret").import_pack(evil)
    session.rollback()


# ------------------------------------------------------------- partial conflict


def test_partial_conflict_aborts_whole_pack_atomically(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    # One valid entry and one entry with a dangling user reference.
    good_gear = _gear_check_entry(ctx["participants"][0], ctx["catalog_id"], "gear-good", when)
    bad_incident = _incident_entry(999999, "incident-bad", when)
    _add_entries(
        manifest,
        gear_checks=[good_gear],
        incidents=[bad_incident],
    )
    report = ActionPackService(session).import_pack(_repack(manifest))
    assert report.outcome == "conflicts"
    conflict_types = {c.conflict_type for c in report.conflicts}
    assert "reference_error" in conflict_types
    # Atomicity: the otherwise-valid gear check was not written.
    assert (
        session.scalar(select(func.count()).select_from(ActivityGearCheck)) == 0
    )
    ledger = session.scalar(select(ActionPackImport))
    assert ledger.outcome == "conflicts"


def test_baseline_revision_conflict_blocks_import(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    # Headquarters bumps the activity revision after export.
    expedition.version += 1
    session.flush()
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        gear_checks=[
            _gear_check_entry(ctx["participants"][0], ctx["catalog_id"], "gear-0001", when)
        ],
    )
    report = ActionPackService(session).import_pack(_repack(manifest))
    assert report.outcome == "conflicts"
    assert any(
        c.conflict_type == "baseline_revision_conflict" and c.entity_type == "expedition"
        for c in report.conflicts
    )
    assert session.scalar(select(func.count()).select_from(ActivityGearCheck)) == 0


def test_already_recorded_slot_is_reported_not_overwritten(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-0001", when)],
    )
    pack = _repack(manifest)
    first = ActionPackService(session).import_pack(pack, actor_id=ctx["organizer"])
    assert first.outcome == "committed"

    # A second, independently-built pack targets the same slot.
    raw2, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest2 = _load_pack(raw2)
    _add_entries(
        manifest2,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-zzzz", when + timedelta(hours=1))],
    )
    report = ActionPackService(session).import_pack(_repack(manifest2))
    assert report.outcome == "conflicts"
    assert any(c.conflict_type == "already_recorded" for c in report.conflicts)
    slot = session.get(ItineraryCheckIn, ctx["slot_id"])
    # Original check-in time is preserved; the later entry never overwrote it.
    assert slot.checked_in_at == when


def test_time_order_violation_is_a_conflict(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    # check-in after the expedition end window
    late = expedition.end_at + timedelta(days=2)
    _add_entries(
        manifest,
        check_ins=[_check_in_entry(ctx["slot_id"], "checkin-late", late)],
    )
    report = ActionPackService(session).import_pack(_repack(manifest))
    assert report.outcome == "conflicts"
    assert any(c.conflict_type == "time_order_error" for c in report.conflicts)


def test_incremental_pack_skips_entries_alseen_then_commits_rest(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        incidents=[_incident_entry(ctx["participants"][0], "incident-shared", when)],
    )
    first_pack = _repack(manifest)
    first = ActionPackService(session).import_pack(first_pack)
    assert first.outcome == "committed"

    # A later pack includes the shared uid (duplicate) plus a brand-new entry.
    manifest["pack_id"] = "pack-incremental-0002"
    _add_entries(
        manifest,
        incidents=[
            _incident_entry(ctx["participants"][0], "incident-shared", when),
            _incident_entry(
                ctx["participants"][0],
                "incident-new",
                when + timedelta(hours=1),
                incident_type="weather",
                description="Sudden storm",
            ),
        ],
    )
    second = ActionPackService(session).import_pack(_repack(manifest))
    assert second.outcome == "committed"
    assert {d["entry_uid"] for d in second.duplicates} == {"incident-shared"}
    assert len(second.applied) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(EmergencyIncident)
            .where(EmergencyIncident.expedition_id == ctx["expedition_id"])
        )
        == 2
    )


# -------------------------------------------------------------------- restart


def test_import_survives_process_restart_with_fresh_database_object(
    session, settings
) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        gear_checks=[
            _gear_check_entry(ctx["participants"][0], ctx["catalog_id"], "gear-0001", when)
        ],
    )
    pack = _repack(manifest)
    ActionPackService(session).import_pack(pack)
    session.commit()

    # Simulate a new process opening the same SQLite file.
    from trailforge.database.session import Database

    reopened = Database(settings)
    with reopened.session() as new_session:
        report = ActionPackService(new_session).import_pack(pack)
        assert report.replayed is True
        assert (
            new_session.scalar(select(func.count()).select_from(ActivityGearCheck)) == 1
        )
    reopened.engine.dispose()


# --------------------------------------------------------------- large volume


def test_large_batch_roundtrip(session) -> None:
    ctx = _setup_activity(session, members=2)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    base = expedition.start_at + timedelta(minutes=10)
    incidents = [
        _incident_entry(
            ctx["participants"][i % 2],
            f"inc-bulk-{i:05d}",
            base + timedelta(minutes=i),
            description=f"Bulk incident number {i}",
        )
        for i in range(300)
    ]
    _add_entries(manifest, incidents=incidents)
    pack = _repack(manifest)
    report = ActionPackService(session).import_pack(pack)
    assert report.outcome == "committed"
    assert len(report.applied) == 300
    assert (
        session.scalar(
            select(func.count())
            .select_from(EmergencyIncident)
            .where(EmergencyIncident.expedition_id == ctx["expedition_id"])
        )
        == 300
    )
    # Idempotent replay of the large pack.
    replay = ActionPackService(session).import_pack(pack)
    assert replay.replayed is True
    assert len(replay.applied) == 300


# ------------------------------------------------------- incident timeline


def test_incident_timeline_update_resolves_offline_incident(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        incidents=[_incident_entry(ctx["participants"][0], "incident-parent", when)],
        incident_updates=[
            {
                "entry_type": "incident_update",
                "uid": "incident-update-1",
                "incident_ref": "incident-parent",
                "status": "resolved",
                "resolution": "Evacuated safely",
                "resolved_at": (when + timedelta(hours=1)).isoformat(),
                "recorded_at": (when + timedelta(hours=1, minutes=2)).isoformat(),
            }
        ],
    )
    report = ActionPackService(session).import_pack(_repack(manifest))
    assert report.outcome == "committed"
    incident = session.scalar(
        select(EmergencyIncident).where(
            EmergencyIncident.expedition_id == ctx["expedition_id"]
        )
    )
    assert incident.status == "resolved"
    assert incident.resolution == "Evacuated safely"
    assert incident.version == 2


def test_incident_update_with_unresolvable_reference_is_conflict(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    when = expedition.start_at + timedelta(hours=2)
    _add_entries(
        manifest,
        incident_updates=[
            {
                "entry_type": "incident_update",
                "uid": "incident-update-orphan",
                "incident_ref": "missing-parent",
                "status": "resolved",
                "resolution": "Nothing",
                "resolved_at": when.isoformat(),
                "recorded_at": when.isoformat(),
            }
        ],
    )
    report = ActionPackService(session).import_pack(_repack(manifest))
    assert report.outcome == "conflicts"
    assert any(c.conflict_type == "reference_error" for c in report.conflicts)


# ----------------------------------------------------------------------- HTTP


def test_export_import_roundtrip_over_http(client) -> None:
    # Seed via the service layer on the app's database.
    db = client.app.state.database
    with db.session() as session:
        ctx = _setup_activity(session)
        expedition = session.get(Expedition, ctx["expedition_id"])
        when = expedition.start_at + timedelta(hours=2)
    export_resp = client.post(
        f"/api/v1/action-packs/expeditions/{ctx['expedition_id']}/export",
        params={"actor_id": ctx["organizer"]},
    )
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"].startswith("application/json")
    envelope = export_resp.json()
    manifest = envelope["manifest"]
    _add_entries(
        manifest,
        gear_checks=[
            _gear_check_entry(
                ctx["participants"][0], ctx["catalog_id"], "gear-http-1", when
            )
        ],
    )
    import_resp = client.post(
        "/api/v1/action-packs/import",
        content=_repack(manifest),
        headers={"Content-Type": "application/json"},
    )
    assert import_resp.status_code == 200
    body = import_resp.json()
    assert body["outcome"] == "committed"
    assert len(body["applied"]) == 1
    # Replay the identical bytes: still 200 but marked replayed, no new rows.
    replay = client.post(
        "/api/v1/action-packs/import",
        content=_repack(manifest),
        headers={"Content-Type": "application/json"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_tampered_pack_is_rejected_over_http(client) -> None:
    db = client.app.state.database
    with db.session() as session:
        ctx = _setup_activity(session)
    export_resp = client.post(
        f"/api/v1/action-packs/expeditions/{ctx['expedition_id']}/export",
        params={"actor_id": ctx["organizer"]},
    )
    envelope = export_resp.json()
    envelope["manifest"]["pack_id"] = "tampered-pack-id"
    resp = client.post(
        "/api/v1/action-packs/import",
        json=envelope,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "action_pack_integrity"


def test_conflicted_entry_uid_can_be_fixed_and_reimported(session) -> None:
    ctx = _setup_activity(session)
    expedition = session.get(Expedition, ctx["expedition_id"])
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    when = expedition.start_at + timedelta(hours=2)
    # First attempt: a valid incident sharing the pack with a bad reference.
    manifest = _load_pack(raw)
    _add_entries(
        manifest,
        incidents=[
            _incident_entry(ctx["participants"][0], "incident-fixme", when),
            _incident_entry(999999, "incident-other-bad", when),
        ],
    )
    first = ActionPackService(session).import_pack(_repack(manifest))
    assert first.outcome == "conflicts"
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == 0

    # Corrected pack keeps the same incident uid but drops the bad reference.
    manifest = _load_pack(raw)
    _add_entries(
        manifest,
        incidents=[_incident_entry(ctx["participants"][0], "incident-fixme", when)],
    )
    second = ActionPackService(session).import_pack(_repack(manifest))
    assert second.outcome == "committed"
    assert len(second.applied) == 1
    assert session.scalar(select(func.count()).select_from(EmergencyIncident)) == 1


# ------------------------------------------------------------------ validation


def test_invalid_entry_payload_is_rejected(session) -> None:
    ctx = _setup_activity(session)
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    _add_entries(
        manifest,
        incidents=[{"entry_type": "incident", "uid": "bad-one"}],  # missing fields
    )
    with pytest.raises(ActionPackValidationError):
        ActionPackService(session).import_pack(_repack(manifest))
    session.rollback()


def test_wrong_expedition_is_rejected(session) -> None:
    ctx = _setup_activity(session)
    raw, _ = ActionPackService(session).export_pack(
        ctx["expedition_id"], actor_id=ctx["organizer"]
    )
    manifest = _load_pack(raw)
    manifest["expedition_id"] = 424242
    with pytest.raises(ActionPackValidationError, match="does not exist"):
        ActionPackService(session).import_pack(_repack(manifest))
    session.rollback()
