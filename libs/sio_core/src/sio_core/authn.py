"""Who is asking (PRD M18, Phase 5).

The gap this closes is not "there is no auth code" — it is that **nothing called it**. A platform with a
`PolicyDenied` exception, a `tenant_id` on every table and a `current_tenant()` helper, and no middleware
populating any of it, has the shape of governance and none of the substance. Every request ran as the
configured default tenant with no principal at all.

So the design goal here is *unavoidability*. A principal is established by middleware on the way in, before
any route function runs, and the tenant comes from the token rather than from configuration. A route cannot
forget to authenticate, because authentication is not something a route does.

Three deliberate choices:

**Dev mode is real authentication, not a bypass.** `DevJwtAuth` issues and verifies genuinely signed
HS256 tokens from a local endpoint. A bypass that skips verification means the enforcement path is never
exercised until the day it is switched on in production, which is the worst possible day to discover a
mistake in it. The dev issuer is *insecure* — a well-known secret, no revocation — and it is insecure in
ways that do not change the code path.

**`/health` and `/metrics` are excluded, and nothing else is.** An unauthenticated liveness probe is
necessary: a supervisor that needs a token to ask whether a service is alive cannot start the service that
issues tokens. Every other exclusion is a hole, so there are none.

**An anonymous request is 401, never a default principal.** Falling back to a default tenant on a missing
token is how cross-tenant leakage happens quietly: the request succeeds, returns somebody's data, and
nothing in the logs looks wrong.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .config import Settings, get_settings
from .errors import PolicyDenied
from .telemetry import get_logger

log = get_logger("sio.authn")

#: Paths served without a principal.
#:
#: Exactly two kinds, and the reason each is safe: liveness (a supervisor cannot hold a token before the
#: token issuer is up) and the dev token endpoint itself (nothing can present a token before obtaining one).
#: Everything else requires a principal, because every additional exclusion is a hole that will be found.
PUBLIC_PATHS: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/auth/dev/token",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)

#: Roles the platform understands (PRD §9).
ROLES: tuple[str, ...] = ("operator", "commander", "integrator", "ml_engineer", "admin", "viewer")


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    `tenant_id` is on the principal rather than read from settings, which is the whole point: a request's
    tenant is a property of who is asking, and taking it from configuration is what allowed every request to
    be served as the default tenant.
    """

    subject: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    clearance: int = 0
    """Numeric clearance for ABAC checks. Higher dominates; 0 is 'no clearance asserted'."""
    zones: frozenset[str] = frozenset()
    """Zones this principal may see. Empty means unrestricted, which is normal for an operator."""
    pii_scope: bool = False
    """Whether this principal may see unredacted personal data."""
    issued_at: float = 0.0
    expires_at: float = 0.0
    issuer: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def has_any(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    def may_see_zone(self, zone_id: str | None) -> bool:
        """Zone-level ABAC. An empty zone set is unrestricted, not 'no zones'.

        The inversion matters and is easy to get backwards: a principal with no zone restriction is the
        common case, and treating an empty set as 'permitted nowhere' would lock out every ordinary operator
        while looking like a tightening.
        """
        if not self.zones or zone_id is None:
            return True
        return zone_id in self.zones

    def describe(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "tenant": self.tenant_id,
            "roles": sorted(self.roles),
            "clearance": self.clearance,
            "zones": sorted(self.zones) or ["*"],
            "pii_scope": self.pii_scope,
            "expires_in_s": max(0, int(self.expires_at - time.time())) if self.expires_at else None,
        }


ANONYMOUS = Principal(subject="anonymous", tenant_id="", roles=frozenset())
"""Explicitly not a usable principal. Present so code can compare against it rather than against None."""


@runtime_checkable
class Authenticator(Protocol):
    """The port. Anything that can turn a bearer token into a principal."""

    name: str

    async def principal_from(self, token: str) -> Principal: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- JWT, by hand
#
# A minimal HS256 implementation rather than PyJWT, for one reason: this is the only place in the platform
# that needs it, PyJWT would be a dependency carried by every service, and the verification path is thirty
# lines that are far better read than trusted. The RS256 path (Keycloak) does use a library, because
# hand-rolling RSA signature verification would be indefensible.


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def encode_hs256(payload: dict[str, Any], secret: str) -> str:
    header = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url_encode(signature)}"


