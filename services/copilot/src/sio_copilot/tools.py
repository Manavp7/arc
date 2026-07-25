"""The copilot's tools (PRD M13).

**The copilot is a client of the platform, not a privileged insider.** Every tool goes through the same
HTTP surface an external caller would use, which has three consequences worth stating: the API is
exercised by the thing users actually drive, the MCP server (P4.2) can expose these same tools without a
second implementation, and a tool cannot reach data the API would not expose.

The one exception is `graph_query`, which uses the `GraphStore` port because multi-hop traversal is not a
REST shape — and that exception comes with the most important decision in this file.

**No raw Cypher, ever.** The obvious `graph_query` takes a query string from the model, and it is a
mistake dressed as flexibility. A language model composing a graph query against a live world model is an
injection vector with extra steps: `DETACH DELETE` is three tokens away from a legitimate traversal, and
the model has no idea which of its outputs are destructive. So the tool takes a starting entity, a set of
relationship types and a depth, and builds the traversal itself. The model chooses *what to ask*; it never
chooses *how to execute*.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import httpx

from sio_core import ServiceIdentity, describe_error, get_logger
from sio_core.llm import ToolSpec

#: Strings a model sends when it means "no value".
#:
#: Collected from observed behaviour rather than imagined: `'null'` is the one that produced a false
#: statement about the site, and the rest are the same mistake in different spellings. Note `'all'` and
#: `'any'` — a model expressing "everything" as a filter VALUE turns a request for everything into a
#: request for nothing.
NULLISH = frozenset(
    {"", "null", "none", "nil", "undefined", "n/a", "na", "any", "all", "*", "-", "unspecified"}
)

log = get_logger("sio.copilot.tools")

# How long any single tool may take. A copilot answering in under ten seconds cannot afford a tool that
# waits thirty, and a timeout that fires is far better than a request that hangs: the graph can say
# "spatial did not answer" and still produce something.
TOOL_TIMEOUT_S = 6.0

MAX_RESULT_CHARS = 4000
"""Cap on what a tool feeds back into the context.

