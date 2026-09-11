"""Reviewer cases pin real frozen labels without pretending a detector found an event."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api import evidence_packages as packages
from sio_api.case_evidence import case_evidence_items, source_identity
from sio_api.case_helpers import CaseCreate, EvidenceAttach, case_metrics, printable_report
from sio_api.cases import Casework, install_case_routes
from sio_api.evaluation_metrics import AnnotationDraft, EvaluationRequest
from sio_api.evaluations import EvaluationLab
from sio_api.video_jobs import video_mutation_lock
from test_cases import FakePool, MemoryStore
from test_evaluations import analysis, draft, label

from sio_core.authn import DevJwtAuth, Principal
from sio_core.config import Settings
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope

TENANT = "tenant-a"
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32
ADMIN = Principal(
    subject="reviewer-alice", tenant_id=TENANT, roles=frozenset({"admin"}), clearance=3
)


@pytest.fixture
async def reviewed():
    store, pool = MemoryStore(), FakePool()
    lab = EvaluationLab(store)
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "title": "Authored review fixture",
            "duration_s": 12,
            "privacy": "full_frame_pixelation",
            "source": "recorded_file",
            "zones": [{"zone_id": "gate"}],
        },
    )
    await store.put(
        TENANT, "analysis", ANALYSIS, analysis([], video_id=VIDEO, analysis_id=ANALYSIS)
    )
    with tenant_scope(TENANT):
        saved = await lab.save(
            VIDEO,
            AnnotationDraft(**draft(annotations=[label(note="<script>reviewer note</script>")])),
            ADMIN,
        )
        frozen = await lab.freeze(VIDEO, saved["revision"], ADMIN)
        report = await lab.report(
            EvaluationRequest(
                inputs=[
                    {"annotation_set_id": frozen["annotation_set_id"], "analysis_ids": [ANALYSIS]}
                ]
            ),
            ADMIN,
        )
    source = {
        "video_id": VIDEO,
        "analysis_id": ANALYSIS,
        "annotation_set_id": frozen["annotation_set_id"],
        "annotation_id": "truth-a",
    }
    return SimpleNamespace(
        store=store,
        pool=pool,
        lab=lab,
        frozen=frozen,
        report=report,
        source=source,
        cases=Casework(store, pool),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"annotation_set_id": None},
        {"annotation_id": None},
        {"video_id": None},
        {"analysis_id": None},
        {"event_id": "fabricated"},
        {"alert_id": "unrelated"},
        {"annotation": {"start_s": 0}},
        {"evidence": {}},
        {"confidence": 0.9},
    ],
)
def test_reviewer_contract_accepts_only_complete_exclusive_source_ids(change):
    body = {
        "video_id": VIDEO,
        "analysis_id": ANALYSIS,
        "annotation_set_id": "ann_frozen",
        "annotation_id": "truth-a",
        **change,
    }
    with pytest.raises(ValidationError):
        CaseCreate(**body)
    with pytest.raises(ValidationError):
        EvidenceAttach(expected_revision=1, **body)


@pytest.mark.asyncio
async def test_empty_detection_run_can_open_frozen_reviewer_case_with_immutable_provenance(
    reviewed,
):
    with tenant_scope(TENANT):
        case = await reviewed.cases.create(CaseCreate(**reviewed.source), ADMIN)
        assert case["event_id"] is None and case["alert_id"] is None
        assert case["at_s"] == 3 and case["verdict"] == "unreviewed"
        evidence = deepcopy(case["evidence"])
        assert evidence["source"] == "reviewer_annotation"
        assert evidence["annotation_set"] == reviewed.frozen
        assert evidence["annotation_set"]["frozen_by"] == "reviewer-alice"
        assert "event" not in evidence and "confidence" not in evidence["annotation"]
        assert "evaluation_report" not in evidence
        reviewed.store.records[
            TENANT, "evaluation_annotations", reviewed.frozen["annotation_set_id"]
        ]["annotations"][0]["note"] = "Later corruption"
        reviewed.store.records[TENANT, "analysis", ANALYSIS]["model"] = {"name": "different model"}
        detail = await reviewed.cases.detail(case["case_id"], ADMIN)
        assert detail["evidence"] == evidence
        assert detail["recorded_evidence_count"] == 1
        item = case_evidence_items(detail)[0]
        assert item["annotation_id"] == "truth-a"
        assert item["time_basis"] == "attachment_time_recording_clock_unknown"
        html = printable_report(
            {"case": detail, "exported_at": "now", "exported_by": ADMIN.subject}
        )
        assert "&lt;script&gt;reviewer note&lt;/script&gt;" in html
        assert "<script>" not in html


@pytest.mark.asyncio
async def test_report_miss_is_pinned_but_not_added_to_prior_identical_source(reviewed):
    with tenant_scope(TENANT):
        case = await reviewed.cases.create(CaseCreate(**reviewed.source), ADMIN)
        linked = await reviewed.cases.create(
            CaseCreate(**reviewed.source, evaluation_report_id=reviewed.report["report_id"]), ADMIN
        )
        assert linked == case and linked["evaluation_report_id"] is None
        assert "evaluation_report" not in linked["evidence"]
        assert source_identity(reviewed.source) == source_identity(
            {**reviewed.source, "evaluation_report_id": "other"}
        )
        del reviewed.store.records[TENANT, "case", case["case_id"]]  # Isolated fixture reset.
        linked = await reviewed.cases.create(
            CaseCreate(**reviewed.source, evaluation_report_id=reviewed.report["report_id"]), ADMIN
        )
        context = deepcopy(linked["evidence"]["evaluation_report"])
        assert context["candidate"] == 1
        assert context["miss"] == reviewed.frozen["annotations"][0]
        assert context["clip"]["analysis_id"] == ANALYSIS
        assert not context["clip"]["matches"]
        reviewed.store.records[TENANT, "evaluation_report", reviewed.report["report_id"]][
            "title"
        ] = "Changed later"
        assert (await reviewed.cases.detail(linked["case_id"], ADMIN))["evidence"][
            "evaluation_report"
        ] == context


@pytest.mark.parametrize(
    "change", ["other_analysis", "other_set", "different_label", "not_missed", "hash"]
)
@pytest.mark.asyncio
async def test_report_requires_exact_clip_analysis_set_and_missed_annotation(reviewed, change):
    report = reviewed.store.records[TENANT, "evaluation_report", reviewed.report["report_id"]]
    clip = report["candidates"][0]["clips"][0]
    if change == "other_analysis":
        clip["analysis_id"] = "another-analysis"
    elif change == "other_set":
        clip["annotation_set_id"] = "another-set"
    elif change == "different_label":
        clip["misses"][0]["end_s"] = 5
    elif change == "not_missed":
        clip["misses"] = []
    else:
        report["annotation_snapshots"][0]["annotation_hash"] = "different"
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await reviewed.cases.create(
            CaseCreate(**reviewed.source, evaluation_report_id=reviewed.report["report_id"]), ADMIN
        )
    assert error.value.status_code == 422
    assert not await reviewed.store.list(TENANT, "case")


@pytest.mark.parametrize(
    "change,status",
    [
        ("unfrozen", 422),
        ("changed_frozen_content", 422),
        ("missing_label", 404),
        ("wrong_video", 404),
        ("pending_analysis", 409),
        ("archived", 409),
    ],
)
@pytest.mark.asyncio
async def test_reviewer_source_requires_frozen_retained_same_recording(reviewed, change, status):
    frozen = reviewed.store.records[
        TENANT, "evaluation_annotations", reviewed.frozen["annotation_set_id"]
    ]
    source = dict(reviewed.source)
    if change == "unfrozen":
        frozen.pop("frozen_at")
    elif change == "changed_frozen_content":
        frozen["annotations"][0]["start_s"] = 2
    elif change == "missing_label":
        source["annotation_id"] = "absent"
    elif change == "wrong_video":
        frozen["video_id"] = "another-recording"
    elif change == "pending_analysis":
        reviewed.store.records[TENANT, "analysis", ANALYSIS]["status"] = "queued"
    else:
        reviewed.store.records[TENANT, "video", VIDEO]["archived_at"] = "now"
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await reviewed.cases.create(CaseCreate(**source), ADMIN)
    assert error.value.status_code == status


@pytest.mark.asyncio
async def test_reviewer_attachment_is_deduplicated_revision_checked_and_original_unchanged(
    reviewed,
):
    reviewed.pool.alerts[TENANT, "alert"] = {
        "alert_id": "alert",
        "title": "Original alert",
        "zone_id": "gate",
    }
    with tenant_scope(TENANT):
        original = await reviewed.cases.create(CaseCreate(alert_id="alert"), ADMIN)
        with pytest.raises(HTTPException) as stale:
            await reviewed.cases.attach(
                original["case_id"], EvidenceAttach(**reviewed.source, expected_revision=2), ADMIN
            )
        assert stale.value.status_code == 409
        attached = await reviewed.cases.attach(
            original["case_id"],
            EvidenceAttach(
                **reviewed.source, expected_revision=1, note="Reviewer noticed this incident"
            ),
            ADMIN,
        )
        assert attached["revision"] == 2 and attached["evidence"] == original["evidence"]
        assert attached["recorded_evidence_count"] == 1
        assert attached["evidence_attachments"][0]["attached_by"] == ADMIN.subject
        duplicate = await reviewed.cases.attach(
            original["case_id"],
            EvidenceAttach(
                **reviewed.source,
                expected_revision=1,
                evaluation_report_id=reviewed.report["report_id"],
            ),
            ADMIN,
        )
        assert duplicate == attached
        assert duplicate["evidence_attachments"][0]["evaluation_report_id"] is None
        with pytest.raises(HTTPException) as absent:
            await reviewed.cases.attach(
                original["case_id"],
                EvidenceAttach(
                    **reviewed.source, expected_revision=1, evaluation_report_id="missing"
                ),
                ADMIN,
            )
        assert absent.value.status_code == 404


@pytest.mark.asyncio
async def test_waiting_reviewer_create_rechecks_archive_under_video_mutation_lock(reviewed):
    with tenant_scope(TENANT):
        lock = video_mutation_lock(reviewed.store, TENANT, VIDEO)
        await lock.acquire()
        task = asyncio.create_task(reviewed.cases.create(CaseCreate(**reviewed.source), ADMIN))
        await asyncio.sleep(0)
        assert not task.done()
        reviewed.store.records[TENANT, "video", VIDEO]["archived_at"] = "now"
        lock.release()
        with pytest.raises(HTTPException) as error:
            await task
        assert error.value.status_code == 409
        assert not await reviewed.store.list(TENANT, "case")


@pytest.mark.parametrize("hidden_in", ["frozen_scopes", "analysis_rules", "report_scopes"])
@pytest.mark.asyncio
async def test_reviewer_creation_and_attachment_require_every_frozen_snapshot_zone(
    reviewed, hidden_in
):
    source = dict(reviewed.source)
    restricted = Principal(
        subject="gate-reviewer",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"gate"}),
        clearance=3,
    )
    with tenant_scope(TENANT):
        if hidden_in == "frozen_scopes":
            stored_analysis = reviewed.store.records[TENANT, "analysis", ANALYSIS]
            stored_analysis["zones"].append({"zone_id": "private"})
            saved = await reviewed.lab.save(
                VIDEO,
                AnnotationDraft(
                    **draft(
                        revision=1,
                        scopes=[
                            {"zone_id": "gate", "event_type": "entry"},
                            {"zone_id": "private", "event_type": "entry"},
                        ],
                    )
                ),
                ADMIN,
            )
            frozen = await reviewed.lab.freeze(VIDEO, saved["revision"], ADMIN)
            source["annotation_set_id"] = frozen["annotation_set_id"]
            stored_analysis["zones"] = [{"zone_id": "gate"}]
        elif hidden_in == "analysis_rules":
            reviewed.store.records[TENANT, "analysis", ANALYSIS]["rules"].append(
                {"rule_id": "private-rule", "zone_id": "private"}
            )
        else:
            reviewed.store.records[TENANT, "evaluation_report", reviewed.report["report_id"]][
                "scopes"
            ].append({"zone_id": "private", "event_type": "entry"})
            source["evaluation_report_id"] = reviewed.report["report_id"]
        with pytest.raises(HTTPException) as denied:
            await reviewed.cases.create(CaseCreate(**source), restricted)
        assert denied.value.status_code == 403
        reviewed.pool.alerts[TENANT, "alert"] = {"alert_id": "alert", "zone_id": "gate"}
        original = await reviewed.cases.create(CaseCreate(alert_id="alert"), ADMIN)
        with pytest.raises(HTTPException) as denied:
            await reviewed.cases.attach(
                original["case_id"], EvidenceAttach(**source, expected_revision=1), restricted
            )
        assert denied.value.status_code == 403
        if hidden_in != "report_scopes":
            attached = await reviewed.cases.attach(
                original["case_id"], EvidenceAttach(**source, expected_revision=1), ADMIN
            )
            assert "private" in attached["evidence_attachments"][0]["evidence_zone_ids"]
            with pytest.raises(HTTPException) as denied:
                await reviewed.cases.load(original["case_id"], restricted)
            assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_routes_enforce_auth_tenant_and_full_frozen_scope_then_return_recording_counts(
    reviewed,
):
    settings = Settings(_env_file=None, auth_mode="dev", auth_required=True)
    issuer, app = DevJwtAuth(settings), FastAPI()
    install_case_routes(app, settings, reviewed.store, reviewed.pool)
    install_governance(app, service="api", settings=settings, authenticator=issuer)

    def headers(tenant=TENANT, role="operator", zones=()):
        return {
            "Authorization": "Bearer "
            + issuer.issue(subject="bob", tenant_id=tenant, roles=(role,), zones=zones, clearance=3)
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/cases", json=reviewed.source)).status_code == 401
        assert (
            await client.post("/api/cases", headers=headers(role="viewer"), json=reviewed.source)
        ).status_code == 403
        assert (
            await client.post("/api/cases", headers=headers(tenant="other"), json=reviewed.source)
        ).status_code == 404
        created = await client.post("/api/cases", headers=headers(), json=reviewed.source)
        assert created.status_code == 200
        case_id = created.json()["case_id"]
        summary = (await client.get("/api/cases", headers=headers())).json()["cases"][0]
        assert summary["recorded_evidence_count"] == 1 and "evidence" not in summary
        assert summary["annotation_set_id"] == reviewed.source["annotation_set_id"]
        # Legacy cached zones cannot hide nested frozen coverage from the current access decision.
        reviewed.store.records[TENANT, "case", case_id]["evidence"]["annotation_set"][
            "scopes"
        ].append({"zone_id": "private", "event_type": "entry"})
        assert (
            await client.get(f"/api/cases/{case_id}", headers=headers(zones=("gate",)))
        ).status_code == 403
        assert (await client.get("/api/cases", headers=headers(zones=("gate",)))).json()[
            "cases"
        ] == []


def test_manual_case_outcomes_do_not_change_detector_precision_or_ground_truth():
    cases = [
        {"verdict": "confirmed", "evidence": {"source": "recorded_file"}},
        {
            "verdict": "false_positive",
            "evidence": {"source": "platform_alert"},
            "evidence_attachments": [{"evidence": {"source": "reviewer_annotation"}}],
        },
        *[
            {
                "verdict": "confirmed",
                "evidence": {"source": "reviewer_annotation"},
                "evidence_attachments": [{"evidence": {"source": "recorded_file"}}],
            }
            for _ in range(4)
        ],
        {"verdict": "unreviewed", "evidence": {"source": "reviewer_annotation"}},
    ]
    metrics = case_metrics(cases, scan_limit=5000)
    assert metrics["reviewed_precision"] == 0.5
    assert metrics["reviewed_count"] == 6 and metrics["detector_reviewed_count"] == 2
    assert metrics["reviewer_annotation_reviewed_count"] == 4
    assert metrics["reviewer_annotation_counts"] == {
        "total": 5,
        "confirmed": 4,
        "false_positive": 0,
        "unreviewed": 1,
    }
    assert metrics["recall"] is None and metrics["missed_incidents"] is None
    assert case_metrics(cases[2:], scan_limit=5000)["reviewed_precision"] is None


@pytest.mark.asyncio
async def test_reviewer_package_uses_pinned_label_without_detector_event_and_keeps_all_refs(
    reviewed, tmp_path, monkeypatch
):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    source = packages.private_video_directory(settings, TENANT, VIDEO)
    source.mkdir(parents=True)
    (source / "playback.mp4").write_bytes(b"protected fixture")
    captured = []

    def builder(path, directory, record, report):
        captured.append((path, deepcopy(record), deepcopy(report)))
        directory.mkdir(parents=True)
        (directory / "package.zip").write_bytes(b"fixture package")
        return {"manifest": {}, "bytes": 15, "sha256": "fixture"}

    monkeypatch.setattr(packages, "build_package", builder)
    manager = packages.EvidencePackageManager(settings, reviewed.store, reviewed.pool)
    with tenant_scope(TENANT):
        case = await reviewed.cases.create(
            CaseCreate(**reviewed.source, evaluation_report_id=reviewed.report["report_id"]), ADMIN
        )
        pending = await manager.create(
            packages.PackageRequest(case_id=case["case_id"], start_s=2, end_s=5),
            SimpleNamespace(state=SimpleNamespace(principal=ADMIN)),
        )
        await asyncio.gather(*manager.tasks)
        assert (await manager.load(TENANT, pending["package_id"]))["status"] == "ready"
    await manager.close()
    path, package, report = captured[0]
    assert path.name == "playback.mp4" and package["event_id"] is None
    assert package["annotation_id"] == "truth-a"
    assert (
        package["case_evidence_refs"][0]["annotation_set_id"]
        == reviewed.source["annotation_set_id"]
    )
    assert package["case_evidence_refs"][0]["evaluation_report_id"] == reviewed.report["report_id"]
    assert report["case"]["evidence"]["annotation_set"] == reviewed.frozen
    assert report["case"]["revision"] == pending["case_revision"]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Local FFmpeg required"
)
@pytest.mark.asyncio
async def test_actual_reviewer_zip_manifest_pins_annotation_and_preserves_empty_detector_source(
    reviewed, tmp_path
):
    source = tmp_path / "playback.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=15",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    with tenant_scope(TENANT):
        case = await reviewed.cases.create(
            CaseCreate(**reviewed.source, evaluation_report_id=reviewed.report["report_id"]), ADMIN
        )
        case = await reviewed.cases.detail(case["case_id"], ADMIN)
    record = {
        "package_id": "pkg_" + "c" * 32,
        "case_id": case["case_id"],
        **reviewed.source,
        "event_id": None,
        "evaluation_report_id": reviewed.report["report_id"],
        "start_s": 2,
        "end_s": 5,
        "created_by": ADMIN.subject,
        "created_at": case["created_at"],
        "annotations": [],
    }
    report = {"case": case, "exported_by": ADMIN.subject, "exported_at": case["created_at"]}
    directory = tmp_path / "package"
    await asyncio.to_thread(packages.build_package, source, directory, record, report)
    with zipfile.ZipFile(directory / "package.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        exported = json.loads(archive.read("case.json"))
        assert manifest["annotation_set_id"] == reviewed.source["annotation_set_id"]
        assert manifest["annotation_id"] == "truth-a" and manifest["event_id"] is None
        assert manifest["evaluation_report_id"] == reviewed.report["report_id"]
        assert exported["case"]["evidence"]["source"] == "reviewer_annotation"
        assert "event" not in exported["case"]["evidence"]
        assert b"<script>" not in archive.read("case.html")
        assert all(
            hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]
            for item in manifest["files"]
        )
