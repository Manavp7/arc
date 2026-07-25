"""Outbound webhooks (PRD M22, Phase 6).

Three things a webhook system has to get right, and each is a place where the obvious implementation is wrong:

* **the signature has to cover a timestamp**, or a captured delivery can be replayed for ever;
* **retries must have a ceiling and an end**, or a permanently dead endpoint accumulates work that never drains;
* **not every failure is worth retrying** — a 404 is the receiver stating a fact that will not change because we
  asked again nine minutes later.

There is also a bug in here that my own test table found: subscribing to an event *type* matched nothing,
silently, while the API's help text advertised it as supported.
"""

from __future__ import annotations

import time

import pytest
from sio_webhooks.delivery import (
    BASE_DELAY_S,
    MAX_ATTEMPTS,
    MAX_DELAY_S,
    RETRYABLE_STATUS,
    Attempt,
    Delivery,
    backoff_delay,
    matches,
    should_retry,
)
from sio_webhooks.signing import (
    DEFAULT_TOLERANCE_S,
    SIGNATURE_HEADER,
    sign,
    verify,
)

BODY = b'{"kind":"Event","type":"fire_detected","severity":"critical"}'
SECRET = "whsec_test_only"


def a_delivery(**overrides) -> Delivery:
    defaults = {
        "delivery_id": "dlv_1",
        "webhook_id": "whk_1",
        "tenant_id": "acme",
        "url": "https://example.test/hook",
        "topic": "events",
        "body": BODY,
        "secret": SECRET,
    }
    defaults.update(overrides)
    return Delivery(**defaults)  # type: ignore[arg-type]


# --- signing --------------------------------------------------------------------------------------
def test_a_valid_signature_verifies() -> None:
    ok, reason = verify(BODY, sign(BODY, SECRET), SECRET)
    assert ok
    # The reason is returned on success too, so a future algorithm rotation is observable rather than a mystery.
    assert "v1" in reason


def test_a_tampered_body_does_not_verify() -> None:
    header = sign(BODY, SECRET)
    ok, reason = verify(BODY + b" ", header, SECRET)
    assert not ok
    assert "does not match" in reason


def test_the_wrong_secret_does_not_verify() -> None:
    ok, _ = verify(BODY, sign(BODY, SECRET), "a-different-secret")
    assert not ok


def test_an_old_delivery_is_refused() -> None:
    """Replay protection, which is the whole reason the timestamp is signed.

    A body-only signature can be resent a year later and the receiver verifies it happily. For a platform that
    dispatches drones that is not a theoretical concern.
    """
    stale = sign(BODY, SECRET, timestamp=int(time.time()) - DEFAULT_TOLERANCE_S - 60)
    ok, reason = verify(BODY, stale, SECRET)
    assert not ok
    # Reported as an AGE, not as "invalid signature": somebody debugging clock skew needs to know the signature
    # was fine and the time was not.
    assert "old" in reason
    assert "does not match" not in reason


def test_a_future_dated_delivery_is_refused_and_blames_the_clock() -> None:
    ahead = sign(BODY, SECRET, timestamp=int(time.time()) + DEFAULT_TOLERANCE_S + 60)
    ok, reason = verify(BODY, ahead, SECRET)
    assert not ok
    assert "future" in reason
    assert "clock" in reason


def test_a_delivery_inside_the_tolerance_verifies() -> None:
    """The tolerance has to absorb skew between two machines nobody controls together."""
    recent = sign(BODY, SECRET, timestamp=int(time.time()) - DEFAULT_TOLERANCE_S + 30)
    assert verify(BODY, recent, SECRET)[0]


def test_a_malformed_header_is_reported_not_crashed() -> None:
    for header in ("", "nonsense", "v1=abc", "t=notanumber,v1=abc", "t=1"):
        ok, reason = verify(BODY, header, SECRET)
        assert not ok
        assert reason


def test_the_separator_prevents_a_signature_collision() -> None:
    """`t=1` over body `23x` and `t=12` over body `3x` must not share a signature.

    Without the literal dot between the timestamp and the body they concatenate to the same string, so two
    different requests would be interchangeable. A small window, closed by one character.
    """
    first = sign(b"23x", SECRET, timestamp=1)
    second = sign(b"3x", SECRET, timestamp=12)
    assert first.split("v1=")[1] != second.split("v1=")[1]


def test_the_signature_header_is_namespaced() -> None:
    """A receiver may sit behind a proxy that adds its own headers."""
    assert SIGNATURE_HEADER.lower().startswith("x-sio-")


# --- what retries, and for how long ---------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS))
def test_transient_failures_retry(code: int) -> None:
    assert should_retry(Attempt(number=1, ok=False, status_code=code))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
def test_a_receiver_stating_a_fact_is_not_retried(code: int) -> None:
    """A 404 means the path is wrong; a 401 means the secret is wrong.

    Neither changes because we asked again nine minutes later, and hammering somebody else's server about it is
    both futile and rude.
    """
    assert not should_retry(Attempt(number=1, ok=False, status_code=code))


def test_a_connection_error_always_retries() -> None:
    """There is no statement from the receiver to respect — it may simply be restarting."""
    assert should_retry(Attempt(number=1, ok=False, status_code=None, error="ConnectError"))


