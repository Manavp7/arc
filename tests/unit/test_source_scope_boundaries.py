"""Principal scope must survive every API surface, including direct media and streams."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sio_api.app import ApiService
from sio_api.queries import ReadModel
from sio_api.stream import Subscriber
from sio_api.timeline import ReplayRegistry, plan_replay
from sio_api.video_review import install_video_routes
from test_video_review import MemoryDocuments, configuration

from sio_core.authn import DevJwtAuth, Principal
from sio_core.guard import install_governance
from sio_core.source_scope import zone_visible, zones_for
from sio_schemas import BusMessage, Event, Topic, utc_now

TENANT = "tenant-a"
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "a" * 32


def headers(settings, *, zones=("allowed",), role="operator", subject="alice"):
    token = DevJwtAuth(settings).issue(
        subject=subject, tenant_id=TENANT, roles=(role,), zones=zones, clearance=3
    )
    return {"Authorization": f"Bearer {token}"}


def test_zone_scope_preserves_unrestricted_accounts_but_refuses_unknown_sources():
    restricted = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"allowed"}),
    )
    assert zones_for(restricted) == ("allowed",)
    assert not zone_visible(zones_for(restricted), None)
    assert not zone_visible(zones_for(restricted), "other")
    assert zone_visible(zones_for(restricted), "allowed")
    assert (
        zones_for(
            Principal(
                subject="admin",
                tenant_id=TENANT,
                roles=frozenset({"admin"}),
                zones=restricted.zones,
            )
        )
        is None
    )
    assert zones_for(Principal(subject="operator", tenant_id=TENANT)) is None


def test_stream_filters_zone_before_queueing_and_before_overflow_counts():
    subscriber = Subscriber(None, tenant_id=TENANT, allowed_zones=("allowed",), maxsize=1)
    for tenant, zone in [
        (TENANT, "allowed"),
        (TENANT, "other"),
        (TENANT, None),
        ("foreign", "allowed"),
    ]:
        subscriber.offer(
            BusMessage.of(
                Topic.EVENTS, Event(tenant_id=tenant, type="entity_appeared", zone_id=zone)
            )
        )
    assert subscriber.queue.qsize() == 1
    assert subscriber.dropped == 0
    assert subscriber.queue.get_nowait().payload["zone_id"] == "allowed"
    subscriber.offer(
        BusMessage(
            topic="entities",
            kind="Entity",
            tenant_id=TENANT,
            payload={"state": {"zone_id": "allowed"}, "related": [{"zone_id": "other"}]},
        )
    )
    assert subscriber.queue.empty()


def test_stream_explanation_zone_cannot_grant_unknown_message_access():
    subscriber = Subscriber(None, tenant_id=TENANT, allowed_zones=("allowed",))
    for kind in ["Event", "Decision", "Track", "Forecast"]:
        subscriber.offer(
            BusMessage(
                topic="events",
                kind=kind,
                tenant_id=TENANT,
                payload={"zone_id": None, "explanation": {"zone_id": "allowed"}},
            )
        )
    assert subscriber.queue.empty()


@pytest.mark.parametrize(
    "service,path",
    [
        ("api", "/api/forecasts/latest"),
        ("prediction", "/forecasts/latest"),
        ("api", "/api/analytics/summary"),
        ("analytics", "/analytics/summary"),
        ("api", "/api/copilot/ask"),
        ("copilot", "/copilot/ask"),
        ("api", "/api/audit"),
        ("governance", "/audit"),
    ],
)
def test_unscoped_computations_refuse_restricted_tokens_at_both_front_doors(
    settings, service, path
):
    settings.tenant_id = TENANT
    settings.audit_enabled = False
    app = FastAPI()

    @app.get(path)
    async def result():
        return {"whole_site_secret": True}

    install_governance(app, service=service, settings=settings)
    with TestClient(app) as client:
        restricted = client.get(path, headers=headers(settings))
        assert restricted.status_code == 403
        assert restricted.json()["rule"] == "source_scope.required"
        assert "whole_site_secret" not in restricted.text
        assert client.get(path, headers=headers(settings, zones=())).status_code == 200


@pytest.fixture
async def recorded(settings):
    settings.audit_enabled = False
    settings.tenant_id = TENANT
    app = FastAPI()
    store = MemoryDocuments()
    manager = install_video_routes(app, settings, store)
    manager.recovered_tenants.add(TENANT)
    install_governance(app, service="api", settings=settings)
    cfg = configuration()
    cfg["zones"][0]["zone_id"] = "other"
    cfg["rules"][0]["zone_id"] = "other"
    video = await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "title": "Forbidden source",
            "duration_s": 10,
            "status": "completed",
            "analysis_id": ANALYSIS,
            "playback_fps": 25,
            **cfg,
        },
        0,
    )
    await store.put(
        TENANT,
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "model": {"mode": "motion"},
            "detections": [],
            "events": [],
            **cfg,
        },
        0,
    )
    await store.put(
        TENANT,
        "video_job",
        "job-a",
        {
            "job_id": "job-a",
            "video_id": VIDEO,
            "analysis_id": ANALYSIS,
            "status": "completed",
            "queued_at": "2026-09-30T00:00:00Z",
            "configuration": cfg,
        },
        0,
    )
    directory = manager.directory(TENANT, VIDEO)
    directory.mkdir(parents=True)
    (directory / "playback.mp4").write_bytes(b"protected-test-media")
    (directory / "poster.jpg").write_bytes(b"protected-test-poster")
    (directory / ANALYSIS).mkdir()
    (directory / ANALYSIS / "0.jpg").write_bytes(b"protected-test-frame")
    return settings, app, store, video


async def test_recorded_lists_and_direct_media_cannot_bypass_restricted_source(recorded):
    settings, app, store, _ = recorded
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers(settings)
    ) as client:
        assert (await client.get("/api/review/videos")).json()["videos"] == []
        assert (await client.get("/api/review/jobs")).json()["jobs"] == []
        for suffix in ["", "/analysis", "/media", "/poster", f"/frames/{ANALYSIS}/0"]:
            response = await client.get(f"/api/review/videos/{VIDEO}{suffix}?zone_id=allowed")
            assert response.status_code == 403, (suffix, response.text)
        assert (
            await client.post(f"/api/review/videos/{VIDEO}/analyze", json={})
        ).status_code == 403
        cfg = configuration()
        cfg["zones"][0]["zone_id"] = cfg["rules"][0]["zone_id"] = "allowed"
        assert (
            await client.put(
                f"/api/review/videos/{VIDEO}/configuration", json={"revision": 1, **cfg}
            )
        ).status_code == 403
    assert (await store.get(TENANT, "video", VIDEO))["revision"] == 1


async def test_exact_analysis_zone_and_archived_media_are_checked(recorded):
    settings, app, store, video = recorded
    video["zones"] = [{"zone_id": "allowed"}]
    video["rules"] = []
    await store.put(TENANT, "video", VIDEO, video, 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers(settings)
    ) as client:
        assert (await client.get(f"/api/review/videos/{VIDEO}/media")).status_code == 200
        assert (
            await client.get(f"/api/review/videos/{VIDEO}/frames/{ANALYSIS}/0")
        ).status_code == 403
        await store.put(TENANT, "video", VIDEO, {**video, "archived_at": "2026-09-30T00:00:00Z"}, 2)
        assert (await client.get(f"/api/review/videos/{VIDEO}/media")).status_code == 409


@pytest.mark.parametrize("path", ["/ws", "/graphql"])
def test_websocket_carries_the_authenticated_zone_scope(settings, memory_bus, path):
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    with TestClient(service.app) as client:
        if path == "/graphql":
            with client.websocket_connect(
                path, headers=headers(settings), subprotocols=["graphql-transport-ws"]
            ) as socket:
                socket.send_json({"type": "connection_init"})
                assert socket.receive_json()["type"] == "connection_ack"
                socket.send_json(
                    {
                        "id": "one",
                        "type": "subscribe",
                        "payload": {"query": "subscription { events { eventId } }"},
                    }
                )
                # GraphQL subscription initialization is asynchronous; confirm its data path in
                # the schema request test and Subscriber test, without timing-based sleeps.
                socket.send_json({"id": "one", "type": "complete"})
        else:
            with client.websocket_connect(path, headers=headers(settings)):
                subscriber = next(iter(service.hub.subscribers))
                assert subscriber.allowed_zones == ("allowed",)


async def test_graphql_collection_and_nested_fields_receive_scope(
    settings, memory_bus, monkeypatch
):
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    entities = AsyncMock(return_value=[])
    events = AsyncMock(return_value=[])
    monkeypatch.setattr(ReadModel, "entities", entities)
    monkeypatch.setattr(ReadModel, "events", events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://test",
        headers=headers(settings),
    ) as client:
        response = await client.post(
            "/graphql",
            json={"query": "{ entities(limit: 1) { entityId } events(limit: 1) { eventId } }"},
        )
        assert response.status_code == 200 and "errors" not in response.json(), response.text
        assert entities.call_args.kwargs["allowed_zones"] == ("allowed",)
        assert events.call_args.kwargs["allowed_zones"] == ("allowed",)
        denied = await client.post(
            "/graphql", json={"query": '{ entities(zoneId: "other") { entityId } }'}
        )
        assert denied.json().get("errors")
        assert entities.await_count == 1


def test_replays_are_owned_and_do_not_list_or_cancel_a_colleagues_session():
    registry = ReplayRegistry()
    end = utc_now()
    session = plan_replay(
        tenant_id=TENANT,
        subject="alice",
        allowed_zones=("allowed",),
        start=end - timedelta(seconds=10),
        end=end,
    )
    registry.add(session)
    assert registry.get(session.replay_id, tenant_id=TENANT, subject="bob") is None
    assert not registry.cancel(session.replay_id, tenant_id=TENANT, subject="bob")
    assert registry.describe(tenant_id=TENANT, subject="bob")["active"] == []
    assert registry.get(session.replay_id, tenant_id=TENANT, subject="alice") is session


def test_audit_gateway_uses_governance_and_preserves_filters(settings, memory_bus, monkeypatch):
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    forwarded = AsyncMock(return_value=httpx.Response(200, json={"entries": []}))
    monkeypatch.setattr(httpx.AsyncClient, "request", forwarded)
    with TestClient(service.app) as client:
        response = client.get(
            "/api/audit?actor=alice&action=review.read&allowed=false&since_minutes=120&limit=7",
            headers=headers(settings, zones=()),
        )
    assert response.status_code == 200
    assert forwarded.call_args.args == ("GET", f"http://127.0.0.1:{settings.governance_port}/audit")
    assert forwarded.call_args.kwargs["params"] == {
        "actor": "alice",
        "action": "review.read",
        "allowed": False,
        "since_minutes": 120,
        "limit": 7,
    }


def test_mcp_http_requires_deployment_admin_even_if_other_dev_auth_disabled(settings, monkeypatch):
    from types import SimpleNamespace

    from sio_copilot.tools import ToolBelt
    from sio_mcp.server import http_app

    settings.tenant_id = TENANT
    settings.auth_required = False
    settings.audit_enabled = True
    publish = AsyncMock()
    monkeypatch.setattr("sio_mcp.server.get_settings", lambda: settings)
    monkeypatch.setattr("sio_core.get_bus", lambda _: SimpleNamespace(publish_message=publish))
    belt = ToolBelt(
        api_url="http://unused",
        spatial_url="http://unused",
        prediction_url="http://unused",
        worldmodel_url="http://unused",
        ingest_url="http://unused",
        tenant_id=TENANT,
    )
    with TestClient(http_app(belt)) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "security-regression", "version": "1"},
            },
        }
        protocol_headers = {"Accept": "application/json, text/event-stream"}
        assert client.post("/mcp/", json=body, headers=protocol_headers).status_code == 401
        assert (
            client.post(
                "/mcp/", json=body, headers={**protocol_headers, **headers(settings, zones=())}
            ).status_code
            == 403
        )
        foreign = DevJwtAuth(settings).issue(
            subject="foreign-admin", tenant_id="foreign", roles=("admin",)
        )
        assert (
            client.post(
                "/mcp/",
                json=body,
                headers={**protocol_headers, "Authorization": f"Bearer {foreign}"},
            ).status_code
            == 403
        )
        accepted = client.post(
            "/mcp/",
            json=body,
            headers={**protocol_headers, **headers(settings, role="admin", zones=())},
        )
        assert accepted.status_code == 200, accepted.text
        assert '"name":"sio"' in accepted.text
    entries = [call.args[0].payload for call in publish.await_args_list]
    assert any(row["actor"] == "alice" and row["allowed"] for row in entries)
    assert any(row["actor"] == "foreign-admin" and not row["allowed"] for row in entries)


async def test_reusable_recording_consumers_enforce_access_zone_not_just_geometric_rules(recorded):
    from sio_api.case_helpers import CaseCreate, SearchQuery
    from sio_api.cases import Casework, case_visible
    from sio_api.evaluations import EvaluationLab
    from sio_api.rule_presets import RulePresets

    from sio_core.tenancy import tenant_scope

    _, _, store, video = recorded
    principal = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"allowed"}),
    )
    video.update(zones=[{"zone_id": "allowed"}], rules=[], evidence_zone_ids=["other"])
    await store.put(TENANT, "video", VIDEO, video, 1)
    pool = type("Pool", (), {"fetch": AsyncMock(return_value=[])})()
    casework = Casework(store, pool)
    with tenant_scope(TENANT):
        from fastapi import HTTPException

        for operation in [
            EvaluationLab(store).versions(VIDEO, principal),
            RulePresets(store).usable_video(VIDEO, principal, 2),
            casework.source(
                CaseCreate(video_id=VIDEO, analysis_id=ANALYSIS, event_id="event"), principal
            ),
        ]:
            with pytest.raises(HTTPException) as denied:
                await operation
            assert denied.value.status_code == 403
        result = await casework.search(SearchQuery(kind="video"), principal)
        assert result["results"] == []
    assert not case_visible(principal, {"evidence": {"source": "recorded_file"}})


def test_each_recorded_source_requires_its_own_scope():
    from fastapi import HTTPException
    from sio_api.review_access import review_scope

    principal = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"allowed"}),
    )
    known = {"video_id": VIDEO, "evidence_zone_ids": ["allowed"]}
    unknown = {"analysis_id": ANALYSIS, "video_id": VIDEO, "zones": []}
    for records in [(known, unknown), (unknown, known), (known, {})]:
        with pytest.raises(HTTPException) as denied:
            review_scope(principal, *records)
        assert denied.value.status_code == 403


async def test_direct_freeze_cannot_return_a_draft_from_a_previously_broader_source(recorded):
    from fastapi import HTTPException
    from sio_api.evaluations import EvaluationLab

    from sio_core.tenancy import tenant_scope

    _, _, store, video = recorded
    principal = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"allowed"}),
    )
    video.update(zones=[{"zone_id": "allowed"}], rules=[], evidence_zone_ids=["allowed"])
    await store.put(TENANT, "video", VIDEO, video, 1)
    draft = await store.put(
        TENANT,
        "evaluation_draft",
        VIDEO,
        {
            "video_id": VIDEO,
            "evidence_zone_ids": ["allowed", "other"],
            "scopes": [{"zone_id": "allowed"}],
        },
        0,
    )
    with tenant_scope(TENANT):
        with pytest.raises(HTTPException) as denied:
            await EvaluationLab(store).freeze(VIDEO, draft["revision"], principal)
        assert denied.value.status_code == 403
    assert await store.list(TENANT, "evaluation_annotations") == []


async def test_evaluation_report_retains_access_zones_outside_annotation_scopes():
    from sio_api.evaluation_metrics import EvaluationRequest
    from sio_api.evaluations import EvaluationLab
    from test_evaluations import Store, analysis, snapshot

    from sio_core.tenancy import tenant_scope

    store = Store()
    lab = EvaluationLab(store)
    broad = Principal(subject="alice", tenant_id=TENANT, roles=frozenset({"operator"}))
    narrow = Principal(
        subject="alice", tenant_id=TENANT, roles=frozenset({"operator"}), zones=frozenset({"gate"})
    )
    await store.put(TENANT, "video", "vid_a", {"video_id": "vid_a", "evidence_zone_ids": ["gate"]})
    frozen = snapshot(video_id="vid_a")
    await store.put(TENANT, "evaluation_annotations", frozen["annotation_set_id"], frozen)
    await store.put(TENANT, "analysis", "ana_a", analysis(evidence_zone_ids=["other"]))
    body = EvaluationRequest(
        inputs=[{"annotation_set_id": frozen["annotation_set_id"], "analysis_ids": ["ana_a"]}]
    )
    with tenant_scope(TENANT):
        report = await lab.report(body, broad)
        assert report["evidence_zone_ids"] == ["gate", "other"]
        # Later source edits cannot strip access from the immutable report snapshot.
        await store.put(TENANT, "analysis", "ana_a", analysis())
        assert not await lab.readable(narrow, report)
        legacy = {
            "scopes": [{"zone_id": "gate"}],
            "video_ids": ["vid_a"],
            "inputs": [{"video_id": "vid_a", "analysis_ids": ["missing"]}],
        }
        assert not await lab.readable(narrow, legacy)


async def test_scoped_fresh_enqueue_pins_source_access_without_an_optional_retry_placeholder(
    recorded, monkeypatch
):
    from sio_api.video_review import VideoReviewManager

    settings, _, store, video = recorded
    cfg = configuration()
    cfg["zones"][0]["zone_id"] = cfg["rules"][0]["zone_id"] = "allowed"
    video.update(**cfg, evidence_zone_ids=["allowed"])
    await store.put(TENANT, "video", VIDEO, video, 1)
    manager = VideoReviewManager(settings, store)
    manager.recovered_tenants.add(TENANT)
    monkeypatch.setattr(manager, "kick", lambda: None)
    monkeypatch.setattr("sio_api.video_jobs.pin_profile", lambda *_: {"resolved_mode": "motion"})
    principal = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        zones=frozenset({"allowed"}),
    )
    result = await manager.enqueue(TENANT, VIDEO, principal.subject, principal=principal)
    assert result["evidence_zone_ids"] == ["allowed"]
    job = await store.get(TENANT, "video_job", result["job_id"])
    assert job["configuration"]["evidence_zone_ids"] == ["allowed"]
