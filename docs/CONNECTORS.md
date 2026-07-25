# Connectors

Every signal enters the platform through a connector. Nine ship in-tree; more can be added
[as plugins](PLUGINS.md) without changing core code.

```
connector ──► Observation ──► raw.<topic> ──► perception / events / fusion
```

A connector's whole job is to produce `Observation` objects. The ingest service handles publishing, topic
routing, backpressure, error isolation and metrics — which is why the interface is small enough to fit on one
screen (`sio_core.connector.Connector`).

## Configuring one

Connectors beyond the defaults are declared in `.sio/plugins.json`, which despite the name configures **any**
registered kind, in-tree or out:

```json
{
  "connectors": [
    {
      "source_id": "wms-dock-bookings",
      "kind": "csv_enterprise",
      "modality": "enterprise",
      "label": "WMS dock bookings",
      "enabled": true,
      "options": {
        "path": "/srv/exports/bookings.csv",
        "interval_s": 60,
        "mapping": { "lat": "latitude", "lon": "longitude", "label": "trailer" }
      }
    }
  ]
}
```

One bad entry does not stop the others: a connector whose options are wrong is reported on `/health` and
`/connectors` by name, with the reason. A plugin that silently fails to load is indistinguishable from one that
loaded and does nothing, and debugging that difference through log archaeology is how people stop using plugins.

## Optional dependencies

Phase 7 connectors are behind extras, and each looks its dependency up **inside `start()`** so a default install
stays fast and a misconfiguration says what to install:

```bash
uv pip install 'sio-ingest[rtsp]'        # opencv-python-headless
uv pip install 'sio-ingest[mqtt]'        # aiomqtt
uv pip install 'sio-ingest[mavlink]'     # pymavlink
uv pip install 'sio-ingest[enterprise]'  # sqlalchemy
uv pip install 'sio-ingest[connectors]'  # all of them
```

A default `uv sync` that pulled in OpenCV (60MB), a MAVLink dialect generator and a database driver would take
minutes and serve almost nobody — a deployment uses one or two of these, not all six.

---

## `camera_rtsp` — a real camera

Modality `video` → `raw.frames`. Needs `[rtsp]`.

| option | default | |
|---|---|---|
| `url` | — | `rtsp://user:pass@host/stream` (required) |
| `publish_fps` | 2 | the rate frames are **published** at |
| `transport` | `tcp` | `tcp` or `udp` |
| `backend` | `opencv` | or `gstreamer` for hardware decode |
| `width` | full | downscale before encoding |
| `store_frames` | true | write JPEGs to object storage |

**It decimates after reading, not instead of reading.** A camera at 25fps is 25 chances a second to run a
detector that takes 80ms, and the naive version falls behind and then reports the world as it was thirty seconds
ago — the worst failure available for a live platform, because the map looks fine. Every frame is read (skipping
reads desynchronises the decoder and produces *corrupt* frames rather than missing ones) and publishing is
throttled to `publish_fps`.

**The frame key goes in `raw_ref`, not the payload.** `services/perception` reads `observation.raw_ref` and skips
any observation without one. The first version put it in `payload["frame_key"]`, which would have meant
perception never processed a single real camera frame — silently, while every counter said healthy.

**TCP by default.** UDP loses packets and OpenCV's recovery is to produce green smears, which a detector then
reports as objects: a failure mode that looks like a model problem.

Credentials are stripped from every log line, health payload and error message. `rtsp://admin:hunter2@host` is
the normal form for a camera, and `describe()` is served on `/health`.

Reconnects after 30 consecutive failed reads — a few dropped frames is a hiccup, thirty in a row is a camera
that has rebooted — after a fixed 3s pause. Not exponential backoff: a camera reboots in about ten seconds, and
backing off to minutes means one that recovered stays dark because we stopped asking.

## `mqtt_iot` — industrial sensors

Modality `iot` → `raw.iot`. Needs `[mqtt]`.

| option | default | |
|---|---|---|
| `host` / `port` | `127.0.0.1` / 1883 | |
| `topics` | `["sio/#"]` | |
| `qos` | 0 | |
| `username` / `password` | — | |
| `mapping` | `{}` | where to find `lat`, `lon`, `label` in the body |

