"""Evaluation uses human-certified coverage, frozen truth and same-tenant provenance."""

from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sio_api.evaluation_metrics import AnnotationDraft, EvaluationRequest, evaluate, merge_intervals
from sio_api.evaluations import install_evaluation_routes
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance


class Store:
    def __init__(self):
        self.rows = {}

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.rows.get((tenant, kind, record_id)))

    async def list(self, tenant, kind, limit=500):
        return [
            deepcopy(row)
            for (owner, category, _), row in self.rows.items()
            if owner == tenant and category == kind
        ][:limit]

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        key = tenant, kind, record_id
        old = self.rows.get(key, {})
        revision = old.get("revision", 0)
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchConflict("Changed")
        self.rows[key] = {
            **deepcopy(data),
            "record_id": record_id,
            "revision": revision + 1,
            "created_at": "2026-09-11T10:00:00Z",
        }
        return deepcopy(self.rows[key])


def label(start=3, end=4, annotation_id="truth-a", **kwargs):
    return {
        "annotation_id": annotation_id,
        "zone_id": "gate",
        "event_type": "entry",
        "start_s": start,
        "end_s": end,
        "note": "Human-observed incident",
        **kwargs,
    }


def event(at=3.5, event_id="event-a", **kwargs):
    return {"event_id": event_id, "at_s": at, "zone_id": "gate", "event_type": "entry", **kwargs}


def draft(**kwargs):
    return {
        "revision": 0,
        "name": "Reviewed gate",
        "partition": "development",
        "footage_origin": "authored",
        "coverage": [{"start_s": 0, "end_s": 10}],
        "scopes": [{"zone_id": "gate", "event_type": "entry"}],
        "annotations": [label()],
        "note": "Test fixture; no accuracy claim",
        **kwargs,
    }


def snapshot(**kwargs):
    return {
        **draft(),
        "annotation_set_id": "ann_" + "a" * 32,
        "annotation_hash": "hash",
        "duration_s": 12,
        **kwargs,
    }


def analysis(events=None, **kwargs):
    return {
        "analysis_id": "ana_a",
        "video_id": "vid_a",
        "status": "completed",
        "events": events if events is not None else [event()],
        "model": {"name": "Motion baseline", "mode": "motion"},
        "rules": [{"rule_id": "entry-a", "event_type": "entry"}],
        "zones": [{"zone_id": "gate", "name": "Gate", "points": [[0, 0], [1, 0], [1, 1]]}],
        **kwargs,
    }


def test_missing_coverage_and_non_finite_intervals_are_rejected():
    for changes in [
        {"coverage": []},
        {"scopes": []},
        {"coverage": [{"start_s": 0, "end_s": float("nan")}]},
        {"annotations": [label(9, 11)]},
        {"annotations": [label(zone_id="other")]},
    ]:
        with pytest.raises(ValidationError):
            AnnotationDraft.model_validate(draft(**changes))


def test_overlapping_coverage_is_counted_once_and_gaps_remain_unknown():
    truth = snapshot(
        coverage=[
            {"start_s": 0, "end_s": 4},
            {"start_s": 3, "end_s": 5},
            {"start_s": 8, "end_s": 10},
        ]
    )
    result = evaluate(truth, analysis([event(3.5), event(6, "unreviewed"), event(9, "false")]), 0)
    assert merge_intervals(truth["coverage"]) == [(0, 5), (8, 10)]
    assert result["metrics"]["reviewed_seconds"] == 7
    assert result["unreviewed_seconds"] == 5
    assert [row["event_id"] for row in result["excluded_events"]] == ["unreviewed"]
    assert result["metrics"]["false_positive"] == 1
    assert result["metrics"]["false_alarms_per_reviewed_hour"] == pytest.approx(3600 / 7)


def test_duplicate_alarms_match_one_incident_once():
    result = evaluate(snapshot(), analysis([event(3.5), event(3.5, "duplicate")]), 0)
    assert result["metrics"]["true_positive"] == 1
    assert result["metrics"]["false_positive"] == 1
    assert result["metrics"]["precision"] == 0.5
    assert result["metrics"]["recall"] == 1


def test_matching_maximizes_cardinality_instead_of_greedy_under_count():
    truth = snapshot(annotations=[label(1, 4, "wide"), label(3, 4, "narrow")])
    result = evaluate(truth, analysis([event(3, "shared"), event(1.5, "wide-only")]), 0)
    assert result["metrics"]["true_positive"] == 2
    shuffled = evaluate(
        {**truth, "annotations": list(reversed(truth["annotations"]))},
        analysis([event(1.5, "wide-only"), event(3, "shared")]),
        0,
    )
    assert result == shuffled


def test_tolerance_never_turns_an_unreviewed_event_into_a_judged_prediction():
    result = evaluate(snapshot(coverage=[{"start_s": 3, "end_s": 10}]), analysis([event(2.9)]), 0.5)
    assert result["metrics"]["false_negative"] == 1
    assert result["metrics"]["false_positive"] == 0
    assert result["metrics"]["precision"] is None
    assert result["metrics"]["recall"] == 0


