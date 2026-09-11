"""Casework keeps evidence authoritative, edits concurrent-safe and reports literal."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api.case_helpers import (
    CaseCreate,
    CasePatch,
    MissionLink,
    NoteCreate,
    SearchQuery,
    case_metrics,
    escape_like,
    printable_report,
)
from sio_api.cases import Casework, install_case_routes
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.fail_next = False

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.records.get((tenant, kind, record_id)))

    async def list(self, tenant, kind, limit=500):
        return deepcopy(
            [
                record
                for (record_tenant, record_kind, _), record in self.records.items()
                if record_tenant == tenant and record_kind == kind
            ][:limit]
        )

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        key = (tenant, kind, record_id)
        old = self.records.get(key)
        revision = old["revision"] if old else 0
        if self.fail_next:
            self.fail_next = False
            if old:
                self.records[key] = {
                    **old,
                    "revision": revision + 1,
                    "summary": "Concurrent colleague edit",
                }
            raise WorkbenchConflict("Changed")
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchConflict("Changed")
        result = {
            **deepcopy(data),
            "record_id": record_id,
            "revision": revision + 1,
            "created_at": (old or {}).get("created_at", "2026-09-11T10:00:00+00:00"),
            "updated_at": "2026-09-11T10:01:00+00:00",
        }
        self.records[key] = result
        return deepcopy(result)


class FakePool:
    def __init__(self):
        self.alerts = {}
        self.events = {}
        self.missions = {}
        self.decisions = {}
        self.calls = []

    async def fetchrow(self, sql, params):
        self.calls.append((sql, params))
        tenant, record_id = params
        if "FROM alerts" in sql:
            value = self.alerts.get((tenant, record_id))
            return {"payload": deepcopy(value)} if value else None
        if "FROM missions" in sql:
            return deepcopy(self.missions.get((tenant, record_id)))
        return None

    async def fetch(self, sql, params):
        self.calls.append((sql, params))
        if " AS id," in sql:
            return []
        tenant, ids, *_ = params
        if "FROM events" in sql:
            return [
                {"payload": deepcopy(event)}
                for (owner, event_id), event in self.events.items()
                if owner == tenant and event_id in ids
            ]
        if "FROM missions" in sql:
            return [
                deepcopy(item)
                for (owner, mission_id), item in self.missions.items()
                if owner == tenant and mission_id in ids
            ]
        if "FROM decisions" in sql:
            triggers = params[2]
            return [
                {"payload": deepcopy(item)}
                for (owner, decision_id), item in self.decisions.items()
                if owner == tenant and (decision_id in ids or item.get("trigger_event") in triggers)
            ]
        return []


@pytest.fixture
def principal():
    return Principal(subject="alice", tenant_id="tenant-a", roles=frozenset({"admin"}), clearance=3)


@pytest.fixture
async def casework():
    store = MemoryStore()
    pool = FakePool()
    await store.put(
        "tenant-a",
        "video",
        "vid_a",
        {
            "video_id": "vid_a",
            "title": "Yard footage",
            "duration_s": 12,
            "privacy": "full_frame_pixelation",
            "source": "recorded_file",
        },
    )
    await store.put(
        "tenant-a",
        "analysis",
        "ana_a",
        {
            "analysis_id": "ana_a",
            "video_id": "vid_a",
            "status": "completed",
            "model": {
                "mode": "motion",
                "name": "Background motion",
                "warning": "Motion is not classified as people",
            },
            "rules": [{"rule_id": "rule-a", "dwell_s": 3}],
            "zones": [{"zone_id": "zone-a", "name": "Gate"}],
            "events": [
                {
                    "event_id": "event-a",
                    "video_id": "vid_a",
                    "analysis_id": "ana_a",
                    "title": "Motion remained at gate",
                    "zone_id": "zone-a",
                    "rule_id": "rule-a",
                    "at_s": 4,
                    "end_s": 7,
                    "frame_url": "/api/review/videos/vid_a/frames/ana_a/12",
                }
            ],
        },
    )
    return Casework(store, pool)


def source_request(**overrides):
    return CaseCreate(video_id="vid_a", analysis_id="ana_a", event_id="event-a", **overrides)


@pytest.mark.asyncio
async def test_video_case_snapshots_exact_analysis_and_is_idempotent(casework, principal):
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        assert case["created_by"] == "alice"
        assert case["evidence"]["model"]["mode"] == "motion"
        assert case["evidence"]["event"]["at_s"] == 4
        assert case["evidence"]["rules"] == [{"rule_id": "rule-a", "dwell_s": 3}]
        again = await casework.create(source_request(title="Do not overwrite it"), principal)
        assert again["case_id"] == case["case_id"]
        assert again["revision"] == 1
        assert again["title"] == "Motion remained at gate"
        casework.store.records[("tenant-a", "analysis", "ana_a")]["rules"][0]["dwell_s"] = 99
        loaded = await casework.load(case["case_id"], principal)
        assert loaded["evidence"]["rules"][0]["dwell_s"] == 3


@pytest.mark.asyncio
async def test_missing_foreign_or_wrong_video_event_cannot_become_evidence(casework, principal):
    with tenant_scope("tenant-b"):
        foreign = Principal(subject="bob", tenant_id="tenant-b", roles=frozenset({"admin"}))
        with pytest.raises(HTTPException) as error:
            await casework.create(source_request(), foreign)
        assert error.value.status_code == 404
    with tenant_scope("tenant-a"):
        with pytest.raises(HTTPException) as error:
            await casework.create(
                CaseCreate(video_id="vid_a", analysis_id="ana_a", event_id="invented"), principal
            )
        assert error.value.status_code == 404
        casework.store.records[("tenant-a", "analysis", "ana_a")]["video_id"] = "another-video"
        with pytest.raises(HTTPException) as error:
            await casework.create(source_request(), principal)
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_incomplete_analysis_cannot_open_case(casework, principal):
    casework.store.records[("tenant-a", "analysis", "ana_a")]["status"] = "processing"
    with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
        await casework.create(source_request(), principal)
    assert error.value.status_code == 409


def test_case_bodies_reject_client_evidence_authorship_and_ambiguous_sources():
    with pytest.raises(ValidationError):
        CaseCreate(video_id="v", analysis_id="a", event_id="e", evidence={"title": "Forged"})
    with pytest.raises(ValidationError):
        CaseCreate(alert_id="a", video_id="v", analysis_id="x", event_id="e")
    with pytest.raises(ValidationError):
        NoteCreate(text="hello", author="commander")
    with pytest.raises(ValidationError):
        CasePatch(expected_revision=1, created_by="commander")
    with pytest.raises(ValidationError):
        CasePatch(expected_revision=1, due_at="2026-09-11T12:00:00")
    with pytest.raises(ValidationError):
        CasePatch(expected_revision=1, title=None)
    with pytest.raises(ValidationError):
        CasePatch(expected_revision=1, owner="  ")


@pytest.mark.asyncio
async def test_case_patch_cas_resolution_note_and_notes_append_preserve_colleague_edit(
    casework, principal
):
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        with pytest.raises(HTTPException) as error:
            await casework.patch(
                case["case_id"], CasePatch(expected_revision=1, status="resolved"), principal
            )
        assert error.value.status_code == 422
        case = await casework.patch(
            case["case_id"],
            CasePatch(
                expected_revision=1, owner="bob", status="investigating", verdict="confirmed"
            ),
            principal,
        )
        assert case["first_response_at"]
        with pytest.raises(HTTPException) as error:
            await casework.patch(
                case["case_id"], CasePatch(expected_revision=1, title="Stale edit"), principal
            )
        assert error.value.status_code == 409
        casework.store.fail_next = True
        note = await casework.note(case["case_id"], NoteCreate(text="Checked the clip"), principal)
        loaded = await casework.load(case["case_id"], principal)
        assert loaded["summary"] == "Concurrent colleague edit"
        assert len(loaded["notes"]) == 1
        assert note["author"] == "alice"
        case = await casework.patch(
            case["case_id"],
            CasePatch(
                expected_revision=loaded["revision"],
                status="resolved",
                resolution_note="Confirmed and handled by supervisor",
            ),
            principal,
        )
        assert case["resolved_at"]
        assert len(case["notes"]) == 2
        assert case["notes"][1]["kind"] == "resolution"
        assert case["evidence"]["event"]["event_id"] == "event-a"


@pytest.mark.asyncio
async def test_link_only_existing_tenant_mission_and_does_not_transition_it(casework, principal):
    casework.pool.missions[("tenant-b", "mission-b")] = {
        "mission_id": "mission-b",
        "state": "draft",
        "zone_id": "zone-a",
    }
    casework.pool.missions[("tenant-a", "mission-a")] = {
        "mission_id": "mission-a",
        "name": "Gate check",
        "state": "draft",
        "zone_id": "zone-a",
    }
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        with pytest.raises(HTTPException) as error:
            await casework.mission(
                case["case_id"], MissionLink(mission_id="mission-b", expected_revision=1), principal
            )
        assert error.value.status_code == 404
        case = await casework.mission(
            case["case_id"], MissionLink(mission_id="mission-a", expected_revision=1), principal
        )
        assert case["mission_ids"] == ["mission-a"]
        detail = await casework.detail(case["case_id"], principal)
        assert detail["missions"][0]["state"] == "draft"
        assert all("UPDATE missions" not in sql for sql, _ in casework.pool.calls)


@pytest.mark.asyncio
async def test_platform_case_loads_authoritative_events_and_later_decision_approvals(
    casework, principal
):
    casework.pool.alerts[("tenant-a", "alert-a")] = {
        "alert_id": "alert-a",
        "title": "Gate incident",
        "event_ids": ["evt-a"],
        "decision_ids": [],
        "zone_id": "zone-a",
    }
    casework.pool.events[("tenant-a", "evt-a")] = {
        "event_id": "evt-a",
        "rule_id": "dwell-v2",
        "zone_id": "zone-a",
    }
    casework.pool.decisions[("tenant-a", "dec-a")] = {
        "decision_id": "dec-a",
        "trigger_event": "evt-a",
        "approval": "approved",
        "approved_by": "commander-a",
    }
    casework.pool.decisions[("tenant-b", "dec-b")] = {
        "decision_id": "dec-b",
        "trigger_event": "evt-a",
        "approval": "approved",
    }
    with tenant_scope("tenant-a"):
        case = await casework.create(CaseCreate(alert_id="alert-a"), principal)
        assert case["evidence"]["events"][0]["rule_id"] == "dwell-v2"
        detail = await casework.detail(case["case_id"], principal)
        assert len(detail["decisions"]) == 1
        assert detail["decisions"][0]["approved_by"] == "commander-a"
        assert all(params[0] == "tenant-a" for _, params in casework.pool.calls)


@pytest.mark.asyncio
async def test_zone_restrictions_apply_to_case_detail_and_search(casework, principal):
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        restricted = Principal(
            subject="alice",
            tenant_id="tenant-a",
            roles=frozenset({"admin"}),
            zones=frozenset({"other-zone"}),
        )
        with pytest.raises(HTTPException) as error:
            await casework.load(case["case_id"], restricted)
        assert error.value.status_code == 403
        assert await casework.visible_cases(restricted) == []
        results = await casework.search(SearchQuery(kind="video_event"), restricted)
        assert results["results"] == []


@pytest.mark.asyncio
async def test_search_literal_parameterization_and_video_time_links(casework, principal):
    query = "x%' OR 1=1 --_"
    with tenant_scope("tenant-a"):
        result = await casework.search(
            SearchQuery(
                q=query, kind="event", source_id="sensor-a", since=datetime(2026, 9, 1, tzinfo=UTC)
            ),
            principal,
        )
        assert result["results"] == []
        sql, params = casework.pool.calls[-1]
        assert query not in sql
        assert "tenant_id = %s" in sql
        assert params[0] == "tenant-a"
        assert f"%{escape_like(query)}%" in params
        result = await casework.search(
            SearchQuery(q="Motion", kind="video_event", source_id="vid_a"), principal
        )
        assert result["results"][0]["at_s"] == 4
        assert result["results"][0]["analysis_id"] == "ana_a"
        assert result["results"][0]["video_id"] == "vid_a"


def test_metrics_use_reviewed_denominator_and_unknown_unmeasured_values():
    empty = case_metrics([], scan_limit=5000)
    assert empty["reviewed_precision"] is None
    assert empty["recall"] is None
    assert empty["median_response_seconds"] is None
    rows = [
        {
            "status": "resolved",
            "verdict": "confirmed",
            "created_at": "2026-09-11T10:00:00Z",
            "first_response_at": "2026-09-11T10:02:00Z",
            "resolved_at": "2026-09-11T10:10:00Z",
        },
        {
            "status": "investigating",
            "verdict": "false_positive",
            "created_at": "2026-09-11T10:00:00Z",
            "first_response_at": "2026-09-11T10:04:00Z",
        },
        {"status": "open", "verdict": "unreviewed"},
    ]
    metrics = case_metrics(rows, scan_limit=5000)
    assert metrics["reviewed_precision"] == 0.5
    assert metrics["reviewed_count"] == 2
    assert metrics["counts"]["total"] == 3
    assert metrics["median_response_seconds"] == 180
    assert metrics["median_resolution_seconds"] == 600
    assert metrics["resolution_sample_count"] == 1
    assert metrics["missed_incidents"] is None


def test_printable_report_escapes_every_untrusted_field():
    dangerous = '<script>alert("bad")</script><img src="https://outside.invalid/x">'
    report = {
        "case": {
            "case_id": "case-a",
            "title": dangerous,
            "summary": dangerous,
            "notes": [{"text": dangerous}],
            "evidence": {"model": dangerous},
        },
        "missions": [{"name": dangerous}],
        "decisions": [{"rationale": dangerous}],
        "exported_by": dangerous,
        "exported_at": "now",
    }
    rendered = printable_report(report)
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "default-src 'none'" in rendered
    assert "Media bytes are not embedded" in rendered


@pytest.fixture
async def client(casework):
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.principal = Principal(
            subject=request.headers.get("test-subject", "alice"),
            tenant_id=request.headers.get("test-tenant", "tenant-a"),
            roles=frozenset({"admin"}),
        )
        with tenant_scope(request.state.principal.tenant_id):
            return await call_next(request)

    install_case_routes(app, None, casework.store, casework.pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
async def test_saved_searches_owned_or_explicitly_shared(client):
    own = await client.post(
        "/api/review/saved-searches", json={"name": "Private gate", "query": {"zone_id": "zone-a"}}
    )
    assert own.status_code == 200
    assert own.json()["owner"] == "alice"
    shared = await client.post(
        "/api/review/saved-searches",
        json={"name": "Shared gate", "query": {"zone_id": "zone-a"}, "shared": True},
    )
    assert shared.status_code == 200
    bob = await client.get("/api/review/saved-searches", headers={"test-subject": "bob"})
    assert [row["name"] for row in bob.json()["saved_searches"]] == ["Shared gate"]
    foreign = await client.get("/api/review/saved-searches", headers={"test-tenant": "tenant-b"})
    assert foreign.json()["saved_searches"] == []


@pytest.mark.asyncio
async def test_case_http_export_has_snapshot_and_authenticated_author(client):
    created = await client.post(
        "/api/cases",
        json={
            "video_id": "vid_a",
            "analysis_id": "ana_a",
            "event_id": "event-a",
            "title": "Gate <check>",
        },
    )
    assert created.status_code == 200
    case_id = created.json()["case_id"]
    note = await client.post(
        f"/api/cases/{case_id}/notes",
        json={"text": "Reviewed by supervisor"},
        headers={"test-subject": "bob"},
    )
    assert note.json()["author"] == "bob"
    report = await client.get(f"/api/cases/{case_id}/export?format=json")
    assert report.status_code == 200
    assert report.json()["case"]["notes"][0]["author"] == "bob"
    assert report.json()["case"]["evidence"]["analysis"]["analysis_id"] == "ana_a"
    assert report.headers["cache-control"] == "no-store"
    printable = await client.get(f"/api/cases/{case_id}/export?format=html")
    assert "Gate &lt;check&gt;" in printable.text
    assert "attachment" in printable.headers["content-disposition"]
    foreign = await client.get(f"/api/cases/{case_id}", headers={"test-tenant": "tenant-b"})
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_search_rejects_invalid_filter_range_and_bounds(client):
    for query in (
        "limit=10000",
        "kind=secrets",
        "since=2026-09-12T00:00:00Z&until=2026-09-11T00:00:00Z",
        "since=2026-09-12T00:00:00",
    ):
        response = await client.get("/api/review/search?" + query)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_resolution_reason_survives_detail_and_ordinary_resolved_case_edits(
    casework, principal
):
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        resolved = await casework.patch(
            case["case_id"],
            CasePatch(
                expected_revision=1, status="resolved", resolution_note="Gate checked and reopened"
            ),
            principal,
        )
        assert resolved["resolution_note"] == "Gate checked and reopened"
        detail = await casework.detail(case["case_id"], principal)
        assert detail["resolution_note"] == "Gate checked and reopened"
        original_note = deepcopy(detail["notes"][0])
        # Matches CasePanel's draft payload: the populated reason is sent on ordinary edits.
        edited = await casework.patch(
            case["case_id"],
            CasePatch(
                expected_revision=detail["revision"],
                title="Gate review completed",
                owner="bob",
                status="resolved",
                resolution_note=detail["resolution_note"],
            ),
            principal,
        )
        assert edited["resolution_note"] == detail["resolution_note"]
        assert edited["notes"] == [original_note]
        assert edited["resolved_at"] == resolved["resolved_at"]
        changed = await casework.patch(
            case["case_id"],
            CasePatch(
                expected_revision=edited["revision"],
                resolution_note="Gate checked; access remains restricted",
            ),
            principal,
        )
        assert changed["resolution_note"] == "Gate checked; access remains restricted"
        assert changed["notes"][0] == original_note
        assert len(changed["notes"]) == 2
        assert changed["notes"][-1]["author"] == "alice"


@pytest.mark.asyncio
async def test_historical_resolution_note_populates_detail_without_mutating_record(
    casework, principal
):
    with tenant_scope("tenant-a"):
        case = await casework.create(source_request(), principal)
        key = ("tenant-a", "case", case["case_id"])
        stored = casework.store.records[key]
        stored.pop("resolution_note", None)
        stored.update(
            status="resolved",
            notes=[
                {"kind": "resolution", "text": "First reason", "author": "alice"},
                {"kind": "resolution", "text": "Latest verified reason", "author": "bob"},
                {"kind": "note", "text": "A later ordinary note", "author": "alice"},
            ],
        )
        before = deepcopy(stored)
        detail = await casework.detail(case["case_id"], principal)
        assert detail["resolution_note"] == "Latest verified reason"
        assert casework.store.records[key] == before
        edited = await casework.patch(
            case["case_id"],
            CasePatch(expected_revision=detail["revision"], title="Retitled archived case"),
            principal,
        )
        assert edited["resolution_note"] == "Latest verified reason"
        assert edited["notes"] == before["notes"]
