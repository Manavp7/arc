# governance (M18 / M19 / M21)

The audit trail, the policy in force, and an honest answer to "is this deployment safe?".

```
every service ──► audit topic ──► governance ──► audit_log (UPDATE/DELETE revoked)
                                             └─► /audit, /audit/denials, /policies, /governance/posture
```

## What this service does not do

It does not enforce anything. Enforcement lives in `sio_core` — `authn.py`, `authz.py`, `guard.py` — and is
installed by the **shared service runtime**, so all fifteen services get it and none opts in.

That arrangement is the point. The plan's diagnosis of this phase was exact: *"today's gap is that nothing
calls it"*. The platform already had a `PolicyDenied` exception, a `tenant_id` on every table and a
`current_tenant()` helper — and no middleware populating any of it, so every request ran as the configured
default tenant with no principal. Putting enforcement in a *service* would have recreated the same gap one
level up: a service that has to be consulted is a service somebody forgets to consult.

## The audit trail

**Allows are recorded, not just denials.** A trail of denials answers "who was stopped". A trail of allows
answers "who did this" — which is the question actually asked after an incident, and the one that cannot be
answered retrospectively if it was never recorded.

**The table revokes UPDATE and DELETE**, in Postgres, not by convention here. A guarantee that depends on
every writer behaving is not a guarantee. It has a consequence worth stating plainly: a mistake in an audit
record cannot be corrected, only annotated by a later record. That is the right trade for a trail whose
entire value is that it cannot be edited — and this service *checks the grants at startup* rather than
trusting the migration ran, because a migration that silently failed is invisible until somebody needs the
guarantee.

**Writing an audit record must never be able to fail a request.** The producer side logs and continues, so
this service's job is to lose as little as possible: it batches (one INSERT per authorisation decision would
make the audit trail the platform's busiest writer), flushes every two seconds, flushes on shutdown, and
reports a backlog in `/health`. A failed batch is **not** re-queued — a record that failed to insert will
usually fail again, and retrying forever would grow the buffer without bound while hiding the cause. The
count surfaces in health instead, because a dropped audit record is invisible by definition and making the
drop visible is the only available defence.

## Endpoints

| | |
|---|---|
| `GET /audit` | the trail, filterable by actor, action, allowed, window |
| `GET /audit/denials` | refusals **grouped** by who and what — fifty rows from one misconfigured integration describe it worse than one row with a count |
| `GET /policies` | the policy as data, so an operator refused something can find out what would permit it without asking an administrator |
| `GET /governance/posture` | what is actually switched on, and what is not |

### `/governance/posture` and the `weaknesses` list

Written for the question "is this deployment safe?", which nobody can answer by reading fifteen services'
configuration. Every field is a fact about the running process rather than a restatement of the docs.

The `weaknesses` list **names what is not protected** rather than leaving a reader to infer it from absence.
An empty list is a claim; a list of three is a to-do. Either is more useful than a page of green ticks that
omits the thing that is off.

One entry exists because of a bug in the first version of this endpoint. It reported
`face_plate_blurring: not retain_raw`, conflating two independent settings — so a deployment with blurring
*on* and raw retention *also on* would have been reported as not blurring, which is false. Blurring always
applies to the frame an operator retrieves; `SIO_RETAIN_RAW` additionally keeps an unblurred copy under a
`raw/` prefix, reachable only via `media.raw`, which requires both a role and the `pii_scope` claim.

## PII

Redaction is on by default, and the default is the point: a privacy control that must be switched on is off
in every deployment where nobody thought about it. Presidio is used when importable; a regex detector always
works, because a governance control that only functions with an optional dependency installed is absent in
most installs. **Which detector ran is reported**, since "redacted" by a regex is a weaker claim than by
Presidio and the reader is entitled to know which they got.

See `libs/sio_core/src/sio_core/pii.py` for why the phone pattern looks the way it does — three failure
modes, one of which (partial redaction) is worse than either extreme.
