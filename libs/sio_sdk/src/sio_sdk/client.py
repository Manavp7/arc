"""A typed client for the SIO API (PRD M22, Phase 6).

An SDK earns its place by absorbing the parts that are tedious and easy to get wrong, not by wrapping `httpx` in
a class. Four of those, and each is a mistake I made while building this platform's own clients:

**Tokens.** Every endpoint needs one, they expire, and a 401 mid-session is not a programming error — it is
Tuesday. The client obtains one, reuses it, and renews on a 401 exactly once. A retry loop would turn a
misconfigured secret into a request storm against the token endpoint, which the console's first version did.

**Typed returns.** `client.entities()` returns `list[Entity]`, not `list[dict]`. A dict-returning client pushes
`row["state"]["geo"]["lat"]` into every caller, and a renamed field then fails at the point of use with a
`KeyError` rather than at the boundary. This platform hit the dict version of that problem twice — a hand-written
TypeScript type that described what its author believed, and a copilot tool reading `detection.frame_id` which
does not exist.

**Streaming.** SSE is easy to get almost right. `EventSource` semantics — named events, reconnection, the retry
field — are a specification most hand-rolled readers implement half of. `subscribe()` is an async iterator that
reconnects with backoff and yields parsed models.

**Errors that say what to do.** The API returns a reason for every denial; an SDK that raises
`HTTPError: 403` throws it away. `SioApiError` carries the detail, the action and the rule, so a
`PermissionError` tells the caller which role they need.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

DEFAULT_URL = "http://127.0.0.1:8000"

#: Renew a token this long before it expires, so a request never starts with one that dies mid-flight.
RENEW_MARGIN_S = 120

Model = TypeVar("Model", bound=BaseModel)


class SioError(Exception):
    """Base for everything this client raises."""


class SioApiError(SioError):
    """An error response, with the API's own explanation preserved.

    The API returns a reason for every denial — "decision.approve needs one of: admin, commander; you have
    operator" — and a client that raises `HTTPError: 403` discards the only useful part. Carrying it means a
    caller can print something actionable instead of a status code.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        url: str = "",
        action: str | None = None,
        rule: str | None = None,
    ) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.url = url
        self.action = action
        self.rule = rule

    @property
    def is_permission_error(self) -> bool:
        return self.status == 403

    @property
    def is_auth_error(self) -> bool:
        return self.status == 401


@dataclass
class Session:
    token: str = ""
    expires_at: float = 0.0
    subject: str = ""
    tenant: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return bool(self.token) and time.time() < self.expires_at - RENEW_MARGIN_S


