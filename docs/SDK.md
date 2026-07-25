# Driving SIO from an external client

The platform exposes its nine world-model tools over the **Model Context Protocol**, so Cursor, Claude
Desktop or any MCP client can query the live world model directly — the same tools the built-in copilot
uses, over the same code path.

## Which transport

| transport | when | endpoint |
|---|---|---|
| **stdio** | a desktop client launches the server itself | — (a subprocess pipe) |
| **streamable HTTP** | deployed, shared, or multi-client | `http://host:8112/mcp` |
| ~~SSE~~ | never for new work | superseded in the 2025-03-26 protocol revision |

**Claude Desktop's config file validates stdio entries only.** Put a `url` field in
`claude_desktop_config.json` and it silently drops the whole `mcpServers` block — no error, the server
simply never appears. Remote servers go in through the Connectors UI or an `mcp-remote` stdio bridge. That
single mismatch is the entire reason a working HTTP MCP server "will not connect".

## Cursor

`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project:

```json
{
  "mcpServers": {
    "sio": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/sio", "python", "-m", "sio_mcp"],
      "env": {
        "SIO_TENANT_ID": "default"
      }
    }
  }
}
```

`--directory` has to be absolute. The client launches this from its own working directory, not yours.

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "sio": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/sio", "python", "-m", "sio_mcp"]
    }
  }
}
```

## Streamable HTTP

```bash
uv run python -m sio_mcp --http --port 8112     # endpoint at /mcp
```

Stateless by default — each request stands alone, which is what survives a restart and a load balancer.
Pass `--stateful` for session tracking and resumable streams; a tool server whose calls are short reads
does not need them.

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://127.0.0.1:8112/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("list_entities", {"entity_type": "truck"})
```

## The tools

| tool | answers |
|---|---|
| `list_entities` | what is on site now, optionally filtered by type or zone |
| `describe_entity` | everything about one entity: position, zone, dwell, sensors, recent events |
| `spatial_query` | within a radius, inside a zone, cameras covering a zone, blind spots |
| `graph_query` | follow relationships from an entity — which camera saw it, which zones it entered |
| `semantic_search` | recorded camera frames by description ("a truck at a loading dock") |
| `timeseries_query` | sensor history and forecasts with prediction intervals |
| `timeline_replay` | reconstruct the site as it was at a past moment |
| `propose_decision` | record a proposed action for a human to approve — **executes nothing** |
| `run_simulation` | inject a what-if into the **simulated** site (fire, power failure) |

Only `run_simulation` is annotated as writing (`readOnlyHint: false`); everything else is a read. A client
that respects annotations will prompt before calling it.

## Two decisions worth knowing about

**`graph_query` takes no query string.** The obvious design accepts Cypher and is a mistake dressed as
flexibility: a language model composing graph queries against a live world model is an injection vector
with extra steps, `DETACH DELETE` is three tokens from a legitimate traversal, and the model has no idea
which of its outputs are destructive. So the tool takes a starting entity, relationship types and a depth,
and builds the traversal itself. **The caller chooses what to ask; it never chooses how to execute.**

**`propose_decision` does not act.** Human-on-the-loop is the design, not a setting. It records a proposal
and says plainly that nothing was carried out. If the decision service is not running it says *that*, too,
rather than reporting a proposal it did not file.

## What the server needs running

The tools are HTTP clients of the platform, so the services they read must be up:

| tool | needs |
|---|---|
| `list_entities`, `describe_entity`, `timeline_replay`, `semantic_search` | `api` |
| `spatial_query` | `spatial` |
| `timeseries_query` | `prediction` (forecasts) or `api` (history) |
| `graph_query` | Neo4j or the Postgres graph adapter |
| `run_simulation` | `ingest` |

```bash
just services && just dev      # or the supervisor's e2e profile
```

A tool whose service is down returns a named failure ("spatial did not answer within 6s") rather than
hanging: the timeout is deliberate, because a copilot that answers in ten seconds cannot afford a tool that
waits thirty.

## Troubleshooting

**The server does not appear in the client.** Almost always the config file, not the server. Check
`command` resolves on the client's `PATH` (use an absolute `uv` path if unsure) and that `--directory` is
absolute. Then run `uv run python -m sio_mcp` in a terminal: it should sit there silently waiting for
JSON-RPC on stdin. Any banner on **stdout** would corrupt the protocol — logging is forced to stderr for
exactly this reason, and it is the most common way an MCP server appears broken while being entirely
correct.

**Tools list but every call fails.** The platform is not running. `curl localhost:8000/health`.

**HTTP connects, then rejects everything.** The session manager was not started. If you are mounting
`http_app()` into another ASGI app yourself, its lifespan must run — `Mount` alone gives you a route that
exists and refuses.
