"""Operator queue semantics, permissions and immutable handover snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sio_api.case_helpers import CasePatch
from sio_api.case_inbox import HandoverCreate, inbox_snapshot, install_inbox_routes
from test_cases import casework as casework
from test_cases import principal as principal
from test_cases import source_request

from sio_core.authn import DevJwtAuth
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture
def inbox_rows():
    rows = [
        {
            "case_id": "a",
            "title": "Late gate inspection",
            "owner": "alice",
            "priority": "low",
            "due_at": "2026-09-11T11:59:59Z",
        },
        {
            "case_id": "b",
            "title": "Late loading bay",
            "owner": "bob",
            "priority": "high",
            "due_at": "2026-09-11T11:00:00Z",
            "status": "investigating",
        },
        {
            "case_id": "c",
            "title": "Tomorrow's boundary",
            "owner": None,
            "priority": "urgent",
            "due_at": "2026-09-12T12:00:00Z",
        },
        {"case_id": "d", "title": "Due now", "owner": "alice", "due_at": "2026-09-11T12:00:00Z"},
        {
            "case_id": "e",
            "title": "Resolved yesterday",
            "owner": "alice",
            "priority": "urgent",
            "due_at": "2026-09-10T12:00:00Z",
            "status": "resolved",
        },
        {
            "case_id": "f",
            "title": "Gate without deadline",
            "owner": "",
            "due_at": "unparseable",
            "priority": None,
        },
        {
            "case_id": "g",
            "title": "Beyond tomorrow",
            "owner": "bob",
            "priority": "low",
            "due_at": "2026-09-12T12:00:01Z",
        },
        {
            "case_id": "h",
            "title": "Offset equals now",
            "owner": "alice",
            "priority": "high",
            "due_at": "2026-09-11T17:30:00+05:30",
        },
    ]
    return [
        {
            "status": "open",
            "zone_id": "gate",
            "revision": 1,
            "evidence": {"private": "not a list field"},
            **row,
        }
        for row in rows
    ]


def test_inbox_counts_boundaries_priority_sort_and_no_source_mutation(inbox_rows):
    before = deepcopy(inbox_rows)
    result = inbox_snapshot(inbox_rows, "alice", NOW)
    assert result["counts"] == {
        "active": 7,
        "mine": 3,
        "unassigned": 2,
        "overdue": 2,
        "due_soon": 3,
        "resolved": 1,
    }
    assert [row["case_id"] for row in result["cases"]] == ["b", "a", "c", "h", "d", "f", "g"]
    cases = {row["case_id"]: row for row in result["cases"]}
    assert cases["c"]["due_soon"] and cases["d"]["due_soon"] and cases["h"]["due_soon"]
    assert not cases["g"]["due_soon"] and not cases["f"]["overdue"]
    assert cases["d"]["priority"] == cases["f"]["priority"] == "normal"
    assert all("evidence" not in row for row in result["cases"])
    assert result["as_of"] == NOW.isoformat() and result["subject"] == "alice"
    assert not result["possibly_truncated"]
    assert inbox_rows == before


@pytest.mark.parametrize(
    "view,expected",
    [
        ("mine", {"a", "d", "h"}),
        ("unassigned", {"c", "f"}),
        ("overdue", {"a", "b"}),
        ("due_soon", {"c", "d", "h"}),
        ("resolved", {"e"}),
        ("all", set("abcdefgh")),
    ],
)
def test_inbox_views_filter_rows_without_changing_global_visible_counts(inbox_rows, view, expected):
    result = inbox_snapshot(inbox_rows, "alice", NOW, view=view)
    assert {row["case_id"] for row in result["cases"]} == expected
    assert result["counts"]["active"] == 7 and result["counts"]["resolved"] == 1
    if view == "resolved":
        assert not result["cases"][0]["overdue"] and not result["cases"][0]["due_soon"]


def test_inbox_priority_query_and_scan_limit_are_explicit(inbox_rows):
    result = inbox_snapshot(inbox_rows, "alice", NOW, view="active", priority="normal", q="GATE")
    assert {row["case_id"] for row in result["cases"]} == {"d", "f"}
    # Owner matching is literal and case-insensitive, not a separate ownership claim.
    assert {
        row["case_id"] for row in inbox_snapshot(inbox_rows, "alice", NOW, q="BOB")["cases"]
    } == {"b", "g"}
    repeated = inbox_snapshot([inbox_rows[0]] * 5000, "alice", NOW, q="absent")
    assert repeated["possibly_truncated"] and repeated["scan_limit"] == 5000
    assert repeated["cases"] == [] and repeated["counts"]["active"] == 5000


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "  "},
        {"text": "\n"},
        {"case_ids": []},
        {"case_ids": ["case-a", "case-a"]},
        {"case_ids": ["case-a", " case-a "]},
        {"case_ids": [""]},
        {"case_ids": ["x" * 201]},
        {"case_ids": [str(index) for index in range(101)]},
        {"author": "forged"},
    ],
)
def test_handover_validation_rejects_ambiguous_or_forged_snapshot_inputs(changes):
    body = {
        "title": "Morning handover",
        "text": "Two cases need review",
        "case_ids": ["case-a"],
        **changes,
    }
    with pytest.raises(ValidationError):
        HandoverCreate.model_validate(body)


def test_handover_and_priority_validation_preserve_intentional_values():
    handover = HandoverCreate(
        title="  Shift change ", text=" Review at gate. ", case_ids=["case-a"]
    )
    assert handover.title == "Shift change" and handover.text == "Review at gate."
    assert "priority" not in CasePatch(expected_revision=1).model_fields_set
    for priority in ("urgent", "high", "normal", "low"):
        assert CasePatch(expected_revision=1, priority=priority).priority == priority
    for priority in (None, "critical", "", 1):
        with pytest.raises(ValidationError):
            CasePatch(expected_revision=1, priority=priority)


def bearer(settings, *, tenant="tenant-a", role="operator", zones=(), subject="alice"):
    token = DevJwtAuth(settings).issue(
        subject=subject, tenant_id=tenant, roles=(role,), zones=zones, clearance=3
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def inbox_app(casework, principal, settings):
    settings.audit_enabled = False
    with tenant_scope("tenant-a"):
        first = await casework.create(source_request(owner="alice", priority="high"), principal)
    # The second case includes evidence from another zone, so narrow access must hide it entirely.
    second = await casework.store.put(
        "tenant-a",
        "case",
        "case-other",
        {
            **first,
            "case_id": "case-other",
            "title": "Restricted mixed-zone case",
            "zone_id": "zone-a",
            "evidence_zone_ids": ["zone-a", "zone-b"],
            "owner": "bob",
        },
        expected_revision=0,
    )
    app = FastAPI()
    install_inbox_routes(app, casework.store, casework.pool)
    install_governance(app, service="api", settings=settings)
    return app, casework, first, second


async def test_inbox_routes_filter_tenant_and_all_evidence_zones_and_enforce_write_roles(
    inbox_app, settings
):
    app, casework, first, second = inbox_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        narrow = bearer(settings, zones=("zone-a",))
        response = await client.get("/api/case-inbox", headers=narrow)
        assert response.status_code == 200, response.text
        assert [row["case_id"] for row in response.json()["cases"]] == [first["case_id"]]
        assert response.json()["counts"]["active"] == 1
        assert (
            await client.get("/api/case-inbox", headers=bearer(settings, tenant="tenant-b"))
        ).json()["cases"] == []
        assert (await client.get("/api/case-inbox")).status_code == 401
        assert (
            await client.get("/api/case-inbox", params={"view": "not-a-view"}, headers=narrow)
        ).status_code == 422
        body = {
            "title": "Shift handover",
            "text": "Review linked cases",
            "case_ids": [first["case_id"]],
        }
        assert (
            await client.post(
                "/api/case-inbox/handovers", json=body, headers=bearer(settings, role="viewer")
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/case-inbox/handovers",
                json={**body, "case_ids": [first["case_id"], second["case_id"]]},
                headers=narrow,
            )
        ).status_code == 403
    assert await casework.store.list("tenant-a", "case_handover") == []


async def test_handover_captures_immutable_summary_and_hides_after_access_narrows(
    inbox_app, settings
):
    app, casework, first, second = inbox_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/case-inbox/handovers",
            json={
                "title": "Evening handover",
                "text": "Check deadlines before the next shift.",
                "case_ids": [first["case_id"], second["case_id"]],
            },
            headers=bearer(settings),
        )
        assert response.status_code == 201, response.text
        captured = response.json()
        assert captured["author"] == "alice" and captured["captured_at"]
        assert captured["cases"][0]["priority"] == "high"
        assert captured["cases"][0]["revision"] == first["revision"]
        assert all("evidence" not in row and "notes" not in row for row in captured["cases"])
        await casework.store.put(
            "tenant-a",
            "case",
            first["case_id"],
            {
                **first,
                "title": "Changed after the shift",
                "priority": "urgent",
                "owner": "new operator",
            },
            expected_revision=first["revision"],
        )
        retained = (await client.get("/api/case-inbox/handovers", headers=bearer(settings))).json()[
            "handovers"
        ]
        assert retained == [captured]
        assert (
            await client.get(
                "/api/case-inbox/handovers", headers=bearer(settings, zones=("zone-a",))
            )
        ).json()["handovers"] == []
        assert (
            await client.get(
                "/api/case-inbox/handovers", headers=bearer(settings, tenant="tenant-b")
            )
        ).json()["handovers"] == []
        # There is no endpoint that rewrites or deletes the recorded shift snapshot.
        for method in ("PUT", "PATCH", "DELETE"):
            assert (
                await client.request(
                    method,
                    f"/api/case-inbox/handovers/{captured['handover_id']}",
                    json={"text": "rewrite"},
                    headers=bearer(settings),
                )
            ).status_code in {404, 405}
    assert (
        await casework.store.get("tenant-a", "case_handover", captured["handover_id"]) == captured
    )