Uses **aiomqtt**, not paho. Paho delivers on its own network thread, so bridging it to an async generator needs a
queue plus `call_soon_threadsafe`, a bound on that queue, and a drop policy — forty lines of concurrency in a
connector, every line a place for a bug that appears once a week under load. aiomqtt exposes
`async for message in client.messages`, which is exactly the shape `observations()` needs.

A bare payload is wrapped rather than rejected: plenty of sensors publish `21.5` on `site/temp/3`, and refusing
that would rule out a large share of real deployments. It is coerced to a **number** — leaving it a string makes
every downstream comparison a string comparison, where `"9" > "10"`.

The topic travels in the payload, because it is frequently the only place the sensor's identity or location
appears: `site/dock_3/temperature` says more than the body does.

## `satellite_stac` — Sentinel-2

Modality `satellite` → `raw.satellite`. No extra needed for metadata.

| option | default | |
|---|---|---|
| `stac_url` | Earth Search v1 | |
| `collection` | `sentinel-2-l2a` | |
| `lat` / `lon` / `box_deg` | site / 0.05 | |
| `max_cloud_percent` | 30 | |
| `lookback_days` | 14 | |
| `assets` | `visual`, `nir` | |
| `interval_s` | 21600 | six hours |

**No API key.** Earth Search is open and Sentinel-2 L2A on AWS is free. A connector needing a credential is one
nobody tries, and demonstrating real satellite ingest with no signup is worth more than the extra collections a
keyed provider would offer.

**Cloud cover is a filter, not metadata.** A 90%-cloud scene is not a worse observation — it is not an
observation, because the site is not in it. One reaching the world model would put a white square in it and a
spurious "conditions changed" in the event stream. Filtered client-side as well as in the query, because not
every STAC implementation honours `query`.

**Capture time is distinct from ingest time.** A pass from three days ago ingested today is an observation *of*
three days ago; conflating them would let a satellite scene contradict a camera about the present.

Built around change rather than a rate: on most days there is no new scene, and a connector that warned about
that would train its operator to ignore the log.

## `drone_mavlink` — ArduPilot / PX4 / DJI

Modality `gps` → `raw.gps`. Needs `[mavlink]`.

| option | default | |
|---|---|---|
| `connection` | `udpin:0.0.0.0:14550` | anything pymavlink accepts |
| `min_interval_s` | 0.5 | position throttle |
| `source_system` | 255 | our own MAVLink id |

There is no `Modality.DRONE`, and inventing one would be wrong anyway: what this produces is a **position
report**. The vehicle is identified in the payload and `source_id` says which link it came over.

**Unit conversions are where this connector can be catastrophically wrong**, so they are named constants with
tests:

* degrees arrive as `int × 1e7` — a missing factor of ten is 500km;
* velocity arrives in cm/s in the **NED** frame, and this platform's `Velocity` is north/east/**up**, so
  `up = -vz`. Without the negation a descending drone reads as climbing, which survives every test that checks
  only magnitudes.

**"Not reported" is not a value.** Battery `-1`, heading `65535`, position `0,0` — each is an autopilot saying it
does not know. Treating them as values gives you a fleet that all appears about to fall out of the sky, every
drone facing 655°, and an entity in the Atlantic skewing every spatial query.

Battery and mode are *state*, not events: they update quietly and ride along on the next position report rather
than producing observations of their own.

Tested against a **fake connection**, not SITL. SITL is right for a pre-flight check and wrong for a test suite:
200MB, 30 seconds to boot, and what it exercises is ArduPilot's flight logic rather than this file's arithmetic.

## `csv_enterprise` — a nightly export

Modality `enterprise` → `raw.enterprise`.

| option | default | |
|---|---|---|
| `path` | — | required |
| `interval_s` | 60 | |
| `mapping` | `{}` | `lat`, `lon`, `label` → your column names |
| `modality` | `enterprise` | override if the rows are something else |

Watches modification time, so a 200MB nightly export costs one `stat` per interval instead of a full parse. The
whole file is re-read when it does change: an export is a snapshot, and diffing snapshots to find "new" rows
guesses at a primary key the file may not have.

