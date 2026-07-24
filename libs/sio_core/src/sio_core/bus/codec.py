"""Shared encoding helpers so every bus adapter puts identical bytes on the wire."""

from __future__ import annotations

from datetime import datetime

from sio_schemas import BusMessage

FIELD = "m"
"""Single stream field holding the JSON envelope.

One field keeps Redis Streams entries compact and makes the format identical across
adapters, so a message published by the memory bus in a test is byte-identical to one
published by Redis in production.
"""


def encode(message: BusMessage) -> dict[str, str]:
    return {FIELD: message.model_dump_json(by_alias=True)}


def decode(
    fields: dict[str, str] | dict[bytes, bytes], *, stream_id: str, delivery_count: int = 1
) -> BusMessage:
    raw = fields.get(FIELD) or fields.get(FIELD.encode())  # type: ignore[arg-type]
    if raw is None:
        raise ValueError(f"bus entry {stream_id} has no {FIELD!r} field")
    if isinstance(raw, bytes):
        raw = raw.decode()
    message = BusMessage.model_validate_json(raw)
    message.stream_id = stream_id
    message.delivery_count = delivery_count
    return message


def ts_to_stream_id(ts: datetime, *, inclusive: bool = True) -> str:
    """Convert a timestamp to a Redis Streams id boundary.

    Auto-generated ids are ``<unix-millis>-<sequence>``, so a millisecond timestamp is a
    valid range boundary. This is what makes time-window replay cheap.
    """
    ms = int(ts.timestamp() * 1000)
    return f"{ms}-0" if inclusive else f"({ms}-0"


def group_name(service: str) -> str:
    """Consumer group for a service. One group per service, so services never steal from
    each other, and scaling a service out shares its group."""
    return f"cg.{service}"
