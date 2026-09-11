"""Tenant-scoped documents with PostgreSQL revisions and optimistic concurrency."""

from __future__ import annotations

import builtins
import json
from datetime import datetime
from typing import Any


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

    async def list_analysis_metadata(
        self, tenant: str, limit: int = 500
    ) -> builtins.list[dict[str, Any]]:
        """List retained-run metadata without transferring frames or event payloads."""
        rows = await self.pool.fetch(
            """
            WITH recent AS MATERIALIZED (
                SELECT record_id, payload, revision, created_at, updated_at
                FROM workbench_documents
                WHERE tenant_id = %s AND kind = %s
                ORDER BY created_at DESC, record_id DESC LIMIT %s
            )
            SELECT record_id, revision, created_at, updated_at,
                jsonb_build_object(
                    'analysis_id', payload->'analysis_id',
                    'video_id', payload->'video_id',
                    'status', payload->'status',
                    'model', COALESCE(payload->'model', '{}'::jsonb),
                    'zones', COALESCE(payload->'zones', '[]'::jsonb),
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
            (tenant, "analysis", max(1, min(limit, 500))),
        )
        return [self._record(row) for row in rows]

    async def list_tenants(self, kind: str) -> builtins.list[str]:
        """Internal worker discovery; never exposed as a cross-tenant HTTP endpoint."""
        rows = await self.pool.fetch(
            "SELECT DISTINCT tenant_id FROM workbench_documents WHERE kind = %s ORDER BY tenant_id",
            (kind,),
        )
        return [str(row["tenant_id"]) for row in rows]

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
            row = await self.pool.fetchrow(
                "UPDATE workbench_documents SET payload = %s::jsonb, revision = revision + 1, updated_at = now() "
                "WHERE tenant_id = %s AND kind = %s AND record_id = %s AND revision = %s RETURNING *",
                (payload, tenant, kind, record_id, expected_revision),
            )
        elif expected_revision == 0:
            row = await self.pool.fetchrow(
                "INSERT INTO workbench_documents (tenant_id, kind, record_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING RETURNING *",
                (tenant, kind, record_id, payload),
            )
        elif expected_revision is None:
            row = await self.pool.fetchrow(
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
        count = await self.pool.execute(sql, params)
        if not count and expected_revision is not None:
            raise WorkbenchConflict("This record changed. Reload it before deleting it.")
        return bool(count)

    async def history(
        self, tenant: str, kind: str, record_id: str
    ) -> builtins.list[dict[str, Any]]:
        return await self.pool.fetch(
            "SELECT revision, payload, recorded_at FROM workbench_revisions "
            "WHERE tenant_id = %s AND kind = %s AND record_id = %s ORDER BY revision DESC LIMIT 100",
            (tenant, kind, record_id),
        )
