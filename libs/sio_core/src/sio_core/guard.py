"""The middleware that makes governance unavoidable (PRD M18, Phase 5).

`authn.py` says who is asking and `authz.py` says what they may do. This is the part that means a route
**cannot forget to use them**, which was the actual gap: the platform had a `PolicyDenied` exception, a
`tenant_id` on every table and a `current_tenant()` helper, and nothing populating any of it.

Three properties, each chosen because its absence is a silent failure:

**A principal is established before any route runs.** Authentication is middleware, not something a handler
does, so there is no handler that can omit it.

**The tenant comes from the token and is bound to the context.** Every store method already takes a
`tenant_id` and every query already filters on it — they were simply all being handed the configured
default. Binding the request's tenant to a contextvar means the existing filters start doing their job
without a single query changing.

**Action names are derived from the route, not written by hand.** A hand-written `require("alerts.write")` on
each handler is a list that will be incomplete, and the missing entries are unenforced endpoints that look
enforced. `action_for` maps method plus path to an action, so a new route is governed by default and
`test_governance.py` can assert that every route resolves to a real policy action.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .authn import (
    ANONYMOUS,
    PUBLIC_PATHS,
    Authenticator,
    DevJwtAuth,
    Principal,
    build_authenticator,
)
from .authz import Decision, authorise
from .config import Settings, get_settings
from .errors import PolicyDenied
from .telemetry import get_logger, set_tenant_id

log = get_logger("sio.guard")

#: Route prefix to policy noun. Ordered: the first match wins, so longer prefixes come first.
#:
#: Derived rather than hand-annotated per handler. A decorator on each route is a list somebody will forget
#: to extend, and a forgotten entry is an unenforced endpoint that looks enforced — the worst of both.
RESOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    # Longest first: `/api/spatial` must be tested before `/api`.
    ("/api/missions", "mission"),
    ("/api/webhooks", "integration"),
    ("/api/analytics", "analytics"),
    ("/api/simulations", "simulation"),
    ("/api/measurements", "events"),
    ("/api/replay", "timeline"),
    ("/api/world", "entities"),
    ("/api/stats", "entities"),
    ("/api/spatial", "spatial"),
    ("/api/timeline", "timeline"),
    ("/api/forecasts", "forecasts"),
    ("/api/workflow", "workflow"),
    ("/api/decisions", "decisions"),
    ("/api/entities", "entities"),
    ("/api/events", "events"),
    ("/api/alerts", "alerts"),
    ("/api/agents", "agents"),
    ("/api/audit", "audit"),
    ("/api/copilot", "copilot"),
    ("/api/search", "search"),
    ("/api/media", "media"),
    ("/api/policies", "policy"),
    ("/api/admin", "admin"),
    # The SSE feed the console lives on, and stored media. Both were unmapped, which meant both were
    # denied to everyone — the live map went blank and every frame 403'd. Found by the route-coverage test,
    # not by using the console, which is the better order.
    # Service-local read surfaces. Each is a diagnostic view over data the platform already exposes
    # through the API, so each is governed as a read of the same noun rather than getting a noun of its own.
    # A mission is the one object a HUMAN owns, so its gate is wider than the machine-first surfaces: an
    # operator may run one. Committing a resource is narrower — see `mission.assign`.
    ("/missions", "mission"),
    # Webhooks are integration surface: creating one sends this platform's data to an external URL, which is
    # the `integrator` role's job and nobody else's.
    ("/webhooks", "integration"),
    ("/analytics", "analytics"),
    ("/simulations", "simulation"),
    ("/fusion", "entities"),
    ("/tracks", "entities"),
    ("/counts", "entities"),
    ("/cross-camera", "entities"),
    ("/detect", "model"),
    ("/stream", "events"),
    ("/media", "media"),
    ("/copilot", "copilot"),
    ("/alerts", "alerts"),
    ("/decisions", "decisions"),
    ("/workflow", "workflow"),
    ("/agents", "agents"),
    ("/simulation", "simulation"),
    ("/spatial", "spatial"),
    ("/timeline", "timeline"),
    ("/forecasts", "forecasts"),
    ("/graphql", "graphql"),
    # The governance service's own routes. Absent from the first version, and the omission proved the
    # design: `GET /audit` on port 8118 resolved to `unmapped.request`, was denied by default, and said so
    # in the log. A new route is visibly ungoverned rather than invisibly unprotected — which is the whole
    # reason the fallback is deny rather than allow.
    ("/governance", "admin"),
    ("/policies", "policy"),
    ("/audit", "audit"),
    # Service-local surfaces that exist on several services.
    ("/detector", "model"),
    ("/tracker", "model"),
    ("/rules", "policy"),
    ("/world", "entities"),
    ("/search", "search"),
    ("/entities", "entities"),
    ("/events", "events"),
    ("/predict", "forecasts"),
    ("/replay", "timeline"),
    ("/mcp", "copilot"),
    ("/connectors", "integration"),
    ("/site", "spatial"),
)

#: Path suffixes that name an action more precisely than the HTTP method does.
#:
#: `POST /decisions/{id}/approve` is not a generic write: it is the single most consequential action in the
#: platform, and it must map to `decision.approve` so the commander-only rule applies. Relying on the method
#: alone would have made it an ordinary `decisions.write` that any operator could perform.
ACTION_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("/approve", "decision.approve"),
    ("/reject", "decision.reject"),
    ("/escalate", "alerts.write"),
    ("/ack", "alerts.write"),
    ("/resolve", "alerts.write"),
    ("/execute", "workflow.execute"),
    ("/inject", "simulation.inject"),
    ("/inject/fire", "simulation.inject"),
    ("/inject/power_failure", "simulation.inject"),
    # Moving a mission through its lifecycle and committing a resource to it both need a commander. Starting a
    # mission arms objectives that dispatch physical things, and assigning a drone decides where one goes —
    # neither is the same kind of act as writing the mission down.
    ("/state", "mission.assign"),
    ("/resources", "mission.assign"),
)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def action_for(method: str, path: str) -> str:
    """The policy action a request maps to.

    Suffix first, because it is more specific: `/decisions/{id}/approve` must be `decision.approve` and not
    `decisions.write`.
    """
    for suffix, action in ACTION_SUFFIXES:
        if path.endswith(suffix):
            return action
    noun = next((name for prefix, name in RESOURCE_PREFIXES if path.startswith(prefix)), "")
    if not noun:
        # An unmapped path gets an action nothing permits, so it is denied by default and the log names the
        # path. A new route is therefore visibly ungoverned rather than invisibly unprotected.
        return "unmapped.request"
    verb = "read" if method.upper() in _READ_METHODS else "write"
    if noun == "copilot":
        return "copilot.ask" if verb == "write" else "copilot.read"
    return f"{noun}.{verb}"


def is_public(path: str) -> bool:
    return any(
        path == public or path.startswith(public.rstrip("/") + "/") for public in PUBLIC_PATHS
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # A cookie is accepted so the browser console can hold a session without putting a token in JavaScript
    # reachable by every script on the page. Same verification either way.
    return request.cookies.get("sio_token")


class GovernanceMiddleware(BaseHTTPMiddleware):
    """Authenticate, authorise, bind the tenant, audit. In that order, for every request."""

    def __init__(
        self,
        app: Any,
        *,
        authenticator: Authenticator | None = None,
        settings: Settings | None = None,
        service: str = "",
        audit: Callable[[Decision, Request], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(app)
        self.settings = settings or get_settings()
        self.authenticator = authenticator or build_authenticator(self.settings)
        self.service = service
        self.audit = audit
        self.enabled = self.settings.auth_required
        self.denials = 0
        self.anonymous = 0
        if not self.enabled:
            # Loud, once, at startup. A platform running with authentication disabled must say so somewhere a
            # human will see it, or the setting outlives the reason it was set.
            log.warning(
                "guard.disabled",
                service=service,
                consequence="every request runs as the default tenant with no principal",
                fix="set SIO_AUTH_REQUIRED=true",
            )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        path = request.url.path
        if is_public(path):
            return await call_next(request)

        if not self.enabled:
            request.state.principal = ANONYMOUS
            return await call_next(request)

        token = _bearer(request)
        if not token:
            self.anonymous += 1
            # 401 and not a default principal. Falling back to a default tenant on a missing token is how
            # cross-tenant leakage happens quietly: the request succeeds, returns somebody's data, and
            # nothing in the logs looks wrong.
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "this endpoint needs a bearer token",
                    "how": "POST /auth/dev/token to get one in dev mode",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            principal = await self.authenticator.principal_from(token)
        except PolicyDenied as exc:
            self.denials += 1
            return JSONResponse(
                status_code=401,
                content={"detail": str(exc.reason or exc)},
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.principal = principal
        # The tenant, bound for the duration of the request. Every store method already takes a tenant_id and
        # every query already filters on it — they were all simply being handed the configured default.
        previous_tenant = None
        try:
            from .telemetry import get_tenant_id

            previous_tenant = get_tenant_id()
            set_tenant_id(principal.tenant_id)

            action = action_for(request.method, path)
            decision = authorise(
                principal,
                action,
                resource=path,
                context={"zone_id": request.query_params.get("zone_id")},
            )
            if self.audit is not None:
                await self.audit(decision, request)
            if not decision.allowed:
                self.denials += 1
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": decision.reason,
                        "action": action,
                        "principal": principal.subject,
                        # Named so an operator can ask for the right thing rather than for admin, which is
                        # what happens when a denial says only "forbidden".
                        "rule": decision.rule,
                    },
                )
            response = await call_next(request)
            response.headers["x-sio-tenant"] = principal.tenant_id
            response.headers["x-sio-principal"] = principal.subject
            return response
        finally:
            set_tenant_id(previous_tenant)


def principal_of(request: Request) -> Principal:
    """The authenticated principal, for a handler that needs to make a finer-grained check."""
    return getattr(request.state, "principal", ANONYMOUS)


def install_dev_token_route(app: FastAPI, settings: Settings | None = None) -> None:
    """Mount `POST /auth/dev/token`.

    Only in dev mode, and it says so in its own response. A token endpoint that quietly appeared in a
    production deployment would be a complete authentication bypass, so the route refuses to exist when
    `auth_mode` is anything else.
    """
    settings = settings or get_settings()
    if settings.auth_mode != "dev":
        return

    issuer = DevJwtAuth(settings)

    @app.post("/auth/dev/token", tags=["auth"])
    async def dev_token(
        subject: str = "dev",
        tenant_id: str | None = None,
        roles: str = "operator",
        clearance: int = 1,
        zones: str = "",
        pii_scope: bool = False,
        ttl_s: int | None = None,
    ) -> dict[str, Any]:
        """Issue a signed dev token. Insecure by design, and genuinely verified."""
        role_tuple = tuple(part.strip() for part in roles.split(",") if part.strip())
        zone_tuple = tuple(part.strip() for part in zones.split(",") if part.strip())
        token = issuer.issue(
            subject=subject,
            tenant_id=tenant_id,
            roles=role_tuple or ("operator",),
            clearance=clearance,
            zones=zone_tuple,
            pii_scope=pii_scope,
            ttl_s=ttl_s,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": ttl_s or settings.jwt_ttl_s,
            "subject": subject,
            "tenant": tenant_id or settings.tenant_id,
            "roles": list(role_tuple),
            "warning": (
                "This is a development issuer: the signing secret is in settings and there is no "
                "revocation. It signs and verifies for real, so the enforcement path is the same one "
                "production uses."
            ),
        }


_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def install_governance(
    app: FastAPI,
    *,
    service: str,
    settings: Settings | None = None,
    authenticator: Authenticator | None = None,
    audit: Callable[[Decision, Request], Awaitable[None]] | None = None,
) -> None:
    """Wire authentication, authorisation and the dev token issuer into a service.

    One call, so a service cannot install half of it.
    """
    settings = settings or get_settings()
    install_dev_token_route(app, settings)
    app.add_middleware(
        GovernanceMiddleware,
        authenticator=authenticator,
        settings=settings,
        service=service,
        audit=audit,
    )
    log.info(
        "guard.installed",
        service=service,
        auth=settings.auth_mode,
        required=settings.auth_required,
        policy=settings.policy_engine,
    )


__all__ = [
    "ACTION_SUFFIXES",
    "RESOURCE_PREFIXES",
    "GovernanceMiddleware",
    "action_for",
    "install_dev_token_route",
    "install_governance",
    "is_public",
    "principal_of",
]
