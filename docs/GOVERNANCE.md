# Governance

What this platform enforces, what it does not, and the reasoning behind each choice.

Written to be read by somebody deciding whether to deploy it, which means the uncomfortable parts are here
too. A governance document that lists only what is protected is a marketing document.

---

## The one-line summary

Authentication is required by default and authorization decisions are audited. The browser uses explicit
sign-in and bearer-authenticated requests and streams. Raw camera buffers are private until perception
processes them; optional originals require a separate media permission. This build records response intent
and supports approvals, but includes no physical gate or drone command adapter.

Check any running deployment for yourself:

```bash
curl -s localhost:8118/governance/posture -H "Authorization: Bearer $TOKEN" | jq
```

It reports what is **actually** switched on, including a `weaknesses` list that names what is not.

---

## Identity and authentication

| | |
|---|---|
| where it lives | `libs/sio_core/src/sio_core/authn.py` |
| installed by | the shared service runtime, so all HTTP services get it and none opts in |
| public paths | Health aliases, metrics, `/auth/config`, development token issuance, documentation, and favicon paths listed in `sio_core.authn.PUBLIC_PATHS` |

**A principal is established by middleware before any route runs.** Authentication is not something a
handler does, so there is no handler that can omit it. This is the whole fix for what the build plan called
the Phase 5 gap: the platform previously had a `PolicyDenied` exception, a `tenant_id` on every table and a
`current_tenant()` helper — and nothing populating any of it, so every request ran as the configured default
tenant with no principal.

**An anonymous request is 401, never a default principal.** Falling back to a default tenant on a missing
token is how cross-tenant leakage happens quietly: the request succeeds, returns somebody's data, and nothing
in the logs looks wrong.

### Dev mode is real authentication, not a bypass

`DevJwtAuth` issues and verifies genuinely signed HS256 tokens from `POST /auth/dev/token`. It is insecure —
a well-known signing secret from settings, no revocation, no rotation — and it is insecure **in ways that do
not change the code path**. The signature is verified, the expiry is enforced, and the algorithm is asserted
by the verifier rather than read from the token.

A dev mode that skipped verification would leave the enforcement path unexercised until the day it was
switched on in production, which is the worst possible day to discover a mistake in it.

The route refuses to exist when `SIO_AUTH_MODE` is anything other than `dev`, because a token endpoint that
quietly appeared in a production deployment would be a complete authentication bypass.

### Keycloak

```bash
just keycloak            # optional Docker/Podman provider and Python verification extra
SIO_AUTH_MODE=keycloak SIO_KEYCLOAK_CLIENT_ID=sio-console SIO_OIDC_AUDIENCE=sio-api just dev
```

The local bootstrap imports a development realm and reconciles its managed public `sio-console` client
on every run. Existing containers, users, passwords, role assignments and other clients are retained.
The browser client requires authorization code with PKCE S256; password grants, implicit flow and service
accounts are disabled. Its only login and logout redirects are `http://localhost:5173/` and
`http://127.0.0.1:5173/`, with matching web origins. `sio-api` remains a separate confidential API client.

The console access token contains the `sio-api` audience. Attribute mappers carry the assigned user's
`tenant`, `clearance`, `pii_scope` and `zones`; the realm-role mapper exposes only already assigned roles
within the browser client's allowed role scope. It does not grant commander or administrator roles.
The development fixture has operator, commander and zoned test users, with passwords matching usernames.
These local credentials and HTTP settings must not be used for production.

