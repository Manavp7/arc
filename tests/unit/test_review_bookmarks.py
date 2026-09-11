"""Private working notes cannot bypass retained-source scope or cleanup locks."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api.review_bookmarks import (
    KIND,
    BookmarkCreate,
    BookmarkDelete,
    BookmarkPatch,
    ReviewBookmarks,
    install_review_bookmark_routes,
)
from sio_api.video_jobs import video_mutation_lock
from sio_api.video_storage import VideoRevision, VideoStorage
from sio_api.workbench_store import WorkbenchConflict
from test_cases import MemoryStore
from test_review_backup import backup

from sio_core.authn import DevJwtAuth, Principal
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope

TENANT = "tenant-a"
ALICE = Principal(subject="alice", tenant_id=TENANT, roles=frozenset({"operator"}), clearance=3)
BOB = Principal(subject="bob", tenant_id=TENANT, roles=frozenset({"admin"}), clearance=3)
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32


class BookmarkStore(MemoryStore):
    async def delete(self, tenant, kind, record_id, expected_revision=None):
        row = self.records.get((tenant, kind, record_id))
        if expected_revision is not None and (not row or row["revision"] != expected_revision):
            raise WorkbenchConflict("Changed")
        return self.records.pop((tenant, kind, record_id), None) is not None


@pytest.fixture
async def work(tmp_path):
    store = BookmarkStore()
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "title": "Authored fixture",
            "status": "ready",
            "duration_s": 12,
            "zones": [{"zone_id": "gate"}],
            "rules": [],
            "analysis_id": "newer-run",
        },
    )
    await store.put(
        TENANT,
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "zones": [{"zone_id": "gate"}],
            "rules": [{"rule_id": "old", "zone_id": "gate"}],
            "events": [],
        },
    )

    async def jobs(_tenant):
        return []

    def directory(tenant, video_id):
        return tmp_path / tenant / video_id

    directory(TENANT, VIDEO).mkdir(parents=True)
    for name in ("original.mp4", "playback.mp4", "poster.jpg"):
        (directory(TENANT, VIDEO) / name).write_bytes(b"authored temporary fixture")
    storage = VideoStorage(SimpleNamespace(store=store, jobs=jobs, directory=directory))
    return ReviewBookmarks(store), storage


def body(at_s=2.5, **extra):
    return BookmarkCreate(
        analysis_id=ANALYSIS, at_s=at_s, title="  Crossing  ", note="  Verify later  ", **extra
    )


async def test_private_notes_survive_manager_restart_and_pin_exact_analysis_without_events(work):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        assert saved["analysis_id"] == ANALYSIS and saved["at_s"] == 2.5
        assert saved["title"] == "Crossing" and saved["note"] == "Verify later"
        assert not {"event_id", "confidence", "case_id", "evidence"} & saved.keys()
        restored = ReviewBookmarks(manager.store)
        result = await restored.list(VIDEO, ALICE)
        assert result["bookmarks"] == [saved] and result["possibly_truncated"] is False
        assert (await restored.list(VIDEO, BOB))["bookmarks"] == []
        other = await restored.create(VIDEO, body(), BOB)
        assert other["bookmark_id"] != saved["bookmark_id"]
        assert (await restored.list(VIDEO, ALICE))["bookmarks"] == [saved]


async def test_duplicate_create_is_idempotent_but_delete_then_recreate_uses_new_identity(work):
    manager, _ = work
    with tenant_scope(TENANT):
        first, again = await asyncio.gather(
            manager.create(VIDEO, body(), ALICE),
            manager.create(VIDEO, body().model_copy(update={"title": "Changed"}), ALICE),
        )
        assert first == again
        assert len((await manager.list(VIDEO, ALICE))["bookmarks"]) == 1
        await manager.delete(first["bookmark_id"], BookmarkDelete(expected_revision=1), ALICE)
        replacement = await manager.create(VIDEO, body(), ALICE)
        assert replacement["bookmark_id"] != first["bookmark_id"]
        assert replacement["revision"] == 1


async def test_partial_edit_keeps_pinned_source_and_stale_edit_or_delete_preserves_newer_note(work):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        updated = await manager.patch(
            saved["bookmark_id"],
            BookmarkPatch(expected_revision=1, note="New observation", at_s=3.25),
            ALICE,
        )
        assert updated["revision"] == 2 and updated["title"] == saved["title"]
        assert updated["analysis_id"] == ANALYSIS and updated["video_id"] == VIDEO
        for run in (
            manager.patch(
                saved["bookmark_id"], BookmarkPatch(expected_revision=1, title="Stale"), ALICE
            ),
            manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), ALICE),
        ):
            with pytest.raises(HTTPException) as error:
                await run
            assert error.value.status_code == 409
        assert (await manager.list(VIDEO, ALICE))["bookmarks"] == [updated]


async def test_other_author_even_admin_and_other_tenant_cannot_modify_bookmark(work):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        for run in (
            manager.patch(
                saved["bookmark_id"], BookmarkPatch(expected_revision=1, note="intrusion"), BOB
            ),
            manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), BOB),
        ):
            with pytest.raises(HTTPException) as error:
                await run
            assert error.value.status_code == 404
    with tenant_scope("tenant-b"):
        other = Principal(
            subject="alice", tenant_id="tenant-b", roles=frozenset({"operator"}), clearance=3
        )
        with pytest.raises(HTTPException) as error:
            await manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), other)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await manager.list(VIDEO, ALICE)
        assert error.value.status_code == 401


@pytest.mark.parametrize("at_s", [-1, float("nan"), float("inf"), float("-inf")])
def test_nonfinite_or_negative_positions_rejected(at_s):
    with pytest.raises(ValidationError):
        body(at_s)
    with pytest.raises(ValidationError):
        BookmarkPatch(expected_revision=1, at_s=at_s)


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": "a" * 121},
        {"note": "a" * 2001},
        {"at_s": None},
        {"analysis_id": "new"},
    ],
)
def test_patch_bounds_and_source_immutability(changes):
    with pytest.raises(ValidationError):
        BookmarkPatch(expected_revision=1, **changes)


async def test_endpoint_positions_allowed_but_out_of_duration_rejected(work):
    manager, _ = work
    with tenant_scope(TENANT):
        assert (await manager.create(VIDEO, body(0), ALICE))["at_s"] == 0
        end = await manager.create(VIDEO, body(12), ALICE)
        assert end["at_s"] == 12
        with pytest.raises(HTTPException) as error:
            await manager.create(VIDEO, body(12.0001), ALICE)
        assert error.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await manager.patch(
                end["bookmark_id"], BookmarkPatch(expected_revision=1, at_s=13), ALICE
            )
        assert error.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await manager.patch(
                end["bookmark_id"], BookmarkPatch(expected_revision=1, at_s=0), ALICE
            )
        assert error.value.status_code == 409


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "interrupted"])
async def test_only_completed_analyses_can_be_bookmarked(work, status):
    manager, _ = work
    row = await manager.store.get(TENANT, "analysis", ANALYSIS)
    await manager.store.put(TENANT, "analysis", ANALYSIS, {**row, "status": status})
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await manager.create(VIDEO, body(), ALICE)
    assert error.value.status_code == 409


async def test_analysis_must_belong_to_exact_video_and_tenant(work):
    manager, _ = work
    row = await manager.store.get(TENANT, "analysis", ANALYSIS)
    await manager.store.put(TENANT, "analysis", ANALYSIS, {**row, "video_id": "other"})
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await manager.create(VIDEO, body(), ALICE)
    assert error.value.status_code == 404


async def test_current_video_and_complete_frozen_analysis_scope_rechecked_on_every_access(work):
    manager, _ = work
    narrow = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        clearance=3,
        zones=frozenset({"gate"}),
    )
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        analysis = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(
            TENANT, "analysis", ANALYSIS, {**analysis, "rules": [{"zone_id": "restricted"}]}
        )
        assert (await manager.list(VIDEO, narrow))["bookmarks"] == []
        for run in (
            manager.list(VIDEO, narrow, ANALYSIS),
            manager.create(VIDEO, body(3), narrow),
            manager.patch(
                saved["bookmark_id"], BookmarkPatch(expected_revision=1, note="No"), narrow
            ),
            manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), narrow),
        ):
            with pytest.raises(HTTPException) as error:
                await run
            assert error.value.status_code == 403
        video = await manager.store.get(TENANT, "video", VIDEO)
        await manager.store.put(
            TENANT, "video", VIDEO, {**video, "zones": [{"zone_id": "restricted"}]}
        )
        with pytest.raises(HTTPException) as error:
            await manager.list(VIDEO, narrow)
        assert error.value.status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"archived_at": "now"},
        {"purged_at": "now"},
        {"purge_started_at": "now"},
        {"status": "purge_failed"},
        {"media_available": False},
    ],
)
async def test_unavailable_video_cannot_gain_bookmark(work, changes):
    manager, _ = work
    video = await manager.store.get(TENANT, "video", VIDEO)
    await manager.store.put(TENANT, "video", VIDEO, {**video, **changes})
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await manager.create(VIDEO, body(), ALICE)
    assert error.value.status_code == 409


async def test_bookmark_protects_footage_and_cas_removal_deletes_only_note(work):
    manager, storage = work
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        candidate = (await storage.candidates(TENANT, [VIDEO], manual=True))[0]
        assert candidate["reasons"] == ["bookmark_linked"] and candidate["linked_bookmarks"] == 1
        with pytest.raises(HTTPException) as error:
            await storage.archive(TENANT, [VideoRevision(video_id=VIDEO, revision=1)], "alice")
        assert error.value.status_code == 409
        before = deepcopy(await manager.store.get(TENANT, "video", VIDEO))
        files = list(storage.manager.directory(TENANT, VIDEO).iterdir())
        await manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), ALICE)
        assert (await storage.candidates(TENANT, [VIDEO], manual=True))[0]["eligible"] is True
        assert await manager.store.get(TENANT, "video", VIDEO) == before
        assert all(path.read_bytes() == b"authored temporary fixture" for path in files)
        assert await manager.store.get(TENANT, "analysis", ANALYSIS)


async def test_create_holds_lifecycle_lock_until_reference_commits_before_archive(work):
    manager, storage = work
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager.store.put

    async def put(tenant, kind, *args, **kwargs):
        if kind == KIND:
            entered.set()
            await release.wait()
        return await original(tenant, kind, *args, **kwargs)

    manager.store.put = put
    with tenant_scope(TENANT):
        create = asyncio.create_task(manager.create(VIDEO, body(), ALICE))
        await asyncio.wait_for(entered.wait(), 2)
        archive = asyncio.create_task(
            storage.archive(TENANT, [VideoRevision(video_id=VIDEO, revision=1)], "alice")
        )
        await asyncio.sleep(0)
        assert not archive.done()
        release.set()
        await create
        with pytest.raises(HTTPException) as error:
            await archive
        assert error.value.status_code == 409


async def test_waiting_create_revalidates_archive_under_shared_lock(work):
    manager, _ = work
    with tenant_scope(TENANT):
        async with video_mutation_lock(manager.store, TENANT, VIDEO):
            task = asyncio.create_task(manager.create(VIDEO, body(), ALICE))
            await asyncio.sleep(0)
            assert not task.done()
            video = await manager.store.get(TENANT, "video", VIDEO)
            await manager.store.put(TENANT, "video", VIDEO, {**video, "archived_at": "now"})
        with pytest.raises(HTTPException) as error:
            await task
        assert error.value.status_code == 409
        assert await manager.store.list(TENANT, KIND) == []


async def test_bounded_scan_reports_incomplete_and_cannot_create_or_move(work, monkeypatch):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create(VIDEO, body(), ALICE)
        monkeypatch.setattr("sio_api.review_bookmarks.SCAN_LIMIT", 1)
        assert (await manager.list(VIDEO, ALICE))["possibly_truncated"] is True
        for run in (
            manager.create(VIDEO, body(3), ALICE),
            manager.patch(saved["bookmark_id"], BookmarkPatch(expected_revision=1, at_s=3), ALICE),
        ):
            with pytest.raises(HTTPException) as error:
                await run
            assert error.value.status_code == 409
        # Removing a known private note remains possible without an unbounded scan.
        await manager.delete(saved["bookmark_id"], BookmarkDelete(expected_revision=1), ALICE)


async def test_quota_check_is_serialized_across_different_recordings(work, monkeypatch):
    manager, _ = work
    monkeypatch.setattr("sio_api.review_bookmarks.MAX_PER_AUTHOR", 1)
    video = await manager.store.get(TENANT, "video", VIDEO)
    analysis = await manager.store.get(TENANT, "analysis", ANALYSIS)
    await manager.store.put(TENANT, "video", "vid_other", {**video, "video_id": "vid_other"})
    await manager.store.put(
        TENANT,
        "analysis",
        "ana_other",
        {**analysis, "analysis_id": "ana_other", "video_id": "vid_other"},
    )
    with tenant_scope(TENANT):
        outcomes = await asyncio.gather(
            manager.create(VIDEO, body(), ALICE),
            manager.create(
                "vid_other", body().model_copy(update={"analysis_id": "ana_other"}), ALICE
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in outcomes) == 1
        assert (
            sum(
                isinstance(result, HTTPException) and result.status_code == 409
                for result in outcomes
            )
            == 1
        )


async def test_http_routes_authentication_and_cas_contract(work, settings):
    manager, _ = work
    settings.audit_enabled = False
    app = FastAPI()
    install_review_bookmark_routes(app, manager.store)
    install_governance(app, service="api", settings=settings)

    def headers(role="operator", subject="alice"):
        token = DevJwtAuth(settings).issue(
            subject=subject, tenant_id=TENANT, roles=(role,), clearance=3
        )
        return {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        collection = f"/api/review/videos/{VIDEO}/bookmarks"
        assert (await client.get(collection)).status_code == 401
        assert (
            await client.post(collection, headers=headers("viewer"), json=body().model_dump())
        ).status_code == 403
        response = await client.post(collection, headers=headers(), json=body().model_dump())
        assert response.status_code == 200
        saved = response.json()
        assert (
            await client.get(collection, headers=headers(), params={"analysis_id": ANALYSIS})
        ).json()["bookmarks"] == [saved]
        path = f"/api/review/bookmarks/{saved['bookmark_id']}"
        assert (
            await client.patch(
                path, headers=headers(), json={"expected_revision": 1, "note": "Updated"}
            )
        ).status_code == 200
        assert (
            await client.request("DELETE", path, headers=headers(), json={"expected_revision": 1})
        ).status_code == 409
        assert (
            await client.request(
                "DELETE", path, headers=headers("admin", "bob"), json={"expected_revision": 2}
            )
        ).status_code == 404
        result = await client.request(
            "DELETE", path, headers=headers(), json={"expected_revision": 2}
        )
        assert result.json() == {"deleted": True, "bookmark_id": saved["bookmark_id"]}


def backup_docs():
    return [
        {"tenant_id": TENANT, "kind": "video", "record_id": VIDEO, "payload": {"video_id": VIDEO}},
        {
            "tenant_id": TENANT,
            "kind": "analysis",
            "record_id": ANALYSIS,
            "payload": {
                "video_id": VIDEO,
                "analysis_id": ANALYSIS,
                "status": "completed",
                "detections": [],
            },
        },
        {
            "tenant_id": TENANT,
            "kind": KIND,
            "record_id": "bookmark",
            "payload": {"video_id": VIDEO, "analysis_id": ANALYSIS, "author": "alice"},
        },
    ]


def test_backup_valid_bookmarks_validate_both_pinned_references():
    refs = backup.media_references(backup_docs())
    assert refs["missing_records"] == [] and refs["unresolved_references"] == []
    assert len(refs["paths"]) == 3


@pytest.mark.parametrize(
    "broken",
    ["missing_video", "missing_analysis", "foreign_analysis", "mismatch", "unfinished", "purged"],
)
def test_backup_cannot_accept_unavailable_or_mismatched_bookmark_sources(broken, tmp_path):
    docs = backup_docs()
    if broken == "missing_video":
        docs.pop(0)
    elif broken == "missing_analysis":
        docs.pop(1)
    elif broken == "foreign_analysis":
        docs[1]["tenant_id"] = "tenant-b"
    elif broken == "mismatch":
        docs[1]["payload"]["video_id"] = "different-video"
    elif broken == "unfinished":
        docs[1]["payload"]["status"] = "failed"
    else:
        docs[0]["payload"].update(status="purged", purged_at="now")
    refs = backup.media_references(docs)
    assert any(item.startswith("review_bookmark:bookmark:") for item in refs["missing_records"])
    with pytest.raises(backup.BackupError, match="missing or unsupported"):
        backup.validate_references(tmp_path, refs)
