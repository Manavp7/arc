# sio-core

The runtime spine every SIO service stands on: configuration, ports and their adapters, a
service base class, telemetry, and the explanation builder.

## Why this library exists

Without it, sixteen services each hand-roll the same Redis consumer loop, the same health
endpoint, the same Postgres pool and the same "which backend am I talking to" branching —
about 400 lines duplicated sixteen times, drifting apart immediately. With it, a service is
a payload handler plus a topic subscription.

## The ports

A *port* is a Protocol; an *adapter* implements it. **No service imports an adapter
directly** — services ask `sio_core.registry` for a port and get whatever the environment
selected. This is enforced by `tests/unit/test_architecture.py`, and it is what makes the
PRD's CPU→GPU swap a configuration change instead of a rewrite.

| Port | Adapters | Selector |
|---|---|---|
| `Bus` | `RedisStreamBus`, `MemoryBus`, `KafkaBus`* | `SIO_BUS_BACKEND` |
| `GraphStore` | `Neo4jGraphStore`, `PostgresGraphStore`, `MemoryGraphStore` | `SIO_GRAPH_BACKEND` |
| `VectorStore` | `PgVectorStore`, `MemoryVectorStore`, `QdrantStore`* | `SIO_VECTOR_BACKEND` |
| `BlobStore` | `MinioBlobStore`, `FileBlobStore` | `SIO_BLOB_BACKEND` |

\* stubbed until Phase 7.

Every port has a `memory`/`file` adapter, which is why the unit-test ring needs no
infrastructure at all.

## Usage

```python
from sio_core import SioService, Settings, get_bus
from sio_schemas import Detection, Topic

class MyService(SioService):
    name = "example"
    subscribes = (Topic.DETECTIONS,)

    async def on_message(self, msg, ctx):
        detection = msg.decode(Detection)
        ...

if __name__ == "__main__":
    MyService().run()
```

That gives you: config from the environment, structured logs with trace ids, a `/health`
endpoint reporting adapter choices and consumer lag, `/metrics` for Prometheus, at-least-once
consumption with idempotency, dead-lettering after `SIO_BUS_MAX_RETRIES`, recovery of stuck
messages via `XAUTOCLAIM`, and graceful shutdown.
