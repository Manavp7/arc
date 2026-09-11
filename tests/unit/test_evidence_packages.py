"""Packages preserve private originals, exact case provenance and bounded authored clips."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import zipfile
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sio_api import evidence_packages as module
from sio_api.evidence_packages import (
    PackageRequest,
    build_package,
    install_evidence_package_routes,
    private_video_directory,
)
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance

VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32


class Store:
    def __init__(self):
        self.rows = {}

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.rows.get((tenant, kind, record_id)))

    async def list_tenants(self, kind):
        return sorted({tenant for tenant, category, _ in self.rows if category == kind})

    async def list(self, tenant, kind, limit=500):
        return [
            deepcopy(value)
            for (owner, category, _), value in self.rows.items()
            if owner == tenant and category == kind
        ][:limit]

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        old = self.rows.get((tenant, kind, record_id))
        revision = old["revision"] if old else 0
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchConflict("Refresh the current revision")
        result = {
            **deepcopy(data),
            "revision": revision + 1,
            "record_id": record_id,
            "created_at": (old or {}).get("created_at", "2026-09-11T08:00:00Z"),
            "updated_at": "2026-09-11T08:01:00Z",
        }
        self.rows[tenant, kind, record_id] = result
        return deepcopy(result)


class Pool:
    async def fetch(self, sql, params):
        return []


def case_record():
    return {
        "case_id": "case-a",
        "title": '<script>alert("case")</script>',
        "status": "investigating",
        "verdict": "unreviewed",
        "revision": 7,
        "video_id": VIDEO,
        "analysis_id": ANALYSIS,
        "event_id": "event-a",
        "evidence": {
            "source": "recorded_file",
            "rules": [{"rule_id": "exact-original-rule"}],
            "model": {"name": "original-model"},
        },
        "notes": [{"text": "Operator's original note", "author": "alice"}],
        "timeline": [],
        "mission_ids": [],
        "evidence_zone_ids": ["gate"],
        "zone_id": "gate",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"start_s": -1},
        {"start_s": 1, "end_s": 1},
        {"end_s": 62},
        {"end_s": float("inf")},
        {"end_s": float("nan")},
        {"annotations": [{"at_s": 4, "text": "Outside"}]},
        {"annotations": [{"at_s": 1, "text": " "}]},
        {"video_id": VIDEO},
        {"source": "/private/original.mp4"},
        {"annotations": [{"at_s": 1, "text": "Observation", "author": "forged"}]},
    ],
)
def test_package_contract_rejects_unbounded_clips_or_client_evidence_and_authorship(changes):
    with pytest.raises(ValidationError):
        PackageRequest.model_validate(
            {"case_id": "case-a", "start_s": 0, "end_s": 3, "annotations": [], **changes}
        )


@pytest.fixture
async def packages_client(tmp_path, monkeypatch):
    store = Store()
    settings = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    await store.put(
        "tenant-a",
        "video",
        VIDEO,
        {"video_id": VIDEO, "duration_s": 3, "privacy": "full_frame_pixelation"},
    )
    await store.put(
        "tenant-a",
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "events": [{"event_id": "event-a", "at_s": 1}],
        },
    )
    await store.put("tenant-a", "case", "case-a", case_record())
    directory = private_video_directory(settings, "tenant-a", VIDEO)
    directory.mkdir(parents=True)
    (directory / "playback.mp4").write_bytes(b"protected-rendition")
    (directory / "original.mp4").write_bytes(b"PRIVATE ORIGINAL NEVER EXPORT")
    calls = []

    def builder(source, output, record, report):
        calls.append((source, deepcopy(record), deepcopy(report)))
        output.mkdir(parents=True)
        (output / "package.zip").write_bytes(b"package")
        return {
            "manifest": {"files": []},
            "bytes": 7,
            "sha256": hashlib.sha256(b"package").hexdigest(),
        }

    monkeypatch.setattr(module, "build_package", builder)
    app = FastAPI()
    issuer = DevJwtAuth(settings)
    lock = asyncio.Lock()
    await lock.acquire()
    manager = install_evidence_package_routes(app, settings, store, Pool(), lock)
    install_governance(app, service="api", settings=settings, authenticator=issuer)

    @app.exception_handler(WorkbenchConflict)
    async def conflicts(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def headers(tenant="tenant-a", role="operator", subject="alice", zones=()):
        return {
            "Authorization": "Bearer "
            + issuer.issue(
                tenant_id=tenant, roles=(role,), subject=subject, zones=zones, clearance=3
            )
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, store, manager, lock, headers, directory, calls
    if lock.locked():
        lock.release()
    await manager.close()


@pytest.mark.asyncio
async def test_package_routes_require_authenticated_case_write_and_existing_scoped_evidence(
    packages_client,
):
    client, store, _, _, headers, _, _ = packages_client
    path = "/api/evidence-packages"
    body = {"case_id": "case-a", "start_s": 0, "end_s": 2}
    assert (await client.get(path)).status_code == 401
    assert (await client.post(path, headers=headers(role="viewer"), json=body)).status_code == 403
    assert (
        await client.post(path, headers=headers(tenant="tenant-b"), json=body)
    ).status_code == 404
    assert (
        await client.post(path, headers=headers(zones=("other-zone",)), json=body)
    ).status_code == 403
    assert (
        await client.post(path, headers=headers(), json={**body, "end_s": 4})
    ).status_code == 422
    store.rows["tenant-a", "video", VIDEO]["archived_at"] = "now"
    assert (await client.post(path, headers=headers(), json=body)).status_code == 409
    store.rows["tenant-a", "video", VIDEO].pop("archived_at")
    store.rows["tenant-a", "analysis", ANALYSIS]["video_id"] = "foreign-recording"
    assert (await client.post(path, headers=headers(), json=body)).status_code == 404
    store.rows["tenant-a", "analysis", ANALYSIS]["video_id"] = VIDEO
    store.rows["tenant-a", "analysis", ANALYSIS]["events"] = [{"event_id": "unrelated"}]
    assert (await client.post(path, headers=headers(), json=body)).status_code == 404
    assert not (await store.list("tenant-a", "evidence_package"))


@pytest.mark.asyncio
async def test_package_pins_exact_recording_before_queue_and_freezes_report_before_case_edits(
    packages_client,
):
    client, store, manager, lock, headers, directory, calls = packages_client
    created = await client.post(
        "/api/evidence-packages",
        headers=headers(subject="author"),
        json={
            "case_id": "case-a",
            "start_s": 0.5,
            "end_s": 2,
            "annotations": [{"at_s": 1, "text": "Observed passage"}],
        },
    )
    assert created.status_code == 202, created.text
    record = created.json()
    assert record["created_by"] == "author" and record["status"] == "queued"
    assert (record["video_id"], record["analysis_id"]) == (VIDEO, ANALYSIS)
    path = "/api/evidence-packages/" + record["package_id"]
    assert (await client.get(path + "/download", headers=headers())).status_code == 409
    assert not calls
    store.rows["tenant-a", "case", "case-a"]["title"] = "Later case edit"
    lock.release()
    await manager.close()
    ready = (await client.get(path, headers=headers())).json()
    assert ready["status"] == "ready"
    assert calls[0][0] == directory / "playback.mp4"
    assert calls[0][2]["case"]["title"].startswith("<script>")
    assert calls[0][2]["case"]["evidence"]["model"]["name"] == "original-model"
    assert (directory / "original.mp4").read_bytes() == b"PRIVATE ORIGINAL NEVER EXPORT"
    downloaded = await client.get(path + "/download", headers=headers())
    assert downloaded.status_code == 200 and downloaded.content == b"package"
    assert downloaded.headers["cache-control"] == "private, no-store"
    for suffix in ["", "/download"]:
        assert (
            await client.get(path + suffix, headers=headers(tenant="tenant-b"))
        ).status_code == 404
        assert (
            await client.get(path + suffix, headers=headers(zones=("other-zone",)))
        ).status_code == 403
    assert (await client.get("/api/evidence-packages", headers=headers(tenant="tenant-b"))).json()[
        "packages"
    ] == []


@pytest.mark.asyncio
async def test_concurrent_package_admission_is_bounded_and_restart_marks_lost_jobs(packages_client):
    client, store, manager, _, headers, _, _ = packages_client
    results = await asyncio.gather(
        *[
            client.post(
                "/api/evidence-packages",
                headers=headers(),
                json={"case_id": "case-a", "start_s": 0, "end_s": 2},
            )
            for _ in range(5)
        ]
    )
    assert sorted(response.status_code for response in results) == [202, 202, 202, 409, 409]
    assert len(manager.active) == 3
    lost_id = "pkg_" + "e" * 32
    await store.put(
        "tenant-a",
        "evidence_package",
        lost_id,
        {"package_id": lost_id, "status": "building", "case_id": "case-a"},
        expected_revision=0,
    )
    response = await client.get("/api/evidence-packages/" + lost_id, headers=headers())
    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    assert "restarted" in response.json()["error"]


@pytest.mark.asyncio
async def test_failed_encoder_removes_partial_outputs_and_keeps_failure_reference(
    packages_client, monkeypatch
):
    client, store, manager, lock, headers, _, _ = packages_client

    def failed(source, output, record, report):
        output.mkdir(parents=True)
        (output / "clip.mp4").write_bytes(b"incomplete")
        raise RuntimeError("private internal path")

    monkeypatch.setattr(module, "build_package", failed)
    record = (
        await client.post(
            "/api/evidence-packages",
            headers=headers(),
            json={"case_id": "case-a", "start_s": 0, "end_s": 2},
        )
    ).json()
    lock.release()
    await manager.close()
    finished = await store.get("tenant-a", "evidence_package", record["package_id"])
    assert finished["status"] == "failed" and "private internal path" not in finished["error"]
    assert not manager.directory("tenant-a", record["package_id"]).exists()
    assert finished["video_id"] == VIDEO


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Local FFmpeg/ffprobe required",
)
def test_actual_zip_has_trimmed_pixelated_silent_media_escaped_html_and_matching_hashes(tmp_path):
    source = tmp_path / "playback.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x96:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    record = {
        "package_id": "pkg_" + "c" * 32,
        "case_id": "case-a",
        "video_id": VIDEO,
        "analysis_id": ANALYSIS,
        "attachment_id": "evidence_" + "d" * 24,
        "event_id": "attached-event",
        "start_s": 0.5,
        "end_s": 2,
        "created_by": "alice",
        "created_at": "2026-09-11T08:00:00Z",
        "annotations": [{"at_s": 1, "text": '<img src=x onerror="bad()"> observed at gate'}],
    }
    report = {
        "exported_at": record["created_at"],
        "exported_by": "alice",
        "case": case_record(),
        "missions": [],
        "decisions": [],
    }
    directory = tmp_path / "package"
    result = build_package(source, directory, record, report)
    with zipfile.ZipFile(directory / "package.zip") as archive:
        assert set(archive.namelist()) == {
            "clip.mp4",
            "case.json",
            "case.html",
            "annotations.json",
            "manifest.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["files"]:
            data = archive.read(entry["path"])
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            assert len(data) == entry["bytes"]
        case_html = archive.read("case.html").decode()
        assert "<script>" not in case_html and "&lt;script&gt;" in case_html
        assert "default-src 'none'" in case_html
        annotations = json.loads(archive.read("annotations.json"))
        assert annotations["annotations"][0]["clip_at_s"] == 0.5
        assert annotations["authored_by"] == "alice"
        assert manifest["case_revision"] == 7
        assert manifest["analysis_id"] == ANALYSIS
        assert manifest["event_id"] == "attached-event"
        assert manifest["attachment_id"] == "evidence_" + "d" * 24
    info = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(directory / "clip.mp4"),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    )
    assert [stream["codec_type"] for stream in info["streams"]] == ["video"]
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (160, 96)
    assert abs(float(info["format"]["duration"]) - 1.5) < 0.2
    # Pixelated output has coarse blocks, rather than the original fixture's fine detail.
    rgb = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(directory / "clip.mp4"),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout

    def pixel(x, y):
        offset = (y * 160 + x) * 3
        return rgb[offset : offset + 3]

    assert max(abs(a - b) for a, b in zip(pixel(52, 50), pixel(57, 51), strict=True)) < 25
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert result["sha256"] == hashlib.sha256((directory / "package.zip").read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_package_can_trim_an_attached_recording_on_an_alert_origin_case(packages_client):
    client, store, manager, lock, headers, directory, calls = packages_client
    attachment_id = "evidence_" + "c" * 24
    current = store.rows["tenant-a", "case", "case-a"]
    attached = {
        **deepcopy(case_record()),
        "attachment_id": attachment_id,
        "attached_by": "investigator",
        "attached_at": "2026-09-11T08:00:00Z",
        "note": "Second viewpoint",
    }
    current.update(
        video_id=None,
        analysis_id=None,
        event_id=None,
        alert_id="origin-alert",
        evidence={
            "source": "platform_alert",
            "alert": {"alert_id": "origin-alert", "ts": "2026-09-11T07:00:00Z"},
        },
        evidence_attachments=[attached],
    )
    path = "/api/evidence-packages"
    body = {"case_id": "case-a", "start_s": 0, "end_s": 2}
    assert (await client.post(path, headers=headers(), json=body)).status_code == 422
    assert (
        await client.post(
            path, headers=headers(), json={**body, "attachment_id": "evidence_" + "0" * 24}
        )
    ).status_code == 404
    response = await client.post(
        path, headers=headers(), json={**body, "attachment_id": attachment_id}
    )
    assert response.status_code == 202, response.text
    package = response.json()
    assert package["attachment_id"] == attachment_id
    assert package["event_id"] == "event-a" and package["video_id"] == VIDEO
    assert package["case_revision"] == current["revision"]
    assert {ref["attachment_id"] for ref in package["case_evidence_refs"]} == {
        "original",
        attachment_id,
    }
    assert package["case_evidence_zone_ids"] == ["gate"]
    current["evidence_attachments"][0]["note"] = (
        "Later stored edit must not rewrite package snapshot"
    )
    lock.release()
    await manager.close()
    assert calls[0][0] == directory / "playback.mp4"
    assert calls[0][2]["case"]["video_id"] is None
    assert calls[0][2]["case"]["evidence_attachments"][0]["note"] == "Second viewpoint"
    assert len(calls[0][2]["case"]["evidence_items"]) == 2
    assert (await client.get(package["download_url"], headers=headers())).status_code == 200


@pytest.mark.asyncio
async def test_package_captured_evidence_zones_remain_restricted_even_if_current_case_changes(
    packages_client,
):
    client, store, manager, lock, headers, _, _ = packages_client
    current = store.rows["tenant-a", "case", "case-a"]
    attachment = {
        "attachment_id": "evidence_" + "e" * 24,
        "alert_id": "restricted-alert",
        "zone_id": "gate",
        "evidence_zone_ids": ["gate"],
        "attached_by": "alice",
        "attached_at": "2026-09-11T08:00:00Z",
        "evidence": {"source": "platform_alert", "rules": [{"zone_id": "private-yard"}]},
    }
    current["evidence_attachments"] = [attachment]
    package = (
        await client.post(
            "/api/evidence-packages",
            headers=headers(),
            json={"case_id": "case-a", "start_s": 0, "end_s": 2},
        )
    ).json()
    assert package["case_evidence_zone_ids"] == ["gate", "private-yard"]
    lock.release()
    await manager.close()
    # No public route removes evidence. Simulate a database repair to verify the ZIP's own guard.
    current["evidence_attachments"] = []
    for path in [f"/api/evidence-packages/{package['package_id']}", package["download_url"]]:
        assert (await client.get(path, headers=headers(zones=("gate",)))).status_code == 403
    assert (await client.get("/api/evidence-packages", headers=headers(zones=("gate",)))).json()[
        "packages"
    ] == []
    assert (await client.get(package["download_url"], headers=headers())).status_code == 200


@pytest.mark.asyncio
async def test_startup_recovers_pending_exports_in_all_known_tenants_without_touching_active_jobs(
    packages_client,
):
    client, store, manager, _, headers, _, _ = packages_client
    active = (
        await client.post(
            "/api/evidence-packages",
            headers=headers(),
            json={"case_id": "case-a", "start_s": 0, "end_s": 2},
        )
    ).json()
    for tenant, code, status in [
        ("tenant-a", "d", "queued"),
        ("tenant-b", "e", "building"),
        ("tenant-a", "f", "ready"),
    ]:
        await store.put(
            tenant,
            "evidence_package",
            "pkg_" + code * 32,
            {
                "package_id": "pkg_" + code * 32,
                "status": status,
                "case_id": "case-a",
                "created_by": "alice",
            },
            expected_revision=0,
        )
    before = module.iso_now()
    await manager.start()
    for tenant, code in [("tenant-a", "d"), ("tenant-b", "e")]:
        recovered = await store.get(tenant, "evidence_package", "pkg_" + code * 32)
        assert recovered["status"] == "interrupted"
        assert recovered["finished_at"] >= before
        assert recovered["revision"] == 2
    assert (await store.get("tenant-a", "evidence_package", active["package_id"]))[
        "status"
    ] == "queued"
    assert (await store.get("tenant-a", "evidence_package", "pkg_" + "f" * 32))["revision"] == 1
    assert manager.recovery_by_tenant["tenant-a"]["interrupted"] == 1
    await manager.start()
    assert (await store.get("tenant-a", "evidence_package", "pkg_" + "d" * 32))["revision"] == 2


@pytest.mark.asyncio
async def test_startup_recovery_reports_its_scan_limit_and_leaves_unscanned_records_explicit(
    packages_client, monkeypatch, caplog
):
    client, store, manager, _, headers, _, _ = packages_client
    monkeypatch.setattr(module, "RECOVERY_SCAN_LIMIT", 2)
    for code in ("a", "b", "c"):
        await store.put(
            "tenant-a",
            "evidence_package",
            "pkg_" + code * 32,
            {"package_id": "pkg_" + code * 32, "status": "queued", "case_id": "case-a"},
            expected_revision=0,
        )
    await manager.start()
    status = manager.recovery_by_tenant["tenant-a"]
    assert status == {"scanned": 2, "scan_limit": 2, "possibly_truncated": True, "interrupted": 2}
    assert "scan limit" in caplog.text
    assert (await store.get("tenant-a", "evidence_package", "pkg_" + "c" * 32))[
        "status"
    ] == "queued"
    # The collection response discloses the bounded startup scan even after lazy recovery.
    assert (await client.get("/api/evidence-packages", headers=headers())).json()[
        "recovery"
    ] == status
