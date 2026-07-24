# sio-schemas

The versioned data contracts every SIO service speaks. Nothing else in the repository may
define a cross-service payload shape — if two services exchange it, it lives here.

```python
from sio_schemas import Detection, Entity, Event, Topic, new_id, utc_now
```

## Contents

| module | contracts |
|---|---|
| `base.py` | `SioModel` (strict base), `Base` id/tenant/trace fields, `new_id`, `utc_now` |
| `enums.py` | `Topic`, `Modality`, `EntityType`, `RelationshipType`, `EventType`, `Severity`, … |
| `geo.py` | `Geo`, `BBox` |
| `perception.py` | `Observation`, `Detection`, `TrackState`, `Track` |
| `world.py` | `Provenance`, `EntityState`, `Entity`, `Relationship` |
| `reasoning.py` | `EvidenceRef`, `Explanation`, `Event`, `Forecast`, `Decision`, `Alert` |
| `ops.py` | `Mission`, `AuditRecord`, `SimulationRun`, `WorkflowRun`, `Webhook` |
| `bus.py` | `BusMessage` — the wire envelope for every topic |

## Rules

1. **JSON field names follow the PRD.** Where the PRD name collides with a Python keyword
   (`class`, `from`), the Python attribute is renamed (`class_name`, `from_id`) and an alias
   preserves the wire name. Serialisation always uses aliases, so the wire format is stable.
2. **Timestamps are timezone-aware UTC.** Naive datetimes are rejected, not coerced silently.
3. **`SCHEMA_VERSION` is stamped on every envelope.** Breaking changes bump the major and
   add a golden fixture under `tests/unit/fixtures/`.
4. **Extra fields are forbidden.** A typo in a producer is a loud error, not silent data loss.

## Exporting JSON Schema

```bash
uv run python -m sio_schemas.export --out docs/schemas
```
