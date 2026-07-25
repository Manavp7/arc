# Extending SIO without changing it

Four extension points, discovered through Python entry points. Install a package; the platform picks it up.

```toml
[project.entry-points."sio.connectors"]
tide_gauge = "my_plugin.connector:TideGaugeConnector"

[project.entry-points."sio.rules"]
tide_flood_warning = "my_plugin.rules:tide_flood_warning"
```

| group | adds | consumed by |
|---|---|---|
| `sio.connectors` | a data source | ingest |
| `sio.rules` | an event rule | events |
| `sio.tools` | a copilot tool | copilot |
| `sio.agents` | an autonomous agent | agents |

A working example lives in [`examples/plugin_demo`](../examples/plugin_demo): a tide gauge connector and a
flood-warning rule. `just plugin-demo` installs it and prints what the platform then sees.

---

## The three things that will waste your afternoon

This document exists mostly for this section. I wrote the example plugin, it installed, both extensions were
discovered and reported as loaded, the connector published observations — **and the rule matched nothing, three
times running.** Each time the platform reported success: rule loaded, rule enabled, no errors. That is the
worst failure shape available, because there is nothing to search for.

Every one of the three was the same mistake: **writing against a plausible convention instead of the real one.**
The conventions were documented in docstrings inside the services, which a plugin author is explicitly told not
to read.

### 1. A rule's `kinds` are FACT kinds, not modalities

```python
"kinds": ("iot",)          # WRONG — silently matches nothing
"kinds": ("observation",)  # right
```

The events engine turns everything it consumes into a `Fact` with one of exactly five kinds:

| fact kind | comes from |
|---|---|
| `observation` | a sensor reading from any connector, whatever its modality |
| `detection` | something a model found in a frame |
| `track` | a tracked object across frames |
| `entity` | a fused entity in the world model |
| `event` | an event from another rule — this is what makes rules composable |

`iot`, `camera`, `gps` are **modalities**, which appear as a *field* on an observation fact. They are not fact
kinds. A rule narrowed to a kind that does not exist is filtered out before its conditions are ever evaluated,
and nothing reports it.

Narrowing by kind is still worth doing — it keeps your rule out of the hot path for signals it cannot possibly
match — but the value has to be one of the five above.

### 2. A connector's payload is reachable at `payload.*`

```python
{"field": "water_level_m", ...}          # WRONG — silently matches nothing
{"field": "payload.water_level_m", ...}  # right
```

An observation fact exposes these fields directly:

`source_id`, `modality`, `metric`, `value`, `unit`, `zone_id`, `lat`, `lon`

…and **your whole payload dict under `payload`**. So a field your own connector invented is reached with a
dotted path. `metric`/`value`/`unit` are lifted to the top level because most in-tree sensors use that shape;
if yours does too, you can match on `metric` and `value` directly.

### 3. Your connector's physics is real, so check the value before blaming the rule

My tide gauge sat at 1.31 m against a 1.5 m threshold and I assumed the rule was broken. The tide was out. The
observation was on the bus and correct; the config was wrong.

Read the actual value before debugging the rule:

```bash
redis-cli --no-raw XREVRANGE raw.iot + - COUNT 20 | grep -o '"water_level_m":[0-9.-]*'
```

---

## Writing a connector

Depend on **`sio-core` and `sio-schemas`, and nothing else.** If you find yourself importing `sio_ingest`, stop:
you are reaching into a service's private module, and it will break on a refactor the platform is entitled to
make. The contract lives in `sio_core.connector` precisely so you do not have to.

```python
from sio_core.connector import Connector, ConnectorConfig
from sio_schemas import Geo, Modality, Observation, utc_now

class TideGaugeConnector(Connector):
    kind = "tide_gauge"          # what a deployment writes in .sio/plugins.json
    modality = Modality.IOT

    async def observations(self):
        while True:
            yield Observation(
                tenant_id="default",
                source_id=self.source_id,
                modality=Modality.IOT,
                ts=utc_now(),
                geo=Geo(lat=self.lat, lon=self.lon),
                confidence=0.95,
                payload={"water_level_m": 2.4},   # reachable as payload.water_level_m
            )
            await asyncio.sleep(self.interval_s)
```

