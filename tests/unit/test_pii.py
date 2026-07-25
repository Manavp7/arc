"""PII redaction (PRD M19, Phase 5).

Two failure modes, opposite in direction and both fatal, so nearly every test here belongs to one or the
other:

* **under-redaction** leaks personal data, obviously;
* **over-redaction** makes the product unusable, and an unusable privacy control gets switched off
  wholesale — which leaks everything.

The second is the one that took six iterations to get right. This platform's text is dense with numbers:
coordinates, speeds, confidences, battery percentages, priority scores, timestamps, entity ids. A phone
pattern loose enough to catch every international format eats all of it.

There is also a third failure mode with no obvious direction: **partial** redaction. `+44 20 7946 0958`
became `+44 20 <PHONE>` on the first attempt, and a surviving fragment is worse than either extreme —
it looks like a deliberate disclosure, so a reader treats it as safe to pass on.
"""

from __future__ import annotations

import re

import pytest

from sio_core.pii import (
    SENSITIVE_FIELDS,
    active_detector,
    redact_payload,
    redact_text,
    redaction_notice,
)

#: Real-world formats. Each must be redacted *entirely* — no surviving digits.
PHONE_NUMBERS = [
    "call +44 20 7946 0958 now",
    "reach him on +1 (555) 123 4567",
    "mobile 07700 900123",
    "ring 555-0143 today",
    "tel: 020 7946 0958",
    "phone 555 123 4567.",
    "+353 1 234 5678",
    "+49 30 12345678",
    "+81 3 1234 5678",
]

#: Real strings this platform produces. Not one character may change.
#:
#: Taken from actual output — alert reasons, forecast summaries, decision effects, log lines — rather than
#: invented, because invented examples are conveniently free of the numbers that cause the trouble.
OPERATIONAL_TEXT = [
    "Forklift 7 entered dock_3 at 14:32 with 82% battery",
    "Truck at 51.50735, -0.12776 moving 12.4 m/s",
    "entity sim-abc-truck-0001 seen by cam-dock-3-4",
    "priority 63.0 critical, 17 occurrences",
    "evt_01KYC8FYEH8Y7RZQRV1WJ0MH1J at 09:26:39",
    "26 moving entities: 12 trucks, 9 people, 3 forklifts, 2 drones",
    "battery steady: 88.1 now, 88.1 predicted in 20 min",
    "Drone 18 reaches fuel store in about 3s (46 m away, suitability 40%)",
    "window 07:31:38 to 07:46:38",
    "resolved 656 alerts, rejected 100 pending decisions in 7 passes",
    "dock 3 will be busy in 20 min",
    "unacknowledged for 10 min (high alerts escalate after 10 min)",
    "17 occurrences folded into one row",
    "23000 messages in dlq.tracks",
    "critical severity, 70% confidence, fuel_store is a critical area (x2)",
    "on held-out data the 90% interval contained the truth 100% of the time over 4 folds",
]


@pytest.mark.parametrize("text", PHONE_NUMBERS)
def test_a_phone_number_is_redacted_whole(text: str) -> None:
    """No surviving digits. A fragment is worse than either extreme.

    `+44 20 7946 0958` became `+44 20 <PHONE>` on the first attempt, because the optional prefix group
    could not absorb a two-digit area code so the match began mid-number. The remaining "+44 20" looks like
    a deliberate disclosure, and enough fragments identify the number anyway.
    """
    result = redact_text(text)
    assert "<PHONE>" in result.text, f"no phone detected in {text!r}"
    leftover = re.findall(r"\d", result.text.replace("<PHONE>", ""))
    assert not leftover, f"partial redaction left {leftover} in {result.text!r}"


@pytest.mark.parametrize("text", OPERATIONAL_TEXT)
def test_operational_text_is_left_exactly_alone(text: str) -> None:
    """Over-redaction is the failure that switches the whole control off.

    Every string here is real output from this platform. An answer with its coordinates or its confidence
    replaced by `<PHONE>` is not a redacted answer, it is a broken one — and the response to a broken
    privacy feature is to disable it, which leaks everything.
    """
    assert redact_text(text).text == text


def test_emails_and_cards_and_plates_are_caught() -> None:
    result = redact_text(
        "AB12 CDE seen; bob.smith@example.com paid with 4111 1111 1111 1111 from 10.0.0.14"
    )
    assert "<EMAIL>" in result.text
    assert "<CREDIT_CARD>" in result.text
    assert "<UK_PLATE>" in result.text
    assert "<IP_ADDRESS>" in result.text
    assert "bob.smith" not in result.text
    assert "4111" not in result.text


def test_a_sensitive_field_is_redacted_whatever_its_value() -> None:
    """ "Bob" defeats every name detector ever written.

    Where the schema already says a field holds a name, redacting by field name is strictly more reliable
    than any amount of pattern matching — and it costs nothing.
    """
    payload = {"driver_name": "Bob", "zone_id": "dock_3", "speed": 12.4}
    result, found = redact_payload(payload)
    assert result["driver_name"] == "<REDACTED>"
    assert result["zone_id"] == "dock_3", "a non-sensitive field must survive"
    assert result["speed"] == 12.4, "a non-string must pass through untouched"
    assert found["FIELD"] == 1


def test_redaction_walks_nested_structures() -> None:
    payload = {
        "entities": [
            {"label": "Truck 1", "driver": "Alice", "contact": "call +44 20 7946 0958"},
            {"label": "Truck 2", "driver": "Bob"},
        ],
        "meta": {"note": "email ops@example.com"},
    }
    result, found = redact_payload(payload)
    assert result["entities"][0]["driver"] == "<REDACTED>"
    assert result["entities"][1]["driver"] == "<REDACTED>"
    assert "<PHONE>" in result["entities"][0]["contact"]
    assert "<EMAIL>" in result["meta"]["note"]
    assert result["entities"][0]["label"] == "Truck 1"
    assert found["FIELD"] == 2


def test_every_sensitive_field_name_is_lowercase() -> None:
    """The lookup is `key.lower() in SENSITIVE_FIELDS`, so a capitalised entry would never match.

    A silent miss in a privacy control, and exactly the kind of thing that is never noticed.
    """
    assert all(name == name.lower() for name in SENSITIVE_FIELDS)


def test_the_notice_says_what_was_removed_and_by_which_detector() -> None:
    """Said out loud rather than implied.

    An answer with a name silently removed reads as an answer that never had one, and the reader draws a
    conclusion from an absence they were not told about. The detector is named because "redacted" by a regex
    is a weaker claim than by Presidio, and the reader is entitled to know which they got.
    """
    notice = redaction_notice({"EMAIL": 2, "PHONE": 1}, "regex")
    assert notice is not None
    assert "2 email" in notice
    assert "1 phone" in notice
    assert "regex" in notice
    assert "pii_scope" in notice, "it must say how to see the unredacted version"


def test_no_notice_when_nothing_was_redacted() -> None:
    """A notice on every response would train the reader to ignore it."""
    assert redaction_notice({}, "regex") is None


def test_an_empty_string_is_handled() -> None:
    assert redact_text("").text == ""


def test_the_active_detector_is_reported_honestly() -> None:
    """Presidio is optional and its absence must not be silent.

    A governance control that only works when an optional dependency is installed is absent in most
    installs. The regex path always runs; which detector ran is reported so the claim is qualified.
    """
    assert active_detector() in ("regex", "presidio")
