"""Rollback-only PostgreSQL investigation from a frozen human label to saved alignment."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sio_api.case_helpers import CaseCreate, CasePatch, EvidenceAttach, case_metrics
from sio_api.cases import Casework
from sio_api.evaluation_metrics import AnnotationDraft, EvaluationRequest
from sio_api.evaluations import EvaluationLab
from sio_api.evidence_comparison import ComparisonSave, EvidenceComparison
from sio_api.workbench_store import WorkbenchStore
from test_workbench import database as database

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope

pytestmark = pytest.mark.infra


async def test_frozen_miss_case_attachment_and_comparison_survive_postgres_reload(database):
    queries, (tenant, other_tenant) = database
    store = WorkbenchStore(queries)
    principal = Principal(
        subject="integration-investigator",
        tenant_id=tenant,
        roles=frozenset({"operator"}),
        clearance=3,
    )
    first_video, second_video = "vid_" + uuid4().hex, "vid_" + uuid4().hex
    first_analysis, second_analysis = "ana_" + uuid4().hex, "ana_" + uuid4().hex
    event_id = "ve_" + uuid4().hex
    for number, (video_id, analysis_id) in enumerate(
        ((first_video, first_analysis), (second_video, second_analysis)), start=1
    ):
        zone = {"zone_id": f"camera-{number}-gate", "name": f"Authored gate {number}"}
        rules = [{"rule_id": "entry", "zone_id": zone["zone_id"], "event_type": "entry"}]
        await store.put(
            tenant,
            "video",
            video_id,
            {
                "video_id": video_id,
                "title": f"Authored investigation clip {number}",
                "status": "ready",
                "duration_s": 8,
                "source": "recorded_file",
                "privacy": "full_frame_pixelation",
                "analysis_id": analysis_id,
                "zones": [zone],
                "rules": rules,
            },
            expected_revision=0,
        )
        await store.put(
            tenant,
            "analysis",
            analysis_id,
            {
                "analysis_id": analysis_id,
                "video_id": video_id,
                "status": "completed",
                "zones": [zone],
                "rules": rules,
                "model": {"mode": "motion", "name": "Authored integration fixture"},
                "events": []
                if number == 1
                else [
                    {
                        "event_id": event_id,
                        "video_id": video_id,
                        "analysis_id": analysis_id,
                        "zone_id": zone["zone_id"],
                        "event_type": "entry",
                        "rule_id": "entry",
                        "title": "Authored second-camera entry",
                        "at_s": 4.25,
                        "confidence": None,
                    }
                ],
            },
            expected_revision=0,
        )

    with tenant_scope(tenant):
        lab = EvaluationLab(store)
        label = {
            "annotation_id": "reviewed-entry",
            "zone_id": "camera-1-gate",
            "event_type": "entry",
            "start_s": 3,
            "end_s": 4,
            "note": "Authored fixture observation; no real-site accuracy claim",
        }
        draft = AnnotationDraft(
            revision=0,
            name="Authored reviewed interval",
            partition="development",
            footage_origin="authored",
            scopes=[{"zone_id": label["zone_id"], "event_type": "entry"}],
            coverage=[{"start_s": 0, "end_s": 8}],
            annotations=[label],
        )
        saved_draft = await lab.save(first_video, draft, principal)
        frozen = await lab.freeze(first_video, saved_draft["revision"], principal)
        report = await lab.report(
            EvaluationRequest(
                title="Authored missed-entry comparison",
                inputs=[
                    {
                        "annotation_set_id": frozen["annotation_set_id"],
                        "analysis_ids": [first_analysis],
                    }
                ],
            ),
            principal,
        )
        clip = report["candidates"][0]["clips"][0]
        assert clip["misses"] == [label]
        assert clip["metrics"]["false_negative"] == 1 and not clip["matches"]

        # A newer editable draft must not replace the frozen set used by the case.
        await lab.save(
            first_video,
            draft.model_copy(update={"revision": saved_draft["revision"], "note": "Later draft"}),
            principal,
        )
        cases = Casework(store, queries)
        case = await cases.create(
            CaseCreate(
                video_id=first_video,
                analysis_id=first_analysis,
                annotation_set_id=frozen["annotation_set_id"],
                annotation_id=label["annotation_id"],
                evaluation_report_id=report["report_id"],
                title="Authored reviewer-originated investigation",
            ),
            principal,
        )
        assert case["event_id"] is None and case["alert_id"] is None
        assert case["evidence"]["source"] == "reviewer_annotation"
        assert "event" not in case["evidence"] and "confidence" not in case["evidence"]
        assert "confidence" not in case["evidence"]["annotation"]
        assert case["evidence"]["annotation"] == label
        assert case["evidence"]["annotation_set"] == frozen
        assert case["evidence"]["annotation_set"]["frozen_by"] == principal.subject
        assert case["evidence"]["evaluation_report"]["miss"] == label
        original_evidence = deepcopy(case["evidence"])

        attached = await cases.attach(
            case["case_id"],
            EvidenceAttach(
                expected_revision=case["revision"],
                video_id=second_video,
                analysis_id=second_analysis,
                event_id=event_id,
                note="Second authored viewpoint",
            ),
            principal,
        )
        assert attached["evidence_count"] == attached["recorded_evidence_count"] == 2
        assert attached["evidence"] == original_evidence
        await cases.patch(
            case["case_id"],
            CasePatch(expected_revision=attached["revision"], verdict="confirmed"),
            principal,
        )
        metrics = case_metrics(await cases.visible_cases(principal), scan_limit=5000)
        assert metrics["reviewed_count"] == metrics["reviewer_annotation_reviewed_count"] == 1
        assert metrics["detector_reviewed_count"] == 0 and metrics["reviewed_precision"] is None
        assert metrics["detector_counts"]["total"] == 0

        before = await store.get(tenant, "case", case["case_id"])
        history_before = await store.history(tenant, "case", case["case_id"])
        right_id = attached["evidence_attachments"][0]["attachment_id"]
        comparison = await EvidenceComparison(store, queries).save(
            case["case_id"],
            ComparisonSave(
                revision=0,
                case_revision=before["revision"],
                left_attachment_id="original",
                right_attachment_id=right_id,
                offset_s=1.25,
                note="Reviewer-declared offset; recording clocks are unknown",
            ),
            principal,
        )
        # Reconstruct readers: assertions now come from SQL, not service object state.
        reloaded_store = WorkbenchStore(queries)
        restored = await EvidenceComparison(reloaded_store, queries).get(case["case_id"], principal)
        assert restored == comparison
        assert restored["revision"] == 1 and restored["offset_s"] == 1.25
        assert restored["updated_by"] == principal.subject
        assert restored["saved_case_revision"] == before["revision"]
        assert restored["time_basis"] == "reviewer_declared_offset"
        assert {item["analysis_id"] for item in restored["evidence_items"]} == {
            first_analysis,
            second_analysis,
        }
        raw_comparison = await reloaded_store.get(tenant, "case_comparison", case["case_id"])
        assert raw_comparison["source_refs"][0]["annotation_set_id"] == frozen["annotation_set_id"]
        assert raw_comparison["source_refs"][1]["event_id"] == event_id
        assert await reloaded_store.get(tenant, "case", case["case_id"]) == before
        assert await reloaded_store.history(tenant, "case", case["case_id"]) == history_before
        assert (
            await reloaded_store.get(tenant, "evaluation_annotations", frozen["annotation_set_id"])
        ) == frozen
        assert (
            await reloaded_store.get(tenant, "evaluation_report", report["report_id"])
        ) == report
        restored_case = await Casework(reloaded_store, queries).detail(case["case_id"], principal)
        assert restored_case["evidence"] == original_evidence

    assert await store.list(other_tenant, "case") == []
    assert await store.list(other_tenant, "case_comparison") == []
    assert await store.list(other_tenant, "evaluation_annotations") == []
