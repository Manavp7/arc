"""An external MCP client driving the SIO server (Tier 4 #3 acceptance).

These are real client sessions over real transports, not calls into the handler functions. That distinction
is the whole test: the failures that matter in MCP are protocol failures — a schema the client rejects, a
stray byte on stdout that corrupts framing, a session manager that was never started — and none of them are
reachable by calling a Python function directly.

The stdio test in particular exercises the constraint that catches everyone: **the host reads stdout as a
JSON-RPC stream**, so anything else written there breaks the connection while the server looks perfectly
healthy from the inside.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.infra, pytest.mark.anyio]

REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_an_external_client_lists_the_tools_over_stdio() -> None:
    """The acceptance criterion, first half: an external client can see what the server offers.

    Launched as a subprocess exactly as Cursor and Claude Desktop launch it, so a stray print to stdout or
    a logging handler pointed at the wrong stream fails this test rather than surfacing later as "the MCP
    server will not start".
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sio_mcp"],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        initialised = await session.initialize()
        assert initialised.serverInfo.name == "sio"
        assert initialised.instructions and "world model" in initialised.instructions.lower()

        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert len(names) == 9, f"expected nine tools, got {sorted(names)}"
        for required in ("graph_query", "list_entities", "spatial_query", "timeline_replay"):
            assert required in names

        # Schemas must survive the round trip: a client that cannot parse one cannot call the tool, and the
        # symptom is a tool that simply never gets used.
        for tool in listed.tools:
            assert tool.description and len(tool.description) > 40, (
                f"{tool.name} has a thin description"
            )
            assert tool.inputSchema.get("type") == "object", f"{tool.name} has a bad schema"
            assert "properties" in tool.inputSchema

        # Only the side-effecting tool is marked as writing.
        by_name = {tool.name: tool for tool in listed.tools}
        assert by_name["run_simulation"].annotations.readOnlyHint is False
        assert by_name["list_entities"].annotations.readOnlyHint is True


async def test_an_external_client_executes_graph_query_over_stdio() -> None:
    """The acceptance criterion, second half: `graph_query` executed from an external client.

    Uses a deliberately unknown entity id, because what is being tested is the protocol path — arguments in,
    structured content out — not whether this particular database happens to hold that entity. An empty
    traversal is a valid answer; a protocol error is not.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sio_mcp"],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("graph_query", {"entity_id": "ent_mcp_probe"})

        assert not result.isError, f"the call failed: {result.content}"
        assert result.content, "a tool call must return content"
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert payload["tool"] == "graph_query"
        assert payload["data"]["entity_id"] == "ent_mcp_probe"
        assert "edge_count" in payload["data"]
        # The source is named so a client can attribute the answer, and it says plainly that no query
        # string was involved.
        assert "parameterised" in payload["source"]


async def test_an_unknown_tool_is_a_protocol_error_not_a_text_reply() -> None:
    """A client asking for a tool that does not exist must be told so in the protocol.

    Answering in prose would leave it to guess whether the call succeeded — and a model reading "unknown
    tool" as tool *output* will happily narrate around it.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "sio_mcp"], cwd=str(REPO_ROOT)
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("delete_everything", {})
        assert result.isError, "an unknown tool must be reported as an error"
        assert "unknown tool" in str(result.content).lower()


async def test_the_mcp_tool_list_matches_the_copilots() -> None:
    """One tool registry, two protocols.

    The failure mode is silent: someone adds a tool to the copilot, forgets the MCP server, and an external
    client is quietly working from a stale capability list. Nothing errors — the client just cannot do
    something it should be able to.
    """
    from sio_copilot.tools import ToolBelt
    from sio_mcp import build_server

    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    server = build_server(belt)
    handler = server.request_handlers
    from mcp import types

    listed = await handler[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
    exposed = {tool.name for tool in listed.root.tools}
    assert exposed == {tool.name for tool in belt.tools()}


async def test_an_external_client_works_over_streamable_http() -> None:
    """The other transport, exercised as a client rather than asserted about.

    Streamable HTTP is what a deployed or shared server speaks. It has its own failure mode that stdio does
    not: the session manager has to be *running* before a request arrives, and mounting the app without
    starting it leaves a route that exists and rejects everything. That trap is only reachable by connecting.
    """
    import asyncio
    import socket

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    port = free_port()
    # An async subprocess, not Popen: blocking process creation inside a coroutine stalls the event loop
    # the client is about to use, which is a real problem and not merely a lint.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sio_mcp",
        "--http",
        "--port",
        str(port),
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # Wait for the port rather than sleeping a guess: a fixed sleep is either flaky or slow.
        for _ in range(80):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.25)
        else:
            pytest.fail("the HTTP server never opened its port")

        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.serverInfo.name == "sio"
                listed = await session.list_tools()
                assert len(listed.tools) == 9

                result = await session.call_tool("graph_query", {"entity_id": "ent_http_probe"})
                assert not result.isError
                payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
                assert payload["data"]["entity_id"] == "ent_http_probe"
    finally:
        process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)
