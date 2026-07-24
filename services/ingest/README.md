# ingest (M1)

Turns external signals into `Observation` envelopes on the bus. Everything downstream is written
against the envelope, never against a source — which is what makes adding a signal type a plugin
rather than a core change.

```
simulator ──┐
weather   ──┼──► ingest ──► raw.gps · raw.frames · raw.iot · raw.weather
RTSP/MQTT ──┘              (+ entities, Phase 1 bridge only)
```

## The connector interface

```python
@register_connector
class MyConnector(Connector):
    kind = "my_source"
    modality = Modality.IOT

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            yield Observation(source_id=self.source_id, modality=self.modality, ...)
```

The service handles publishing, topic routing by modality, backpressure, per-connector failure
isolation (one unplugged camera must not stall the other thirty-nine), restart-after-failure, and
health reporting. Out-of-tree connectors register through a `sio.connectors` entry point, so a
third party ships one as a pip-installable package.

## The yard simulator

A scripted 420 x 260 m distribution centre: 6 dock doors, 2 gates, staging, a restricted fuel
store, 8 cameras with real fields of view, 8 fixed sensors, and a cast of trucks, forklifts,
workers and a patrol drone.

Behaviour is **scripted state machines, not random walks**, because the demo has to produce the
PRD's use cases on purpose:

- trucks queue at Gate A, dock, dwell, and leave via Gate B — with dwell times drawn so that
  roughly one in three exceeds fifteen minutes, giving UC1 both positives and negatives;
- workers occasionally enter the restricted fuel store, seeding the `unauthorized_entry` rule;
- the drone drains a battery and swaps it, which is what the battery forecast and the
  "drone battery death" simulation act on;
- GPS carries ~2 m of scatter, so fusion has something to actually fuse;
- cameras only emit frames when something is genuinely inside their field of view, with ground-truth
  boxes attached for the Phase 2 detection eval.

Everything is seeded (`SIO_SIM_SEED`), so a run is reproducible — which is what makes the e2e
scenario tests deterministic.

### Injecting incidents

```bash
curl -X POST "localhost:8101/simulation/inject/fire?zone_id=dock_3"
curl -X POST "localhost:8101/simulation/inject/power_failure"
```

A fire ramps the dock's thermal sensor (a ramp, not a step — a step would flatter the anomaly
detector) and marks frames from covering cameras. This is how `just demo` and the fire-playbook e2e
test produce UC2 on demand.

## The Phase 1 entity bridge

Until `perception → tracking → fusion` exists (Phase 2), the simulator also publishes
ground-truth entities so the live map has something to show. They carry
`attributes.simulated = true` and provenance saying so, and the whole path is one flag:
`SIO_SIM_PUBLISH_ENTITIES=false` once fusion is live.

## Endpoints

| | |
|---|---|
| `GET /connectors` | registered kinds, running sources, publish counts per topic |
| `GET /site` | the site as GeoJSON |
| `GET /simulation` | agent counts, frames emitted, active incidents |
| `POST /simulation/inject/fire` | start a fire in a zone |
| `POST /simulation/inject/power_failure` | cut power to the office |
