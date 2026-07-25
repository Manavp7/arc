"""Simulation service: what-if projections from the live world (PRD M11, Phase 6).

Reads the world once, projects a scenario against that frozen copy, and publishes the result. The live site is
never touched — see the note at the top of `world.py` for why that boundary is enforced by the types rather
than by discipline.

Two decisions worth stating:

**Every run records the instant it was seeded from.** A projection is only meaningful relative to a state of
the world, and one that cannot say which state it started from cannot be checked afterwards. Checking
afterwards is the only way anybody finds out whether these numbers are worth anything, so the field is
populated on every run and surfaced in the API.

**Results are published to the bus, so the decision engine can consume them.** The PRD asks for that
("results feed the decision engine") and it is the difference between a novelty and a tool: a projection
nobody acts on is a chart.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from sio_core import PgPool, ServiceIdentity, SioService, describe_error, get_pg_pool
from sio_core.explain import ExplanationBuilder
from sio_schemas import RunStatus, SimulationRun, Topic

from .scenarios import SCENARIOS
from .world import WorldSnapshot, snapshot_from_api

#: How many finished runs to keep in memory for the API. Postgres holds them all.
RECENT_RUNS = 50


class RunRequest(BaseModel):
    scenario: str
    params: dict[str, Any] = {}


class SimulationService(SioService):
    """Answers "what would happen if" without making it happen."""

    name = "simulation"
    subscribes = ()
    tick_interval_s = 0.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.identity = ServiceIdentity("simulation", self.settings)
        self.api_url = f"http://127.0.0.1:{self.settings.api_port}"

        async def attach_token(request: httpx.Request) -> None:
            request.headers["Authorization"] = f"Bearer {self.identity.token()}"

        self.client = httpx.AsyncClient(timeout=20.0, event_hooks={"request": [attach_token]})
        self._runs: list[SimulationRun] = []
        self._completed = 0
        self._failed = 0
        self._snapshot_ms: list[float] = []

    async def setup(self) -> None:
        await self.pool.open()
        self.log.info("simulation.ready", scenarios=sorted(SCENARIOS))

    async def teardown(self) -> None:
        await self.client.aclose()

    async def health_checks(self) -> dict[str, str]:
        return {"postgres": "ok" if await self.pool.ping() else "unreachable"}

    async def health_info(self) -> dict[str, str]:
        mean = sum(self._snapshot_ms) / len(self._snapshot_ms) if self._snapshot_ms else 0.0
        return {
            "scenarios": str(len(SCENARIOS)),
            "runs_completed": str(self._completed),
            "runs_failed": str(self._failed),
            "mean_snapshot_ms": f"{mean:.0f}",
        }

    # ------------------------------------------------------------------ the world
    async def snapshot(self) -> WorldSnapshot:
        """Read the site once, into a frozen copy.

        Read through the API rather than straight from Postgres, deliberately: the API applies the same
        tenant scoping and the same active-entity window the console sees, so a projection is about the world
        an operator is looking at rather than about every row ever written. A simulation seeded from an
        entity that left three hours ago is a projection of a site that does not exist.
        """
        started = time.perf_counter()
        entities: list[dict[str, Any]] = []
        zones: list[dict[str, Any]] = []
        open_alerts = 0

        try:
            response = await self.client.get(
                f"{self.api_url}/api/entities",
                params={"limit": 500, "active_within_s": 300, "include_static": True},
            )
            response.raise_for_status()
            entities = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not read entities: {describe_error(exc)}"
            ) from exc

        try:
            response = await self.client.get(f"{self.api_url}/api/spatial/zones")
            response.raise_for_status()
            zones = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not read zones: {describe_error(exc)}"
            ) from exc

        try:
            inbox = await self.client.get(f"{self.api_url}/api/alerts", params={"limit": 200})
            if inbox.status_code == 200:
                open_alerts = int(inbox.json().get("open") or 0)
        except httpx.HTTPError:
            # Not fatal: alert count colours a projection but no scenario depends on it. A what-if that
            # refuses to run because the inbox is unreachable is less useful than one that runs without it.
            pass

        self._snapshot_ms.append((time.perf_counter() - started) * 1000)
        del self._snapshot_ms[:-20]
        return snapshot_from_api(
            taken_at=datetime.now(UTC),
            entities=entities,
            zones=zones,
            open_alerts=open_alerts,
            tenant_id=self.settings.tenant_id,
        )

    # -------------------------------------------------------------------- running
    async def run_scenario(self, scenario_name: str, params: dict[str, Any]) -> SimulationRun:
        scenario = SCENARIOS.get(scenario_name)
        if scenario is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown scenario {scenario_name!r}; available: {', '.join(sorted(SCENARIOS))}",
            )

        world = await self.snapshot()
        run = SimulationRun(
            tenant_id=self.settings.tenant_id,
            scenario=scenario_name,
            params=params,
            status=RunStatus.RUNNING,
            seeded_from_ts=world.taken_at,
        )

        try:
            projection = scenario.project(world, params)
        except Exception as exc:
            self._failed += 1
            run.status = RunStatus.FAILED
            run.error = describe_error(exc)
            run.finished_ts = datetime.now(UTC)
            self.log.error("simulation.failed", scenario=scenario_name, error=run.error)
            await self._persist(run)
            return run

        explanation = ExplanationBuilder(summary=projection.summary)
        explanation.add_model(f"scenario:{scenario_name}", note=scenario.question)
        explanation.add_note(
            f"seeded from the world as it was at {world.taken_at.isoformat()}: "
            f"{len(world.entities)} entities across {len(world.zones)} zones"
        )
        # Assumptions in the explanation, not merely in the payload. Every constant in these projections was
        # chosen rather than measured, and an operator shown a number without them will treat a guess as a
        # measurement.
        for assumption in projection.assumptions:
            explanation.add_note(f"assumes: {assumption}")
        for recommendation in projection.recommendations:
            explanation.add_note(f"suggests: {recommendation}")
        explanation.add_note(
            "this is a projection against a frozen copy of the world; nothing on the live site was changed"
        )
        explanation.confidence(projection.confidence)

        run.status = RunStatus.COMPLETED
        run.finished_ts = datetime.now(UTC)
        run.results = {
            "summary": projection.summary,
            "detail": projection.detail,
            "assumptions": projection.assumptions,
            "recommendations": projection.recommendations,
            "world": world.describe(),
        }
        run.kpi_deltas = projection.kpi_deltas
        run.impacted_entities = projection.impacted_entities
        run.timeline = projection.timeline
        run.confidence = projection.confidence
        run.explanation = explanation.build()

        self._completed += 1
        self._runs.append(run)
        del self._runs[:-RECENT_RUNS]
        await self._persist(run)
        # Published so the decision engine can act on a projection. A what-if nobody acts on is a chart.
        await self.publish(Topic.SIMULATION, run)
        self.log.info(
            "simulation.completed",
            scenario=scenario_name,
            impacted=len(projection.impacted_entities),
            kpis=list(projection.kpi_deltas),
            confidence=projection.confidence,
        )
        return run

    async def _persist(self, run: SimulationRun) -> None:
        try:
            await self.pool.execute(
                """
                INSERT INTO simulation_runs (
                    tenant_id, run_id, scenario, status, started_ts, finished_ts,
                    seeded_from, confidence, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    finished_ts = EXCLUDED.finished_ts,
                    confidence = EXCLUDED.confidence,
                    payload = EXCLUDED.payload
                """,
                (
                    run.tenant_id,
                    run.run_id,
                    run.scenario,
                    str(run.status),
                    run.started_ts,
                    run.finished_ts,
                    run.seeded_from_ts,
                    run.confidence,
                    # Everything else lives in `payload`, matching the table as designed rather than widening
                    # it. Only the fields something queries BY are promoted to columns — scenario, status,
                    # time — and `kpi_deltas` and `impacted_entities` are read back with the run, never
                    # filtered on. The same call I made for audit_log: a column is a commitment, and the
                    # cheapest schema is the one that only commits to what is queried.
                    run.to_json(),
                ),
            )
        except Exception as exc:
            # A projection that ran is still useful even if it could not be filed. Logged rather than raised,
            # because failing the request would discard an answer the caller is waiting for over a storage
            # problem they cannot act on.
            self.log.warning("simulation.persist_failed", run=run.run_id, error=describe_error(exc))

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/simulations/scenarios", tags=["simulation"])
        async def scenarios() -> dict[str, Any]:
            """The scenarios, with their parameters — one source for the API, the UI and the copilot tool."""
            return {
                "scenarios": [
                    {
                        "name": scenario.name,
                        "question": scenario.question,
                        "parameters": scenario.parameters,
                    }
                    for scenario in SCENARIOS.values()
                ]
            }

        @app.post("/simulations", tags=["simulation"])
        async def run(request: RunRequest) -> dict[str, Any]:
            """Project a scenario. Changes nothing on the live site."""
            result = await self.run_scenario(request.scenario, request.params)
            if result.status == RunStatus.FAILED:
                raise HTTPException(status_code=500, detail=result.error or "the scenario failed")
            return result.to_wire()

        @app.get("/simulations", tags=["simulation"])
        async def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
            rows = await self.pool.fetch(
                """
                SELECT payload FROM simulation_runs
                 WHERE tenant_id = %s ORDER BY started_ts DESC LIMIT %s
                """,
                (self.settings.tenant_id, limit),
            )
            return {
                "runs": [row["payload"] for row in rows],
                "counters": {"completed": self._completed, "failed": self._failed},
            }

        @app.get("/simulations/{run_id}", tags=["simulation"])
        async def run_detail(run_id: str) -> dict[str, Any]:
            row = await self.pool.fetchrow(
                "SELECT payload FROM simulation_runs WHERE tenant_id = %s AND run_id = %s",
                (self.settings.tenant_id, run_id),
            )
            if row is None:
                raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
            return dict(row["payload"])

        @app.get("/simulations/world/snapshot", tags=["simulation"])
        async def world_snapshot() -> dict[str, Any]:
            """What a projection would be seeded from, right now.

            Exposed because "why did the simulation say that?" is usually answered by looking at what it was
            given, and an operator cannot inspect a frozen copy that only ever existed inside one request.
            """
            world = await self.snapshot()
            return world.describe()


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


__all__ = ["RECENT_RUNS", "SimulationService"]
