"""World-model service: keeps the graph and its relational projection current."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI

from sio_core import (
    MessageContext,
    SioService,
    get_blob,
    get_embedder,
    get_graph,
    get_pg_pool,
    get_vectors,
)
from sio_core.tenancy import current_tenant
from sio_schemas import BusMessage, Entity, Observation, Relationship, Topic, Track


class WorldModelService(SioService):
    """Consumes entities, relationships and tracks; persists them durably.

    Two stores, on purpose. The graph answers "how is this connected to that" (multi-hop
    traversal for the copilot); Postgres answers "what is within 500 m of here" and holds the
    movement history that replay and analytics read. Writing both from one consumer keeps them
    consistent without a distributed transaction, because the graph write is idempotent and the
    SQL write is an upsert — a retry converges rather than duplicating.
    """

    name = "worldmodel"
    subscribes = (Topic.ENTITIES, Topic.TRACKS, Topic.RAW_FRAMES)
    tick_interval_s = 30.0

    FRAME_COLLECTION = "frames"
    """pgvector collection holding CLIP embeddings of stored frames."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.graph = get_graph(self.settings)
        self.pool = get_pg_pool(self.settings)
        self.vectors = get_vectors(self.settings)
        self.blob = get_blob(self.settings)
        self.embedder = get_embedder(self.settings)
        self._entities_seen = 0
        self._relationships_seen = 0
        self._states_written = 0
        self._frames_indexed = 0
        self._frames_skipped = 0
        self._embed_ms: list[float] = []
        self._last_embed_at: dict[str, float] = {}

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
        checks["vectors"] = "ok" if await self.vectors.ping() else "unreachable"
        return checks

    async def health_info(self) -> dict[str, str]:
        return {
            "embedder": self.embedder.name,
            "frames_indexed": str(self._frames_indexed),
            "frames_skipped": str(self._frames_skipped),
            "mean_embed_ms": f"{sum(self._embed_ms) / len(self._embed_ms):.0f}"
            if self._embed_ms
            else "n/a",
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind == "Entity":
            await self._handle_entity(message.decode(Entity))
        elif message.kind == "Relationship":
            await self._handle_relationship(message.decode(Relationship))
        elif message.kind == "Track":
            await self._handle_track(message.decode(Track))
        elif message.kind == "Observation" and message.topic == str(Topic.RAW_FRAMES):
            await self._index_frame(message.decode(Observation), ctx)
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
        """Persist an edge to the graph *and* to the relational projection.

        Both, because they answer different questions and different consumers read each: the graph
        answers multi-hop traversal for the copilot, the table backs SQL joins, timeline replay and
        analytics. An earlier version wrote only the graph, so 59 SEEN_BY edges existed in Neo4j while
        the API — which reads Postgres — reported zero relationships. Everything looked fine from
        either side alone.
        """
        await self.graph.upsert_relationship(relationship)
        await self.pool.execute(
            """
            INSERT INTO relationships (
                rel_id, tenant_id, from_id, type, to_id,
                ts_valid_from, ts_valid_to, confidence, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, rel_id) DO UPDATE SET
                ts_valid_to = EXCLUDED.ts_valid_to,
                confidence  = EXCLUDED.confidence,
                payload     = EXCLUDED.payload
            """,
            (
                relationship.id,
                relationship.tenant_id,
                relationship.from_id,
                str(relationship.type),
                relationship.to_id,
                relationship.ts_valid_from,
                relationship.ts_valid_to,
                relationship.confidence,
                relationship.to_json(),
            ),
        )
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
                -- Cast the parameter explicitly, and no CASE.
                --
                -- Postgres cannot infer a type for a bare placeholder inside a "WHEN ... IS NULL"
                -- test, so the original CASE failed with "could not determine data type of parameter
                -- $11" for EVERY track. The CASE was unnecessary anyway: PostGIS functions are STRICT,
                -- so a NULL WKT already yields a NULL geography.
                --
                -- (And placeholders inside a SQL comment still count: writing the old expression out
                -- in a comment here made psycopg see 13 placeholders for 12 parameters.)
                ST_SetSRID(ST_GeomFromText(%s::text), 4326)::geography,
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

    # ------------------------------------------------------------- frame indexing
    async def _index_frame(self, observation: Observation, ctx: MessageContext) -> None:
        """Embed a stored frame and index it, so every frame is searchable (PRD M2).

        Rate-limited per camera and skipped for stale frames. At 2 fps consecutive frames are near
        duplicates, so embedding every one costs ~60 ms of CPU each to add almost no information — and
        after a restart the replayed backlog would otherwise be embedded in full before anything
        current was.
        """
        if not observation.raw_ref:
            return
        if ctx.age_s > self.settings.perception_max_age_s:
            self._frames_skipped += 1
            return
        interval = 1.0 / max(0.05, self.settings.frame_index_hz)
        now = time.monotonic()
        if now - self._last_embed_at.get(observation.source_id, 0.0) < interval:
            self._frames_skipped += 1
            return
        self._last_embed_at[observation.source_id] = now

        try:
            data = await self.blob.get(observation.raw_ref)
        except Exception:
            self._frames_skipped += 1
            return

        vector, width, height = await asyncio.to_thread(self._embed_bytes, data)
        if vector is None:
            return

        frame_id = str(observation.payload.get("frame_id") or observation.id)
        await self.pool.execute(
            """
            INSERT INTO frames (
                tenant_id, frame_id, source_id, ts, object_key, width, height,
                redacted, detections, trace_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, frame_id) DO UPDATE SET
                object_key = EXCLUDED.object_key,
                detections = GREATEST(frames.detections, EXCLUDED.detections)
            """,
            (
                observation.tenant_id,
                frame_id,
                observation.source_id,
                observation.ts,
                observation.raw_ref,
                width,
                height,
                bool(self.settings.blur_faces or self.settings.blur_plates),
                len(observation.payload.get("visible", [])),
                observation.trace_id,
            ),
        )
        await self.vectors.upsert(
            self.FRAME_COLLECTION,
            frame_id,
            vector,
            tenant_id=observation.tenant_id,
            metadata={
                "source_id": observation.source_id,
                "object_key": observation.raw_ref,
                "ts": observation.ts.isoformat(),
                "embedder": self.embedder.name,
            },
            ts=observation.ts,
        )
        self._frames_indexed += 1

    def _embed_bytes(self, data: bytes) -> tuple[list[float] | None, int | None, int | None]:
        """Decode and embed one frame. Runs in a worker thread."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None, None, None
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None, None, None
        started = time.perf_counter()
        vector = self.embedder.embed_image(image)
        self._embed_ms = [*self._embed_ms[-49:], (time.perf_counter() - started) * 1000]
        return vector, int(image.shape[1]), int(image.shape[0])

    async def search_frames(
        self, query: str, *, tenant_id: str, limit: int = 12, source_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Semantic frame search: text in, frames out (PRD M2 acceptance criterion).

        The query is embedded with the *same* model that embedded the frames, which is the whole
        reason the embedder is shared rather than per-service.
        """
        vector = await asyncio.to_thread(self.embedder.embed_text, query)
        filters = {"source_id": source_id} if source_id else None
        hits = await self.vectors.search(
            self.FRAME_COLLECTION,
            vector,
            tenant_id=tenant_id,
            limit=limit,
            filters=filters,
        )
        return [
            {
                "frame_id": frame_id,
                "score": round(score, 4),
                "source_id": metadata.get("source_id"),
                "ts": metadata.get("ts"),
                "object_key": metadata.get("object_key"),
                "media_url": f"/media/{metadata.get('object_key')}"
                if metadata.get("object_key")
                else None,
                "embedder": metadata.get("embedder"),
            }
            for frame_id, score, metadata in hits
        ]

    # ---------------------------------------------------------------------- tick
    async def tick(self) -> None:
        counts = await self.graph.counts(tenant_id=current_tenant())
        self.log.info(
            "worldmodel.stats",
            entities=counts.get("entities", 0),
            relationships=counts.get("relationships", 0),
            open_relationships=counts.get("open_relationships", 0),
            states_written=self._states_written,
            frames_indexed=self._frames_indexed,
            frames_skipped=self._frames_skipped,
            mean_embed_ms=round(sum(self._embed_ms) / len(self._embed_ms), 1)
            if self._embed_ms
            else None,
        )

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/search/frames", tags=["worldmodel"])
        async def search(q: str, limit: int = 12, source_id: str | None = None) -> dict[str, Any]:
            """Semantic frame search. `q` is natural language: 'a truck at the gate'."""
            results = await self.search_frames(
                q, tenant_id=current_tenant(), limit=limit, source_id=source_id
            )
            return {"query": q, "embedder": self.embedder.name, "results": results}

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
