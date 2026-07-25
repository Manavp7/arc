"""A flood-warning rule, added from outside the tree.

Reads `water_level_m` — a field that exists only because this package's connector publishes it. Nothing in the
core knows the field exists, which is the point: the platform accepted a new *kind* of signal and a new
conclusion about it without being told about either in advance.

Exported as a **callable** rather than a value, so the threshold can be read from the environment at load time.
The plugin loader accepts a `Rule`, a list of them, a dict, or a callable returning any of those — the three
shapes a rule author naturally reaches for. Insisting on one would make most authors write a wrapper that
exists only to satisfy the loader.
"""

from __future__ import annotations

import os
from typing import Any

#: Metres above datum at which the warning fires.
#:
#: Read from the environment because a threshold is a deployment decision, not a code one — a gauge on an
#: estuary and one in a dock have different meaningful levels, and forcing a fork of the plugin to change a
#: number would defeat the purpose of shipping it as a package.
DEFAULT_THRESHOLD_M = 1.5


def threshold_m() -> float:
    try:
        return float(os.environ.get("SIO_TIDE_WARNING_M", DEFAULT_THRESHOLD_M))
    except ValueError:
        # A bad value falls back rather than raising: a malformed environment variable must not stop the rule
        # from loading, because the consequence of not loading is silence about flooding.
        return DEFAULT_THRESHOLD_M


def tide_flood_warning() -> dict[str, Any]:
    """The rule, as the dict shape the loader validates into a `Rule`.

    Returned as a dict rather than a constructed `Rule` so this package does not import anything from the
    events service — `Rule` lives there, and depending on it would couple the plugin to a service's internals
    exactly as the connector contract used to.
    """
    level = threshold_m()
    return {
        "id": "tide_flood_warning",
        "emits": "anomaly_detected",
        "severity": "high",
        "description": (
            f"Water level above {level} m at a tide gauge. Added by sio-plugin-demo, which also supplies the "
            f"gauge connector that produces the reading."
        ),
        # "observation", not "iot".
        #
        # The kind is the FACT kind the events engine assigns — one of entity, observation, event, detection,
        # track — not the observation's modality. I wrote "iot" and the rule was filtered out before its
        # condition was ever evaluated, so it sat loaded and enabled and matched nothing.
        #
        # Second wrong guess in this one rule, and the same class as the first: writing against a plausible
        # convention instead of the real one, with no error either time. Narrowing by kind is still worth doing
        # — it keeps the rule out of the hot path for signals it cannot possibly match — but the value has to
        # be right, and docs/PLUGINS.md now lists the five.
        "kinds": ("observation",),
        # `payload.water_level_m`, not `water_level_m`.
        #
        # The platform exposes a sensor observation's payload as a nested dict under `payload`, so a rule
        # reaches its own connector's fields with a dotted path. I got this wrong first time and the rule
        # loaded, enabled, matching nothing — the worst failure shape available, because everything reported
        # success. The convention was documented only in a docstring inside the events service, which a plugin
        # author is told not to read; it is now in docs/PLUGINS.md.
        "when": [{"field": "payload.water_level_m", "op": ">=", "value": level}],
        # Twenty minutes, because a tide crosses a threshold slowly and re-crosses it on the noise. A short
        # cooldown here would produce a burst of identical warnings on one rising tide, which is how an alert
        # channel gets muted.
        "cooldown_seconds": 1200.0,
        "cooldown_key": ("source_id",),
        "confidence": 0.85,
        "explanation": (
            "The tide gauge reported a water level above the configured warning threshold. This rule and the "
            "connector that feeds it both come from an installed plugin; neither required a change to the "
            "platform."
        ),
        "attributes": {"plugin": "sio-plugin-demo", "threshold_m": level},
    }


__all__ = ["DEFAULT_THRESHOLD_M", "threshold_m", "tide_flood_warning"]
