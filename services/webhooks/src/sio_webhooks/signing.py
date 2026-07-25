"""Webhook signatures (PRD M22, Phase 6).

A signature over the body alone is not enough, and the reason is worth stating because it is the mistake almost
every first implementation makes: **a body-only signature can be replayed forever.** Anyone who captures one
valid delivery can resend it a year later and the receiver will verify it happily — which for a platform that
dispatches drones is not a theoretical concern.

So the signature covers a timestamp *and* the body, and the receiver rejects anything older than a tolerance.
This is the scheme Stripe and GitHub converged on independently, which is a reasonable signal it is the right
shape:

    X-SIO-Signature: t=1784986224,v1=5257a869e7...
    X-SIO-Delivery: dlv_01KY...

`v1` is a version tag, present from the start. Adding one later means every receiver has to handle both formats
during a migration nobody planned; having one from the beginning makes rotating the algorithm a header change.

**Comparison is constant-time.** `==` on a signature leaks its correct prefix through timing, and the correct
call is one character longer.
"""

from __future__ import annotations

import hashlib
import hmac
import time

#: How old a signed request may be before a receiver should refuse it.
#:
#: Five minutes, which is the value both Stripe and GitHub use. It has to absorb clock skew between two
#: machines nobody controls together, and a tighter window turns an NTP hiccup into a delivery outage.
DEFAULT_TOLERANCE_S = 300

#: The header names. Prefixed, because a receiver may be behind a proxy that adds its own.
SIGNATURE_HEADER = "X-SIO-Signature"
DELIVERY_HEADER = "X-SIO-Delivery"
TOPIC_HEADER = "X-SIO-Topic"
ATTEMPT_HEADER = "X-SIO-Attempt"


def sign(body: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Produce the `t=...,v1=...` signature header value.

    The signed string is `f"{timestamp}.{body}"`, with a literal dot. The separator matters: without it,
    `t=1` over body `23...` and `t=12` over body `3...` produce the same input, so two different requests
    could share a signature. It is a small window and it costs one character to close.
    """
    stamp = int(time.time()) if timestamp is None else timestamp
    payload = f"{stamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def verify(
    body: bytes,
    header: str,
    secret: str,
    *,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    now: int | None = None,
) -> tuple[bool, str]:
    """Check a signature, returning `(ok, reason)`.

    Shipped as part of the platform rather than left to every receiver to reimplement, because the parts people
    get wrong — the timestamp, the constant-time compare, the tolerance — are exactly the parts that matter, and
    a receiver that verifies incorrectly is worse than one that does not verify at all: it believes it is
    protected.

    `reason` is returned even on success so a caller can log which version verified, which is what makes a
    future algorithm rotation observable rather than a mystery.
    """
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    stamp_raw = parts.get("t")
    provided = parts.get("v1")
    if not stamp_raw or not provided:
        return False, "malformed signature header: expected 't=...,v1=...'"

    try:
        stamp = int(stamp_raw)
    except ValueError:
        return False, "the timestamp is not an integer"

    current = int(time.time()) if now is None else now
    age = current - stamp
    if age > tolerance_s:
        # Replay protection. Deliberately reported as an age, not as "invalid signature": a receiver debugging
        # a clock-skew problem needs to know the signature was fine and the time was not.
        return False, f"the request is {age}s old, beyond the {tolerance_s}s tolerance"
    if age < -tolerance_s:
        return False, f"the request is timestamped {-age}s in the future; check the clocks"

    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        return False, "the signature does not match"
    return True, "verified with v1"


__all__ = [
    "ATTEMPT_HEADER",
    "DEFAULT_TOLERANCE_S",
    "DELIVERY_HEADER",
    "SIGNATURE_HEADER",
    "TOPIC_HEADER",
    "sign",
    "verify",
]
