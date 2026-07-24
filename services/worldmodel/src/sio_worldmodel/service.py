"""World-model service: keeps the graph and its relational projection current."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from sio_core import MessageContext, SioService, get_graph, get_pg_pool
from sio_core.tenancy import current_tenant
from sio_schemas import BusMessage, Entity, Relationship, Topic, Track


class WorldModelService(SioService):
    """Consumes entities, relationships and tracks; persists them durably.

    Two stores, on purpose. The graph answers "how is this connected to that" (multi-hop
    traversal for the copilot); Postgres answers "what is within 500 m of here" and holds the
    movement history that replay and analytics read. Writing both from one consumer keeps them
    consistent without a distributed transaction, because the graph write is idempotent and the
    SQL write is an upsert — a retry converges rather than duplicating.
    """

    name = "worldmodel"
    subscribes = (Topic.ENTITIES, Topic.TRACKS)
    tick_interval_s = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.graph = get_graph(self.settings)
        self.pool = get_pg_pool(self.settings)
        self._entities_seen = 0
        self._relationships_seen = 0
        self._states_written = 0

    # ----------------------------------------------------------------- lifecycle
    async def setup(self) -> None:
        await self.pool.open()
        if not await self.graph.ping():
            # Not fatal: the graph may still be starting under `just dev`, and the service
            # should come up and keep retrying rather than crash-loop the whole stack.
            self.log.warning(
                "graph.unreachable",
                backend=self.settings.graph_backend,
                hint="run: just services && just neo4j-init",
            )
        self.log.info("worldmodel.ready", graph=self.settings.graph_backend)

    async def teardown(self) -> None:
        await self.graph.close()

    async def health_checks(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        checks["graph"] = "ok" if await self.graph.ping() else "unreachable"
        checks["postgres"] = "ok" if await self.pool.ping() else "unreachable"
        return checks

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind == "Entity":
            await self._handle_entity(message.decode(Entity))
        elif message.kind == "Relationship":
            await self._handle_relationship(message.decode(Relationship))
        elif message.kind == "Track":
            await self._handle_track(message.decode(Track))
        else:
            # An unknown payload is not an error: a newer producer may publish something this
            # consumer has never heard of, and skipping it is the correct forward-compatible
            # behaviour.
            self.log.debug("worldmodel.skipped", kind=message.kind)

    async def _handle_entity(self, entity: Entity) -> None:
        await self.graph.upsert_entity(entity)
        await self._write_entity_row(entity)
        await self._write_state_row(entity)
        self._entities_seen += 1

    async def _handle_relationship(self, relationship: Relationship) -> None:
        await self.graph.upsert_relationship(relationship)
        self._relationships_seen += 1

    async def _handle_track(self, track: Track) -> None:
        """Persist the track and its link to an entity.

        Tracks are sensor-scoped identities; entities are real-world ones. Keeping the link means
        "which camera saw this truck, and when" (UC3) is answerable without re-deriving it.
        """
        latest = track.latest
        path_wkt = self._trajectory_wkt(track)
        await self.pool.execute(
            """
            INSERT INTO tracks (
                tenant_id, track_id, source_id, class_name, status, entity_id,
                start_ts, last_ts, hits, confidence, path, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_GeomFromText(%s), 4326)::geography END,
                %s::jsonb
            )
            ON CONFLICT (tenant_id, track_id) DO UPDATE SET
                status     = EXCLUDED.status,
                entity_id  = COALESCE(EXCLUDED.entity_id, tracks.entity_id),
                last_ts    = GREATEST(tracks.last_ts, EXCLUDED.last_ts),
                hits       = GREATEST(tracks.hits, EXCLUDED.hits),
                confidence = EXCLUDED.confidence,
                path       = COALESCE(EXCLUDED.path, tracks.path),
                payload    = EXCLUDED.payload
            """,
            (
                track.tenant_id,
                track.track_id,
                track.source_id,
                track.class_name,
                str(track.status),
                track.entity_id,
                track.start_ts,
                track.last_ts,
                track.hits,
                track.confidence,
                path_wkt,
                path_wkt,
                track.to_json(),
            ),
        )
        if latest is not None and track.entity_id:
            self.log.debug("worldmodel.track", track=track.track_id, entity=track.entity_id)

    @staticmethod
    def _trajectory_wkt(track: Track) -> str | None:
        """LINESTRING for the track path, or None when there are too few fixes.

        PostGIS rejects a one-point linestring, and a track with a single fix has no path worth
        storing yet.
        """
        points = [
            f"{state.geo.lon} {state.geo.lat}" for state in track.states if state.geo is not None
        ]
        if len(points) < 2:
            return None
        return f"LINESTRING({', '.join(points)})"

    async def _write_entity_row(self, entity: Entity) -> None:
        geo = entity.state.geo
        await self.pool.execute(
            """
            INSERT INTO entities (
                entity_id, tenant_id, type, label, confidence, is_static,
                geom, zone_id, h3_cell, first_seen, last_seen, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision),
                           4326)::geography,
                %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, entity_id) DO UPDATE SET
                type       = EXCLUDED.type,
                label      = COALESCE(EXCLUDED.label, entities.label),
                confidence = EXCLUDED.confidence,
                is_static  = EXCLUDED.is_static,
                geom       = COALESCE(EXCLUDED.geom, entities.geom),
                zone_id    = COALESCE(EXCLUDED.zone_id, entities.zone_id),
                h3_cell    = COALESCE(EXCLUDED.h3_cell, entities.h3_cell),
                first_seen = LEAST(entities.first_seen, EXCLUDED.first_seen),
                last_seen  = GREATEST(entities.last_seen, EXCLUDED.last_seen),
                payload    = EXCLUDED.payload || jsonb_build_object(
                                 'first_seen',
                                 to_jsonb(LEAST(entities.first_seen, EXCLUDED.first_seen)),
                                 'last_seen',
                                 to_jsonb(GREATEST(entities.last_seen, EXCLUDED.last_seen))
                             ),
                updated_at = now()
            """,
            (
                entity.entity_id,
                entity.tenant_id,
                str(entity.type),
                entity.label,
                entity.confidence,
                entity.is_static,
                geo.lon if geo else None,
                geo.lat if geo else None,
                entity.state.zone_id,
                entity.state.h3_cell,
                entity.first_seen,
                entity.last_seen,
                entity.to_json(),
            ),
        )

    async def _write_state_row(self, entity: Entity) -> None:
        """Append the entity's state at this instant.

        Static infrastructure (cameras, gates, docks) does not move, so writing a state row per
        message for them would be pure noise — and at 20 messages/second it would dominate the
        table.
        """
        state = entity.state
        if entity.is_static or state.geo is None:
            return
        velocity = state.velocity
        await self.pool.execute(
            """
            INSERT INTO entity_states (
                tenant_id, entity_id, ts, geom, speed_mps, heading_deg,
                zone_id, h3_cell, confidence, payload
            ) VALUES (
                %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision),
                           4326)::geography,
                %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, entity_id, ts) DO NOTHING
            """,
            (
                entity.tenant_id,
                entity.entity_id,
                state.ts,
                state.geo.lon,
                state.geo.lat,
                velocity.speed_mps if velocity else None,
                state.heading_deg,
                state.zone_id,
                state.h3_cell,
                state.confidence,
                state.model_dump_json(by_alias=True),
            ),
        )
        self._states_written += 1

    # ---------------------------------------------------------------------- tick
    async def tick(self) -> None:
        counts = await self.graph.counts(tenant_id=current_tenant())
        self.log.info(
            "worldmodel.stats",
            entities=counts.get("entities", 0),
            relationships=counts.get("relationships", 0),
            open_relationships=counts.get("open_relationships", 0),
            states_written=self._states_written,
        )

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/counts", tags=["worldmodel"])
        async def counts() -> dict[str, Any]:
            tenant = current_tenant()
            graph_counts = await self.graph.counts(tenant_id=tenant)
            row = await self.pool.fetchrow(
                "SELECT (SELECT count(*) FROM entities WHERE tenant_id = %s) AS entities, "
                "(SELECT count(*) FROM entity_states WHERE tenant_id = %s) AS states, "
                "(SELECT count(*) FROM tracks WHERE tenant_id = %s) AS tracks",
                (tenant, tenant, tenant),
            )
            return {
                "tenant": tenant,
                "graph": graph_counts,
                "sql": dict(row or {}),
                "consumed": self._entities_seen,
            }
