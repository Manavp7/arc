# Governance, privacy and explainability

The PRD's fifth principle is "governance as runtime": privacy, authorisation, audit and lineage
are enforced *in the pipeline*, not described in a document. This page records what is enforced
today, what is enforced by the database rather than by code, and what arrives in Phase 5.

## Already enforced (Phase 0)

### Immutability, enforced by Postgres

`audit_log`, `events`, `observations` and `detections` carry `BEFORE UPDATE OR DELETE` triggers
that raise. This is deliberately not a `REVOKE`: the local development stack connects as the
cluster owner, and privilege-based protection would not apply to exactly the account most likely
to make a mistake.

```
UPDATE on public.audit_log is forbidden: this table is append-only (SIO governance)
```

Retention still has to delete eventually, so deletion is gated on a session flag that only the
retention job sets:

```sql
SET LOCAL sio.retention_job = 'on';
DELETE FROM events WHERE ts < now() - interval '365 days';
```

Verified in `tests/integration/test_stores_infra.py`.

### Tenant scoping in the schema

`tenant_id` is the leading column of every primary key and every index. An index that did not
lead with it would make a cross-tenant scan cheap, which is how leaks happen. Every store method
takes `tenant_id` explicitly — it is a parameter, not an ambient assumption. Phase 5 wires the
authenticated principal into it and adds a negative test suite that attempts cross-tenant reads
on every route.

### Safe defaults, asserted by tests

| Flag | Default | Meaning |
|---|---|---|
| `SIO_ENABLE_FACE_RECOGNITION` | `false` | Face **recognition** is off. PRD NG2/R4. Enabling it requires both this flag and a policy decision. |
| `SIO_BLUR_FACES` | `true` | Person-head regions are blurred before media reaches storage. |
| `SIO_BLUR_PLATES` | `true` | OCR-detected plate regions are blurred before media reaches storage. |
| `SIO_REDACT_PII` | `true` | Presidio redaction on text leaving the system. |
| `SIO_RETAIN_RAW` | `false` | Un-blurred originals are not kept. |
| `SIO_AGENT_REQUIRE_APPROVAL` | `true` | Agents propose; humans approve. |
| `SIO_AUDIT_ENABLED` | `true` | Every query, tool call, decision and approval is recorded. |

`tests/unit/test_config.py::test_governance_flags_default_to_the_safe_position` fails if any of
these drifts. A safe default that nothing checks is a safe default until someone edits it.

### Explainability as a mechanism

`ExplanationBuilder` (`sio_core.explain`) produces the bundle attached to every event, alert,
decision and copilot answer: evidence references (frames, detections, events, **and the exact
query executed**), confidence, contributing sensors, a chronologically sorted timeline, related
entities, and alternatives considered and rejected.

Two details that matter more than they look:

- **Confidence is noisy-OR** (`1 - Π(1 - sᵢ)`, capped at 0.99). Two independent 0.8 signals give
  0.96, and nothing is ever certain — which is the honest behaviour for sensor fusion.
- **Degraded answers announce themselves.** When a fallback path produces an answer (the LLM
  failed to select a tool, a model was unavailable), `explanation.degraded` is true and the
  reason is in `notes`. A degraded answer that looks normal is worse than no answer.

### Traceability

One `trace_id` is attached at ingestion and travels through detection → track → entity → event →
decision → audit row, across every process. `trace_context()` binds it to structured logs
automatically, so an incident can be reconstructed by grepping a single id.

## Arriving in Phase 5

- **Authentication always on.** `DevJwtAuth` issues locally signed HS256 tokens for development;
  `KeycloakOidcAuth` (OIDC discovery + JWKS) is a configuration flip. `/health` and `/metrics` are
  the only unauthenticated routes.
- **Authorisation via a `PolicyEngine` port.** The embedded evaluator interprets the same policy
  documents that `infra/opa/policies/*.rego` express, so the always-tested default and the
  production engine cannot drift apart in meaning. RBAC roles: operator, commander, integrator,
  ml_engineer, admin. ABAC attributes: tenant, clearance, zone, time-of-day, PII scope.
  OpenFGA covers relationship checks ("may view camera Y because assigned to mission Z").
- **PII redaction in the pipeline.** Presidio for text; face/plate blurring applied in
  `perception` *before* any frame reaches MinIO.
- **Audit API and admin UI** over the append-only table, including denials.
- **Cross-tenant negative tests** iterating every API route.

## Compliance posture

GDPR/CCPA/BIPA-aware by construction rather than by claim: face recognition is off by default and
policy-gated; biometric identifiers are not stored unless explicitly enabled; media is redacted
before storage; retention is configurable per stream (`SIO_RETAIN_*_DAYS`) with deletion possible
only through the audited retention path; data residency is a deployment concern documented in
`DEPLOYMENT.md`.

This is an engineering posture, not legal advice. Enabling face recognition in a jurisdiction
with biometric consent law (Illinois BIPA, Texas CUBI, EU GDPR Art. 9) requires legal review
before the flag is flipped, and the flag exists so that review has something concrete to gate.
