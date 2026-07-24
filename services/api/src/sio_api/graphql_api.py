"""GraphQL surface (PRD §11).

Exists alongside REST rather than instead of it, because they answer different needs: REST is what
`curl`, the SDK and the web client's simple reads use; GraphQL is for the "give me these entities
*with* their recent events *and* their zone in one round trip" shape that an operator console
actually wants, plus subscriptions for live updates.

Both are backed by the same :class:`ReadModel`, so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import strawberry

from sio_core.tenancy import current_tenant
from sio_schemas import Entity as EntityModel
from sio_schemas import Event as EventModel

from .queries import ReadModel


@strawberry.type
class Geo:
    lat: float
    lon: float
    alt: float | None = None


@strawberry.type
class EntityState:
    ts: datetime
    geo: Geo | None
    heading_deg: float | None
    zone_id: str | None
    confidence: float


@strawberry.type
class Provenance:
    source_id: str
    modality: str
    ts: datetime
    confidence: float
    note: str | None


@strawberry.type
class Entity:
    entity_id: str
    type: str
    label: str | None
    confidence: float
    is_static: bool
    first_seen: datetime
    last_seen: datetime
    state: EntityState
    provenance: list[Provenance]
    attributes: strawberry.scalars.JSON

    @strawberry.field(description="Seconds between first and last sighting")
    def dwell_s(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

    @strawberry.field(description="Events referencing this entity, newest first")
    async def events(self, limit: int = 20) -> list[Event]:
        read = ReadModel()
        found = await read.events(tenant_id=current_tenant(), entity_id=self.entity_id, limit=limit)
        return [to_graphql_event(event) for event in found]

    @strawberry.field(description="Recent positions, newest first")
    async def history(self, limit: int = 100) -> list[EntityState]:
        read = ReadModel()
        rows = await read.entity_history(self.entity_id, tenant_id=current_tenant(), limit=limit)
        return [
            EntityState(
                ts=row["ts"],
                geo=Geo(lat=float(row["lat"]), lon=float(row["lon"]))
                if row["lat"] is not None
                else None,
                heading_deg=row["heading_deg"],
                zone_id=row["zone_id"],
                confidence=float(row["confidence"] or 1.0),
            )
            for row in rows
        ]


@strawberry.type
class EvidenceRef:
    kind: str
    ref: str
    ts: datetime | None
    source_id: str | None
    score: float | None
    note: str | None


@strawberry.type
class Explanation:
    """The evidence bundle. Exposed in the graph so a UI can expand any answer (PRD M20)."""

    summary: str | None
    confidence: float
    sources: list[str]
    related_entities: list[str]
    degraded: bool
    evidence: list[EvidenceRef]
    notes: list[str]


@strawberry.type
class Event:
    event_id: str
    type: str
    severity: str
    ts: datetime
    detected_ts: datetime
    confidence: float
    zone_id: str | None
    entities: list[str]
    source_ids: list[str]
    geo: Geo | None
    rule_id: str | None
    explanation: Explanation

    @strawberry.field(description="Seconds between the event happening and SIO noticing")
    def detection_latency_s(self) -> float:
        return (self.detected_ts - self.ts).total_seconds()


@strawberry.type
class Zone:
    zone_id: str
    name: str
    kind: str
    restricted: bool
    capacity: int | None
    geometry: strawberry.scalars.JSON


@strawberry.type
class Stats:
    entities: int
    moving_entities: int
    events: int
    states: int
    observations: int
    zones: int


def to_graphql_geo(geo: Any) -> Geo | None:
    if geo is None:
        return None
    return Geo(lat=geo.lat, lon=geo.lon, alt=geo.alt)


def to_graphql_entity(entity: EntityModel) -> Entity:
    return Entity(
        entity_id=entity.entity_id,
        type=str(entity.type),
        label=entity.label,
        confidence=entity.confidence,
        is_static=entity.is_static,
        first_seen=entity.first_seen,
        last_seen=entity.last_seen,
        state=EntityState(
            ts=entity.state.ts,
            geo=to_graphql_geo(entity.state.geo),
            heading_deg=entity.state.heading_deg,
            zone_id=entity.state.zone_id,
            confidence=entity.state.confidence,
        ),
        provenance=[
            Provenance(
                source_id=p.source_id,
                modality=str(p.modality),
                ts=p.ts,
                confidence=p.confidence,
                note=p.note,
            )
            for p in entity.provenance[-10:]
        ],
        attributes=entity.attributes,
    )


def to_graphql_event(event: EventModel) -> Event:
    explanation = event.explanation
    return Event(
        event_id=event.event_id,
        type=str(event.type),
        severity=str(event.severity),
        ts=event.ts,
        detected_ts=event.detected_ts,
        confidence=event.confidence,
        zone_id=event.zone_id,
        entities=event.entities,
        source_ids=event.source_ids,
        geo=to_graphql_geo(event.geo),
        rule_id=event.rule_id,
        explanation=Explanation(
            summary=explanation.summary,
            confidence=explanation.confidence,
            sources=explanation.sources,
            related_entities=explanation.related_entities,
            degraded=explanation.degraded,
            notes=explanation.notes,
            evidence=[
                EvidenceRef(
                    kind=str(e.kind),
                    ref=e.ref,
                    ts=e.ts,
                    source_id=e.source_id,
                    score=e.score,
                    note=e.note,
                )
                for e in explanation.evidence
            ],
        ),
    )


@strawberry.type
class Query:
    @strawberry.field(description="Entities in the world model")
    async def entities(
        self,
        type: str | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_static: bool = True,
    ) -> list[Entity]:
        read = ReadModel()
        found = await read.entities(
            tenant_id=current_tenant(),
            entity_type=type,
            zone_id=zone_id,
            limit=limit,
            offset=offset,
            include_static=include_static,
        )
        return [to_graphql_entity(entity) for entity in found]

    @strawberry.field
    async def entity(self, entity_id: str) -> Entity | None:
        read = ReadModel()
        found = await read.entity(entity_id, tenant_id=current_tenant())
        return to_graphql_entity(found) if found else None

    @strawberry.field(description="Events, newest first")
    async def events(
        self,
        type: str | None = None,
        severity: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        read = ReadModel()
        found = await read.events(
            tenant_id=current_tenant(),
            event_type=type,
            severity=severity,
            entity_id=entity_id,
            limit=limit,
        )
        return [to_graphql_event(event) for event in found]

    @strawberry.field(description="Events within a time window, oldest first")
    async def timeline(
        self, start: datetime | None = None, end: datetime | None = None, limit: int = 200
    ) -> list[Event]:
        read = ReadModel()
        found = await read.timeline(tenant_id=current_tenant(), start=start, end=end, limit=limit)
        return [to_graphql_event(event) for event in found]

    @strawberry.field(description="The world as it stood at an instant (UC5)")
    async def world_at(self, ts: datetime, limit: int = 500) -> list[Entity]:
        read = ReadModel()
        found = await read.world_at(ts, tenant_id=current_tenant(), limit=limit)
        return [to_graphql_entity(entity) for entity in found]

    @strawberry.field(description="Entities within a radius, nearest first")
    async def nearby(
        self, lat: float, lon: float, radius_m: float = 500, type: str | None = None
    ) -> list[Entity]:
        read = ReadModel()
        found = await read.nearby(
            tenant_id=current_tenant(), lat=lat, lon=lon, radius_m=radius_m, entity_type=type
        )
        return [to_graphql_entity(entity) for entity, _distance in found]

    @strawberry.field
    async def zones(self) -> list[Zone]:
        read = ReadModel()
        return [
            Zone(
                zone_id=zone["zone_id"],
                name=zone["name"],
                kind=zone["kind"],
                restricted=zone["restricted"],
                capacity=zone["capacity"],
                geometry=zone["geometry"],
            )
            for zone in await read.zones(tenant_id=current_tenant())
        ]

    @strawberry.field
    async def stats(self) -> Stats:
        read = ReadModel()
        values = await read.stats(tenant_id=current_tenant())
        return Stats(
            entities=int(values.get("entities") or 0),
            moving_entities=int(values.get("moving_entities") or 0),
            events=int(values.get("events") or 0),
            states=int(values.get("states") or 0),
            observations=int(values.get("observations") or 0),
            zones=int(values.get("zones") or 0),
        )


@strawberry.type
class Subscription:
    @strawberry.subscription(description="Live events as they are detected")
    async def events(self) -> AsyncGenerator[Event, None]:
        """Subscribe to the live event feed.

        Uses the same :class:`StreamHub` as the SSE endpoint, so a GraphQL subscriber and an SSE
        client see an identical sequence.
        """
        from sio_schemas import Event as EventPayload

        from .app import get_hub

        hub = get_hub()
        with hub.subscribe(topics=["events"]) as subscriber:
            while True:
                message = await subscriber.queue.get()
                if message.kind != "Event":
                    continue
                yield to_graphql_event(message.decode(EventPayload))

    @strawberry.subscription(description="Live entity updates")
    async def entities(self) -> AsyncGenerator[Entity, None]:
        from sio_schemas import Entity as EntityPayload

        from .app import get_hub

        hub = get_hub()
        with hub.subscribe(topics=["entities"]) as subscriber:
            while True:
                message = await subscriber.queue.get()
                if message.kind != "Entity":
                    continue
                yield to_graphql_entity(message.decode(EntityPayload))


schema = strawberry.Schema(query=Query, subscription=Subscription)
