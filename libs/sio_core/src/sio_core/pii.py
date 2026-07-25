"""Personal data, redacted by default (PRD M19, Phase 5).

Two things are being protected here and they need different mechanisms:

* **text** — names, plates, phone numbers, emails, faces described in prose. Copilot answers, OCR output,
  generated reports. Handled here.
* **pixels** — faces and number plates in stored frames. Handled in `vision/redact.py`, before anything
  reaches object storage, because a frame written unblurred is unblurred forever.

**Redaction is on by default and the default is the point.** A privacy control that must be switched on is
one that is off in every deployment where nobody thought about it, which is all of them. `pii.view` plus the
`pii_scope` claim turns it off for a principal who has both, and that combination is deliberately harder to
obtain than a role alone.

**Presidio is optional and its absence is not silent.** The PRD names Presidio, and it is genuinely better
than regular expressions at names and addresses. But a governance control that only functions when an
optional dependency is installed is a control that is absent in most installs — so there is a regex
detector that always works, Presidio is used when importable, and `active_detector()` reports which one ran.
The reported detector is carried into the response, because "redacted" from a weaker detector is a weaker
claim and the reader is entitled to know which they got.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .telemetry import get_logger

log = get_logger("sio.pii")

#: What a redaction replaces a match with. Typed rather than blanked, because "<PERSON>" tells a reader that
#: a name was there and "████" tells them only that something was removed — and the shape of the sentence
#: usually makes the difference between a comprehensible answer and a confusing one.
PLACEHOLDER = "<{kind}>"

#: Patterns that always work, with no optional dependency.
#:
#: Ordered by specificity: an email contains no digits but a phone number does, and a plate pattern will
#: happily match part of a longer identifier, so the longer and more structured patterns are tried first.
#: Getting this order wrong produces half-redacted strings, which are worse than either extreme because they
#: look deliberate.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    (
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    ),
    (
        "CREDIT_CARD",
        re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    ),
    (
        "PHONE",
        # Conservative about *whether* it matches, greedy about *how much* it takes.
        #
        # The first version left fragments behind: "+44 20 7946 0958" became "+44 20 <PHONE>" and
        # "07700 900123" became "0<PHONE>23", because the optional prefix groups could not absorb a
        # two-digit area code or a leading trunk zero, so the match began mid-number.
        #
        # A half-redacted phone number is worse than either extreme. The surviving fragment looks like a
        # deliberate disclosure, so a reader treats it as safe to pass on — and enough fragments identify
        # the number anyway.
        #
        # Still conservative on the other axis: at least two separated digit groups are required, so a lone
        # six-digit identifier is not eaten. An over-redacted answer is unusable, which is how a privacy
        # control ends up switched off wholesale.
        # NO DOT as a separator, deliberately. Adding one caught the European `01.23.45.67` format and
        # immediately ate "51.50735" out of "Truck at 51.50735, -0.12776" — and this platform's text is
        # full of decimals: coordinates, speeds, confidences, battery percentages. Over-redacting
        # coordinates out of every answer is how a privacy control gets switched off wholesale, so the
        # dot loses.
        # Two alternatives, because a country code licenses a looser rest.
        #
        # With `+353 1 234 5678` the area code is a single digit, which a 2-5 digit group cannot match — so
        # the first attempt left "+353 1 <PHONE>", exactly the fragment problem this pattern exists to
        # avoid. But allowing a single-digit first group *unconditionally* would match far too much ordinary
        # text ("dock 3 will be busy in 20 min"), so it is permitted only after an explicit `+NN`.
        re.compile(
            r"(?:"
            r"\+\d{1,3}[\s-]?(?:\(?\d{1,5}\)?[\s-])+\d{2,9}"  # international: groups may be 1 digit
            r"|"
            r"\(?\d{2,5}\)?[\s-](?:\d{2,4}[\s-])*\d{2,9}"  # domestic: first group needs 2+ digits
            r")\b"
        ),
    ),
    (
        "UK_PLATE",
        re.compile(r"\b[A-Z]{2}\d{2}\s?[A-Z]{3}\b"),
    ),
    (
        "US_PLATE",
        re.compile(r"\b[A-Z]{3}[-\s]?\d{3,4}\b"),
    ),
    (
        "IP_ADDRESS",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ),
    (
        "NATIONAL_ID",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
)

#: Field names whose *values* are personal regardless of what they look like.
#:
#: A driver's name is a name whether or not a detector recognises it, and "Bob" defeats every name detector
#: ever written. Redacting by field name where the schema already says what a field means is more reliable
#: than any amount of pattern matching.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "driver_name",
        "driver",
        "person_name",
        "full_name",
        "email",
        "phone",
        "phone_number",
        "address",
        "home_address",
        "national_id",
        "employee_id",
        "badge_number",
        "plate",
        "plate_text",
        "licence_plate",
        "license_plate",
        "face_embedding",
        "voice_embedding",
    }
)


@dataclass
class Redaction:
    """The result of redacting one string, and what was found."""

    text: str
    found: dict[str, int] = field(default_factory=dict)
    detector: str = "regex"

    @property
    def changed(self) -> bool:
        return bool(self.found)

    def describe(self) -> dict[str, Any]:
        return {"detector": self.detector, "found": dict(self.found), "changed": self.changed}


@lru_cache(maxsize=1)
def presidio_available() -> bool:
    try:
        import presidio_analyzer  # noqa: F401
        import presidio_anonymizer  # noqa: F401
    except ImportError:
        return False
    return True


def active_detector() -> str:
    """Which detector will run. Carried into responses so a claim of redaction is qualified honestly."""
    return "presidio" if presidio_available() else "regex"


def redact_text(text: str, *, detector: str | None = None) -> Redaction:
    """Redact personal data in a string.

    The regex path always runs, even when Presidio is available. Presidio finds names and addresses that a
    pattern cannot; a pattern catches structured identifiers that Presidio's default recognisers miss or
    score below their threshold. Running both and taking the union costs microseconds on the string lengths
    involved here, and the alternative is choosing which class of leak to accept.
    """
    if not text:
        return Redaction(text="", detector=detector or active_detector())

    result = text
    found: dict[str, int] = {}

    for kind, pattern in PATTERNS:
        matches = pattern.findall(result)
        if matches:
            result = pattern.sub(PLACEHOLDER.format(kind=kind), result)
            found[kind] = found.get(kind, 0) + len(matches)

    chosen = detector or active_detector()
    if chosen == "presidio" and presidio_available():
        result, extra = _presidio_pass(result)
        for kind, count in extra.items():
            found[kind] = found.get(kind, 0) + count

    return Redaction(text=result, found=found, detector=chosen)


def _presidio_pass(text: str) -> tuple[str, dict[str, int]]:
    """Presidio's turn, for the entity types a regex cannot reach."""
    try:
        from presidio_anonymizer import AnonymizerEngine

        analyzer = _presidio_analyzer()
        results = analyzer.analyze(
            text=text,
            language="en",
            entities=["PERSON", "LOCATION", "NRP", "DATE_TIME", "MEDICAL_LICENSE"],
        )
        # A confidence floor, because Presidio's default recognisers will label a zone name a LOCATION and
        # a truck's label a PERSON. Redacting "dock_3" out of every answer makes the copilot useless, which
        # is how a privacy control gets switched off wholesale.
        results = [item for item in results if item.score >= 0.6]
        if not results:
            return text, {}
        anonymised = AnonymizerEngine().anonymize(text=text, analyzer_results=results)
        counts: dict[str, int] = {}
        for item in results:
            counts[item.entity_type] = counts.get(item.entity_type, 0) + 1
        return anonymised.text, counts
    except Exception as exc:
        # Never let a redactor's failure become an unredacted response. The regex pass has already run, so
        # returning its output is a degraded result rather than a leak.
        log.warning("pii.presidio_failed", error=type(exc).__name__, detail=str(exc)[:120])
        return text, {}


