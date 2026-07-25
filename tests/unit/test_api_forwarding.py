"""Every forwarded endpoint is actually called (PRD M17).

This file exists because of a total feature outage that shipped through a green `just check`.

The API forwards seventeen routes to sibling services. A rename of one keyword argument —
`timeout` to `http_timeout_s`, to satisfy a lint — was applied to the definition and to two of the
four call sites, because the formatter had reflowed the other two onto separate lines before the
replacement ran. The result: `POST /api/copilot/ask` returned 500 with
`_forward() got an unexpected keyword argument 'timeout'`, and the entire copilot feature was dead
in the console. Nothing caught it, because a proxy that is never invoked in a test is not tested by
type checking, linting, or a schema.

So each test here **calls the route** through the ASGI app with the downstream service replaced by a
stub transport. That is the cheapest thing that exercises the whole path — the route function, the
argument names, the forwarding call, the status mapping — without needing eight services running.

The important assertion is the boring one: the route returns 200 rather than 500. A wrong keyword
argument, a renamed setting, a typo in a path — all present as a 500, and all are invisible until
something calls the route.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

#: Every forwarded route: method, path, and the downstream JSON it should hand back untouched.
FORWARDED: list[tuple[str, str, Any]] = [
    ("GET", "/api/alerts?limit=5", {"alerts": [], "groups": []}),
    ("GET", "/api/alerts/alt_123", {"alert_id": "alt_123"}),
    ("POST", "/api/alerts/alt_123/ack", {"alert_id": "alt_123", "state": "acknowledged"}),
    ("POST", "/api/alerts/alt_123/resolve", {"alert_id": "alt_123", "state": "resolved"}),
    ("POST", "/api/alerts/alt_123/escalate", {"alert_id": "alt_123", "state": "escalated"}),
    ("GET", "/api/decisions?limit=5", {"decisions": []}),
    ("GET", "/api/decisions/dec_1", {"decision_id": "dec_1"}),
    ("POST", "/api/decisions/dec_1/approve", {"decision_id": "dec_1", "approval": "approved"}),
    ("POST", "/api/decisions/dec_1/reject", {"decision_id": "dec_1", "approval": "rejected"}),
    ("GET", "/api/forecasts", {"forecasts": []}),
    ("GET", "/api/forecasts/latest", {"forecasts": {}}),
    ("GET", "/api/workflow/runs", {"runs": 0, "recent": []}),
    ("GET", "/api/workflow/playbooks", {"playbooks": []}),
    ("GET", "/api/agents", {"agents": []}),
    ("GET", "/api/agents/cycles", {"cycles": []}),
    ("GET", "/api/audit", {"entries": []}),
    # The one that broke. A generous timeout is passed here and nowhere else, which is exactly why
    # this call site had a different shape and was the one the rename missed.
    ("POST", "/api/copilot/ask", {"answer": "There are 12 vehicles on site.", "confidence": 0.8}),
]


class StubDownstream(httpx.AsyncBaseTransport):
    """Answers any request with a recorded payload, and remembers what it was asked."""

    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.payload = payload if payload is not None else {"ok": True}
        self.status = status
        self.seen: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, str(request.url)))
        return httpx.Response(
            self.status,
            content=json.dumps(self.payload).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


@pytest.fixture
def app_and_stub(monkeypatch: pytest.MonkeyPatch):
    """The real API app, with every outbound HTTP client pointed at a stub."""
    from sio_api.app import ApiService

    stub = StubDownstream()
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = stub
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    service = ApiService()
    return service.app, stub


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    FORWARDED,
    ids=[f"{m} {p.split('?')[0]}" for m, p, _ in FORWARDED],
)
def test_every_forwarded_route_reaches_its_service(
    app_and_stub, method: str, path: str, payload: Any
) -> None:
    """A 500 here means the route cannot even call its own forwarding helper.

    Which is precisely the bug this file was written for: green lint, green types, green `just check`,
    and a dead feature in the browser.
    """
    app, stub = app_and_stub
    stub.payload = payload
    with TestClient(app) as client:
        response = client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 200, (
        f"{method} {path} returned {response.status_code}: {response.text[:300]}"
    )
    assert response.json() == payload, "the proxy must not reinterpret its downstream's payload"
    assert stub.seen, f"{method} {path} never actually called a downstream service"


def test_a_downstream_404_is_not_reported_as_the_service_being_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning a 404 into a 503 would tell an operator the service is down when the id was wrong.

    Two very different remedies — restart something, or check what you typed — so the distinction has
    to survive the hop.
    """
    from sio_api.app import ApiService

    stub = StubDownstream({"detail": "unknown alert 'alt_nope'"}, status=404)
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = stub
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    with TestClient(ApiService().app) as client:
        response = client.get("/api/alerts/alt_nope")
    assert response.status_code == 404
    # And the downstream's own message survives: "unknown alert 'alt_nope'" is actionable where
    # "the alerts service returned 404" is not, though both are technically true.
    assert "alt_nope" in response.json()["detail"]


def test_an_unreachable_service_is_named_rather_than_returning_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list is indistinguishable from "nothing is happening".

    Which is the one thing an operator must not be told when a service has fallen over — they would
    conclude the site is quiet.
    """
    from sio_api.app import ApiService

    class Dead(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = Dead()
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    with TestClient(ApiService().app) as client:
        response = client.get("/api/alerts")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "alerts" in detail, "the failing service must be named"
    assert "ConnectError" in detail or "connection refused" in detail


def test_no_forward_call_uses_the_old_timeout_keyword() -> None:
    """A lint for the exact mistake, because the formatter reflowed the call sites out of reach.

    Belt and braces alongside the round-trip tests above: this one fails at the point the mistake is
    made rather than at the point it is invoked, and it names the keyword.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "services/api/src/sio_api/app.py").read_text()
    # Calls only — the definition legitimately mentions `timeout=` in a comment explaining the rename.
    calls = re.findall(r"await _forward\((?:[^()]|\([^()]*\))*\)", source)
    offenders = [call for call in calls if re.search(r"\btimeout\s*=", call)]
    assert not offenders, f"use http_timeout_s, not timeout: {offenders}"