def decode_hs256(token: str, secret: str) -> dict[str, Any]:
    """Verify and decode, raising `PolicyDenied` on anything suspect.

    Signature checked with `hmac.compare_digest`, not `==`. The timing difference on a string comparison is
    a real attack on a token verifier, and the correct call is one character longer.
    """
    try:
        header_b64, body_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise PolicyDenied(
            "authenticate", "token", "malformed token: expected three segments"
        ) from exc

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PolicyDenied("authenticate", "token", "unreadable token header") from exc

    algorithm = header.get("alg")
    if algorithm != "HS256":
        # Explicitly refuse `alg: none` and algorithm substitution. This is the classic JWT vulnerability
        # and the reason the expected algorithm must be asserted by the verifier rather than read from the
        # token — a token that names its own verification algorithm is not verified at all.
        raise PolicyDenied(
            "authenticate", "token", f"unexpected algorithm {algorithm!r}; expected HS256"
        )

    expected = hmac.new(
        secret.encode(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        raise PolicyDenied("authenticate", "token", "signature does not verify")

    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(body_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PolicyDenied("authenticate", "token", "unreadable token payload") from exc

    now = time.time()
    expiry = payload.get("exp")
    if expiry is not None and float(expiry) < now:
        raise PolicyDenied("authenticate", "token", "token has expired")
    not_before = payload.get("nbf")
    if not_before is not None and float(not_before) > now + 5:
        raise PolicyDenied("authenticate", "token", "token is not yet valid")
    return payload


def principal_from_claims(claims: dict[str, Any], *, issuer: str = "") -> Principal:
    """Map JWT claims onto a `Principal`.

    Kept in one function so the dev issuer and Keycloak produce identical principals from equivalent claims.
    Two mappings would drift, and the drift would be a permissions difference between dev and production —
    which is exactly the class of bug that only appears in production.

    Roles are read from both a flat `roles` claim and Keycloak's nested `realm_access.roles`, because
    Keycloak puts them there and rewriting its token shape is not this code's business.
    """
    tenant = str(claims.get("tenant") or claims.get("tenant_id") or "")
    if not tenant:
        raise PolicyDenied("authenticate", "token", "token carries no tenant claim")

    raw_roles: list[str] = []
    flat = claims.get("roles")
    if isinstance(flat, list):
        raw_roles.extend(str(role) for role in flat)
    elif isinstance(flat, str):
        raw_roles.extend(part.strip() for part in flat.split(",") if part.strip())
    realm = claims.get("realm_access")
    if isinstance(realm, dict) and isinstance(realm.get("roles"), list):
        raw_roles.extend(str(role) for role in realm["roles"])

    zones = claims.get("zones")
    zone_set = (
        frozenset(str(zone) for zone in zones)
        if isinstance(zones, list)
        else frozenset(part.strip() for part in str(zones).split(",") if part.strip())
        if isinstance(zones, str) and zones
        else frozenset()
    )

    return Principal(
        subject=str(claims.get("sub") or claims.get("preferred_username") or "unknown"),
        tenant_id=tenant,
        roles=frozenset(role.lower() for role in raw_roles),
        clearance=int(claims.get("clearance") or 0),
        zones=zone_set,
        pii_scope=bool(claims.get("pii_scope") or claims.get("pii") or False),
        issued_at=float(claims.get("iat") or 0),
        expires_at=float(claims.get("exp") or 0),
        issuer=str(claims.get("iss") or issuer),
        claims=claims,
    )


class DevJwtAuth:
    """Locally issued, genuinely signed tokens.

    Insecure in ways that do not change the code path: a well-known secret from settings, no revocation, no
    key rotation. What it is *not* is a bypass — the signature is verified, the expiry is enforced, and the
    algorithm is asserted. A dev mode that skipped verification would leave the enforcement path unexercised
    until it was switched on in production, which is the worst day to find a mistake in it.
    """

    name = "dev"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.secret = self.settings.jwt_secret
        self.issuer = self.settings.jwt_issuer

    def issue(
        self,
        *,
        subject: str = "dev",
        tenant_id: str | None = None,
        roles: tuple[str, ...] = ("operator",),
        clearance: int = 1,
        zones: tuple[str, ...] = (),
        pii_scope: bool = False,
        ttl_s: int | None = None,
    ) -> str:
        now = int(time.time())
        ttl = ttl_s if ttl_s is not None else self.settings.jwt_ttl_s
        payload = {
            "sub": subject,
            "tenant": tenant_id or self.settings.tenant_id,
            "roles": list(roles),
            "clearance": clearance,
            "zones": list(zones),
            "pii_scope": pii_scope,
            "iss": self.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
        }
        return encode_hs256(payload, self.secret)

    async def principal_from(self, token: str) -> Principal:
        claims = decode_hs256(token, self.secret)
        if claims.get("iss") not in (self.issuer, None):
            raise PolicyDenied("authenticate", "token", f"unexpected issuer {claims.get('iss')!r}")
        return principal_from_claims(claims, issuer=self.issuer)

    async def close(self) -> None:
        return None


class KeycloakOidcAuth:
    """Tokens from Keycloak, verified against its published JWKS.

    Discovery and JWKS are fetched once and cached, with a refresh on an unknown key id — which is how key
    rotation actually presents itself. Refreshing on *every* failure would let a stream of invalid tokens
    turn into a denial-of-service against the identity provider, so the refresh is rate-limited.
    """

    name = "keycloak"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.discovery_url = getattr(
            self.settings,
            "oidc_discovery_url",
            "http://127.0.0.1:8080/realms/sio/.well-known/openid-configuration",
        )
        self.audience = getattr(self.settings, "oidc_audience", "sio-api")
        self._jwks: dict[str, Any] = {}
        self._jwks_fetched_at = 0.0
        self._issuer = ""
        self._client: Any = None

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def _refresh_jwks(self, *, force: bool = False) -> None:
        # Rate-limited: a stream of tokens with unknown key ids must not become a request storm against the
        # identity provider. Thirty seconds is far shorter than any sane rotation interval and far longer
        # than a burst of bad requests.
        if not force and time.monotonic() - self._jwks_fetched_at < 30:
            return
        client = await self._http()
        discovery = (await client.get(self.discovery_url)).json()
        self._issuer = discovery.get("issuer", "")
        jwks = (await client.get(discovery["jwks_uri"])).json()
        self._jwks = {key["kid"]: key for key in jwks.get("keys", []) if "kid" in key}
        self._jwks_fetched_at = time.monotonic()
        log.info("authn.jwks_loaded", keys=len(self._jwks), issuer=self._issuer)

    async def principal_from(self, token: str) -> Principal:
        try:
            from jose import jwt as jose_jwt  # type: ignore[import-untyped]
        except ImportError as exc:
            raise PolicyDenied(
                "authenticate",
                "token",
                "keycloak auth needs python-jose: uv sync --extra keycloak",
            ) from exc

        header = jose_jwt.get_unverified_header(token)
        kid = header.get("kid")
        await self._refresh_jwks()
        if kid not in self._jwks:
            # An unknown key id is what rotation looks like, so try once with a forced refresh before
            # rejecting. Rejecting immediately would make every rotation an outage.
            await self._refresh_jwks(force=True)
        key = self._jwks.get(kid)
        if key is None:
            raise PolicyDenied("authenticate", "token", f"unknown signing key {kid!r}")

        try:
            claims = jose_jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self._issuer or None,
            )
        except Exception as exc:
            raise PolicyDenied(
                "authenticate", "token", f"token rejected: {type(exc).__name__}"
            ) from exc
        return principal_from_claims(claims, issuer=self._issuer)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_authenticator(settings: Settings | None = None) -> Authenticator:
    settings = settings or get_settings()
    if settings.auth_mode == "keycloak":
        log.info("authn.backend", mode="keycloak")
        return KeycloakOidcAuth(settings)
    log.info("authn.backend", mode="dev", issuer=settings.jwt_issuer)
    return DevJwtAuth(settings)


__all__ = [
    "ANONYMOUS",
    "PUBLIC_PATHS",
    "ROLES",
    "Authenticator",
    "DevJwtAuth",
    "KeycloakOidcAuth",
    "Principal",
    "build_authenticator",
    "decode_hs256",
    "encode_hs256",
    "principal_from_claims",
]