`start`, `stop`, `health` and `describe` are optional. The service handles publishing, backpressure, error
isolation and metrics — you write only the part specific to your source, which is why this interface is small.

**`stop` must not raise.** It runs during shutdown, where an exception masks whatever prompted the shutdown and
leaves the remaining connectors unstopped.

### Nothing runs merely by being installed

A plugin declares what it *can* do. A deployment says what should actually run, in `.sio/plugins.json`:

```json
{
  "connectors": [
    {
      "source_id": "tide-embarcadero",
      "kind": "tide_gauge",
      "label": "Embarcadero tide gauge",
      "modality": "iot",
      "options": { "lat": 37.7768, "lon": -122.4181, "interval_s": 60 }
    }
  ]
}
```

Installing a package must not start reading somebody's camera. `enabled: false` keeps a stanza in the file
while turning it off, so switching it back on is one word.

A `kind` no installed plugin provides is reported with the list of kinds that *are* registered, and the other
connectors still start. One bad stanza does not take down ingest.

---

## Writing a rule

Export a `Rule`, a list of them, a **dict**, or a callable returning any of those. All four work, because all
four are what somebody reaches for first.

Prefer the dict: it means your package does not import `Rule` from the events service, which keeps the same
decoupling the connector contract has.

```python
def tide_flood_warning() -> dict:
    return {
        "id": "tide_flood_warning",
        "emits": "anomaly_detected",
        "severity": "high",
        "kinds": ("observation",),                                   # see pitfall 1
        "when": [{"field": "payload.water_level_m", "op": ">=", "value": 1.5}],   # see pitfall 2
        "cooldown_seconds": 1200.0,
        "cooldown_key": ("source_id",),
        "explanation": "The gauge reported a level above the warning threshold.",
        "attributes": {"plugin": "my-plugin"},
    }
```

A **callable** lets you read configuration at load time — a threshold is a deployment decision, and forcing a
fork of the plugin to change a number defeats the point of shipping it as a package.

### Your rule cannot override the site's

If your rule's `id` collides with one defined in the site's own rule files, **yours is rejected** and the
collision is recorded in the rule set's errors. An installed package silently redefining somebody's fire rule
would be invisible — the rule would still fire, on your terms — so it is refused rather than resolved.

Pick an id nobody else will: prefix it with your package name if in doubt.

### Cooldowns

Think about the physical process. A tide crosses a threshold slowly and re-crosses it on noise, so the example
uses twenty minutes keyed on `source_id`. A short cooldown produces a burst of identical warnings on one rising
tide, which is how an alert channel gets muted.

---

## When a plugin does not load

Failures are never fatal — a third-party plugin must not stop the platform from starting — and they are
**reported rather than only logged**, because a plugin that silently fails to load is indistinguishable from one
that loaded and does nothing.

```bash
uv run python -c "from sio_core.plugins import discover_all; print(discover_all())"
curl -s localhost:8101/connectors -H "Authorization: Bearer $TOKEN" | jq   # includes plugin errors
curl -s localhost:8107/events/rules -H "Authorization: Bearer $TOKEN" | jq .errors
```

Exporting the wrong object is a *reported* failure, not a silent skip: the platform tells you it expected a
`Connector` subclass and what it got.

---

## Verifying your plugin

The example's tests are worth copying, and two of them exist because my own were wrong in instructive ways:

- **Parse imports, do not grep for them.** My first version searched for the string `sio_ingest` anywhere and
  failed on a docstring explaining why the import is absent. A test that forbids *discussing* a coupling
  produces false positives forever, and the second one is when somebody deletes the test.
- **Check "uses the public contracts" at package level, not per file.** `rules.py` imports neither library
  deliberately — it returns a plain dict. A per-file assertion objected to the decoupling it existed to
  enforce.

There is also a test asserting that **no file under `services/` or `libs/` mentions the example plugin**. That
is what makes "no core changes" a check rather than a claim.

```bash
just plugin-demo                              # install the example
uv run pytest tests/unit/test_plugins.py -v   # 22 tests
just plugin-demo-remove                       # and confirm the platform runs without it
```