class SioClient:
    """An async client for one SIO deployment.

    Async because the interesting operations are streaming and concurrent, and a sync wrapper over an async core
    is easy to add while the reverse is not. `SyncSioClient` below is that wrapper, for a script that wants four
    lines rather than an event loop.

    Usage::

        async with SioClient() as sio:
            for entity in await sio.entities(limit=10):
                print(entity.label, entity.state.zone_id)

            answer = await sio.ask("What is on site right now?")
            print(answer.text)

            async for message in sio.subscribe("events"):
                print(message.kind, message.payload)
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        token: str | None = None,
        subject: str = "sdk",
        roles: tuple[str, ...] = ("operator",),
        clearance: int = 1,
        timeout_s: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.subject = subject
        self.roles = roles
        self.clearance = clearance
        self._session = Session(token=token or "", expires_at=float("inf") if token else 0.0)
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._minting: asyncio.Lock = asyncio.Lock()

    async def __aenter__(self) -> SioClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------- auth
    async def authenticate(self) -> Session:
        """Obtain a token, reusing a valid one.

        Guarded by a lock, so a caller firing five concurrent requests on a cold client mints one token rather
        than five — which is both wasteful and makes the audit trail read as five sign-ins.
        """
        if self._session.valid:
            return self._session
        async with self._minting:
            if self._session.valid:
                return self._session
            response = await self._client.post(
                f"{self.url}/auth/dev/token",
                params={
                    "subject": self.subject,
                    "roles": ",".join(self.roles),
                    "clearance": self.clearance,
                },
            )
            if response.status_code != 200:
                raise SioApiError(
                    response.status_code,
                    "could not obtain a token. In a Keycloak deployment the dev issuer is disabled by "
                    "design — pass token=... instead.",
                    url=str(response.url),
                )
            body = response.json()
            claims = _claims_of(body["access_token"])
            self._session = Session(
                token=body["access_token"],
                expires_at=float(claims.get("exp", time.time() + 3600)),
                subject=str(claims.get("sub", self.subject)),
                tenant=str(claims.get("tenant", "")),
                roles=tuple(claims.get("roles", ())),
            )
            return self._session

    async def _headers(self) -> dict[str, str]:
        session = await self.authenticate()
        return {"Authorization": f"Bearer {session.token}"}

    # ------------------------------------------------------------------ requests
    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None
    ) -> Any:
        """One request, authenticated, with a single retry on 401.

        Once, not in a loop: a misconfigured secret would otherwise become a request storm against the token
        endpoint. The console's first version did exactly that.
        """
        for attempt in (1, 2):
            response = await self._client.request(
                method,
                f"{self.url}{path}",
                params={key: value for key, value in (params or {}).items() if value is not None},
                json=json_body,
                headers=await self._headers(),
            )
            if response.status_code == 401 and attempt == 1:
                self._session = Session()
                continue
            if response.status_code >= 400:
                raise _error_from(response)
            if "json" in response.headers.get("content-type", ""):
                return response.json()
            return response.text
        raise SioError("unreachable")

    async def _models(self, model: type[Model], path: str, **params: Any) -> list[Model]:
        payload = await self.request("GET", path, params=params)
        rows = payload if isinstance(payload, list) else payload.get(_collection_key(payload), [])
        return [model.model_validate(row) for row in rows]

    # ------------------------------------------------------------------- world
    async def entities(
        self,
        *,
        entity_type: str | None = None,
        zone_id: str | None = None,
        active_within_s: float | None = 300,
        include_static: bool = False,
        limit: int = 100,
    ) -> list[Any]:
        """Entities on site, newest observation first.

        `active_within_s` defaults to five minutes and `include_static` to False, which together mean "things
        that are here and moving" — the question a caller almost always means. This platform deletes nothing, so
        an unfiltered query legitimately returns every entity that has ever existed, and an SDK whose default is
        "all of history" produces a first experience of confusing volume.
        """
        from sio_schemas import Entity

        return await self._models(
            Entity,
            "/api/entities",
            type=entity_type,
            zone_id=zone_id,
            active_within_s=active_within_s,
            include_static=include_static,
            limit=limit,
        )

    async def entity(self, entity_id: str) -> Any:
        from sio_schemas import Entity

        return Entity.model_validate(await self.request("GET", f"/api/entities/{entity_id}"))

    async def events(self, *, limit: int = 50, event_type: str | None = None) -> list[Any]:
        from sio_schemas import Event

        return await self._models(Event, "/api/events", limit=limit, type=event_type)

    async def alerts(self, *, state: str | None = None, limit: int = 50) -> list[Any]:
        from sio_schemas import Alert

        payload = await self.request(
            "GET", "/api/alerts", params={"state": state, "limit": limit, "grouped": False}
        )
        return [Alert.model_validate(row) for row in payload.get("alerts", [])]

    async def zones(self) -> list[dict[str, Any]]:
        return list(await self.request("GET", "/api/spatial/zones"))

    # --------------------------------------------------------------- reasoning
    async def ask(self, question: str) -> CopilotAnswer:
        """Ask the copilot. Slow by nature — a local model takes seconds.

        Returns the trace as well as the text, because an answer whose provenance you cannot inspect is one you
        should not act on, and the tool calls are the provenance.
        """
        payload = await self.request("POST", "/api/copilot/ask", json_body={"question": question})
        return CopilotAnswer(
            question=question,
            text=str(payload.get("answer", "")),
            confidence=float(payload.get("confidence", 0.0)),
            explanation=payload.get("explanation") or {},
            trace=payload.get("trace") or {},
            redaction=payload.get("redaction"),
        )

    async def simulate(self, scenario: str, **params: Any) -> dict[str, Any]:
        """Project a what-if. Changes nothing on the site.

        Named `simulate` rather than `run_simulation` to match the copilot tool that projects rather than the one
        that injects — the distinction cost this platform a real bug, where a tool called `run_simulation` was
        the one that started fires.
        """
        return dict(
            await self.request(
                "POST", "/api/simulations", json_body={"scenario": scenario, "params": params}
            )
        )

    async def forecasts(self) -> dict[str, Any]:
        return dict(await self.request("GET", "/api/forecasts/latest"))

    async def analytics(self, *, hours: int = 24) -> dict[str, Any]:
        return dict(await self.request("GET", "/api/analytics/summary", params={"hours": hours}))

    async def decisions(self, *, approval: str | None = "pending", limit: int = 20) -> list[Any]:
        from sio_schemas import Decision

        payload = await self.request(
            "GET", "/api/decisions", params={"approval": approval, "limit": limit}
        )
        return [Decision.model_validate(row) for row in payload.get("decisions", [])]

    async def approve(self, decision_id: str, *, option_id: str | None = None) -> dict[str, Any]:
        """Approve a recommendation, which is what authorises the platform to act.

        Exposed on the SDK deliberately, and gated by the same policy as the UI: `decision.approve` needs a
        commander. A client constructed with the default `("operator",)` gets a 403 with the reason, which is the
        correct outcome — the gate is not a UI affordance.
        """
        return dict(
            await self.request(
                "POST",
                f"/api/decisions/{decision_id}/approve",
                json_body={"option_id": option_id, "approved_by": self.subject},
            )
        )

    # --------------------------------------------------------------- streaming
    async def subscribe(
        self, *topics: str, reconnect: bool = True, max_backoff_s: float = 10.0
    ) -> AsyncIterator[StreamMessage]:
        """Live messages, as an async iterator.

        Reconnects with backoff by default. SSE is easy to get almost right — named events, reconnection, the
        `retry` field — and a hand-rolled reader usually implements half the specification. In particular:
        **`EventSource.onmessage` fires only for frames with no `event:` name**, so a reader that handles only
        unnamed frames receives nothing from a server that names them. This platform's own console shipped that
        bug and presented as a live map that never updated.
        """
        attempt = 0
        while True:
            try:
                async for message in self._stream_once(topics):
                    attempt = 0
                    yield message
            except (httpx.HTTPError, SioApiError):
                if not reconnect:
                    raise
                attempt += 1
                delay = min(max_backoff_s, 0.5 * 2**attempt)
                await asyncio.sleep(delay)
                continue
            if not reconnect:
                return

    async def _stream_once(self, topics: tuple[str, ...]) -> AsyncIterator[StreamMessage]:
        params = {"topics": ",".join(topics)} if topics else {}
        async with self._client.stream(
            "GET",
            f"{self.url}/stream",
            params=params,
            headers=await self._headers(),
            timeout=httpx.Timeout(None, connect=10.0),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error_from(response)
            name = ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    # Named frames, which a reader handling only `data:` would silently drop.
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        # One malformed frame must not tear down the stream: the next one is probably fine, and
                        # a caller iterating a live feed cannot recover from an exception mid-loop.
                        continue
                    yield StreamMessage(
                        kind=str(payload.get("kind") or name or "unknown"),
                        payload=payload.get("payload", payload),
                        raw=payload,
                    )
                elif not line:
                    name = ""


@dataclass
class StreamMessage:
    kind: str
    payload: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    def as_model(self, model: type[Model]) -> Model:
        return model.model_validate(self.payload)


@dataclass
class CopilotAnswer:
    question: str
    text: str
    confidence: float
    explanation: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    redaction: str | None = None

    @property
    def tools_used(self) -> list[str]:
        return list(self.trace.get("tools_used", []))

    @property
    def was_redacted(self) -> bool:
        return self.redaction is not None

    def __str__(self) -> str:
        return self.text


# ------------------------------------------------------------------------ sync
class SyncSioClient:
    """A blocking wrapper, for a script that wants four lines rather than an event loop.

    Every method mirrors `SioClient`, minus `subscribe` — a blocking infinite iterator in a script is a trap, and
    somebody who wants a live feed is better served by writing the four lines of `asyncio.run`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async def call() -> Any:
            async with SioClient(*self._args, **self._kwargs) as client:
                return await getattr(client, method)(*args, **kwargs)

        return asyncio.run(call())

    def entities(self, **kwargs: Any) -> list[Any]:
        return self._run("entities", **kwargs)

    def events(self, **kwargs: Any) -> list[Any]:
        return self._run("events", **kwargs)

    def alerts(self, **kwargs: Any) -> list[Any]:
        return self._run("alerts", **kwargs)

    def zones(self) -> list[dict[str, Any]]:
        return self._run("zones")

    def ask(self, question: str) -> CopilotAnswer:
        return self._run("ask", question)

    def simulate(self, scenario: str, **params: Any) -> dict[str, Any]:
        return self._run("simulate", scenario, **params)

    def analytics(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("analytics", **kwargs)

    def forecasts(self) -> dict[str, Any]:
        return self._run("forecasts")


# --------------------------------------------------------------------- helpers
def _error_from(response: httpx.Response) -> SioApiError:
    detail = response.reason_phrase
    action = rule = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail", detail))
            action = body.get("action")
            rule = body.get("rule")
    except Exception:
        pass
    return SioApiError(
        response.status_code, detail, url=str(response.url), action=action, rule=rule
    )


def _claims_of(token: str) -> dict[str, Any]:
    """Read a JWT's claims WITHOUT verifying it.

    Safe here and worth being explicit about: the client is reading its own freshly issued token to find out when
    to renew it. It is not making a trust decision — the server verifies, always. A client that verified its own
    token would be checking its own homework.
    """
    import base64

    try:
        body = token.split(".")[1]
        padded = body + "=" * (-len(body) % 4)
        return dict(json.loads(base64.urlsafe_b64decode(padded)))
    except Exception:
        return {}


def _collection_key(payload: dict[str, Any]) -> str:
    """The list inside a wrapped response.

    The API wraps some collections (`{"alerts": [...]}`) and returns others bare. Rather than a table mapping
    endpoints to keys — which would go stale — this finds the single list-valued key.
    """
    lists = [key for key, value in payload.items() if isinstance(value, list)]
    return lists[0] if len(lists) == 1 else "items"


__all__ = [
    "DEFAULT_URL",
    "CopilotAnswer",
    "Session",
    "SioApiError",
    "SioClient",
    "SioError",
    "StreamMessage",
    "SyncSioClient",
]