@lru_cache(maxsize=1)
def _presidio_analyzer() -> Any:
    from presidio_analyzer import AnalyzerEngine

    # Built once: loading the NLP model per call turns a 2 ms redaction into a 3-second one, and this runs
    # on every copilot answer.
    return AnalyzerEngine()


def redact_payload(payload: Any, *, detector: str | None = None) -> tuple[Any, dict[str, int]]:
    """Walk a JSON-shaped structure, redacting strings and sensitive fields.

    Two mechanisms, because they fail differently. Pattern matching catches an email wherever it appears,
    including inside prose the schema knows nothing about. Field-name matching catches "Bob" in
    `driver_name`, which defeats every name detector ever written.
    """
    found: dict[str, int] = {}

    def walk(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {name: walk(value, name) for name, value in node.items()}
        if isinstance(node, list):
            return [walk(item, key) for item in node]
        if isinstance(node, str):
            if key.lower() in SENSITIVE_FIELDS and node:
                found["FIELD"] = found.get("FIELD", 0) + 1
                return PLACEHOLDER.format(kind="REDACTED")
            redaction = redact_text(node, detector=detector)
            for kind, count in redaction.found.items():
                found[kind] = found.get(kind, 0) + count
            return redaction.text
        return node

    return walk(payload), found


def redaction_notice(found: dict[str, int], detector: str) -> str | None:
    """A sentence for the reader, or None when nothing was redacted.

    Said out loud rather than implied. An answer with a name silently removed reads as an answer that never
    had one, and the reader draws a conclusion from an absence they were not told about.
    """
    if not found:
        return None
    parts = ", ".join(
        f"{count} {kind.lower().replace('_', ' ')}" for kind, count in sorted(found.items())
    )
    return (
        f"Personal data was removed from this response ({parts}), detected by the {detector} detector. "
        "A principal with the pii.view permission and the pii_scope claim sees it unredacted."
    )


__all__ = [
    "PATTERNS",
    "PLACEHOLDER",
    "SENSITIVE_FIELDS",
    "Redaction",
    "active_detector",
    "presidio_available",
    "redact_payload",
    "redact_text",
    "redaction_notice",
]
