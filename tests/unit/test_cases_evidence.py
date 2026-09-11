"""Exact media provenance never substitutes a nearby or private frame."""

import pytest
from sio_api.case_evidence import resolve_frames, safe_frame_key


class EvidencePool:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, params):
        self.calls.append((sql, params))
        if "FROM detections" in sql:
            return [
                {
                    "detection_id": "det-a",
                    "observation_id": "obs-a",
                    "source_id": "camera-a",
                    "model_name": "model-v2",
                }
            ]
        if "FROM observations" in sql:
            return [
                {
                    "observation_id": "obs-a",
                    "source_id": "camera-a",
                    "raw_ref": "pending/tenants/tenant-a/frames/frame-a.jpg",
                    "payload": {"payload": {"frame_id": "frame-a"}},
                }
            ]
        if "FROM frames" in sql:
            return [
                {
                    "frame_id": "frame-a",
                    "source_id": "camera-a",
                    "ts": "2026-09-11T10:00:00Z",
                    "object_key": "tenants/tenant-a/frames/frame-a.jpg",
                    "redacted": True,
                    "width": 100,
                    "height": 50,
                },
                {
                    "frame_id": "frame-b",
                    "source_id": "camera-a",
                    "ts": "2026-09-11T10:00:01Z",
                    "object_key": "raw/tenants/tenant-a/frame-b.jpg",
                    "redacted": False,
                },
                {
                    "frame_id": "frame-c",
                    "source_id": "camera-a",
                    "ts": "2026-09-11T10:00:02Z",
                    "object_key": "tenants/tenant-b/frame-c.jpg",
                    "redacted": True,
                },
            ]
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_detection_observation_frame_chain_is_exact_and_tenant_scoped():
    pool = EvidencePool()
    result = await resolve_frames(
        pool,
        "tenant-a",
        {},
        [
            {
                "evidence": [
                    {"kind": "detection", "ref": "det-a"},
                    {"kind": "observation", "ref": "obs-missing"},
                ]
            }
        ],
    )
    assert len(result["resolved_frames"]) == 1
    frame = result["resolved_frames"][0]
    assert frame["media_url"] == "/media/tenants/tenant-a/frames/frame-a.jpg"
    assert frame["evidence_refs"] == ["det-a"]
    assert frame["detections"][0]["model_name"] == "model-v2"
    assert result["unresolved_media"][0]["ref"] == "obs-missing"
    assert all(params[0] == "tenant-a" for _, params in pool.calls)
    assert all("tenant_id = %s" in sql for sql, _ in pool.calls)
    assert "redacted = true" in pool.calls[-1][0]
    assert "frame-a" in pool.calls[-1][1][1]


@pytest.mark.parametrize(
    "key",
    [
        "pending/tenants/tenant-a/a.jpg",
        "raw/tenants/tenant-a/a.jpg",
        "tenants/tenant-b/a.jpg",
        "/a.jpg",
        "https://example.com/a.jpg",
        "a/../b.jpg",
        "a\\b.jpg",
        "a.html",
    ],
)
def test_private_external_or_non_image_keys_are_not_exposed(key):
    assert safe_frame_key(key, "tenant-a") is False


@pytest.mark.asyncio
async def test_no_reference_produces_explicit_empty_evidence_without_queries():
    pool = EvidencePool()
    result = await resolve_frames(pool, "tenant-a", {}, [])
    assert result["resolved_frames"] == []
    assert result["unresolved_media"] == []
    assert pool.calls == []
