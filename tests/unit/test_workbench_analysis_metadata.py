"""Metadata scans retain tenant binding and record chronology without media payloads."""

from datetime import UTC, datetime

import pytest
from sio_api.workbench_store import WorkbenchStore


class MetadataPool:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, params):
        self.calls.append((query, params))
        return [
            {
                "record_id": "ana_one",
                "revision": 3,
                "created_at": datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 9, 11, 12, 1, tzinfo=UTC),
                "payload": {
                    "analysis_id": "ana_one",
                    "video_id": "vid_one",
                    "status": "completed",
                    "model": {"mode": "motion"},
                    "zones": [],
                    "rules": [],
                    "sample_count": 10,
                    "detected_classes": ["motion"],
                },
            }
        ]


async def test_metadata_rows_preserve_revisions_timestamps_and_small_projection():
    pool = MetadataPool()
    rows = await WorkbenchStore(pool).list_analysis_metadata("tenant-a", 20)
    assert rows == [
        {
            "record_id": "ana_one",
            "revision": 3,
            "created_at": "2026-09-11T12:00:00+00:00",
            "updated_at": "2026-09-11T12:01:00+00:00",
            "analysis_id": "ana_one",
            "video_id": "vid_one",
            "status": "completed",
            "model": {"mode": "motion"},
            "zones": [],
            "rules": [],
            "sample_count": 10,
            "detected_classes": ["motion"],
        }
    ]
    assert not {"detections", "events"} & rows[0].keys()
    assert pool.calls[0][1] == ("tenant-a", "analysis", 20)


@pytest.mark.parametrize("requested,expected", [(-1, 1), (0, 1), (1, 1), (500, 500), (5000, 500)])
async def test_metadata_scan_limits_and_tenant_values_are_bound_parameters(requested, expected):
    pool = MetadataPool()
    tenant = "tenant'with-special-text"
    await WorkbenchStore(pool).list_analysis_metadata(tenant, requested)
    query, params = pool.calls[0]
    assert params == (tenant, "analysis", expected)
    assert tenant not in query
    # Select newest created rows before deriving classes from bounded saved frames.
    assert "AS MATERIALIZED" in query
    assert "ORDER BY created_at DESC, record_id DESC LIMIT %s" in query
    assert "detections[0 to 359].objects[0 to 31]" in query
