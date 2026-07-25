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

#: The false positives the live system actually produced, and the shapes behind them.
#:
#: These were not caught by the invented corpus below — they came from asking the running copilot a question
#: about entity counts and reading its response, which announced that "1 ip address, 4 phone" had been
#: redacted. The four "phone numbers" were the ISO timestamps in its own explanation timeline, and the "ip
#: address" was the loopback URL in a note saying where the answer came from. The redaction had corrupted the
#: provenance it was attached to.
LIVE_FALSE_POSITIVES = [
    "2026-07-25T11:01:50.645644Z",
    "queried http://127.0.0.1:8000/api/entities",
    "seen at 10.0.0.14 internally",
    "window 2026-07-24 to 2026-07-25",
    "history spans 2026-07-24T19:24:54 to 2026-07-25T09:06:17",
    "forecast for 2026-7-4",
    "version 1.2.3",
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
    """A public IP here, not `10.0.0.14`.

    This test originally used a private address and asserted it was redacted, which encoded the behaviour
    before the false-positive fix. A private or loopback address identifies infrastructure rather than a
    person, and redacting one out of an explanation destroys provenance without protecting anybody.
    """
    result = redact_text(
        "AB12 CDE seen; bob.smith@example.com paid with 4111 1111 1111 1111 from 203.0.113.42"
    )
    assert "<EMAIL>" in result.text
    assert "<CREDIT_CARD>" in result.text
    assert "<UK_PLATE>" in result.text
    assert "<IP_ADDRESS>" in result.text
    assert "bob.smith" not in result.text
    assert "4111" not in result.text
    assert "203.0.113" not in result.text


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


# --- the false positives the live system produced -----------------------------------------------
@pytest.mark.parametrize("text", LIVE_FALSE_POSITIVES)
def test_the_live_false_positives_are_left_alone(text: str) -> None:
    """Every one of these was redacted by the shipped patterns, in the running system.

    Two invariants fixed them, and both are properties of the world rather than tuned thresholds:

    * **no phone number has fewer than seven digits**, which rules out "2026-07";
    * **`YYYY-MM-DD` is a date in every locale**, which rules out "2026-07-24" — eight digits, so the floor
      alone could not.

    And a loopback or private address identifies infrastructure, not a person. Redacting `127.0.0.1` out of a
    note that says where an answer came from destroys the provenance without protecting anybody.
    """
    assert redact_text(text).text == text


def test_a_public_ip_is_still_redacted() -> None:
    """Excluding private ranges must not exclude the addresses that can identify somebody."""
    result = redact_text("connection from 203.0.113.42")
    assert "<IP_ADDRESS>" in result.text
    assert "203.0.113" not in result.text


def test_a_short_digit_run_is_never_a_phone_number() -> None:
    """Seven digits is not a tuning parameter: it is the shortest phone number that exists."""
    from sio_core.pii import _MINIMUM_DIGITS

    assert _MINIMUM_DIGITS["PHONE"] == 7
    assert redact_text("bay 12-34").text == "bay 12-34"
    assert redact_text("ratio 100-200").text == "ratio 100-200"


def test_a_notice_is_not_produced_for_an_operational_answer() -> None:
    """The harm of a false positive is double.

    It corrupts the text — an explanation with its source URL replaced by `<IP_ADDRESS>` no longer says where
    the answer came from — and it erodes the notice itself. An operator who sees "personal data was removed"
    on an answer about truck counts learns to ignore the sentence, and then misses the one that matters.
    """
    from sio_core.pii import active_detector, redaction_notice

    answer = (
        "There are 33 moving entities seen in the last 5 minutes: 12 trucks, 12 people, "
        "3 forklifts and 2 drones. Queried at 2026-07-25T11:01:50Z."
    )
    result = redact_text(answer)
    assert result.text == answer
    assert redaction_notice(result.found, active_detector()) is None
