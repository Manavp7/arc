# mcp (Tier 4 #3)

The copilot's nine tools, exposed to any external MCP client.

```
Cursor / Claude Desktop ──stdio──┐
                                 ├──► sio_mcp ──► the same ToolBelt ──► platform HTTP APIs
any client ──streamable HTTP─────┘
```

## This service is short, and that is the point

The copilot was deliberately built as a **client** of the platform rather than a privileged insider, so its
`ToolBelt` was already the right shape to hand to someone else: JSON Schema in, JSON out, no shared state,
no direct database reach. Exposing it over MCP is an adapter, not a second implementation — which is exactly
what that decision bought.

**One tool registry, two protocols.** A test asserts the MCP tool list equals the copilot's, because the
failure here is silent: someone adds a tool to the copilot, forgets this server, and an external client is
quietly working from a stale capability list. Nothing errors; the client just cannot do something it should
be able to.

## Two transports, both real

**stdio** is how Cursor and Claude Desktop actually connect — they launch this as a subprocess and speak
over its stdin and stdout. Which means **nothing may be written to stdout that is not protocol.** A single
stray print, or a logging handler defaulting to stdout, corrupts the JSON-RPC framing and the client reports
a server that will not start. Logging is forced to stderr before the transport opens, and this is the most
common way an MCP server appears broken while being entirely correct.

**Streamable HTTP** serves anything deployed, at `/mcp`, stateless by default — each request stands alone,
which is what survives a restart and a load balancer. Session tracking buys resumable streams, and a tool
server whose calls are short reads does not need them.

SSE is deliberately not offered. It was superseded in the 2025-03-26 protocol revision, and a new server
built on it starts life deprecated.

## Differences from the copilot's own use of these tools

**The full data, not the brief view.** The copilot's tools return a compact summary to the model, because a
1.5–3 B model's context is small and prompt size is latency. An external client is more likely to be a
frontier model or a program, so truncating for someone else's constraint would be the wrong default.

**An unknown tool is a protocol error, not a text reply.** Answering in prose would leave the client to
guess whether the call succeeded, and a model reading "unknown tool" as tool *output* will narrate around
it.

**The simulation guard reads differently here.** `run_simulation` refuses unless the request shows explicit
what-if intent — that guard exists because a small model, asked what to do about a fire, chose to start one.
An external client calling `run_simulation` **by name** has asked for it far more clearly than a model
choosing it from a list, so the call itself is recorded as the intent. Only `run_simulation` is annotated
`readOnlyHint: false`; a client that respects annotations will prompt first.

## Running it

```bash
uv run python -m sio_mcp                      # stdio, for a desktop client
uv run python -m sio_mcp --http --port 8112   # streamable HTTP at /mcp
```

Client configuration snippets, the tool table, and troubleshooting live in
[`docs/SDK.md`](../../docs/SDK.md).

## Tests

`tests/integration/test_mcp_client.py` drives **real client sessions over both transports** rather than
calling the handlers. That distinction is the test: the failures that matter in MCP are protocol failures — a
schema the client rejects, a stray byte on stdout, a session manager that was never started — and none of
them is reachable by calling a Python function directly.
