"""Base model, identifier and timestamp primitives shared by every SIO contract."""

from __future__ import annotations

import os
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"
"""Wire-format version stamped onto every envelope.

Bump the minor for additive changes, the major for anything a consumer could choke on.
Every major bump adds a golden fixture under ``tests/unit/fixtures/``.
"""

DEFAULT_TENANT = "default"

# Crockford base32, so ids are readable, case-insensitive and free of look-alike glyphs.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_B32[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a k-sortable, prefixed identifier, e.g. ``obs_01J7Q2M8Z4KX9F3TBN``.

    ULID-shaped (48-bit millisecond timestamp + 80 bits of randomness) so that ids sort in
    creation order. That matters a lot here: entity, event and audit ids are used as
    tie-breakers when replaying the timeline, and random uuid4s would scramble that order.
    Pure stdlib on purpose — this module must stay dependency-light.
    """
    ms = int(time.time() * 1000)
    rand = secrets.randbits(80)
    return f"{prefix}_{_b32(ms, 10)}{_b32(rand, 16)}"


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _ensure_utc(value: Any) -> Any:
    """Coerce input to a tz-aware UTC datetime, rejecting ambiguous naive values.

    Accepts datetimes, ISO-8601 strings (including a trailing ``Z``) and epoch
    seconds/milliseconds. A *naive* datetime is an error rather than an assumption: guessing
    the zone of sensor timestamps is how replay windows silently drift by hours.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected: timestamps must be timezone-aware "
                "(use sio_schemas.utc_now() or attach tzinfo)"
            )
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Heuristic: anything past ~year 33658 in seconds is really milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("z", "Z")):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError(f"naive timestamp string rejected: {value!r} has no UTC offset")
        return parsed.astimezone(UTC)
    return value


Timestamp = Annotated[datetime, BeforeValidator(_ensure_utc)]
"""A timezone-aware UTC datetime. Naive values are rejected at the boundary."""

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""A probability-like score in [0, 1]. Every inference in SIO carries one."""


class SioModel(BaseModel):
    """Strict base for all contracts.

    - ``extra="forbid"``: an unexpected field is a producer bug, and silently dropping it
      is how schema drift goes unnoticed for weeks.
    - ``serialize_by_alias=True``: the wire format always matches the PRD field names, even
      where Python needs a different attribute name (``class`` → ``class_name``).
    - ``validate_assignment=True``: mutating a model in a consumer re-validates it.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        validate_assignment=True,
        ser_json_timedelta="float",
        str_strip_whitespace=True,
    )

    def to_wire(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict using wire (alias) names."""
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)


def default_tenant() -> str:
    """Tenant used for locally produced data when a caller does not supply one."""
    return os.environ.get("SIO_TENANT_ID", DEFAULT_TENANT)


class TenantScoped(SioModel):
    """Mixin for anything that must never leak across tenants.

    Every store adapter filters on ``tenant_id``; every API response is scoped to the
    caller's tenant. Having it on the contract (not just the table) means a service cannot
    forget to carry it.
    """

    tenant_id: str = Field(default_factory=default_tenant, min_length=1, max_length=64)


class Traced(SioModel):
    """Mixin carrying the correlation id that threads one signal through the whole pipeline.

    A frame's ``trace_id`` survives detection → track → entity → event → decision → audit,
    which is what makes an explanation's evidence chain reconstructible after the fact.
    """

    trace_id: str = Field(default_factory=lambda: new_id("trc"))
