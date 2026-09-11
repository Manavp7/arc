"""Rollback-only PostgreSQL flow across presets, attached evidence and notifications."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sio_api.case_helpers import CaseCreate, EvidenceAttach
from sio_api.cases import Casework
from sio_api.notifications import MarkRead, NotificationManager
from sio_api.rule_presets import PresetSave, PreviewRequest, RulePresets
from sio_api.workbench_store import WorkbenchStore
from test_workbench import database as database

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope

pytestmark = pytest.mark.infra


async def test_presets_case_attachment_and_personal_read_state_survive_new_managers(database):
    queries, (tenant, other_tenant) = database
    store = WorkbenchStore(queries)
    principal = Principal(
        subject="integration-reviewer", tenant_id=tenant, roles=frozenset({"operator"}), clearance=3
    )
    source_id, target_id = "vid_" + uuid4().hex, "vid_" + uuid4().hex
    points = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    source = await store.put(
        tenant,
        "video",
        source_id,
        {
            "video_id": source_id,
            "title": "Authored source",
            "status": "ready",
            "duration_s": 6,
            "source": "recorded_file",
            "privacy": "full_frame_pixelation",
            "zones": [{"zone_id": "source-zone", "name": "Source", "points": points}],
            "rules": [
                {
                    "rule_id": "entry",
                    "name": "Entry fixture",
                    "zone_id": "source-zone",
                    "event_type": "entry",
                    "target_class": "any",
                    "threshold_s": 5,
                    "cooldown_s": 10,
                    "enabled": True,
                }
            ],
        },
        expected_revision=0,
    )
    target = await store.put(
        tenant,
        "video",
        target_id,
        {
            "video_id": target_id,
            "title": "Authored target",
            "status": "ready",
            "duration_s": 6,
            "source": "recorded_file",
            "privacy": "full_frame_pixelation",
            "zones": [{"zone_id": "target-zone", "name": "Target", "points": points}],
            "rules": [],
        },
        expected_revision=0,
    )
    with tenant_scope(tenant):
        presets = RulePresets(store)
        preset = await presets.save(
            PresetSave(
                name="Rollback fixture preset",
                source_video_id=source_id,
                source_revision=source["revision"],
                slots=[
                    {"slot_id": "entry-area", "name": "Entry area", "source_zone_id": "source-zone"}
                ],
            ),
            principal,
        )
        preview = await presets.preview(
            PreviewRequest(
                preset_id=preset["preset_id"],
                version_id=preset["current_version_id"],
                targets=[
                    {
                        "video_id": target_id,
                        "revision": target["revision"],
                        "zone_map": {"entry-area": "target-zone"},
                    }
                ],
            ),
            principal,
        )
        result = await RulePresets(WorkbenchStore(queries)).apply(preview["preview_id"], principal)
        assert result["results"][0]["status"] == "applied"
        target = await store.get(tenant, "video", target_id)
        assert target["rules"][0]["zone_id"] == "target-zone"
        assert target["zones"][0]["points"] == points
        assert (await store.get(tenant, "video", source_id))["revision"] == source["revision"]

        async def analysis_for(video, zone):
            analysis_id, event_id = "ana_" + uuid4().hex, "ve_" + uuid4().hex
            event = {
                "event_id": event_id,
                "at_s": 3,
                "zone_id": zone,
                "rule_id": video["rules"][0]["rule_id"],
                "title": "Authored entry",
                "track_id": "region-1",
                "confidence": None,
            }
            await store.put(
                tenant,
                "analysis",
                analysis_id,
                {
                    "video_id": video["video_id"],
                    "analysis_id": analysis_id,
                    "status": "completed",
                    "events": [event],
                    "zones": video["zones"],
                    "rules": video["rules"],
                    "model": {"mode": "motion", "name": "Authored integration fixture"},
                },
                expected_revision=0,
            )
            return analysis_id, event_id

        source_analysis, source_event = await analysis_for(source, "source-zone")
        target_analysis, target_event = await analysis_for(target, "target-zone")
        now = datetime.now(UTC)
        casework = Casework(store, queries)
        case = await casework.create(
            CaseCreate(
                video_id=source_id,
                analysis_id=source_analysis,
                event_id=source_event,
                owner=principal.subject,
                due_at=now + timedelta(hours=1),
            ),
            principal,
        )
        attached = await casework.attach(
            case["case_id"],
            EvidenceAttach(
                expected_revision=case["revision"],
                video_id=target_id,
                analysis_id=target_analysis,
                event_id=target_event,
                note="Second authored recording",
            ),
            principal,
        )
        assert len(attached["evidence_attachments"]) == 1
        restored_case = await Casework(WorkbenchStore(queries), queries).detail(
            case["case_id"], principal
        )
        assert restored_case["evidence_count"] == 2
        assert restored_case["analysis_id"] == source_analysis
        assert restored_case["evidence_attachments"][0]["analysis_id"] == target_analysis
        assert len(await store.history(tenant, "case", case["case_id"])) == 2

        manager = NotificationManager(
            SimpleNamespace(tenant_id=tenant), store, queries, clock=lambda: now
        )
        notices = await manager.list_for(tenant, principal)
        assert {notice["kind"] for notice in notices["notifications"]} == {
            "case_assigned",
            "case_due_soon",
        }
        await manager.mark(
            tenant,
            principal,
            MarkRead(
                notifications=[
                    {"notification_id": notice["notification_id"], "revision": notice["revision"]}
                    for notice in notices["notifications"]
                ]
            ),
        )
        restarted = NotificationManager(
            SimpleNamespace(tenant_id=tenant), WorkbenchStore(queries), queries, clock=lambda: now
        )
        retained = await restarted.list_for(tenant, principal)
        assert retained["unread_count"] == 0 and len(retained["notifications"]) == 2
        assert all(notice["read_at"] for notice in retained["notifications"])
        assert await store.list(other_tenant, "notification") == []
        assert await store.list(other_tenant, "rule_preset") == []