def test_verified_absence_has_no_recall_denominator():
    result = evaluate(snapshot(annotations=[]), analysis([event(3)]), 0)
    assert result["metrics"]["recall"] is None
    assert result["metrics"]["precision"] == 0
    assert result["metrics"]["median_signed_delay_s"] is None
    assert result["metrics"]["matched_delay_samples"] == 0


def test_event_scopes_are_distinct_and_signed_delay_is_preserved():
    result = evaluate(
        snapshot(),
        analysis(
            [event(2.8), event(3, "dwell", event_type="dwell"), event(3, "other", zone_id="yard")]
        ),
        0.5,
    )
    assert result["metrics"]["true_positive"] == 1
    assert result["metrics"]["median_signed_delay_s"] == pytest.approx(-0.2)
    assert len(result["excluded_events"]) == 2


def test_candidate_counts_and_duplicate_versions_must_be_valid():
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(
            {"inputs": [{"annotation_set_id": "ann_" + "a" * 32, "analysis_ids": ["a", "a"]}]}
        )
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(
            {
                "inputs": [
                    {"annotation_set_id": "ann_" + "a" * 32, "analysis_ids": ["a"]},
                    {"annotation_set_id": "ann_" + "b" * 32, "analysis_ids": ["a", "b"]},
                ]
            }
        )


@pytest.fixture
async def client(tmp_path):
    store = Store()
    settings = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    issuer = DevJwtAuth(settings)
    app = FastAPI()
    install_evaluation_routes(app, store)
    install_governance(app, service="api", settings=settings, authenticator=issuer)

    @app.exception_handler(WorkbenchConflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    await store.put(
        "tenant-a",
        "video",
        "vid_a",
        {
            "video_id": "vid_a",
            "duration_s": 12,
            "title": "Authored clip",
            "zones": analysis()["zones"],
        },
    )
    await store.put("tenant-a", "analysis", "ana_a", analysis())
    await store.put("tenant-a", "analysis", "ana_b", analysis([event(9)], analysis_id="ana_b"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:

        def headers(tenant="tenant-a", role="operator", zones=()):
            return {
                "Authorization": "Bearer "
                + issuer.issue(
                    tenant_id=tenant, roles=(role,), subject="reviewer", clearance=3, zones=zones
                )
            }

        yield http, store, headers


async def freeze(http, headers, body=None, video_id="vid_a"):
    saved = await http.put(
        f"/api/review/evaluations/videos/{video_id}/annotations",
        headers=headers(),
        json=body or draft(),
    )
    assert saved.status_code == 200, saved.text
    frozen = await http.post(
        f"/api/review/evaluations/videos/{video_id}/freeze",
        headers=headers(),
        json={"revision": saved.json()["revision"]},
    )
    assert frozen.status_code == 200, frozen.text
    return frozen.json()


@pytest.mark.asyncio
async def test_reports_retain_original_annotation_analysis_and_model_after_edits(client):
    http, store, headers = client
    frozen = await freeze(http, headers)
    response = await http.post(
        "/api/review/evaluations/reports",
        headers=headers(),
        json={
            "inputs": [
                {
                    "annotation_set_id": frozen["annotation_set_id"],
                    "analysis_ids": ["ana_a", "ana_b"],
                }
            ],
            "tolerance_s": 0,
        },
    )
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["candidates"][0]["metrics"]["recall"] == 1
    assert report["candidates"][1]["metrics"]["recall"] == 0
    assert report["footage_origins"] == ["authored"]
    await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(revision=1, annotations=[]),
    )
    await store.put("tenant-a", "analysis", "ana_a", analysis([], model={"mode": "new"}))
    retained = (
        await http.get(f"/api/review/evaluations/reports/{report['report_id']}", headers=headers())
    ).json()
    assert retained == report
    assert retained["annotation_snapshots"][0]["annotations"] == [label()]
    assert retained["candidates"][0]["clips"][0]["provenance"]["model"]["mode"] == "motion"
    assert report["video_ids"] == ["vid_a"]
    assert report["analysis_ids"] == ["ana_a", "ana_b"]


@pytest.mark.asyncio
async def test_stale_draft_save_and_freeze_preserve_newer_annotation(client):
    http, _, headers = client
    await freeze(http, headers)
    stale = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(revision=0, annotations=[]),
    )
    assert stale.status_code == 409
    updated = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(revision=1, note="Newer review"),
    )
    assert updated.status_code == 200
    stale_freeze = await http.post(
        "/api/review/evaluations/videos/vid_a/freeze", headers=headers(), json={"revision": 1}
    )
    assert stale_freeze.status_code == 409
    versions = (await http.get("/api/review/evaluations/videos/vid_a", headers=headers())).json()
    assert versions["draft"]["note"] == "Newer review"
    assert len(versions["annotation_sets"]) == 1