Refuses a missing path at **startup** rather than logging every minute — that is a deployment mistake, and
finding it in a log an hour later wastes the hour.

## `sql_enterprise` — a live query

Modality `enterprise` → `raw.enterprise`. Needs `[enterprise]`.

| option | default | |
|---|---|---|
| `dsn` | — | any SQLAlchemy URL |
| `query` | — | a single `SELECT` |
| `interval_s` | 300 | |
| `mapping` | `{}` | |

**Read-only, enforced at configuration time**, before a connection is opened — a guarantee checked after the
connection is open is one checked after the statement could have run:

```
ValueError: this connector is read-only and will not run a DELETE statement.
Only SELECT and WITH are permitted — a connector that can write to a system of
record is one nobody will let you install.
```

A **whitelist**, not a blacklist of `DROP`/`DELETE`/`UPDATE`: a blacklist has to anticipate every way a specific
dialect can write and it only takes one gap. A second statement is also refused, because
`SELECT 1; DROP TABLE bookings` starts with SELECT and a prefix check cannot vouch for what follows a semicolon.

Runs on a thread. SQLAlchemy's sync engine blocks, and blocking the loop would make a slow enterprise database a
platform-wide outage.

### Both enterprise connectors

**Unmapped columns are kept**, not dropped. The platform does not know which of a customer's forty columns will
matter, and discarding them means a later question — "was this trailer flagged hazmat?" — cannot be answered
without re-ingesting history this platform deliberately never deletes.

**Neither asserts identity**, and structurally cannot: `Observation` has no `entity_id`. Fusion decides identity,
and a WMS trailer number able to overrule a track the perception stack has followed for ten minutes would be a
bad trade. The external key travels in the payload as a hint.

## `traffic_incidents` — the roads in

Modality `traffic` → `raw.traffic`.

| option | default | |
|---|---|---|
| `url` | — | a GeoJSON incident feed (required) |
| `lat` / `lon` | site | |
| `radius_km` | 25 | |
| `provider` | `open_incidents` | |
| `interval_s` | 300 | |

A truck's arrival time is decided on the motorway, not at the gate. If the approach is blocked, every dock slot
behind it slips, and the platform can say so twenty minutes before the yard notices.

`open_incidents` is a *shape*, not a vendor: any endpoint returning GeoJSON features with a
`properties.description` works, which covers most national and municipal open-data feeds. A commercial provider
overrides one method (`_fetch`) rather than reimplementing the distance filter, the deduplication and the
mapping.

Distance uses **haversine**, not a flat approximation — treating degrees as a grid is wrong by tens of percent at
the latitudes where most freight moves — and travels with the incident, because "3km from the gate" and "24km
away" warrant different responses and the consumer should not redo the trigonometry.

Deduplicated, because incidents persist in a feed for hours: without it a single closed lane produces an
observation every five minutes for a day and the event engine keeps re-deciding about it.

## `weather_openmeteo` — conditions

Modality `weather` → `raw.weather`. Shipped in Phase 1. No key required. Feeds the fire-spread simulation's wind
model, which is why it exists rather than being decoration.

## `simulator` — the synthetic yard

Modality `manual` → `raw.enterprise`. The default when nothing else is configured, and what `just demo` runs on.

---

## Adding one

In-tree: subclass `Connector`, decorate with `@register_connector`, import it in
`services/ingest/src/sio_ingest/connectors/__init__.py`. **The import matters** — a connector that is never
imported is never registered, and `build_connector` reports it as unknown while the file sits there looking
complete.

Out-of-tree: see [PLUGINS.md](PLUGINS.md). The contract lives in `sio_core.connector` precisely so a plugin never
has to import a service's internals.

Either way, declare a **real** `Modality`. Three of the Phase 7 connectors initially declared members that do not
exist (`Modality.DRONE`, `Modality.CAMERA`); two failed loudly at import, and the third — `MANUAL` on an
enterprise connector — would have been accepted silently and made every downstream modality filter wrong. And
check `TOPIC_BY_MODALITY`: a modality with no entry falls back to `raw.iot`, which is where the events engine
reads sensor readings.
