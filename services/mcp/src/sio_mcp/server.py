"""MCP server: the copilot's tools, exposed to any external client (Tier 4 #3).

This service is short, and that is the point. The copilot was built as a *client* of the platform rather
than a privileged insider, so its `ToolBelt` is already the right shape to hand to someone else: JSON
Schema in, JSON out, no shared state, no direct database reach. Exposing it over MCP is an adapter, not a
second implementation — which is exactly what that decision was for.

**One tool registry, two protocols.** A test asserts the MCP tool list matches the copilot's, because the
failure mode here is silent: someone adds a tool to the copilot, forgets the MCP server, and an external
client is quietly working with a stale capability list. Nothing errors, the client just cannot do
something it should be able to.

Two transports, both real:

* **stdio** — how Cursor and Claude Desktop actually connect. They launch the server as a subprocess and
  speak over its stdin and stdout, which means **nothing may be written to stdout that is not protocol**.
  That constraint drives the logging setup below and is the single most common way an MCP server appears
  broken while working perfectly.
* **streamable-http** — for anything deployed or shared. The endpoint lives at `/mcp`. SSE is deliberately
  not offered: it was superseded in the 2025-03-26 protocol revision, and a new server built on it starts
  life deprecated.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from sio_copilot.tools import ToolBelt

from sio_core import get_graph, get_logger, get_settings
from sio_core.config import Settings

log = get_logger("sio.mcp")

SERVER_NAME = "sio"
INSTRUCTIONS = """Spatial Intelligence OS: a live world model of a monitored logistics yard, built from \
cameras, GPS trackers and IoT sensors.

Use `list_entities` for what is on site now, `timeline_replay` to reconstruct a past moment, \
`spatial_query` for coverage and zone questions, `graph_query` to follow relationships from an entity, \
`semantic_search` to find recorded camera frames by description, and `timeseries_query` for readings and \
forecasts.

Everything returned is evidence from the world model. `run_simulation` affects only the simulated site, \
and `propose_decision` records a proposal for a human to approve rather than acting."""


def build_belt(settings: Settings | None = None) -> ToolBelt:
    """The same tool belt the copilot uses, pointed at the same services."""
    settings = settings or get_settings()
    return ToolBelt(
        api_url=f"http://127.0.0.1:{settings.api_port}",
        spatial_url=f"http://127.0.0.1:{settings.spatial_port}",
        prediction_url=f"http://127.0.0.1:{settings.prediction_port}",
        worldmodel_url=f"http://127.0.0.1:{settings.worldmodel_port}",
        ingest_url=f"http://127.0.0.1:{settings.ingest_port}",
        graph=get_graph(settings),
        tenant_id=settings.tenant_id,
    )


def build_server(belt: ToolBelt) -> Server:
    """An MCP server over the copilot's tools."""
    server: Server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        # The specs come straight from the belt, so the two protocols cannot describe different tools.
        return [
            types.Tool(
                name=tool.spec.name,
                description=tool.spec.description,
                inputSchema=tool.spec.parameters,
                annotations=types.ToolAnnotations(
                    # Honest annotations. `run_simulation` is the only tool that changes anything, and a
                    # client that respects these will prompt before calling it; one that does not at least
                    # has no excuse.
                    readOnlyHint=tool.spec.name != "run_simulation",
                    destructiveHint=False,
                    idempotentHint=tool.spec.name != "run_simulation",
                    openWorldHint=True,
                ),
            )
            for tool in belt.tools()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
        tools = belt.by_name()
        tool = tools.get(name)
        if tool is None:
            # A protocol-level error, not a text reply: the client asked for something that does not
            # exist, and telling it so in prose would leave it to guess whether the call succeeded.
            raise ValueError(f"unknown tool {name!r}; available: {', '.join(sorted(tools))}")

        # An external client is not the copilot's agent, so it never set the belt's question. Side-effecting
        # tools check that question for intent, and an empty one means the guard cannot judge — so it is
        # set explicitly to the call itself, which reads as consent: an external client calling
        # `run_simulation` by name has asked for it far more clearly than a model choosing it.
        belt.question = f"mcp client called {name} with {json.dumps(arguments, default=str)}"

        result = await belt._timed(name, tool.run(arguments))
        log.info(
            "mcp.tool_called",
            tool=name,
            ok=result.ok,
            latency_ms=round(result.latency_ms, 1),
            source=result.source,
        )
        if not result.ok:
            raise ValueError(f"{name} failed: {result.error}")

        # The FULL data, not the brief view. The brief exists to fit a 1.5-3 B model's context; an external
        # client is more likely to be a frontier model or a program, and truncating for someone else's
        # constraint would be the wrong default.
        payload = {
            "tool": name,
            "source": result.source,
            "latency_ms": round(result.latency_ms, 1),
            "evidence": result.evidence,
            "data": result.data,
        }
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    return server


async def run_stdio(belt: ToolBelt) -> None:
    """Serve over stdio, the transport desktop clients use.

    **Nothing may be written to stdout that is not protocol.** The host reads stdout as a JSON-RPC stream,
    so a single stray print — or a logging handler defaulting to stdout — corrupts the framing and the
    client reports a server that will not start. This is the most common way an MCP server appears broken
    while being entirely correct, so logging is forced to stderr before the transport opens.
    """
    import logging

    from mcp.server.stdio import stdio_server

    root = logging.getLogger()
    for handler in list(root.handlers):
        stream = getattr(handler, "stream", None)
        if stream is sys.stdout:
            root.removeHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))

    server = build_server(belt)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def http_app(belt: ToolBelt, *, stateless: bool = True) -> Any:
    """A Starlette app serving MCP over streamable HTTP at ``/mcp``.

    Stateless by default: each request stands alone, which is what survives a restart and a load balancer.
    Session tracking buys resumable streams, and for a tool server whose calls are short reads that is a
    cost without a matching benefit.
    """
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.types import Receive, Scope, Send

    server = build_server(belt)
    manager = StreamableHTTPSessionManager(app=server, event_store=None, stateless=stateless)

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        # The session manager must be running before a request arrives. Mounting the app without this is
        # the documented trap: the route exists, and every call fails.
        async with manager.run():
            log.info("mcp.http_ready", stateless=stateless, path="/mcp")
            yield

    async def health(request: Any) -> Any:
        """Health for the supervisor and for anything watching a deployment.

        Reports the tools it is exposing and whether the platform behind them is reachable, because "the
        MCP server is up" is not the useful question — a server listing nine tools whose services are all
        down is up and useless.
        """
        from starlette.responses import JSONResponse

        reachable = False
        with contextlib.suppress(Exception):
            client = await belt._http()
            response = await client.get(f"{belt.api_url}/health", timeout=2.0)
            reachable = response.status_code == 200
        return JSONResponse(
            {
                "service": "mcp",
                "status": "ok" if reachable else "degraded",
                "transport": "streamable-http",
                "endpoint": "/mcp",
                "stateless": stateless,
                "tools": [tool.name for tool in belt.tools()],
                "platform_reachable": reachable,
                "checks": {
                    "api": "ok" if reachable else "unreachable: tools will fail",
                },
            }
        )

    return Starlette(
        routes=[Route("/health", health), Mount("/mcp", app=handle)],
        lifespan=lifespan,
    )


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "build_belt", "build_server", "http_app", "run_stdio"]
