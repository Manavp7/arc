"""Entry point for the SIO MCP server.

Two transports, chosen by argument rather than by config, because the two callers are different kinds of
thing: a desktop client *launches* this file and speaks over the pipe, while a deployment runs it as a
long-lived HTTP service. A config file cannot know which one is happening.

    uv run python -m sio_mcp              # stdio: what Cursor and Claude Desktop launch
    uv run python -m sio_mcp --http       # streamable HTTP on the configured port, endpoint /mcp
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sio_core import get_settings

from .server import build_belt, http_app, run_stdio


def main() -> int:
    parser = argparse.ArgumentParser(description="SIO MCP server")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--stateful", action="store_true", help="track sessions (default is stateless)"
    )
    arguments = parser.parse_args()

    settings = get_settings()
    belt = build_belt(settings)

    if not arguments.http:
        # stdio. Note that NOTHING may be printed to stdout from here on — see run_stdio.
        asyncio.run(run_stdio(belt))
        return 0

    import uvicorn

    port = arguments.port or settings.mcp_port
    app = http_app(belt, stateless=not arguments.stateful)
    print(f"SIO MCP server on http://{arguments.host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=arguments.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
