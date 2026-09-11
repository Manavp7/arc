"""Additional case sources are authored, immutable, bounded and jointly permission scoped."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api.case_helpers import CaseCreate, EvidenceAttach, printable_report
from sio_api.cases import Casework, install_case_routes
from sio_api.video_jobs import video_mutation_lock
from test_cases import FakePool, MemoryStore

from sio_core.authn import DevJwtAuth, Principal
from sio_core.config import Settings
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope

TENANT = "tenant-a"
ADMIN = Principal(subject="alice", tenant_id=TENANT, roles=frozenset({"admin"}), clearance=3)


async def recording(store, number, zone="gate", tenant=TENANT):
    video_id, analysis_id, event_id = f"vid_{number}", f"ana_{number}", f"event_{number}"
    await store.put(
        tenant,
        "video",
        video_id,
        {
            "video_id": video_id,
            "title": f"Recording {number}",
            "duration_s": 12,
            "privacy": "full_frame_pixelation",
            "source": "recorded_file",
            "analysis_id": "later-run",
        },
    )
    await store.put(
        tenant,
        "analysis",
        analysis_id,
        {
            "video_id": video_id,
            "analysis_id": analysis_id,
            "status": "completed",
            "model": {"name": "retained-original-model"},
            "rules": [{"rule_id": "original-rule"}],
            "zones": [{"zone_id": zone}],
            "events": [
                {
                    "event_id": event_id,
                    "video_id": video_id,
                    "analysis_id": analysis_id,
                    "zone_id": zone,
                    "at_s": 3.5,
                    "title": f"Recorded event {number}",
                    "frame_url": f"/api/review/videos/{video_id}/frames/{analysis_id}/7",
                }
            ],
        },
    )
    return {"video_id": video_id, "analysis_id": analysis_id, "event_id": event_id}


@pytest.fixture
async def work():
    store, pool = MemoryStore(), FakePool()
    first = await recording(store, 1)
    second = await recording(store, 2)
    pool.alerts[TENANT, "alert-a"] = {
        "alert_id": "alert-a",
        "title": "Platform gate alert",
        "zone_id": "gate",
        "ts": "2026-09-11T09:00:00Z",
        "event_ids": ["platform-event"],
        "decision_ids": ["decision-a"],
    }
    pool.events[TENANT, "platform-event"] = {
        "event_id": "platform-event",
        "type": "entry",
        "zone_id": "gate",
        "ts": "2026-09-11T08:59:50Z",
        "rule_id": "live-original-rule",
    }
    pool.decisions[TENANT, "decision-a"] = {
        "decision_id": "decision-a",
        "trigger_event": "platform-event",
        "approval": "pending",
    }
    yield Casework(store, pool), first, second


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": {}},
        {"attached_by": "forged"},
        {"attachment_id": "forged"},
        {"video_id": None},
        {"alert_id": "also-alert"},
        {"note": "a" * 4001},
        {"expected_revision": 0},
    ],
)
def test_attachment_input_only_accepts_one_source_reference_and_authored_note(change):
    with pytest.raises(ValidationError):
        EvidenceAttach.model_validate(
            {"expected_revision": 1, "video_id": "v", "analysis_id": "a", "event_id": "e", **change}
        )


@pytest.mark.asyncio
async def test_attaching_retained_recording_preserves_original_and_freezes_new_snapshot(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        result = await casework.attach(
            original["case_id"],
            EvidenceAttach(**second, expected_revision=1, note="Same incident from another view"),
            ADMIN,
        )
        assert result["revision"] == 2 and result["evidence_count"] == 2
        assert result["recorded_evidence_count"] == 2
        for field in (
            "video_id",
            "analysis_id",
            "event_id",
            "evidence",
            "zone_id",
            "evidence_zone_ids",
        ):
            assert result[field] == original[field]
        attachment = result["evidence_attachments"][0]
        assert attachment["attached_by"] == "alice"
        assert attachment["note"] == "Same incident from another view"
        assert attachment["analysis_id"] == second["analysis_id"]
        assert attachment["evidence"]["model"]["name"] == "retained-original-model"
        assert attachment["evidence"]["event"]["frame_url"].endswith("/ana_2/7")
        casework.store.records[TENANT, "analysis", "ana_2"]["model"]["name"] = "mutated-later"
        detail = await casework.detail(original["case_id"], ADMIN)
        assert detail["evidence_attachments"][0] == attachment
        assert detail["timeline"][-1]["kind"] == "evidence_attached"
        persisted = await casework.store.get(TENANT, "case", original["case_id"])
        assert "evidence_items" not in persisted and "recorded_evidence_count" not in persisted


@pytest.mark.asyncio
async def test_duplicate_original_or_attachment_is_idempotent_even_with_old_revision(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        duplicated_origin = await casework.attach(
            original["case_id"], EvidenceAttach(**first, expected_revision=1), ADMIN
        )
        assert duplicated_origin["revision"] == 1 and not duplicated_origin["evidence_attachments"]
        attached = await casework.attach(
            original["case_id"],
            EvidenceAttach(**second, expected_revision=1, note="Original reason"),
            ADMIN,
        )
        duplicate = await casework.attach(
            original["case_id"],
            EvidenceAttach(**second, expected_revision=1, note="Attempted replacement reason"),
            ADMIN,
        )
        assert duplicate == attached


@pytest.mark.asyncio
async def test_stale_case_save_and_concurrent_edit_do_not_append_evidence(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=2), ADMIN
            )
        assert error.value.status_code == 409
        casework.store.fail_next = True
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
            )
        assert error.value.status_code == 409
        stored = await casework.store.get(TENANT, "case", original["case_id"])
        assert (
            stored["summary"] == "Concurrent colleague edit" and not stored["evidence_attachments"]
        )


@pytest.mark.asyncio
async def test_all_attachment_zones_gate_case_detail_lists_and_future_writes(work):
    casework, first, _ = work
    restricted = Principal(
        subject="gate-operator",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"gate"}),
        clearance=3,
    )
    other = await recording(casework.store, 3, zone="private-yard")
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**other, expected_revision=1), restricted
            )
        assert error.value.status_code == 403
        await casework.attach(
            original["case_id"], EvidenceAttach(**other, expected_revision=1), ADMIN
        )
        with pytest.raises(HTTPException) as error:
            await casework.detail(original["case_id"], restricted)
        assert error.value.status_code == 403
        assert await casework.visible_cases(restricted) == []
        assert len(await casework.visible_cases(ADMIN)) == 1
    with tenant_scope("tenant-b"):
        outsider = Principal(
            subject="outsider", tenant_id="tenant-b", roles=frozenset({"admin"}), clearance=3
        )
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**other, expected_revision=2), outsider
            )
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_foreign_missing_event_and_incomplete_run_cannot_supply_attachment(work):
    casework, first, second = work
    foreign = await recording(casework.store, 8, tenant="tenant-b")
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        for source in [
            foreign,
            {**second, "analysis_id": "missing-run"},
            {**second, "event_id": "missing-event"},
        ]:
            with pytest.raises(HTTPException) as error:
                await casework.attach(
                    original["case_id"], EvidenceAttach(**source, expected_revision=1), ADMIN
                )
            assert error.value.status_code == 404
        casework.store.records[TENANT, "analysis", "ana_2"]["status"] = "running"
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
            )
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_video_availability_is_rechecked_after_waiting_for_archive_lock(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        lock = video_mutation_lock(casework.store, TENANT, second["video_id"])
        await lock.acquire()
        operation = asyncio.create_task(
            casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
            )
        )
        await asyncio.sleep(0)
        assert not operation.done()
        casework.store.records[TENANT, "video", second["video_id"]]["archived_at"] = (
            "2026-09-11T10:00:00Z"
        )
        lock.release()
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 409
        assert not (await casework.load(original["case_id"], ADMIN))["evidence_attachments"]


@pytest.mark.asyncio
async def test_alert_case_can_gain_recording_and_recording_case_can_gain_platform_provenance(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        alert_case = await casework.create(CaseCreate(alert_id="alert-a"), ADMIN)
        mixed = await casework.attach(
            alert_case["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
        )
        assert mixed["video_id"] is None and mixed["recorded_evidence_count"] == 1
        assert mixed["evidence_items"][0]["time_basis"] == "platform_source_time"
        assert mixed["evidence_items"][1]["time_basis"] == "attachment_time_recording_clock_unknown"
        assert mixed["evidence_items"][1]["at_s"] == 3.5
        video_case = await casework.create(CaseCreate(**first), ADMIN)
        appended = await casework.attach(
            video_case["case_id"],
            EvidenceAttach(
                alert_id="alert-a", expected_revision=1, note="<script>unsafe note</script>"
            ),
            ADMIN,
        )
        alert = appended["evidence_attachments"][0]
        assert alert["evidence"]["events"][0]["rule_id"] == "live-original-rule"
        assert appended["decisions"][0]["decision_id"] == "decision-a"
        report = printable_report(
            {
                "case": appended,
                "missions": [],
                "decisions": appended["decisions"],
                "exported_by": "alice",
                "exported_at": "now",
            }
        )
        assert "<script>unsafe" not in report and "&lt;script&gt;unsafe" in report
        assert "ana_1" in report and "live-original-rule" in report and "evidence_" in report


@pytest.mark.asyncio
async def test_total_source_limit_includes_original_and_does_not_block_existing_duplicates(work):
    casework, first, second = work
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        key = TENANT, "case", original["case_id"]
        casework.store.records[key]["evidence_attachments"] = [
            {
                "attachment_id": str(index),
                "alert_id": f"a{index}",
                "evidence": {"source": "platform_alert"},
            }
            for index in range(19)
        ]
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
            )
        assert error.value.status_code == 409
        duplicate = await casework.attach(
            original["case_id"], EvidenceAttach(**first, expected_revision=1), ADMIN
        )
        assert duplicate["evidence_count"] == 20


@pytest.mark.asyncio
async def test_route_permissions_summary_counts_and_exports_cover_attachments(work, tmp_path):
    casework, _first, second = work
    settings = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    app, issuer = FastAPI(), DevJwtAuth(settings)
    install_case_routes(app, settings, casework.store, casework.pool)
    install_governance(app, service="api", settings=settings, authenticator=issuer)
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(alert_id="alert-a"), ADMIN)

    def auth(role="operator", tenant=TENANT):
        return {
            "Authorization": "Bearer "
            + issuer.issue(tenant_id=tenant, roles=(role,), subject="route-author", clearance=3)
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/cases/{original['case_id']}/evidence"
        body = {**second, "expected_revision": 1, "note": "Authored route attachment"}
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.post(path, headers=auth("viewer"), json=body)).status_code == 403
        result = await client.post(path, headers=auth(), json=body)
        assert result.status_code == 200, result.text
        assert result.json()["evidence_attachments"][0]["attached_by"] == "route-author"
        summary = (await client.get("/api/cases", headers=auth())).json()["cases"][0]
        assert summary["video_id"] is None and summary["recorded_evidence_count"] == 1
        assert "evidence_attachments" not in summary and "evidence_items" not in summary
        exported = await client.get(f"/api/cases/{original['case_id']}/export", headers=auth())
        assert len(exported.json()["case"]["evidence_items"]) == 2
        assert (
            await client.get(
                f"/api/cases/{original['case_id']}/export", headers=auth(tenant="tenant-b")
            )
        ).status_code == 404


@pytest.mark.parametrize(
    "metadata",
    [
        {"zones": [{"zone_id": "private-yard"}]},
        {"rules": [{"rule_id": "other-zone-rule", "zone_id": "private-yard"}]},
    ],
)
@pytest.mark.asyncio
async def test_complete_retained_analysis_layout_is_scoped_beyond_selected_event(work, metadata):
    casework, first, second = work
    restricted = Principal(
        subject="gate-operator",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"gate"}),
        clearance=3,
    )
    casework.store.records[TENANT, "analysis", second["analysis_id"]].update(metadata)
    with tenant_scope(TENANT):
        original = await casework.create(CaseCreate(**first), ADMIN)
        with pytest.raises(HTTPException) as error:
            await casework.create(CaseCreate(**second), restricted)
        assert error.value.status_code == 403
        with pytest.raises(HTTPException) as error:
            await casework.attach(
                original["case_id"], EvidenceAttach(**second, expected_revision=1), restricted
            )
        assert error.value.status_code == 403
        attached = await casework.attach(
            original["case_id"], EvidenceAttach(**second, expected_revision=1), ADMIN
        )
        assert attached["evidence_attachments"][0]["evidence_zone_ids"] == ["gate", "private-yard"]
        # Legacy snapshots may contain only the selected event in their cached zone list.
        casework.store.records[TENANT, "case", original["case_id"]]["evidence_attachments"][0][
            "evidence_zone_ids"
        ] = ["gate"]
        assert await casework.visible_cases(restricted) == []
        with pytest.raises(HTTPException) as error:
            await casework.load(original["case_id"], restricted)
        assert error.value.status_code == 403
