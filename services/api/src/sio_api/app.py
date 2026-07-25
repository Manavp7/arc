"""The API service: REST, GraphQL, SSE and media, over one read model."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from sio_core import MessageContext, SioService, describe_error, get_blob, get_pg_pool
from sio_core.telemetry import set_trace_id
from sio_core.tenancy import current_tenant
from sio_schemas import BusMessage, Entity, Event, HealthStatus, new_id, utc_now

from .queries import ReadModel
from .stream import StreamHub
from .timeline import (
    DEFAULT_PRESENCE_WINDOW_S,
    ReplayRegistry,
    TimelineReader,
    plan_replay,
)

_hub: StreamHub | None = None


def get_hub() -> StreamHub:
    """The process-wide stream hub (used by GraphQL subscriptions)."""
    if _hub is None:
        raise RuntimeError("stream hub is not initialised; the API service is not running")
    return _hub


class ApiService(SioService):
    """Read-only surface over the world model, plus the live stream.

    Subscribes to nothing: reads come from Postgres and live updates come from a bus *tail*, which
    means the API never competes with a real consumer for messages and never has a cursor that
    could fall behind.
    """

    name = "api"
    subscribes = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool = get_pg_pool(self.settings)
        self.read = ReadModel(self.pool)
        self.timeline = TimelineReader(self.pool)
        self.replays = ReplayRegistry()
        self.blob = get_blob(self.settings)
        self.hub = StreamHub(self.bus)
        global _hub
        _hub = self.hub

    async def setup(self) -> None:
        await self.pool.open()
        await self.hub.start()
        self.log.info("api.ready", port=self.port, base_url=self.settings.api_base_url)

    async def teardown(self) -> None:
        await self.hub.stop()

    async def on_message(
        self, message: BusMessage, ctx: MessageContext
    ) -> None:  # pragma: no cover
        raise NotImplementedError("the API subscribes to nothing; it tails the bus instead")

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        with contextlib.suppress(Exception):
            checks["blob"] = "ok" if await self.blob.ping() else "unreachable"
        return checks

    async def health_info(self) -> dict[str, str]:
        stats = self.hub.stats()
        return {
            "stream_clients": str(stats["clients"]),
            "stream_forwarded": str(stats["forwarded"]),
        }

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=self.settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.middleware("http")
        async def trace_middleware(request: Request, call_next: Any) -> Response:
            """Give every request a trace id and echo it back.

            A UI bug report can then quote one id that appears in the API log, in the services that
            handled the underlying messages, and in the audit table.
            """
            trace_id = request.headers.get("X-Trace-Id") or new_id("trc")
            set_trace_id(trace_id)
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response

        api = APIRouter(prefix="/api", tags=["world"])
        self._register_rest(api)
        app.include_router(api)
        self._register_stream(app)
        self._register_media(app)
        self._register_graphql(app)

    # ----------------------------------------------------------------------- REST
    def _register_rest(self, api: APIRouter) -> None:
        read = self.read

        @api.get("/health", response_model=HealthStatus)
        async def api_health() -> HealthStatus:
            """Mirrors the root /health so the web client has one prefix for everything."""
            return await self.health()

        @api.get("/stats")
        async def stats() -> dict[str, Any]:
            tenant = current_tenant()
            values = await read.stats(tenant_id=tenant)
            values["by_type"] = await read.entity_counts(tenant_id=tenant)
            values["stream"] = self.hub.stats()
            return values

        @api.get("/entities", response_model=list[Entity])
        async def list_entities(
            type: str | None = None,
            zone_id: str | None = None,
            since: datetime | None = None,
            active_within_s: float | None = Query(
                default=None, description="Only entities seen within this many seconds"
            ),
            include_static: bool = True,
            limit: int = Query(default=200, le=1000),
            offset: int = 0,
        ) -> list[Entity]:
            return await read.entities(
                tenant_id=current_tenant(),
                entity_type=type,
                zone_id=zone_id,
                since=since,
                active_within_s=active_within_s,
                include_static=include_static,
                limit=limit,
                offset=offset,
            )

        @api.get("/entities/{entity_id}", response_model=Entity)
        async def get_entity(entity_id: str) -> Entity:
            entity = await read.entity(entity_id, tenant_id=current_tenant())
            if entity is None:
                raise HTTPException(status_code=404, detail=f"entity {entity_id!r} not found")
            return entity

        @api.get("/entities/{entity_id}/history")
        async def entity_history(
            entity_id: str, limit: int = Query(default=200, le=2000)
        ) -> list[dict[str, Any]]:
            return await read.entity_history(entity_id, tenant_id=current_tenant(), limit=limit)

        @api.get("/events", response_model=list[Event])
        async def list_events(
            type: str | None = None,
            severity: str | None = None,
            entity_id: str | None = None,
            zone_id: str | None = None,
            since: datetime | None = None,
            limit: int = Query(default=100, le=1000),
            offset: int = 0,
        ) -> list[Event]:
            return await read.events(
                tenant_id=current_tenant(),
                event_type=type,
                severity=severity,
                entity_id=entity_id,
                zone_id=zone_id,
                since=since,
                limit=limit,
                offset=offset,
            )

        @api.get("/timeline", response_model=list[Event])
        async def timeline(
            from_: datetime | None = Query(default=None, alias="from"),
            to: datetime | None = None,
            limit: int = Query(default=500, le=5000),
        ) -> list[Event]:
            return await read.timeline(tenant_id=current_tenant(), start=from_, end=to, limit=limit)

        @api.get("/world/at")
        async def world_at(
            ts: datetime,
            limit: int = Query(default=500, le=2000),
            presence_window_s: float = Query(default=DEFAULT_PRESENCE_WINDOW_S, gt=0, le=3600),
        ) -> dict[str, Any]:
            """The world as it stood at ``ts`` — the scrubber's data source (UC5).

            ``presence_window_s`` is how stale an entity's last report may be and still count as present.
            It is a parameter rather than a constant because it is a judgement: too short and a replay
            flickers, too long and departed objects linger as ghosts at their final positions.
            """
            world = await self.timeline.world_at(
                ts,
                tenant_id=current_tenant(),
                limit=limit,
                presence_window_s=presence_window_s,
            )
            return {
                "ts": world["ts"],
                "entities": [entity.to_wire() for entity in world["entities"]],
                "count": len(world["entities"]),
                "counts": world["counts"],
                "presence_window_s": world["presence_window_s"],
            }

        @api.get("/timeline/bounds")
        async def timeline_bounds() -> dict[str, Any]:
            """How far back the record goes, so a scrubber knows what it may scrub over."""
            return await self.timeline.bounds(tenant_id=current_tenant())

        @api.get("/timeline/density")
        async def timeline_density(
            from_: datetime | None = Query(default=None, alias="from"),
            to: datetime | None = None,
            buckets: int = Query(default=120, ge=8, le=1000),
        ) -> dict[str, Any]:
            """Event counts per bucket, for the scrubber's activity strip.

            Counted in the database and returned as a fixed number of buckets, so the payload is the same
            size whether the window is an hour or a week. Fetching every event instead would make the UI
            slower the further back you look, which is exactly when you need it.
            """
            end = to or utc_now()
            start = from_ or (end - timedelta(hours=1))
            return await self.timeline.density(
                tenant_id=current_tenant(), start=start, end=end, buckets=buckets
            )

        @api.post("/replay")
        async def create_replay(
            from_: datetime | None = Query(default=None, alias="from"),
            to: datetime | None = None,
            speed: float = Query(default=20.0, gt=0, le=600),
            step_s: float | None = Query(default=None, gt=0, le=3600),
        ) -> dict[str, Any]:
            """Plan a replay of a window, and report what will actually be delivered.

            Returns the plan rather than starting the stream, because the plan can differ from the
            request: the frame count is capped, so a long window gets a wider step. Telling the client
            the *effective* speed matters — one told "1x" while receiving a frame a minute has been
            misled about what it is watching.
            """
            end = to or utc_now()
            start = from_ or (end - timedelta(minutes=10))
            if start >= end:
                raise HTTPException(status_code=400, detail="'from' must precede 'to'")
            session = plan_replay(
                tenant_id=current_tenant(), start=start, end=end, speed=speed, step_s=step_s
            )
            self.replays.add(session)
            return {
                **session.describe(),
                "stream": f"/api/replay/{session.replay_id}/stream",
            }

        @api.get("/replay/{replay_id}/stream")
        async def stream_replay(replay_id: str) -> StreamingResponse:
            """Stream reconstructed frames over SSE at the planned rate."""
            session = self.replays.get(replay_id)
            if session is None:
                raise HTTPException(
                    status_code=404, detail=f"unknown or expired replay {replay_id!r}"
                )

            async def frames() -> AsyncIterator[bytes]:
                try:
                    async for frame in self.timeline.replay_frames(session):
                        yield f"event: ReplayFrame\ndata: {json.dumps(frame)}\n\n".encode()
                    yield b"event: ReplayComplete\ndata: {}\n\n"
                except asyncio.CancelledError:
                    # The client hung up. Cancel the session so the registry does not hold a dead one
                    # and the loop stops doing database work nobody is reading.
                    session.cancelled = True
                    raise

            return StreamingResponse(
                frames(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @api.delete("/replay/{replay_id}")
        async def cancel_replay(replay_id: str) -> dict[str, Any]:
            return {"cancelled": self.replays.cancel(replay_id)}

        @api.get("/replay")
        async def list_replays() -> dict[str, Any]:
            return self.replays.describe()

        @api.get("/spatial/nearby")
        async def nearby(
            lat: float,
            lon: float,
            radius_m: float = Query(default=500, gt=0, le=100_000),
            type: str | None = None,
            limit: int = Query(default=100, le=1000),
        ) -> list[dict[str, Any]]:
            """PRD M6: 'trucks within 500 m'. Distance is returned, not just membership."""
            found = await read.nearby(
                tenant_id=current_tenant(),
                lat=lat,
                lon=lon,
                radius_m=radius_m,
                entity_type=type,
                limit=limit,
            )
            return [
                {**entity.to_wire(), "distance_m": round(distance, 1)} for entity, distance in found
            ]

        @api.get("/spatial/zones")
        async def zones() -> list[dict[str, Any]]:
            return await read.zones(tenant_id=current_tenant())

        @api.get("/spatial/coverage/{zone_id}")
        async def coverage(zone_id: str) -> list[dict[str, Any]]:
            """PRD M6: 'cameras covering Gate B'."""
            return await read.cameras_covering(tenant_id=current_tenant(), zone_id=zone_id)

        # --- one front door ----------------------------------------------------------------
        #
        # The console talks to the API and nothing else. Alerts, decisions, forecasts, playbook runs and
        # agents each live in their own service, and a UI that knew all six ports would be coupled to the
        # deployment topology — every port change becoming a frontend change, and CORS on five origins.
        #
        # So the API forwards. It is a proxy and says so: it does not reinterpret payloads, it does not add
        # fields, and when a service is down it returns 503 NAMING the service rather than an empty list.
        # An empty list is indistinguishable from "nothing is happening", which is the one thing an operator
        # must not be told when a service has actually fallen over.
        async def _forward(
            service: str,
            port: int,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            method: str = "GET",
            body: Any = None,
            # Named for what it is: the downstream HTTP timeout, not a cancellation deadline for this
            # coroutine. ASYNC109 flags `timeout=` on an async def because callers reasonably expect the
            # latter.
            http_timeout_s: float = 15.0,
        ) -> Any:
            import httpx

            url = f"http://127.0.0.1:{port}{path}"
            try:
                async with httpx.AsyncClient(timeout=http_timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        params={k: v for k, v in (params or {}).items() if v is not None},
                        json=body,
                    )
                    if response.status_code >= 400:
                        # Pass the downstream status through. Turning a 404 into a 503 would tell an
                        # operator the service is down when the id was simply wrong.
                        raise HTTPException(
                            status_code=response.status_code,
                            detail=_detail_of(response)
                            or f"{service} returned {response.status_code}",
                        )
                    return response.json()
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"the {service} service is not reachable: {describe_error(exc)}",
                ) from exc

        @api.get("/alerts", tags=["alerts"])
        async def alerts(
            state: str | None = None, grouped: bool = True, limit: int = Query(50, le=500)
        ) -> Any:
            """The alerts inbox, forwarded from the alerts service."""
            return await _forward(
                "alerts",
                self.settings.alerts_port,
                "/alerts",
                params={"state": state, "grouped": grouped, "limit": limit},
            )

        @api.get("/alerts/{alert_id}", tags=["alerts"])
        async def alert_detail(alert_id: str) -> Any:
            return await _forward("alerts", self.settings.alerts_port, f"/alerts/{alert_id}")

        @api.post("/alerts/{alert_id}/ack", tags=["alerts"])
        async def acknowledge_alert(alert_id: str, body: dict[str, Any] | None = None) -> Any:
            return await _forward(
                "alerts",
                self.settings.alerts_port,
                f"/alerts/{alert_id}/ack",
                method="POST",
                body=body or {"ack_by": "operator"},
            )

        @api.post("/alerts/{alert_id}/resolve", tags=["alerts"])
        async def resolve_alert(alert_id: str, body: dict[str, Any] | None = None) -> Any:
            return await _forward(
                "alerts",
                self.settings.alerts_port,
                f"/alerts/{alert_id}/resolve",
                method="POST",
                body=body or {"resolved_by": "operator"},
            )

        @api.post("/alerts/{alert_id}/escalate", tags=["alerts"])
        async def escalate_alert(alert_id: str, reason: str = "escalated by hand") -> Any:
            return await _forward(
                "alerts",
                self.settings.alerts_port,
                f"/alerts/{alert_id}/escalate",
                method="POST",
                params={"reason": reason},
            )

        @api.get("/decisions", tags=["decisions"])
        async def decisions(approval: str | None = None, limit: int = Query(20, le=200)) -> Any:
            return await _forward(
                "decision",
                self.settings.decision_port,
                "/decisions",
                params={"approval": approval, "limit": limit},
            )

        @api.get("/decisions/{decision_id}", tags=["decisions"])
        async def decision_detail(decision_id: str) -> Any:
            return await _forward(
                "decision", self.settings.decision_port, f"/decisions/{decision_id}"
            )

        @api.post("/decisions/{decision_id}/approve", tags=["decisions"])
        async def approve_decision(decision_id: str, body: dict[str, Any] | None = None) -> Any:
            """Approve a recommendation. Forwarded, never decided here.

            Worth being explicit: the API does not implement approval. It forwards to the service that owns
            decisions, which is the only thing that can change an approval state — so there is no second
            path to authorising an action.
            """
            return await _forward(
                "decision",
                self.settings.decision_port,
                f"/decisions/{decision_id}/approve",
                method="POST",
                body=body or {"approved_by": "operator"},
                http_timeout_s=30.0,
            )

        @api.post("/decisions/{decision_id}/reject", tags=["decisions"])
        async def reject_decision(decision_id: str, body: dict[str, Any] | None = None) -> Any:
            return await _forward(
                "decision",
                self.settings.decision_port,
                f"/decisions/{decision_id}/reject",
                method="POST",
                body=body or {"rejected_by": "operator"},
            )

        @api.get("/forecasts", tags=["prediction"])
        async def forecasts(target: str | None = None, limit: int = Query(20, le=200)) -> Any:
            return await _forward(
                "prediction",
                self.settings.prediction_port,
                "/forecasts",
                params={"target": target, "limit": limit},
            )

        @api.get("/forecasts/latest", tags=["prediction"])
        async def latest_forecasts() -> Any:
            return await _forward("prediction", self.settings.prediction_port, "/forecasts/latest")

        @api.get("/workflow/runs", tags=["workflow"])
        async def workflow_runs(limit: int = Query(20, le=200)) -> Any:
            return await _forward(
                "workflow", self.settings.workflow_port, "/workflow/runs", params={"limit": limit}
            )

        @api.get("/workflow/playbooks", tags=["workflow"])
        async def workflow_playbooks() -> Any:
            return await _forward("workflow", self.settings.workflow_port, "/workflow/playbooks")

        @api.get("/agents", tags=["agents"])
        async def agents() -> Any:
            return await _forward("agents", self.settings.agents_port, "/agents")

        @api.get("/agents/cycles", tags=["agents"])
        async def agent_cycles() -> Any:
            return await _forward("agents", self.settings.agents_port, "/agents/cycles")

        @api.get("/audit", tags=["governance"])
        async def audit(limit: int = Query(50, le=500)) -> Any:
            """The audit trail. Forwarded from the agents service, which owns the writes."""
            return await _forward(
                "agents", self.settings.agents_port, "/agents/audit", params={"limit": limit}
            )

        @api.post("/copilot/ask", tags=["copilot"])
        async def copilot_ask(body: dict[str, Any]) -> Any:
            """Ask the copilot. Generous timeout: a local model takes seconds, not milliseconds."""
            return await _forward(
                "copilot",
                self.settings.copilot_port,
                "/copilot/ask",
                method="POST",
                body=body,
                http_timeout_s=120.0,
            )

        @api.get("/search/frames")
        async def search_frames(
            q: str,
            limit: int = Query(default=12, le=50),
            source_id: str | None = None,
        ) -> dict[str, Any]:
            """Semantic frame search (PRD M2).

            Proxied from the world model rather than reimplemented, so the query is embedded by the
            same model that embedded the frames. Two implementations would drift, and a search that
            embeds its query differently from its index returns confident nonsense.
            """
            import httpx

            url = f"http://127.0.0.1:{self.settings.worldmodel_port}/search/frames"
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(
                        url, params={"q": q, "limit": limit, "source_id": source_id}
                    )
                    response.raise_for_status()
                    return dict(response.json())
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"semantic search unavailable (is the worldmodel service running?): {exc}",
                ) from exc

        @api.get("/measurements")
        async def measurements(
            metric: str,
            source_id: str | None = None,
            since: datetime | None = None,
            limit: int = Query(default=500, le=5000),
        ) -> list[dict[str, Any]]:
            return await read.measurements(
                tenant_id=current_tenant(),
                metric=metric,
                source_id=source_id,
                since=since or utc_now() - timedelta(hours=1),
                limit=limit,
            )

    # --------------------------------------------------------------------- stream
    def _register_stream(self, app: FastAPI) -> None:
        @app.get("/stream", tags=["stream"])
        async def stream(topics: str | None = None) -> StreamingResponse:
            """Server-Sent Events: live entities, events, alerts, decisions, forecasts."""
            wanted = [t.strip() for t in topics.split(",")] if topics else None

            async def generator() -> Any:
                with self.hub.subscribe(wanted) as subscriber:
                    async for frame in self.hub.events(subscriber):
                        yield frame

            return StreamingResponse(
                generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    # nginx buffers streaming responses by default, which would batch live updates
                    # into useless clumps.
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        @app.websocket("/ws")
        async def websocket(socket: WebSocket, topics: str | None = None) -> None:
            """Same feed over WebSocket, for clients that prefer it (and future two-way use)."""
            await socket.accept()
            wanted = [t.strip() for t in topics.split(",")] if topics else None
            try:
                with self.hub.subscribe(wanted) as subscriber:
                    while True:
                        message = await subscriber.queue.get()
                        await socket.send_text(message.model_dump_json(by_alias=True))
            except WebSocketDisconnect:
                return
            except Exception as exc:
                self.log.debug("ws.closed", error=describe_error(exc))
                with contextlib.suppress(Exception):
                    await socket.close()

        @app.get("/stream/stats", tags=["stream"])
        async def stream_stats() -> dict[str, Any]:
            return self.hub.stats()

    # ---------------------------------------------------------------------- media
    def _register_media(self, app: FastAPI) -> None:
        @app.get("/media/{key:path}", tags=["media"])
        async def media(key: str) -> Response:
            """Serve stored media through the API rather than a presigned URL.

            Presigned URLs bypass authorisation, and Phase 5 puts every read behind the same policy
            check. Routing media through here now means that change is a middleware, not a redesign.
            """
            try:
                data = await self.blob.get(key)
            except Exception as exc:
                raise HTTPException(status_code=404, detail=f"media {key!r} not found") from exc
            content_type = "image/jpeg"
            if key.endswith(".png"):
                content_type = "image/png"
            elif key.endswith(".mp4"):
                content_type = "video/mp4"
            elif key.endswith(".json"):
                content_type = "application/json"
            return Response(
                content=data,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )

    # -------------------------------------------------------------------- graphql
    def _register_graphql(self, app: FastAPI) -> None:
        from strawberry.fastapi import GraphQLRouter

        from .graphql_api import schema

        app.include_router(GraphQLRouter(schema, path=""), prefix="/graphql", tags=["graphql"])


def _detail_of(response: object) -> str | None:
    """Pull a FastAPI error detail out of a downstream response, if it has one.

    Forwarding the downstream *message* matters: "unknown alert alt_123" is actionable and "the alerts
    service returned 404" is not, even though both are technically true.
    """
    try:
        body = response.json()  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
    return None