`just keycloak` installs the optional `keycloak` Python dependency group. For an existing provider, install
it with `uv sync --extra keycloak`, then configure `SIO_OIDC_DISCOVERY_URL`, `SIO_OIDC_AUDIENCE` and
`SIO_KEYCLOAK_CLIENT_ID` as described in [Operating the console](OPERATIONS.md#authentication).
Existing `.env` files that still select `SIO_KEYCLOAK_CLIENT_ID=sio-api` must select `sio-console` for browser
sign-in; the bootstrap prints a command with that override. Nondefault console ports or paths require matching
explicit redirects, logout URLs and web origins.
A custom `KEYCLOAK_PORT` is printed in the bootstrap's discovery-URL instruction. If the existing local
provider's administrator credentials changed, set `KEYCLOAK_ADMIN` and `KEYCLOAK_ADMIN_PASSWORD` for the helper.

The `sio-console` client is package-managed: rerunning bootstrap restores its mappers, role scope and redirect
allowlist. Put deployment-specific customization in a separately named client. The helper never assigns roles
to existing users and never resets existing passwords.

`KeycloakOidcAuth` verifies RS256 signatures, issuer, audience, expiry and required tenant claims against the
provider's discovery/JWKS endpoints. JWT verification is local: logout ends the browser session and provider
refresh session, but an already copied access token can remain valid until expiry. There is no per-request
introspection or revocation lookup.

Fixture contract tests verify client configuration and idempotent reconciliation. Browser tests use a mock
provider. Live provider registration, login, refresh and logout remain deployment-specific verification.
See the [Keycloak administration guide](https://www.keycloak.org/docs/latest/server_admin/) for provider setup.

### Service identities

In development, background work uses short-lived signed tokens with a `service` role and a subject such as
`service:agents`. This local issuer is not an OIDC machine-identity integration. In Keycloak mode, background
workflow reports require `SIO_WORKFLOW_SERVICE_TOKEN`; other background integrations need deployment-specific
machine credentials before they can be considered operational.

Three deliberate choices: **ten minutes** rather than the life of the process, because a long-lived token is
a credential with no revocation; **the `service` role rather than `admin`**, because giving internal calls
admin and stopping thinking is exactly how "internal" becomes a synonym for "unaudited"; and **its own
subject**, so "who did this" has a truthful answer even when the answer is "a timer did".

The API **propagates the caller's token** when forwarding rather than substituting its own. This is the
difference between a proxy and a confused deputy: substituting would make every downstream audit row read
`service:api`, losing the only fact that matters after an incident, and would scope the downstream query to
the API's tenant rather than the caller's.

---

## Authorisation

| | |
|---|---|
| where it lives | `libs/sio_core/src/sio_core/authz.py` — one `POLICY` table |
| engines | `EmbeddedPolicyEngine` (default), `OpaPolicyEngine`, and the Rego is **generated** |
| model | RBAC (six roles) plus ABAC (tenant, clearance, zone, PII scope) |

### One policy, two engines, generated Rego

The rules live in a single table. The embedded engine evaluates it; `infra/opa/policies/sio.rego` is
generated from it by `just policies`, and a test asserts the checked-in file matches — so a rule added in
Python and not regenerated fails CI rather than diverging quietly.

Two hand-written implementations of one authorisation policy **will** drift, and the drift is a permission
difference between environments, which is the class of bug that only appears in production.

There is also a conformance test that runs every principal × action × context combination — 810 of them —
through both the embedded engine and a real `opa` binary and asserts they agree. It found a bug nothing else
would have: `Rule.matches` never implemented the `*.suffix` pattern, so the catch-all `*.read` rule was dead
code in Python while OPA honoured it. Ten read actions were denied to every principal except admins.

### Roles

| role | what it is for |
|---|---|
| `viewer` | read-only |
| `operator` | the day job: triage the inbox, acknowledge, reject recommendations |
| `commander` | **authorises action in the physical world**: approve decisions, execute playbooks |
| `integrator` | connectors and webhooks |
| `ml_engineer` | models and thresholds |
| `admin` | administrative surfaces, within one tenant |
| `service` | machine principals; the actions their jobs need and no more |

### The axis that matters is not read-versus-write

It is **whether something happens physically**. Starting a replay, running a forecast, reloading rules and
simulating an event are all writes that compute and change nothing in the world, so they are granted broadly.
`decision.approve` and `workflow.execute` sit behind a commander *and* a clearance, because they dispatch
responders.

Rejecting is deliberately easier than approving: rejecting results in nothing happening.

### Three ordering bugs worth knowing about

Every one was a security control whose correctness depended on the order of checks, and every one was silent
by construction. They are fixed, and the reasoning is in the code so a future edit does not undo them.

1. **The admin bypass sat above the tenant check**, so an admin could read another tenant's data. An admin is
   an admin *of a tenant*; there is deliberately no cross-tenant role, and if one is ever needed it must be
   an explicit `platform_admin`, granted separately and audited differently — never an accident of rule
   ordering.
2. **The generated Rego did not reproduce first-match-wins.** Python applies the first matching rule; Rego's
   `allow` is a union. A zoned operator was denied by one engine and allowed by the other.
3. **The admin bypass granted actions no rule defines**, including `unmapped.request` — the action given to
   any route the action map does not recognise. An admin would have silently reached every ungoverned
   endpoint added in a later phase.

### Denials explain themselves

```json
{
  "detail": "decision.approve needs one of: admin, commander; you have operator",
  "action": "decision.approve",
  "principal": "bob",
  "rule": "decision.approve"
}
```

A policy decision an operator cannot understand is one they route around, usually by asking for a broader
role. `GET /policies` returns the whole policy as data for the same reason: reading the rule is faster than
asking whoever administers the platform.

### OPA denies when unreachable

`OpaPolicyEngine` does **not** fall back to the embedded engine. An operator who set `SIO_POLICY_ENGINE=opa`
did so to have OPA's answers, and silently substituting different ones is worse than an outage because
nobody learns it happened.

---

## Multi-tenancy

The API binds `tenant_id` from the verified principal for its queries, replay sessions and stream subscribers.
Domain services still include deployment-scoped state, so their middleware rejects tokens from any other
deployment tenant before handlers run. Deploy separate domain-service stacks per tenant until those stores
and internal calls support shared tenancy end to end. A `?tenant_id=` parameter or `X-Tenant-Id` header does
not change the authenticated tenant.

Successful governed HTTP responses include `x-sio-tenant` and `x-sio-principal`; public health routes are
exempt. WebSockets authenticate before acceptance and bind the same tenant context.

**This is the one control whose failure is invisible.** A cross-tenant read does not error, does not look
unusual in a log, and returns plausible data. So `tests/unit/test_tenant_isolation.py` is adversarial rather
than illustrative: it enumerates every route from the live OpenAPI schema — so a route added later is attacked
the moment it exists — and tries a token for the wrong tenant, a parameter override, a header override, and
forged, unsigned, expired and tenant-less tokens.

---

## Personal data

| | |
|---|---|
| text | `libs/sio_core/src/sio_core/pii.py`, applied at the response boundary |
| pixels | `services/perception/src/sio_perception/redact.py`, applied before private real-camera buffers are promoted to retrievable media |

### Redaction is on by default

A privacy control that must be switched on is off in every deployment where nobody thought about it, which is
all of them.

Seeing unredacted data requires **both** the `pii.view` action and the `pii_scope` claim on the token. A role
is granted once and forgotten; a scope claim is minted per token, so seeing personal data is a decision taken
at issuing time rather than a standing property of a job title. The admin bypass deliberately does not cover
it.

### Redaction happens at the boundary, not inside the agent

The copilot needs real values to reason with — it cannot compute a dwell time from `<REDACTED>` — so the
boundary is the last point at which the data is needed and the first at which it leaves.

The **explanation** is redacted too. An answer with the name removed and the name still sitting in the
evidence list beneath it is a redaction in appearance only, and the evidence list is exactly where an OCR'd
plate ends up.

### Presidio is optional and its absence is not silent

The regex detector always works; Presidio is used when importable and is genuinely better at names and
addresses. **Which detector ran is reported in the response**, because "redacted" by a regex is a weaker
claim than by Presidio and the reader is entitled to know which they got.

```bash
uv sync --extra pii    # installs Presidio
```

### Three failure modes, and the middle one is the worst

- **Under-redaction** leaks personal data.
- **Over-redaction** makes the product unusable — and an unusable privacy control gets switched off
  wholesale, which leaks everything. This platform's text is dense with numbers, and the shipped patterns
  once announced that four phone numbers had been removed from an answer about truck counts: they were the
  ISO timestamps in the explanation, and the "IP address" was the loopback URL saying where the answer came
  from. The redaction had corrupted the provenance it was attached to.
- **Partial redaction** is worse than either. `+44 20 7946 0958` once became `+44 20 <PHONE>`. The surviving
  fragment looks like a deliberate disclosure, so a reader treats it as safe to pass on.

The fixes are invariants rather than tuned thresholds: no phone number has fewer than seven digits;
`YYYY-MM-DD` is a date in every locale; a loopback or private IP identifies infrastructure, not a person.

### Faces and plates

Real camera bytes are first buffered under `pending/`, which the media API rejects. Perception applies the
configured blurring pass, writes processed media under a tenant-qualified key and publishes `frames.ready`.
The world model skips private buffers and indexes only the processed reference. Storage administrators can
still access buffers directly; this is an application-access boundary, not encryption or absence of raw bytes.

`SIO_RETAIN_RAW=true` additionally keeps the unblurred original under a `raw/` prefix, reachable only through
`media.raw`, which requires a role **and** the `pii_scope` claim. It is off by default, and the posture
endpoint reports it separately from blurring — conflating the two once made that endpoint report a deployment
as not blurring when it was.

### Face recognition is off, and turning it on logs a warning

`SIO_ENABLE_FACE_RECOGNITION` has legal consequences — Illinois BIPA, Texas CUBI, GDPR Article 9 — and
enabling it writes a warning naming this document, so somebody reading the logs afterwards can see exactly
when it was turned on.

**This platform does not identify individuals by face by default and should not be configured to without
legal review.** Re-identification across cameras uses appearance embeddings that are not tied to an identity
and are not persisted beyond the track.

---

## The audit trail

| | |
|---|---|
| producer | every service, via the shared runtime, one record per authorisation decision |
| consumer | `services/governance`, batched, flushed every 2 s and on shutdown |
| storage | `audit_log`, with UPDATE and DELETE forbidden by trigger |

**Allows are recorded, not just denials.** A trail of denials answers "who was stopped". A trail of allows
answers "who did this" — the question actually asked after an incident, and the one that cannot be answered
retrospectively.

**Append-only is enforced in Postgres, by trigger.** Not by REVOKE, deliberately: a REVOKE does nothing
against a superuser or table owner, which is what a dev stack connects as. The consequence is worth stating:
a mistake in an audit record cannot be corrected, only annotated by a later record.

The governance service **proves** this at startup rather than trusting the migration ran — it writes a probe
row and attempts to update it, so either the trigger raises or only the probe is affected. Two earlier
versions of that check were wrong in opposite directions, and both produced a governance report that had not
verified its own claim.

A failed audit batch is **not** re-queued. A record that failed to insert will usually fail again, and
retrying forever would grow the buffer without bound while hiding the cause. The count surfaces in `/health`,
because a dropped audit record is invisible by definition and making the drop visible is the only defence
available.

Retention deletion is permitted only when the session sets `sio.retention_job = on`, so the one legitimate
deletion path is explicit and self-identifying.

---

## Human-on-the-loop

Structural, not a setting.

- The decision service **recommends and cannot execute**.
- The agents service **executes only an approved decision**, and only a *recent* one — an approval is
  authorisation to act now, not a standing licence, so a replayed consumer group cannot turn an old approval
  into a new action.
- `decision.approve` needs a commander and clearance 2.
- Every proposal, approval, rejection and execution is audited.

---

## Regulatory posture

Stated as posture, not compliance. Nobody has audited this.

| | |
|---|---|
| **GDPR** | Art. 5 data minimisation: faces and plates blurred by default, raw retention opt-in. Art. 15/17 subject access and erasure: `audit_log` is append-only, so erasure is a retention job under `sio.retention_job`, not an UPDATE. Art. 22: no automated decision affects a person without a human approving it. Art. 30: the audit trail is the processing record. Art. 9 biometrics: face recognition off by default. |
| **CCPA/CPRA** | Personal data is redacted by default in every response; access requires an explicit scope claim, which is auditable per token. |
| **BIPA / CUBI** | No biometric identifier is collected or stored by default. Appearance embeddings for cross-camera tracking are not identity-linked and are not persisted beyond the track. Enabling face recognition needs a legal basis and written policy that this project does not provide. |
| **Residency** | Single-region by default; every store is configurable. Multi-region residency is not implemented. |
| **Retention** | Configurable per table; the deletion path is explicit and audited. No default expiry is set, which is a deliberate omission — a default that silently deleted evidence would be worse than none. |

---

## What is not protected

The honest list. Each is a known gap, not an oversight.

- **The dev signing secret is in settings** and is the same in every checkout. `SIO_AUTH_MODE=dev` is for
  development. The posture endpoint reports this as a weakness.
- **Access-token revocation is not checked per request.** A copied dev or OIDC access token can remain valid
  until expiry; browser logout clears local credentials and requests provider logout.
- **No rate limiting.** A valid token can be used as fast as the platform will answer.
- **No field-level encryption at rest.** Postgres and MinIO are encrypted only if the underlying volumes are.
- **Presidio is optional**, so a default install detects structured identifiers and not names or addresses.
- **Browser tokens are readable by same-origin JavaScript.** Access and refresh credentials live in tab-scoped
  `sessionStorage`; API calls and streams send Authorization headers. They are cleared on logout. An XSS
  vulnerability could still read them; this is not an HttpOnly server-session architecture.
- **No mutual TLS between services.** Internal traffic is plain HTTP on loopback.
- **OpenFGA relationship checks are modelled but not wired** (`infra/openfga/model.fga`), so
  "user X may view camera Y because it is assigned to mission Z" is expressible and not enforced.
- **One deployment tenant per domain-service stack.** Middleware rejects other tenant identities while shared
  service storage and background calls are being made fully request-scoped.

---

## Verifying any of this yourself

```bash
just e2e                                    # includes tests/e2e/test_governance.py
uv run pytest tests/unit/test_authz.py      # 810-combination conformance if `opa` is installed
uv run pytest tests/unit/test_tenant_isolation.py
curl -s localhost:8118/governance/posture -H "Authorization: Bearer $TOKEN" | jq .weaknesses
curl -s localhost:8118/audit/denials -H "Authorization: Bearer $TOKEN" | jq
```