Not a nicety. A 1.5-3 B model has a small context, and a tool returning two hundred entities will push the
question itself out of the window — the model then answers a question it can no longer see. Truncation is
reported so the synthesis step knows it is working from a sample.
"""


@dataclass
class ToolResult:
    """What a tool produced, and everything the explanation will need to cite it."""

    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    evidence: list[str] = field(default_factory=list)
    """Ids a reader could look up themselves: entity ids, event ids, frame keys."""
    source: str = ""
    """Which endpoint or store answered, so a claim can be traced to its origin."""
    truncated: bool = False
    brief: dict[str, Any] | None = None
    """A compact view for the model. The full `data` still goes to the UI and the explanation.

    Measured, not guessed: `list_entities` returned 25 entities as ~2,500 characters of JSON, and the
    synthesis turn took 9.3 seconds of a 13-second answer — the model was spending its time READING, and
    then dutifully enumerating truck names nobody asked for. A 3 B model on CPU processes a prompt at a few
    hundred tokens a second, so prompt size is latency.

    Two audiences, two payloads. The model needs the shape of the answer; the console needs the rows.
    """

    def for_model(self) -> str:
        """The string handed back to the model — the brief view when a tool provided one.

        JSON rather than prose: a small model re-narrating a table loses numbers, and the synthesis step
        reads better when it is quoting values it can see.
        """
        if not self.ok:
            return json.dumps({"error": self.error or "tool failed"})
        rendered = json.dumps(self.brief if self.brief is not None else self.data, default=str)
        if len(rendered) > MAX_RESULT_CHARS:
            self.truncated = True
            return rendered[:MAX_RESULT_CHARS] + f'... (truncated; {len(rendered)} chars total)"}}'
        return rendered

    def describe(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "source": self.source,
            "evidence": self.evidence[:10],
            "error": self.error,
            "truncated": self.truncated,
        }


@dataclass
class Tool:
    """A tool: its schema for the model, and the code that runs it."""

    spec: ToolSpec
    run: Callable[[dict[str, Any]], Awaitable[ToolResult]]

    @property
    def name(self) -> str:
        return self.spec.name


class ToolBelt:
    """The nine tools, wired to service endpoints."""

    def __init__(
        self,
        *,
        api_url: str,
        spatial_url: str,
        prediction_url: str,
        worldmodel_url: str,
        ingest_url: str,
        graph: Any = None,
        tenant_id: str = "default",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.spatial_url = spatial_url.rstrip("/")
        self.prediction_url = prediction_url.rstrip("/")
        self.worldmodel_url = worldmodel_url.rstrip("/")
        self.ingest_url = ingest_url.rstrip("/")
        self.graph = graph
        self.tenant_id = tenant_id
        self._client: httpx.AsyncClient | None = None
        self.identity = ServiceIdentity("copilot")
        self.calls = 0
        self.failures = 0
        self.question = ""
        self._zone_ids: set[str] | None = None
        """Known zone ids, cached after the first lookup. See `_resolve_zone`."""
        """The question being answered, so a side-effecting tool can check the user actually asked for it.

        Set per request by the agent. See `run_simulation`: the tool description says "only when the user
        explicitly asks to simulate", and the eval caught the pinned model ignoring that instruction on
        exactly the question where it matters most.
        """
        self.refusals = 0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Authenticated with the copilot's own service identity.
            #
            # The tool belt calls the platform's own services, and those endpoints now require a principal.
            # The alternative to a service identity is exempting internal traffic, which is a hole that
            # grows — and it would leave every tool call unattributable in the audit trail. Every one of
            # these appears as `service:copilot`, distinguishable from a person.
            #
            # `event_hooks` rather than default headers, because the token is short-lived: fixing it at
            # client construction would pin whichever token was current when the process started, and the
            # client outlives it.
            async def attach_token(request: httpx.Request) -> None:
                request.headers["Authorization"] = f"Bearer {self.identity.token()}"

            self._client = httpx.AsyncClient(
                timeout=TOOL_TIMEOUT_S, event_hooks={"request": [attach_token]}
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ helpers
    async def _get(self, url: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        client = await self._http()
        try:
            response = await client.get(
                url, params={k: v for k, v in (params or {}).items() if v is not None}
            )
            response.raise_for_status()
            return response.json(), None
        except httpx.TimeoutException:
            # Named specifically. "Timed out" is actionable — the service is up but slow — and lumping it
            # in with a connection error would send an operator looking in the wrong place.
            return None, f"{url} did not answer within {TOOL_TIMEOUT_S:.0f}s"
        except httpx.HTTPError as exc:
            return None, f"{url} failed: {exc}"
        except json.JSONDecodeError:
            return None, f"{url} returned something that was not JSON"

    @staticmethod
    def _coerce(arguments: dict[str, Any], **casts: Callable[[Any], Any]) -> dict[str, Any]:
        """Coerce argument types at the tool boundary, where the tool knows what it wants.

        Small models routinely send `"500"` where the schema says a number. Rejecting that would be
        pedantically correct and would make the copilot look broken for a reason no user could act on, so
        the tool coerces and carries on. A value that genuinely cannot be coerced is dropped rather than
        guessed.

        It also strips **null-ish strings**, and that is not a nicety — it prevents the worst class of bug
        this product can have. Asked "what is on site right now?", the model called:

            list_entities(entity_type='null', limit='50', zone_id='null')

        The literal string `'null'`. That reached the API as `type=null`, which correctly matched no entity
        of type "null", returned an empty list, and the copilot told the operator:

            "There are no entities on site right now."

        Fifty entities were on site. A fluent, confident, false statement about the physical world is far
        worse than an error message, because there is nothing about it for the reader to distrust.

        Every one of these spellings means "no filter" and none of them means "filter for the word null".
        """
        out = {
            key: value
            for key, value in arguments.items()
            # A real JSON `null` is dropped along with its string spellings. This is not the same rule
            # restated: `_coerce` used to leave None in place, and the tool then did
            # `int(arguments.get("limit", 50))` — where `.get` with a default returns None for a key that
            # is PRESENT AND NULL, so the default never applied and `int(None)` raised. The copilot
            # answered "I could not answer that: list_entities: TypeError: int() argument must be a
            # string...", which is a stack trace shown to an operator.
            #
            # A value of None means "not provided", which is exactly what absent means. Treating them
            # identically removes the trap for every tool at once, rather than each remembering to guard.
            if value is not None
            and not (isinstance(value, str) and value.strip().lower() in NULLISH)
        }
        for key, cast in casts.items():
            if key in out:
                try:
                    out[key] = cast(out[key])
                except (TypeError, ValueError):
                    del out[key]
        return out

    async def _resolve_zone(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Normalise a zone name to a real zone id, or say the zone is not known.

        Zone ids are snake_case; a model given the prose name says "fuel store". Passing that through
        matches nothing, and an empty result then reads as "there is nothing there" — the third time the
        same shape has bitten this file, after the literal string `'null'` and a real JSON null.

        The pattern is worth stating plainly, because a fourth version of it will appear: **a filter that
        cannot match anything must never produce a confident negative.** Every route to an empty result has
        to be distinguishable from an actually-empty world.

        Two steps, in order of how much they assume. Normalising (spaces and hyphens to underscores,
        lowercased) fixes the common case with no guessing at all. Only if that still names no known zone is
        the caller told, with the real ids listed so the model can retry — which it usually does correctly.
        """
        wanted = arguments.get("zone_id")
        if not wanted or not isinstance(wanted, str):
            return arguments, None

        if self._zone_ids is None:
            zones, error = await self._get(f"{self.api_url}/api/spatial/zones", {})
            if error or not isinstance(zones, list):
                # Cannot check, so do not block: passing the value through unchanged is the behaviour that
                # existed before, and refusing to answer because the zone list is unreachable would be a
                # worse failure than an unmatched filter.
                return arguments, None
            self._zone_ids = {str(zone.get("zone_id")) for zone in zones if zone.get("zone_id")}

        normalised = wanted.strip().lower().replace(" ", "_").replace("-", "_")
        if normalised in self._zone_ids:
            return {**arguments, "zone_id": normalised}, None

        # A near match, for a plural or a partial name.
        near = [zone for zone in sorted(self._zone_ids) if normalised in zone or zone in normalised]
        if len(near) == 1:
            return {**arguments, "zone_id": near[0]}, None

        known = ", ".join(sorted(self._zone_ids)[:12])
        return arguments, (
            f"There is no zone called {wanted!r} on this site. The zones are: {known}."
            + (f" Did you mean one of {', '.join(near[:3])}?" if near else "")
        )

    async def _timed(self, name: str, coroutine: Awaitable[ToolResult]) -> ToolResult:
        started = time.perf_counter()
        self.calls += 1
        try:
            result = await coroutine
        except Exception as exc:
            self.failures += 1
            log.warning("copilot.tool_failed", tool=name, error=describe_error(exc))
            result = ToolResult(name=name, ok=False, error=f"{type(exc).__name__}: {exc}")
        result.latency_ms = (time.perf_counter() - started) * 1000
        if not result.ok:
            self.failures += 1
        return result

    # -------------------------------------------------------------------- tools
    def tools(self) -> list[Tool]:
        """Every tool, in the order the model sees them.

        Order matters more than it should for small models: the first few tools in a list are chosen
        disproportionately often, so the general-purpose ones come first and the destructive-adjacent one
        (`run_simulation`) comes last.
        """
        return [
            Tool(
                spec=ToolSpec(
                    name="list_entities",
                    description=(
                        "List entities currently on site. Use for questions like 'how many trucks are "
                        "here' or 'what is on site'. Returns id, type, label, zone and last-seen time."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "entity_type": {
                                "type": "string",
                                "description": "Filter by type: truck, person, forklift, drone, vehicle",
                            },
                            "zone_id": {"type": "string", "description": "Filter to one zone"},
                            "limit": {
                                "type": "integer",
                                "description": "Maximum results (default 50)",
                            },
                        },
                        "required": [],
                    },
                ),
                run=self.list_entities,
            ),
            Tool(
                spec=ToolSpec(
                    name="describe_entity",
                    description=(
                        "Everything known about one entity: its type, label, position, zone, dwell time, "
                        "which sensors saw it, and its recent events. Use when a specific entity is named."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "description": "The entity id"}
                        },
                        "required": ["entity_id"],
                    },
                ),
                run=self.describe_entity,
            ),
            Tool(
                spec=ToolSpec(
                    name="spatial_query",
                    description=(
                        "Spatial questions: what is within a radius of a point, what is in a zone, which "
                        "cameras cover a zone, or where the site has no camera coverage."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "enum": [
                                    "within_radius",
                                    "in_zone",
                                    "cameras_covering",
                                    "blind_spots",
                                ],
                                "description": "Which spatial question to ask",
                            },
                            "zone_id": {"type": "string"},
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                            "radius_m": {"type": "number"},
                            "entity_type": {"type": "string"},
                        },
                        "required": ["question"],
                    },
                ),
                run=self.spatial_query,
            ),
            Tool(
                spec=ToolSpec(
                    name="graph_query",
                    description=(
                        "Traverse relationships from one entity: which cameras saw it, which zones it "
                        "entered, what it is linked to. Use for 'which camera last saw X' questions."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "description": "Entity to start from"},
                            "relationship": {
                                "type": "string",
                                "description": "Edge type to follow: seen_by, entered, same_as",
                            },
                            "depth": {"type": "integer", "description": "Hops to follow (1 or 2)"},
                        },
                        "required": ["entity_id"],
                    },
                ),
                run=self.graph_query,
            ),
            Tool(
                spec=ToolSpec(
                    name="semantic_search",
                    description=(
                        "Search recorded camera frames by description, e.g. 'a truck at a loading dock' or "
                        "'smoke'. Use when the question is about what was seen rather than what was counted."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "What to look for"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                ),
                run=self.semantic_search,
            ),
            Tool(
                spec=ToolSpec(
                    name="timeseries_query",
                    description=(
                        "Historical sensor readings and forecasts: temperature, occupancy, throughput, "
                        "battery. Use for 'what will', 'trend', or 'over the last hour' questions."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "description": "temperature_c, occupancy, throughput, battery_pct, vibration_mm_s",
                            },
                            "forecast": {
                                "type": "boolean",
                                "description": "True for the forecast, false for recorded history",
                            },
                            "zone_id": {"type": "string"},
                        },
                        "required": ["metric"],
                    },
                ),
                run=self.timeseries_query,
            ),
            Tool(
                spec=ToolSpec(
                    name="timeline_replay",
                    description=(
                        "Reconstruct the site as it was at a past moment, or list what happened in a "
                        "window. Use for 'what happened at', 'earlier', or 'ten minutes ago' questions."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "minutes_ago": {
                                "type": "number",
                                "description": "How far back to reconstruct, in minutes",
                            },
                            "window_minutes": {
                                "type": "number",
                                "description": "Width of the window to list events from",
                            },
                        },
                        "required": [],
                    },
                ),
                run=self.timeline_replay,
            ),
            Tool(
                spec=ToolSpec(
                    name="propose_decision",
                    description=(
                        "Record a proposed course of action for a human to approve. Nothing is executed. "
                        "Use when the user asks what should be done about something."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "What is proposed, in one line",
                            },
                            "rationale": {"type": "string", "description": "Why"},
                            "entity_id": {"type": "string"},
                        },
                        "required": ["summary"],
                    },
                ),
                run=self.propose_decision,
            ),
            Tool(
                spec=ToolSpec(
                    name="run_simulation",
                    description=(
                        "Inject a what-if scenario into the simulated site: a fire in a zone, or a power "
                        "failure. Only use when the user explicitly asks to simulate or test something."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "scenario": {"type": "string", "enum": ["fire", "power_failure"]},
                            "zone_id": {"type": "string"},
                            "duration_s": {"type": "number"},
                        },
                        "required": ["scenario"],
                    },
                ),
                run=self.run_simulation,
            ),
        ]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools()]

    def by_name(self) -> dict[str, Tool]:
        return {tool.name: tool for tool in self.tools()}

    # --------------------------------------------------------------- executors
    async def list_entities(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = self._coerce(arguments, limit=int)
        arguments, zone_problem = await self._resolve_zone(arguments)
        if zone_problem is not None:
            # An unresolvable zone must not become an empty entity list. The third instance of this exact
            # shape: asked "are there any drones in the fuel store?", the model sent
            # `zone_id='fuel store'` — with a space — which matched no zone, and the copilot answered "No
            # drone was seen in fuel store in the last 5 minutes." That happened to be true, and would have
            # been said just as confidently with a drone parked there.
            return ToolResult(
                name="list_entities",
                ok=True,
                source=f"{self.api_url}/api/spatial/zones",
                brief={"error": zone_problem, "note": f"Say exactly: {zone_problem}"},
                data={"error": zone_problem},
            )
        data, error = await self._get(
            f"{self.api_url}/api/entities",
            {
                "type": arguments.get("entity_type"),
                "zone_id": arguments.get("zone_id"),
                "limit": min(int(arguments.get("limit", 50)), 100),
                "include_static": False,
                "active_within_s": 300,
            },
        )
        if error:
            return ToolResult(name="list_entities", ok=False, error=error, source=self.api_url)
        rows = data or []
        requested = min(int(arguments.get("limit", 50)), 100)
        # A count that hit its own limit is a floor, not a total. Saying "28 entities are on site" when the
        # query stopped at 28 is a confident false statement, and the model will repeat it verbatim — it has
        # no way to know the list was truncated unless told.
        capped = len(rows) >= requested
        return ToolResult(
            name="list_entities",
            ok=True,
            source=f"{self.api_url}/api/entities",
            evidence=[row.get("entity_id", "") for row in rows[:10]],
            brief=_entity_brief(rows, arguments, capped=capped),
            data={
                "count": len(rows),
                "by_type": _counted(row.get("type") for row in rows),
                "entities": [
                    {
                        "entity_id": row.get("entity_id"),
                        "type": row.get("type"),
                        "label": row.get("label"),
                        "zone": (row.get("state") or {}).get("zone_id"),
                        "last_seen": row.get("last_seen"),
                    }
                    for row in rows[:25]
                ],
            },
        )

    async def describe_entity(self, arguments: dict[str, Any]) -> ToolResult:
        entity_id = str(arguments.get("entity_id") or "").strip()
        if not entity_id:
            return ToolResult(name="describe_entity", ok=False, error="entity_id is required")
        data, error = await self._get(f"{self.api_url}/api/entities/{entity_id}")
        if error:
            return ToolResult(name="describe_entity", ok=False, error=error, source=self.api_url)
        events, _ = await self._get(f"{self.api_url}/api/events", {"limit": 10})
        related = [event for event in (events or []) if entity_id in (event.get("entities") or [])]
        state = (data or {}).get("state") or {}
        return ToolResult(
            name="describe_entity",
            ok=True,
            source=f"{self.api_url}/api/entities/{entity_id}",
            evidence=[entity_id, *[event.get("event_id", "") for event in related[:5]]],
            data={
                "entity_id": entity_id,
                "type": (data or {}).get("type"),
                "label": (data or {}).get("label"),
                "confidence": (data or {}).get("confidence"),
                "zone": state.get("zone_id"),
                "position": state.get("geo"),
                "first_seen": (data or {}).get("first_seen"),
                "last_seen": (data or {}).get("last_seen"),
                "sensors": sorted(
                    {entry.get("source_id", "") for entry in (data or {}).get("provenance", [])}
                ),
                "modalities": ((data or {}).get("attributes") or {}).get("modalities"),
                "recent_events": [
                    {"type": event.get("type"), "ts": event.get("ts"), "zone": event.get("zone_id")}
                    for event in related[:5]
                ],
            },
        )

    async def spatial_query(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = self._coerce(arguments, lat=float, lon=float, radius_m=float)
        question = str(arguments.get("question") or "in_zone")
        if question == "blind_spots":
            data, error = await self._get(f"{self.spatial_url}/spatial/blind_spots")
            source = "blind_spots"
        elif question == "cameras_covering":
            zone = arguments.get("zone_id") or "gate_a"
            data, error = await self._get(f"{self.spatial_url}/spatial/cameras_covering/{zone}")
            source = f"cameras_covering/{zone}"
        elif question == "within_radius":
            data, error = await self._get(
                f"{self.spatial_url}/spatial/within",
                {
                    "lat": arguments.get("lat"),
                    "lon": arguments.get("lon"),
                    "radius_m": arguments.get("radius_m", 500),
                    "entity_type": arguments.get("entity_type"),
                },
            )
            source = "within"
        else:
            zone = arguments.get("zone_id")
            if not zone:
                # A zone question with no zone: list them, which is a better answer than an error and is
                # usually what the user meant.
                data, error = await self._get(f"{self.spatial_url}/spatial/zones")
                source = "zones"
            else:
                data, error = await self._get(f"{self.spatial_url}/spatial/contains/{zone}")
                source = f"contains/{zone}"
        if error:
            return ToolResult(name="spatial_query", ok=False, error=error, source=self.spatial_url)
        return ToolResult(
            name="spatial_query",
            ok=True,
            source=f"{self.spatial_url}/spatial/{source}",
            brief=_brief_spatial(question, data),
            data=data,
        )

    async def graph_query(self, arguments: dict[str, Any]) -> ToolResult:
        """Traverse from an entity. Parameterised, never a query string — see the module docstring."""
        entity_id = str(arguments.get("entity_id") or "").strip()
        if not entity_id:
            return ToolResult(name="graph_query", ok=False, error="entity_id is required")
        if self.graph is None:
            return ToolResult(name="graph_query", ok=False, error="no graph store is configured")
        wanted = arguments.get("relationship")
        neighbours = await self.graph.neighbors(
            entity_id,
            tenant_id=self.tenant_id,
            types=[str(wanted)] if wanted else None,
        )
        edges = [
            {
                "relationship": str(relationship.type),
                "to": relationship.to_id,
                "since": relationship.ts_valid_from.isoformat()
                if relationship.ts_valid_from
                else None,
                "until": relationship.ts_valid_to.isoformat() if relationship.ts_valid_to else None,
                "still_open": relationship.ts_valid_to is None,
                "neighbour_type": str(neighbour.type) if neighbour else None,
                "neighbour_label": neighbour.label if neighbour else None,
            }
            for relationship, neighbour in neighbours
        ]
        return ToolResult(
            name="graph_query",
            ok=True,
            source="graph store (parameterised traversal, not a query string)",
            evidence=[entity_id, *[edge["to"] for edge in edges[:8] if edge["to"]]],
            brief={
                "entity_id": entity_id,
                "edge_count": len(edges),
                "most_recent": (edges[-1] if edges else None),
                "cameras": sorted(
                    {edge["to"] for edge in edges if edge["relationship"] == "seen_by"}
                )[:5],
            },
            data={
                "entity_id": entity_id,
                "edge_count": len(edges),
                "edges": edges[:20],
                # The most recent open edge answers "which camera can see it now"; the most recent closed
                # one answers "which camera last saw it". Both are asked, so both are surfaced.
                "most_recent": max(edges, key=lambda edge: edge["since"] or "", default=None),
            },
        )

    async def semantic_search(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = self._coerce(arguments, limit=int)
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(name="semantic_search", ok=False, error="query is required")
        data, error = await self._get(
            f"{self.api_url}/api/search/frames",
            {"q": query, "limit": min(int(arguments.get("limit", 5)), 10)},
        )
        if error:
            return ToolResult(name="semantic_search", ok=False, error=error, source=self.api_url)
        results = (data or {}).get("results", [])
        return ToolResult(
            name="semantic_search",
            ok=True,
            source=f"{self.api_url}/api/search/frames",
            evidence=[row.get("frame_key") or row.get("observation_id") or "" for row in results],
            data={
                "query": query,
                "matches": [
                    {
                        "score": row.get("score"),
                        "source_id": row.get("source_id"),
                        "ts": row.get("ts"),
                        "frame_key": row.get("frame_key"),
                    }
                    for row in results
                ],
            },
        )

    async def timeseries_query(self, arguments: dict[str, Any]) -> ToolResult:
        metric = str(arguments.get("metric") or "temperature_c")
        wants_forecast = bool(arguments.get("forecast", True))
        if wants_forecast:
            data, error = await self._get(
                f"{self.prediction_url}/forecasts/latest",
            )
            if error:
                return ToolResult(
                    name="timeseries_query", ok=False, error=error, source=self.prediction_url
                )
            forecasts = (data or {}).get("forecasts", {})
            wanted = {
                key: value
                for key, value in forecasts.items()
                if metric.split("_")[0] in key or key.startswith(metric)
            } or forecasts
            trimmed = {
                key: {
                    "summary": value.get("summary"),
                    "confidence": value.get("confidence"),
                    "model": value.get("model"),
                    "interval_level": value.get("interval_level"),
                    "last_point": (value.get("points") or [{}])[-1],
                }
                for key, value in list(wanted.items())[:6]
            }
            return ToolResult(
                name="timeseries_query",
                ok=True,
                source=f"{self.prediction_url}/forecasts/latest",
                data={"metric": metric, "forecasts": trimmed},
            )

        data, error = await self._get(
            f"{self.api_url}/api/measurements", {"metric": metric, "limit": 60}
        )
        if error:
            return ToolResult(name="timeseries_query", ok=False, error=error, source=self.api_url)
        rows = data if isinstance(data, list) else (data or {}).get("measurements", [])
        values = [row.get("value") for row in rows if isinstance(row.get("value"), (int, float))]
        return ToolResult(
            name="timeseries_query",
            ok=True,
            source=f"{self.api_url}/api/measurements",
            data={
                "metric": metric,
                "samples": len(values),
                # A summary rather than sixty rows: the model has a small context, and the shape of the
                # series is what the question is about.
                "latest": values[0] if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": round(sum(values) / len(values), 3) if values else None,
            },
        )

    async def timeline_replay(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = self._coerce(arguments, minutes_ago=float, window_minutes=float)
        minutes_ago = float(arguments.get("minutes_ago") or 10.0)
        window = float(arguments.get("window_minutes") or 5.0)
        from datetime import datetime, timedelta

        at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        world, world_error = await self._get(
            f"{self.api_url}/api/world/at", {"ts": at.isoformat(), "limit": 100}
        )
        events, _ = await self._get(
            f"{self.api_url}/api/timeline",
            {
                "from": (at - timedelta(minutes=window / 2)).isoformat(),
                "to": (at + timedelta(minutes=window / 2)).isoformat(),
                "limit": 25,
            },
        )
        if world_error:
            return ToolResult(
                name="timeline_replay", ok=False, error=world_error, source=self.api_url
            )
        entities = (world or {}).get("entities", [])
        return ToolResult(
            name="timeline_replay",
            ok=True,
            source=f"{self.api_url}/api/world/at",
            evidence=[event.get("event_id", "") for event in (events or [])[:8]],
            brief={
                "at": at.isoformat(),
                "minutes_ago": minutes_ago,
                "counts": (world or {}).get("counts"),
                "events": [
                    {"type": event.get("type"), "severity": event.get("severity")}
                    for event in (events or [])[:6]
                ],
            },
            data={
                "at": at.isoformat(),
                "minutes_ago": minutes_ago,
                "counts": (world or {}).get("counts"),
                "entities": [
                    {
                        "label": entity.get("label"),
                        "type": entity.get("type"),
                        "zone": (entity.get("state") or {}).get("zone_id"),
                    }
                    for entity in entities[:15]
                ],
                "events": [
                    {
                        "type": event.get("type"),
                        "severity": event.get("severity"),
                        "ts": event.get("ts"),
                        "summary": (event.get("explanation") or {}).get("summary"),
                    }
                    for event in (events or [])[:10]
                ],
            },
        )

    async def propose_decision(self, arguments: dict[str, Any]) -> ToolResult:
        """Record a proposal. Nothing is executed.

        The tool exists so a copilot can be asked "what should we do" and produce something an operator can
        act on, without the copilot itself acting. Human-on-the-loop is the whole design (M14), so this
        writes a pending proposal and says clearly that it has not been carried out.
        """
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            return ToolResult(name="propose_decision", ok=False, error="summary is required")
        client = await self._http()
        payload = {
            "summary": summary,
            "rationale": str(arguments.get("rationale") or ""),
            "entity_id": arguments.get("entity_id"),
            "proposed_by": "copilot",
        }
        try:
            response = await client.post(f"{self.api_url}/api/decisions", json=payload)
            if response.status_code == 404:
                # The decision service is a later phase. Say so plainly rather than pretending the
                # proposal was filed — a copilot that claims to have recorded something it did not is
                # worse than one that admits the gap.
                return ToolResult(
                    name="propose_decision",
                    ok=True,
                    source="not recorded",
                    data={
                        "recorded": False,
                        "proposal": payload,
                        "note": (
                            "The decision service is not available in this build, so this proposal was "
                            "NOT filed. It is reported here as advice only."
                        ),
                    },
                )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            return ToolResult(name="propose_decision", ok=False, error=f"could not record: {exc}")
        return ToolResult(
            name="propose_decision",
            ok=True,
            source=f"{self.api_url}/api/decisions",
            evidence=[str(body.get("decision_id", ""))],
            data={"recorded": True, "decision": body, "awaiting_approval": True},
        )

    #: Words that mean the user is asking for a what-if, not for advice.
    SIMULATION_INTENT = (
        "simulate",
        "simulation",
        "what if",
        "what-if",
        "test the",
        "inject",
        "drill",
        "dry run",
        "pretend",
        "scenario",
    )

    def wants_simulation(self) -> bool:
        return any(phrase in self.question.lower() for phrase in self.SIMULATION_INTENT)

    async def run_simulation(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = self._coerce(arguments, duration_s=float)
        scenario = str(arguments.get("scenario") or "").strip()

        # A GUARD IN CODE, not just in the tool description.
        #
        # The tool-calling eval caught the pinned model choosing this tool for "There is a fire at dock 3.
        # What should we do?" — asked for advice about a fire, it wanted to START one. The description
        # already said "only use when the user explicitly asks to simulate or test something"; the model
        # ignored it, and instructions a model can ignore are not controls.
        #
        # So the only side-effecting tool refuses unless the question itself asked for a what-if. A
        # refusal is returned as a *successful* result carrying an explanation, because the agent should
        # go on to answer the real question rather than treat this as an outage.
        if self.question and not self.wants_simulation():
            self.refusals += 1
            log.warning(
                "copilot.simulation_refused",
                question=self.question[:120],
                scenario=scenario,
            )
            return ToolResult(
                name="run_simulation",
                ok=True,
                source="refused",
                data={
                    "injected": False,
                    "refused": True,
                    "reason": (
                        "This tool injects an incident into the simulated site and the question did not "
                        "ask for a simulation. Nothing was injected. Ask again with the word 'simulate' "
                        "if that is what you want."
                    ),
                },
            )

        if scenario not in ("fire", "power_failure"):
            return ToolResult(
                name="run_simulation",
                ok=False,
                error="scenario must be 'fire' or 'power_failure'",
            )
        client = await self._http()
        params: dict[str, Any] = {"duration_s": arguments.get("duration_s", 300)}
        if scenario == "fire":
            params["zone_id"] = arguments.get("zone_id") or "dock_3"
        try:
            response = await client.post(
                f"{self.ingest_url}/simulation/inject/{scenario}", params=params
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            return ToolResult(name="run_simulation", ok=False, error=f"could not inject: {exc}")
        return ToolResult(
            name="run_simulation",
            ok=True,
            source=f"{self.ingest_url}/simulation/inject/{scenario}",
            data={"injected": body, "note": "This affects the SIMULATED site only."},
        )


def _brief_spatial(question: str, data: Any) -> dict[str, Any]:
    """Compact spatial results. Each question shape has a different essential answer."""
    if not isinstance(data, dict):
        return {"question": question, "result": str(data)[:200]}
    if question == "blind_spots":
        return {
            "question": question,
            "coverage_fraction": data.get("coverage_fraction"),
            "covered_m2": data.get("covered_m2"),
            "uncovered_m2": data.get("uncovered_m2"),
            "site_m2": data.get("site_m2"),
        }
    if question == "cameras_covering":
        return {
            "question": question,
            "zone_id": data.get("zone_id"),
            "cameras": [camera.get("source_id") for camera in data.get("cameras", [])],
        }
    if question == "within_radius":
        return {
            "question": question,
            "count": data.get("count"),
            "radius_m": data.get("radius_m"),
            "nearest": [row.get("label") for row in (data.get("results") or [])[:5]],
        }
    return {
        "question": question,
        "zone_id": data.get("zone_id"),
        "occupants": len(data.get("confirmed") or data.get("postgis") or []),
    }


def _entity_brief(
    rows: list[dict[str, Any]], arguments: dict[str, Any], *, capped: bool
) -> dict[str, Any]:
    """What the model is told about an entity query, phrased so it cannot overstate the result.

    Defence in depth behind the null-ish argument strip. Even with sane arguments, a legitimate filter can
    match nothing — no trucks on site, nobody in dock 3 — and "no trucks" must never become "nothing is on
    site". The model repeats what it is given, so what it is given has to carry the distinction: an empty
    result is reported together with the filter that produced it, and the note says explicitly that this is
    not a statement about the whole site.
    """
    filters = {
        key: value
        for key, value in arguments.items()
        if key in ("entity_type", "zone_id") and value is not None
    }
    brief: dict[str, Any] = {
        "count": len(rows),
        "counting": "moving entities seen in the last 5 minutes, excluding fixed infrastructure",
        "by_type": _counted(row.get("type") for row in rows),
        "examples": [row.get("label") or row.get("entity_id") for row in rows[:5]],
    }
    if capped:
        # A count that hit its own limit is a floor. The model has no way to know the list was truncated.
        brief["count_is_at_least"] = True
    if filters:
        brief["filtered_by"] = filters
    if not filters.get("zone_id"):
        # State the scope when no zone was asked for, because the model will otherwise supply one from the
        # question. Asked "what is on the helipad?", it called `list_entities(entity_type='drone')` with no
        # zone at all and answered "There are 2 drones on the helipad" — a site-wide count attributed to a
        # place it never queried.
        brief["scope"] = (
            "the WHOLE site, not any particular zone. Do not attribute this count to a location; "
            "call list_entities again with zone_id to scope it."
        )
    if not rows:
        if filters:
            # PRESCRIPTIVE, not prohibitive. The first version said "do not report it as the site being
            # empty", and the model half-complied: it named the filter correctly and then added "The site
            # is empty of moving vehicles in the last 5 minutes" — generalising a zone-scoped query to the
            # whole site in the very next sentence.
            #
            # Small models follow "say this" far more reliably than "do not say that", because a
            # prohibition still leaves them to invent the alternative. So hand them the sentence.
            scope = ", ".join(f"{key}={value}" for key, value in sorted(filters.items()))
            brief["note"] = (
                f"No match for {scope}. Answer with exactly this and nothing more: "
                f"'No {filters.get('entity_type', 'entity')} was seen"
                + (f" in {filters['zone_id']}" if filters.get("zone_id") else "")
                + " in the last 5 minutes.' This result covers ONLY that filter, so say nothing about "
                "the rest of the site."
            )
        else:
            brief["note"] = "no moving entity has been seen anywhere on site in the last 5 minutes"
    return brief


def _counted(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


__all__ = ["Tool", "ToolBelt", "ToolResult"]