def test_backoff_grows_and_then_stops_growing() -> None:
    """The ceiling matters more than the base.

    Without one, attempt 8 is two hours out — so a delivery can outlive the incident it describes, and a webhook
    that arrives after the fire is out is worse than none because somebody will act on it.
    """
    delays = [backoff_delay(n, jitter=False) for n in range(1, 12)]
    assert delays[0] == BASE_DELAY_S
    assert delays == sorted(delays)
    assert max(delays) == MAX_DELAY_S
    assert delays[-1] == delays[-2], "the delay must plateau, not grow for ever"


def test_backoff_is_jittered() -> None:
    """Without jitter, two hundred subscribers retry at the same instant after an outage.

    The receiver's first moment back online is then its worst, and it falls over again.
    """
    samples = {backoff_delay(4) for _ in range(40)}
    assert len(samples) > 30, "the delay looks deterministic; the thundering herd is not prevented"
    assert all(0 <= sample <= backoff_delay(4, jitter=False) for sample in samples)


def test_a_delivery_gives_up_after_the_attempt_limit() -> None:
    """Retrying for ever makes the queue a monument to a URL somebody deleted last March."""
    delivery = a_delivery()
    for number in range(1, MAX_ATTEMPTS + 1):
        delivery.record(Attempt(number=number, ok=False, status_code=503))
    assert delivery.status == "failed"
    assert delivery.attempt_count == MAX_ATTEMPTS
    assert delivery.next_retry_at is None
    assert delivery.describe()["will_retry"] is False


def test_a_delivery_stops_immediately_on_a_permanent_failure() -> None:
    delivery = a_delivery()
    delivery.record(Attempt(number=1, ok=False, status_code=404))
    assert delivery.status == "failed"
    assert delivery.attempt_count == 1, "a 404 must not consume five attempts"


def test_a_successful_delivery_is_terminal() -> None:
    delivery = a_delivery()
    delivery.record(Attempt(number=1, ok=False, status_code=502))
    assert delivery.status == "pending"
    assert delivery.next_retry_at is not None
    delivery.record(Attempt(number=2, ok=True, status_code=200))
    assert delivery.status == "delivered"
    assert delivery.delivered_at is not None
    assert delivery.next_retry_at is None


def test_a_pending_delivery_says_it_will_retry() -> None:
    """ "failed" alone leaves the reader to guess whether anything more will happen."""
    delivery = a_delivery()
    delivery.record(Attempt(number=1, ok=False, status_code=503))
    described = delivery.describe()
    assert described["status"] == "pending"
    assert described["will_retry"] is True
    assert described["next_retry_at"] is not None


# --- subscription matching ------------------------------------------------------------------------
def test_a_star_subscription_receives_everything() -> None:
    assert matches(["*"], "alerts", "Alert", None)
    assert matches(["*"], "events", "Event", "fire_detected")


def test_a_topic_subscription_receives_that_topic() -> None:
    assert matches(["alerts"], "alerts", "Alert", None)
    assert not matches(["alerts"], "events", "Event", "fire_detected")


def test_subscribing_to_an_event_type_works() -> None:
    """The bug my own test table found.

    The first version compared subscriptions against the message KIND — the schema class name, `"Event"` — so
    subscribing to `fire_detected` matched nothing, silently, while the API's help text and the docstring both
    advertised it as supported. Receiving every event and filtering client-side is exactly the traffic a webhook
    exists to avoid.
    """
    assert matches(["fire_detected"], "events", "Event", "fire_detected")
    assert not matches(["fire_detected"], "events", "Event", "zone_entered")


def test_a_prefix_subscription_receives_a_family() -> None:
    assert matches(["alerts.*"], "alerts.critical", "Alert", None)
    assert not matches(["alerts.*"], "events", "Event", None)


def test_an_empty_topic_list_receives_nothing() -> None:
    """Nothing, not everything.

    The failure of this interpretation is a surprised subscriber; the failure of the other is a subscriber's
    server melting. The API refuses to create such a subscription in the first place.
    """
    assert not matches([], "alerts", "Alert", "anything")
    assert not matches([], "events", "Event", "fire_detected")


def test_matching_by_message_kind_still_works() -> None:
    """A coarser granularity, kept because somebody will want every Alert regardless of topic."""
    assert matches(["Alert"], "anything", "Alert", None)


# --- the service's own promises --------------------------------------------------------------------
def test_raw_topics_are_not_forwarded() -> None:
    """Thirty raw GPS fixes a second is not a webhook workload.

    A subscriber who wants raw data wants the bus. Forwarding it would make the first `*` subscription an
    accidental denial-of-service against its own receiver.
    """
    from sio_webhooks.service import FORWARDED

    forwarded = {str(topic) for topic in FORWARDED}
    assert not any(name.startswith("raw.") for name in forwarded)
    assert "events" in forwarded
    assert "alerts" in forwarded


def test_delivery_concurrency_is_bounded() -> None:
    """A burst across many subscribers would otherwise open unbounded connections.

    The first thing that breaks is then this service, not the receivers.
    """
    from sio_webhooks.service import CONCURRENCY, POST_TIMEOUT_S

    assert 1 <= CONCURRENCY <= 64
    assert POST_TIMEOUT_S <= 30, "a receiver needing longer is doing work it should be queueing"


def test_the_delivery_record_carries_enough_to_debug_a_signature_mismatch() -> None:
    """The most common webhook support question, answerable from the log alone."""
    delivery = a_delivery()
    delivery.record(Attempt(number=1, ok=False, status_code=401, error="bad signature"))
    described = delivery.describe()
    assert described["url"]
    assert described["topic"]
    assert described["attempts"][0]["status_code"] == 401
    assert described["attempts"][0]["error"] == "bad signature"
    assert described["attempts"][0]["duration_ms"] is not None
