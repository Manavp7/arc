"""The wire envelope every bus topic carries.

One envelope for all topics means a consumer can log, audit, dead-letter or replay a message
without knowing its payload type — and `trace_id` survives every hop, which is what makes
end-to-end explanations possible.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import SCHEMA_VERSION, SioModel, TenantScoped, Timestamp, new_id, utc_now

_REGISTRY: dict[str, type[SioModel]] = {}


def register_payload[M: SioModel](model: type[M]) -> type[M]:
    """Register a model so :meth:`BusMessage.decode` can rebuild it from ``kind``."""
    _REGISTRY[model.__name__] = model
    return model


def payload_model(kind: str) -> type[SioModel] | None:
    return _REGISTRY.get(kind)


class BusMessage(TenantScoped):
    """Envelope for one message on one topic.

    ``payload`` is a plain dict on the wire: the bus is not in the business of importing
    every domain model, and a consumer that only forwards messages should not have to
    validate them.
    """

    id: str = Field(default_factory=lambda: new_id("msg"))
    topic: str
    kind: str = Field(description="Payload model name, e.g. Detection — used to decode")
    ts: Timestamp = Field(default_factory=utc_now)
    producer: str = Field(default="unknown", description="Service that published this")
    trace_id: str = Field(default_factory=lambda: new_id("trc"))
    schema_version: str = SCHEMA_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)

    # Set by the bus adapter on read; never published.
    stream_id: str | None = Field(default=None, exclude=True)
    delivery_count: int = Field(default=0, exclude=True)

    @classmethod
    def of(
        cls,
        topic: str,
        model: SioModel,
        *,
        producer: str = "unknown",
        tenant_id: str | None = None,
        trace_id: str | None = None,
    ) -> BusMessage:
        """Wrap a domain model for publication, inheriting its tenant and trace when present."""
        inherited_tenant = tenant_id or getattr(model, "tenant_id", None)
        inherited_trace = trace_id or getattr(model, "trace_id", None)
        kwargs: dict[str, Any] = {
            "topic": str(topic),
            "kind": type(model).__name__,
            "producer": producer,
            "payload": model.to_wire(),
        }
        if inherited_tenant:
            kwargs["tenant_id"] = inherited_tenant
        if inherited_trace:
            kwargs["trace_id"] = inherited_trace
        return cls(**kwargs)

    def decode[M: SioModel](self, model: type[M]) -> M:
        """Validate the payload as ``model``, propagating the envelope's trace id."""
        payload = dict(self.payload)
        if "trace_id" in model.model_fields and not payload.get("trace_id"):
            payload["trace_id"] = self.trace_id
        if "tenant_id" in model.model_fields and not payload.get("tenant_id"):
            payload["tenant_id"] = self.tenant_id
        return model.model_validate(payload)

    def decode_auto(self) -> SioModel | None:
        """Decode using the registered model for ``kind``, or None when unknown.

        Unknown kinds are deliberately not an error: a newer producer may publish a payload
        an older consumer has never heard of, and the consumer should be able to skip it.
        """
        model = payload_model(self.kind)
        return self.decode(model) if model else None