@pytest.mark.asyncio
async def test_partition_is_fixed_and_reports_cannot_mix_holdout_and_development(client):
    http, store, headers = client
    one = await freeze(http, headers)
    changed = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(revision=1, partition="held_out"),
    )
    assert changed.status_code == 409
    await store.put(
        "tenant-a",
        "video",
        "vid_b",
        {"video_id": "vid_b", "duration_s": 12, "title": "Holdout", "zones": analysis()["zones"]},
    )
    await store.put(
        "tenant-a", "analysis", "ana_c", analysis(video_id="vid_b", analysis_id="ana_c")
    )
    two = await freeze(http, headers, draft(partition="held_out"), "vid_b")
    mixed = await http.post(
        "/api/review/evaluations/reports",
        headers=headers(),
        json={
            "inputs": [
                {"annotation_set_id": one["annotation_set_id"], "analysis_ids": ["ana_a"]},
                {"annotation_set_id": two["annotation_set_id"], "analysis_ids": ["ana_c"]},
            ]
        },
    )
    assert mixed.status_code == 422
    assert await store.list("tenant-a", "evaluation_report") == []


@pytest.mark.asyncio
async def test_tenant_zone_and_role_boundaries_cover_annotations_and_reports(client):
    http, _, headers = client
    frozen = await freeze(http, headers)
    for request_headers in [headers("tenant-b"), headers(zones=("yard",))]:
        blocked = await http.get("/api/review/evaluations/videos/vid_a", headers=request_headers)
        assert blocked.status_code in (403, 404)
        report = await http.post(
            "/api/review/evaluations/reports",
            headers=request_headers,
            json={
                "inputs": [
                    {"annotation_set_id": frozen["annotation_set_id"], "analysis_ids": ["ana_a"]}
                ]
            },
        )
        assert report.status_code in (403, 404)
    viewer = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(role="viewer"),
        json=draft(revision=1),
    )
    assert viewer.status_code == 403
    anonymous = await http.get("/api/review/evaluations")
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_version_candidates_expose_completed_status_and_exclude_ineligible_runs(client):
    http, store, headers = client
    for tenant, analysis_id, changes in [
        ("tenant-a", "ana_running", {"status": "running"}),
        ("tenant-a", "ana_failed", {"status": "failed"}),
        ("tenant-a", "ana_other_clip", {"video_id": "vid_other"}),
        ("tenant-a", "ana_hidden", {"zones": [{"zone_id": "private"}]}),
        ("tenant-b", "ana_foreign", {}),
    ]:
        await store.put(
            tenant, "analysis", analysis_id, analysis(analysis_id=analysis_id, **changes)
        )
    response = await http.get(
        "/api/review/evaluations/videos/vid_a", headers=headers(zones=("gate",))
    )
    assert response.status_code == 200
    candidates = response.json()["analyses"]
    assert {row["analysis_id"] for row in candidates} == {"ana_a", "ana_b"}
    assert all(row["status"] == "completed" and row["video_id"] == "vid_a" for row in candidates)
    assert all(row["model"] and row["rules"] and row["zones"] for row in candidates)


@pytest.mark.asyncio
async def test_analysis_must_be_completed_and_belong_to_same_clip(client):
    http, store, headers = client
    frozen = await freeze(http, headers)
    for version, code in [(analysis(video_id="other"), 404), (analysis(status="running"), 409)]:
        await store.put("tenant-a", "analysis", "ana_wrong", version)
        response = await http.post(
            "/api/review/evaluations/reports",
            headers=headers(),
            json={
                "inputs": [
                    {
                        "annotation_set_id": frozen["annotation_set_id"],
                        "analysis_ids": ["ana_wrong"],
                    }
                ]
            },
        )
        assert response.status_code == code


@pytest.mark.asyncio
async def test_archived_input_cannot_gain_new_evaluation_reference(client):
    http, store, headers = client
    frozen = await freeze(http, headers)
    video = await store.get("tenant-a", "video", "vid_a")
    await store.put("tenant-a", "video", "vid_a", {**video, "archived_at": "2026-09-11T12:00:00Z"})
    response = await http.post(
        "/api/review/evaluations/reports",
        headers=headers(),
        json={
            "inputs": [
                {"annotation_set_id": frozen["annotation_set_id"], "analysis_ids": ["ana_a"]}
            ]
        },
    )
    assert response.status_code == 409
    response = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(revision=1),
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_intervals_cannot_exceed_actual_clip_and_freeze_is_idempotent(client):
    http, _, headers = client
    response = await http.put(
        "/api/review/evaluations/videos/vid_a/annotations",
        headers=headers(),
        json=draft(coverage=[{"start_s": 0, "end_s": 13}]),
    )
    assert response.status_code == 422
    frozen = await freeze(http, headers)
    again = await http.post(
        "/api/review/evaluations/videos/vid_a/freeze", headers=headers(), json={"revision": 1}
    )
    assert again.json()["annotation_set_id"] == frozen["annotation_set_id"]
