"""Durable deduplication, deadlines, recipient isolation and current-access notification reads."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sio_api.notifications import MarkRead, NotificationManager, install_notification_routes
from test_cases import FakePool, MemoryStore

from sio_core.authn import DevJwtAuth, Principal
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 11, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class NotificationStore(MemoryStore):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        old = self.records.get((tenant, kind, record_id))
        result = await super().put(tenant, kind, record_id, data, expected_revision)
        result.update(
            created_at=old["created_at"] if old else self.clock().isoformat(),
            updated_at=self.clock().isoformat(),
        )
        self.records[tenant, kind, record_id] = result
        return deepcopy(result)

    async def list_tenants(self, kind):
        return sorted({tenant for tenant, record_kind, _ in self.records if record_kind == kind})


@pytest.fixture
def notifications(settings):
    clock = Clock()
    store = NotificationStore(clock)
    manager = NotificationManager(settings, store, FakePool(), clock=clock)
    return manager, store, clock


def who(subject="alice", tenant="tenant-a", zones=(), role="operator"):
    return Principal(
        subject=subject,
        tenant_id=tenant,
        roles=frozenset({role}),
        zones=frozenset(zones),
        clearance=3,
    )


async def case(store, *, case_id="case-a", owner="alice", tenant="tenant-a", due=None, **changes):
    return await store.put(
        tenant,
        "case",
        case_id,
        {
            "case_id": case_id,
            "owner": owner,
            "title": "Gate review",
            "status": "open",
            "due_at": due,
            "zone_id": "zone-a",
            "evidence_zone_ids": ["zone-a"],
            **changes,
        },
    )


async def page(manager, principal=None, **options):
    principal = principal or who()
    with tenant_scope(principal.tenant_id):
        return await manager.list_for(principal.tenant_id, principal, **options)


def selection(records):
    return MarkRead(
        notifications=[
            {"notification_id": row["notification_id"], "revision": row["revision"]}
            for row in records
        ]
    )


async def test_assignment_and_deadline_dedup_restart_and_case_edits_preserve_read_state(
    notifications,
):
    manager, store, clock = notifications
    record = await case(store, due=(clock() + timedelta(hours=2)).isoformat())
    first = await page(manager)
    assert {row["kind"] for row in first["notifications"]} == {"case_assigned", "case_due_soon"}
    assert first["unread_count"] == 2
    with tenant_scope("tenant-a"):
        await manager.mark("tenant-a", who(), selection(first["notifications"]))
    after_mark = deepcopy(store.records)
    await manager.reconcile("tenant-a")
    assert store.records == after_mark
    clock.advance(minutes=1)
    await store.put(
        "tenant-a", "case", record["case_id"], {**record, "summary": "An unrelated summary edit"}
    )
    restarted = NotificationManager(manager.settings, store, FakePool(), clock=clock)
    second = await page(restarted)
    assert second["unread_count"] == 0
    assert {row["notification_id"] for row in second["notifications"]} == {
        row["notification_id"] for row in first["notifications"]
    }
    assert all(row["read_at"] for row in second["notifications"])


async def test_restart_after_notification_write_before_marker_commit_does_not_duplicate(
    notifications, monkeypatch
):
    manager, store, clock = notifications
    await case(store, due=(clock() + timedelta(hours=1)).isoformat())
    original_put = store.put
    fail_once = True

    async def crash_on_marker(tenant, kind, record_id, data, expected_revision=None):
        nonlocal fail_once
        if kind == "notification_source" and fail_once:
            fail_once = False
            raise RuntimeError("Authored interruption after notification commit")
        return await original_put(tenant, kind, record_id, data, expected_revision)

    monkeypatch.setattr(store, "put", crash_on_marker)
    with pytest.raises(RuntimeError):
        await manager.reconcile("tenant-a")
    written = await store.list("tenant-a", "notification")
    assert len(written) == 2
    restarted = NotificationManager(manager.settings, store, FakePool(), clock=clock)
    result = await page(restarted)
    assert {row["notification_id"] for row in result["notifications"]} == {
        row["notification_id"] for row in written
    }
    assert len(await store.list("tenant-a", "notification")) == 2


async def test_reassignment_suppresses_old_recipient_and_new_assignment_has_new_identity(
    notifications,
):
    manager, store, clock = notifications
    record = await case(store, due=(clock() + timedelta(hours=1)).isoformat())
    old = await page(manager)
    await store.put("tenant-a", "case", record["case_id"], {**record, "owner": "bob"})
    assert (await page(manager))["notifications"] == []
    bob = await page(manager, who("bob"))
    assert len(bob["notifications"]) == 2
    await store.put("tenant-a", "case", record["case_id"], record)
    again = await page(manager)
    assert len(again["notifications"]) == 2
    assert {row["notification_id"] for row in again["notifications"]}.isdisjoint(
        row["notification_id"] for row in old["notifications"]
    )
    assert (await page(manager, who("bob")))["notifications"] == []


async def test_deadline_date_changes_offsets_overdue_and_resolution_follow_server_clock(
    notifications,
):
    manager, store, clock = notifications
    record = await case(store, due="2026-09-11T19:30:00+05:30")
    first = await page(manager)
    notice = next(row for row in first["notifications"] if row["kind"] == "case_due_soon")
    await store.put(
        "tenant-a", "case", record["case_id"], {**record, "due_at": "2026-09-11T14:00:00Z"}
    )
    assert (
        next(
            row for row in (await page(manager))["notifications"] if row["kind"] == "case_due_soon"
        )["notification_id"]
        == notice["notification_id"]
    )
    await store.put(
        "tenant-a", "case", record["case_id"], {**record, "due_at": "2026-09-11T15:00:00Z"}
    )
    changed = await page(manager)
    assert (
        next(row for row in changed["notifications"] if row["kind"] == "case_due_soon")[
            "notification_id"
        ]
        != notice["notification_id"]
    )
    clock.advance(hours=3, seconds=1)
    overdue = await page(manager)
    assert {row["kind"] for row in overdue["notifications"]} == {"case_assigned", "case_overdue"}
    assert sum(row["kind"] == "case_overdue" for row in (await page(manager))["notifications"]) == 1
    current = await store.get("tenant-a", "case", record["case_id"])
    await store.put("tenant-a", "case", record["case_id"], {**current, "status": "resolved"})
    assert (await page(manager))["notifications"] == []
    await store.put("tenant-a", "case", record["case_id"], {**current, "status": "open"})
    reopened = await page(manager)
    old_id = next(
        row["notification_id"] for row in overdue["notifications"] if row["kind"] == "case_overdue"
    )
    new_id = next(
        row["notification_id"] for row in reopened["notifications"] if row["kind"] == "case_overdue"
    )
    assert new_id != old_id


async def test_periodic_worker_generates_due_notice_without_an_open_ui_and_stops_cleanly(
    notifications,
):
    manager, store, clock = notifications
    await case(store, tenant="other-tenant", due=(clock() + timedelta(hours=25)).isoformat())
    manager.interval_s = 0.01
    await manager.start()
    try:
        assert {row["kind"] for row in await store.list("other-tenant", "notification")} == {
            "case_assigned"
        }
        clock.advance(hours=2)
        for _ in range(100):
            if len(await store.list("other-tenant", "notification")) == 2:
                break
            await asyncio.sleep(0.01)
        assert {row["kind"] for row in await store.list("other-tenant", "notification")} == {
            "case_assigned",
            "case_due_soon",
        }
    finally:
        await manager.close()
    assert manager.task.done()


@pytest.mark.parametrize(
    "kind,initial,terminal,expected",
    [
        ("video_job", "running", "completed", "analysis_completed"),
        ("video_job", "running", "failed", "analysis_failed"),
        ("evidence_package", "building", "ready", "package_completed"),
        ("evidence_package", "building", "failed", "package_failed"),
        ("evidence_package", "building", "interrupted", "package_failed"),
    ],
)
async def test_terminal_work_baselines_old_results_and_notifies_new_transitions(
    notifications, kind, initial, terminal, expected
):
    manager, store, clock = notifications
    await case(store, owner=None)
    await store.put("tenant-a", "video", "video-a", {"video_id": "video-a", "status": "ready"})
    await store.put(
        "tenant-a",
        "analysis",
        "analysis-a",
        {"analysis_id": "analysis-a", "video_id": "video-a", "zones": [{"zone_id": "zone-a"}]},
    )
    source = {
        "job_id": "job-a",
        "package_id": "package-a",
        "created_by": "alice",
        "case_id": "case-a",
        "video_id": "video-a",
        "analysis_id": "analysis-a",
        "status": terminal,
        "finished_at": (clock() - timedelta(days=1)).isoformat(),
    }
    await store.put("tenant-a", kind, "old", {**source, "job_id": "old", "package_id": "old"})
    assert (await page(manager))["notifications"] == []
    clock.advance(seconds=1)
    key = "job-a" if kind == "video_job" else "package-a"
    await store.put("tenant-a", kind, key, {**source, "status": initial, "finished_at": None})
    await manager.reconcile("tenant-a")
    clock.advance(seconds=1)
    await store.put("tenant-a", kind, key, {**source, "finished_at": clock().isoformat()})
    result = await page(manager)
    assert [row["kind"] for row in result["notifications"]] == [expected]
    restarted = NotificationManager(manager.settings, store, FakePool(), clock=clock)
    assert [row["notification_id"] for row in (await page(restarted))["notifications"]] == [
        result["notifications"][0]["notification_id"]
    ]


async def test_first_notification_baseline_keeps_new_package_restart_interruptions_visible(
    notifications, monkeypatch
):
    from sio_api.evidence_packages import EvidencePackageManager

    manager, store, clock = notifications
    boot = clock()
    clock.now = boot - timedelta(days=1)
    await case(store, owner=None)
    pending_ids = []
    for index, status in enumerate(("queued", "building", "interrupted")):
        package_id = "pkg_" + str(index + 1) * 32
        await store.put(
            "tenant-a",
            "evidence_package",
            package_id,
            {
                "package_id": package_id,
                "case_id": "case-a",
                "created_by": "alice",
                "status": status,
                "finished_at": clock().isoformat() if status == "interrupted" else None,
            },
        )
        if status != "interrupted":
            pending_ids.append(package_id)
    clock.now = boot + timedelta(seconds=1)
    monkeypatch.setattr("sio_api.evidence_packages.iso_now", lambda: clock().isoformat())
    packages = EvidencePackageManager(manager.settings, store, FakePool())
    await packages.start()
    assert packages.recovery_by_tenant["tenant-a"]["interrupted"] == 2
    for package_id in pending_ids:
        record = await store.get("tenant-a", "evidence_package", package_id)
        assert record["status"] == "interrupted" and record["finished_at"] == clock().isoformat()
    # The first notification scan occurs after package startup recovery, exactly as API lifespan does.
    result = await page(manager)
    assert len(result["notifications"]) == 2
    assert {row["kind"] for row in result["notifications"]} == {"package_failed"}
    assert {row["target"]["package_id"] for row in result["notifications"]} == set(pending_ids)
    clock.advance(seconds=1)
    restarted = NotificationManager(manager.settings, store, FakePool(), clock=clock)
    assert {row["notification_id"] for row in (await page(restarted))["notifications"]} == {
        row["notification_id"] for row in result["notifications"]
    }


async def test_current_zone_owner_and_captured_package_scope_hide_list_and_target(notifications):
    manager, store, clock = notifications
    record = await case(store)
    notice = (await page(manager))["notifications"][0]
    restricted = who(zones=("zone-a",))
    with tenant_scope("tenant-a"):
        assert (await manager.target("tenant-a", restricted, notice["notification_id"]))["target"][
            "id"
        ] == record["case_id"]
    await store.put(
        "tenant-a", "case", record["case_id"], {**record, "evidence_zone_ids": ["zone-a", "zone-b"]}
    )
    assert (await page(manager, restricted))["notifications"] == []
    with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
        await manager.target("tenant-a", restricted, notice["notification_id"])
    assert error.value.status_code == 404
    await store.put("tenant-a", "case", record["case_id"], {**record, "owner": None})
    await manager.reconcile("tenant-a")
    clock.advance(seconds=1)
    await store.put(
        "tenant-a",
        "evidence_package",
        "package-a",
        {
            "package_id": "package-a",
            "case_id": record["case_id"],
            "created_by": "alice",
            "status": "ready",
            "finished_at": clock().isoformat(),
            "case_evidence_zone_ids": ["zone-b"],
        },
    )
    assert len((await page(manager))["notifications"]) == 1
    assert (await page(manager, restricted))["notifications"] == []


async def test_bulk_mark_is_recipient_bound_validates_whole_snapshot_and_keeps_new_arrivals_unread(
    notifications,
):
    manager, store, clock = notifications
    await case(store, due=(clock() + timedelta(hours=1)).isoformat())
    first = (await page(manager))["notifications"]
    with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
        await manager.mark("tenant-a", who("bob"), selection(first))
    assert error.value.status_code == 404
    await store.put(
        "tenant-a",
        "notification",
        first[1]["notification_id"],
        {**first[1], "read_at": clock().isoformat()},
        expected_revision=first[1]["revision"],
    )
    with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
        await manager.mark("tenant-a", who(), selection(first))
    assert error.value.status_code == 409
    assert not (await store.get("tenant-a", "notification", first[0]["notification_id"]))["read_at"]
    snapshot = (await page(manager))["notifications"]
    clock.advance(seconds=1)
    await case(store, case_id="new-case")
    await manager.reconcile("tenant-a")
    with tenant_scope("tenant-a"):
        await manager.mark("tenant-a", who(), selection(snapshot))
    remaining = await page(manager, unread_only=True)
    assert len(remaining["notifications"]) == remaining["unread_count"] == 1
    assert remaining["notifications"][0]["target"]["case_id"] == "new-case"


async def test_notification_pagination_uses_created_time_not_read_state_update_time(notifications):
    manager, store, clock = notifications
    for index in range(4):
        await case(store, case_id=f"case-{index}")
        await manager.reconcile("tenant-a")
        clock.advance(minutes=1)
    first = await page(manager, limit=2)
    assert first["has_more"] and first["next_cursor"]
    with tenant_scope("tenant-a"):
        await manager.mark("tenant-a", who(), selection(first["notifications"]))
    second = await page(manager, limit=2, before=first["next_cursor"])
    assert not second["has_more"]
    assert {
        row["target"]["case_id"] for row in first["notifications"] + second["notifications"]
    } == {f"case-{i}" for i in range(4)}
    assert (await page(manager, limit=2))["notifications"][0]["notification_id"] == first[
        "notifications"
    ][0]["notification_id"]
    with pytest.raises(HTTPException) as error:
        await page(manager, before="not-a-cursor")
    assert error.value.status_code == 422


async def test_scan_bound_reports_incomplete_counts_instead_of_claiming_total(
    notifications, monkeypatch
):
    manager, store, _ = notifications
    monkeypatch.setattr("sio_api.notifications.SCAN_LIMIT", 2)
    for index in range(3):
        await case(store, case_id=f"case-{index}")
    result = await page(manager)
    assert result["counts_complete"] is False and result["scan_limit"] == 2


async def test_viewer_can_mark_own_notification_and_never_other_subject_or_tenant(settings):
    settings.audit_enabled = False
    clock = Clock()
    store = NotificationStore(clock)
    app = FastAPI()
    manager = install_notification_routes(app, settings, store, FakePool())
    manager.clock, manager.boot_time = clock, clock()
    install_governance(app, service="api", settings=settings)
    await case(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:

        def headers(subject="alice", tenant="tenant-a"):
            token = DevJwtAuth(settings).issue(
                subject=subject, tenant_id=tenant, roles=("viewer",), clearance=3
            )
            return {"Authorization": f"Bearer {token}"}

        response = await client.get("/api/notifications", headers=headers())
        assert response.status_code == 200, response.text
        notice = response.json()["notifications"][0]
        body = selection([notice]).model_dump()
        assert (
            await client.post("/api/notifications/read", json=body, headers=headers("bob"))
        ).status_code == 404
        assert (
            await client.post(
                "/api/notifications/read", json=body, headers=headers(tenant="tenant-b")
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/notifications/{notice['notification_id']}/target", headers=headers("bob")
            )
        ).status_code == 404
        assert (
            await client.post("/api/notifications/read", json=body, headers=headers())
        ).status_code == 200
        assert (await client.get("/api/notifications", headers=headers())).json()[
            "unread_count"
        ] == 0
        assert (await client.get("/api/notifications")).status_code == 401
