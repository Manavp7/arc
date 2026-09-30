"""Tenant-scoped documents with PostgreSQL revisions and optimistic concurrency."""

from __future__ import annotations

import base64
import builtins
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from .media_coordination import coordinated_fetchrow


def record_cursor(record: dict[str, Any]) -> str:
    value = [
        record["created_at"],
        record.get("record_id") or record.get("video_id") or record.get("analysis_id"),
    ]
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        if len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError
        parsed = datetime.fromisoformat(value[0].replace("Z", "+00:00"))
        if parsed.tzinfo is None or not value[1] or len(value[1]) > 200:
            raise ValueError
        return parsed.isoformat(), value[1]
    except (ValueError, TypeError, UnicodeError) as error:
        raise HTTPException(422, "Invalid history cursor") from error


class WorkbenchConflict(Exception):
    """A caller edited an obsolete revision or reused an existing identifier."""


class WorkbenchStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def _record(row: dict[str, Any]) -> dict[str, Any]:
        def stamp(value: Any) -> str:
            return value.isoformat() if isinstance(value, datetime) else str(value)

        return {
            **row["payload"],
            "record_id": row["record_id"],
            "revision": row["revision"],
            "created_at": stamp(row["created_at"]),
            "updated_at": stamp(row["updated_at"]),
        }

    async def get(self, tenant: str, kind: str, record_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM workbench_documents WHERE tenant_id = %s AND kind = %s AND record_id = %s",
            (tenant, kind, record_id),
        )
        return self._record(row) if row else None

    async def list(self, tenant: str, kind: str, limit: int = 500) -> builtins.list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM workbench_documents WHERE tenant_id = %s AND kind = %s "
            "ORDER BY updated_at DESC, record_id LIMIT %s",
            (tenant, kind, max(1, min(limit, 5000))),
        )
        return [self._record(row) for row in rows]

    async def page(
        self,
        tenant: str,
        kind: str,
        *,
        cursor: str = "",
        limit: int = 100,
        video_id: str = "",
        statuses: tuple[str, ...] = (),
        active_only: bool = False,
    ) -> tuple[builtins.list[dict[str, Any]], str | None]:
        """Immutable creation-order keyset; filters are applied before the page limit."""
        clauses = ["tenant_id = %s", "kind = %s"]
        params: builtins.list[Any] = [tenant, kind]
        if cursor:
            clauses.append("(created_at, record_id) < (%s::timestamptz, %s)")
            params.extend(decode_cursor(cursor))
        if video_id:
            clauses.append("payload->>'video_id' = %s")
            params.append(video_id)
        if statuses:
            clauses.append("payload->>'status' = ANY(%s)")
            params.append(list(statuses))
        if active_only:
            clauses.append(
                "COALESCE(payload->>'archived_at', '') = '' AND COALESCE(payload->>'purged_at', '') = ''"
            )
        count = max(1, min(limit, 500))
        rows = await self.pool.fetch(
            "SELECT * FROM workbench_documents WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, record_id DESC LIMIT %s",
            (*params, count + 1),
        )
        records = [self._record(row) for row in rows[:count]]
        return records, record_cursor(records[-1]) if len(rows) > count else None

    async def iter_records(self, tenant: str, kind: str, *, cursor: str = "", **filters):
        while True:
            records, next_cursor = await self.page(tenant, kind, cursor=cursor, **filters)
            for record in records:
                yield record
            if not next_cursor:
                break
            cursor = next_cursor

    async def all_records(self, tenant: str, kind: str, **filters) -> builtins.list[dict[str, Any]]:
        return [record async for record in self.iter_records(tenant, kind, **filters)]

    async def list_analysis_metadata(
        self, tenant: str, limit: int = 500, *, video_id: str = "", cursor: str = ""
    ) -> builtins.list[dict[str, Any]]:
        """List retained-run metadata without transferring frames or event payloads."""
        filters = ""
        params: builtins.list[Any] = [tenant, "analysis"]
        if video_id:
            filters += " AND payload->>'video_id' = %s"
            params.append(video_id)
        if cursor:
            filters += " AND (created_at, record_id) < (%s::timestamptz, %s)"
            params.extend(decode_cursor(cursor))
        params.append(max(1, min(limit, 500)))
        rows = await self.pool.fetch(
            """
            WITH recent AS MATERIALIZED (
                SELECT record_id, payload, revision, created_at, updated_at
                FROM workbench_documents
                WHERE tenant_id = %s AND kind = %s
            """
            + filters
            + """
                ORDER BY created_at DESC, record_id DESC LIMIT %s
            )
            SELECT record_id, revision, created_at, updated_at,
                jsonb_build_object(
                    'analysis_id', payload->'analysis_id',
                    'video_id', payload->'video_id',
                    'status', payload->'status',
                    'model', COALESCE(payload->'model', '{}'::jsonb),
                    'zones', COALESCE(payload->'zones', '[]'::jsonb),
                    'evidence_zone_ids', COALESCE(payload->'evidence_zone_ids', '[]'::jsonb),
                    'rules', COALESCE(payload->'rules', '[]'::jsonb),
                    'sample_count', CASE
                        WHEN jsonb_typeof(payload->'detections') = 'array'
                        THEN jsonb_array_length(payload->'detections')
                        ELSE NULL END,
                    'detected_classes', (
                        SELECT COALESCE(jsonb_agg(label ORDER BY label), '[]'::jsonb)
                        FROM (
                            SELECT DISTINCT label
                            FROM jsonb_array_elements_text(jsonb_path_query_array(
                                payload,
                                '$.detections[0 to 359].objects[0 to 31].class_name ? (@.type() == "string")'
                            )) AS classes(label)
                            WHERE length(label) BETWEEN 1 AND 64 AND btrim(label) <> ''
                        ) labels
                    )
                ) AS payload
            FROM recent
            ORDER BY created_at DESC, record_id DESC
            """,
            tuple(params),
        )
        return [self._record(row) for row in rows]

    async def list_tenants(self, kind: str) -> builtins.list[str]:
        """Internal worker discovery; never exposed as a cross-tenant HTTP endpoint."""
        rows = await self.pool.fetch(
            "SELECT DISTINCT tenant_id FROM workbench_documents WHERE kind = %s ORDER BY tenant_id",
            (kind,),
        )
        return [str(row["tenant_id"]) for row in rows]

    async def reference_counts(self, tenant: str, video_ids: builtins.list[str]):
        rows = await self.pool.fetch(
            """
            WITH targets AS (
                SELECT unnest(%s::text[]) AS video_id, unnest(%s::text[]) AS target_id
                UNION ALL
                SELECT payload->>'video_id', record_id FROM workbench_documents
                WHERE tenant_id=%s AND kind='analysis' AND payload->>'video_id'=ANY(%s)
            )
            SELECT t.video_id,e.kind,count(DISTINCT e.record_id) AS count
            FROM targets t JOIN workbench_reference_edges e
              ON e.tenant_id=%s AND e.reference_id=t.target_id
            GROUP BY t.video_id,e.kind
        """,
            (video_ids, video_ids, tenant, video_ids, tenant),
        )
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["video_id"], {})[row["kind"]] = row["count"]
        return counts

    async def recovery_jobs(self, tenant: str) -> builtins.list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT j.* FROM workbench_documents j
            WHERE j.tenant_id = %s AND j.kind = 'video_job' AND (
              j.payload->>'status' IN ('queued','running','cancelling','interrupted') OR EXISTS (
                SELECT 1 FROM workbench_documents v WHERE v.tenant_id=j.tenant_id
                AND v.kind='video' AND v.record_id=j.payload->>'video_id'
                AND v.payload->>'analysis_id'=j.payload->>'analysis_id'
                AND v.payload->>'status' IN ('analyzing','queued','running')
              )) ORDER BY j.created_at, j.record_id""",
            (tenant,),
        )
        return [self._record(row) for row in rows]

    async def put(
        self,
        tenant: str,
        kind: str,
        record_id: str,
        data: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                k: v
                for k, v in data.items()
                if k not in {"record_id", "revision", "created_at", "updated_at"}
            },
            allow_nan=False,
        )
        if expected_revision is not None and expected_revision > 0:
            row = await coordinated_fetchrow(
                self.pool,
                "UPDATE workbench_documents SET payload = %s::jsonb, revision = revision + 1, updated_at = now() "
                "WHERE tenant_id = %s AND kind = %s AND record_id = %s AND revision = %s RETURNING *",
                (payload, tenant, kind, record_id, expected_revision),
            )
        elif expected_revision == 0:
            row = await coordinated_fetchrow(
                self.pool,
                "INSERT INTO workbench_documents (tenant_id, kind, record_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING RETURNING *",
                (tenant, kind, record_id, payload),
            )
        elif expected_revision is None:
            row = await coordinated_fetchrow(
                self.pool,
                "INSERT INTO workbench_documents (tenant_id, kind, record_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (tenant_id, kind, record_id) DO UPDATE "
                "SET payload = EXCLUDED.payload, revision = workbench_documents.revision + 1, updated_at = now() "
                "RETURNING *",
                (tenant, kind, record_id, payload),
            )
        else:
            raise WorkbenchConflict("Revision must be a non-negative integer.")
        if not row:
            raise WorkbenchConflict("This record changed. Reload it before saving your edits.")
        return self._record(row)

    async def delete(
        self,
        tenant: str,
        kind: str,
        record_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        sql = (
            "DELETE FROM workbench_documents WHERE tenant_id = %s AND kind = %s AND record_id = %s"
        )
        params: tuple[Any, ...] = (tenant, kind, record_id)
        if expected_revision is not None:
            sql += " AND revision = %s"
            params += (expected_revision,)
        row = await coordinated_fetchrow(self.pool, sql + " RETURNING record_id", params)
        if not row and expected_revision is not None:
            raise WorkbenchConflict("This record changed. Reload it before deleting it.")
        return bool(row)

    async def history(
        self, tenant: str, kind: str, record_id: str
    ) -> builtins.list[dict[str, Any]]:
        return await self.pool.fetch(
            "SELECT revision, payload, recorded_at FROM workbench_revisions "
            "WHERE tenant_id = %s AND kind = %s AND record_id = %s ORDER BY revision DESC LIMIT 100",
            (tenant, kind, record_id),
        )
