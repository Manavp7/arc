"""Vector store and blob store contracts (in-memory / filesystem adapters)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sio_core.errors import NotFound, StoreError
from sio_core.stores.blob import FileBlobStore
from sio_core.stores.vectors import DEFAULT_DIM, MemoryVectorStore, cosine_similarity

TENANT = "default"


def unit_vector(index: int, dim: int = DEFAULT_DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index % dim] = 1.0
    return vector


# ------------------------------------------------------------------------ vectors
@pytest.fixture
def vectors() -> MemoryVectorStore:
    return MemoryVectorStore()


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([0, 0], [1, 0]) == 0.0, "zero vectors must not divide by zero"


async def test_vector_upsert_search_and_get(vectors: MemoryVectorStore) -> None:
    await vectors.upsert(
        "frames", "frame_1", unit_vector(0), tenant_id=TENANT, metadata={"cam": "a"}
    )
    await vectors.upsert(
        "frames", "frame_2", unit_vector(1), tenant_id=TENANT, metadata={"cam": "b"}
    )

    hits = await vectors.search("frames", unit_vector(0), tenant_id=TENANT, limit=2)
    assert [item_id for item_id, _, _ in hits] == ["frame_1", "frame_2"]
    assert hits[0][1] == pytest.approx(1.0)
    assert hits[0][2]["cam"] == "a"

    stored = await vectors.get("frames", "frame_1", tenant_id=TENANT)
    assert stored is not None and len(stored[0]) == DEFAULT_DIM


async def test_vector_search_filters_and_threshold(vectors: MemoryVectorStore) -> None:
    await vectors.upsert("frames", "a", unit_vector(0), tenant_id=TENANT, metadata={"cam": "gate"})
    await vectors.upsert("frames", "b", unit_vector(0), tenant_id=TENANT, metadata={"cam": "dock"})

    filtered = await vectors.search(
        "frames", unit_vector(0), tenant_id=TENANT, filters={"cam": "dock"}
    )
    assert [i for i, _, _ in filtered] == ["b"]
    assert await vectors.search("frames", unit_vector(5), tenant_id=TENANT, min_score=0.5) == []


async def test_vector_collections_and_tenants_are_separate(vectors: MemoryVectorStore) -> None:
    await vectors.upsert("frames", "x", unit_vector(0), tenant_id=TENANT)
    await vectors.upsert("faces", "x", unit_vector(0), tenant_id=TENANT)
    await vectors.upsert("frames", "x", unit_vector(0), tenant_id="acme")

    assert await vectors.count("frames", tenant_id=TENANT) == 1
    assert await vectors.count("faces", tenant_id=TENANT) == 1
    assert await vectors.count("frames", tenant_id="acme") == 1
    assert await vectors.search("frames", unit_vector(0), tenant_id="other") == []


async def test_vector_dimension_is_enforced(vectors: MemoryVectorStore) -> None:
    """A silent dimension mismatch would corrupt every later similarity score."""
    with pytest.raises(StoreError, match="512-d"):
        await vectors.upsert("frames", "bad", [0.1, 0.2], tenant_id=TENANT)


async def test_vector_delete(vectors: MemoryVectorStore) -> None:
    await vectors.upsert("frames", "gone", unit_vector(0), tenant_id=TENANT)
    await vectors.delete("frames", "gone", tenant_id=TENANT)
    assert await vectors.get("frames", "gone", tenant_id=TENANT) is None
    assert await vectors.ping() is True


# -------------------------------------------------------------------------- blobs
@pytest.fixture
def blobs(tmp_path: Path) -> FileBlobStore:
    return FileBlobStore(tmp_path / "media")


async def test_blob_put_get_exists_delete(blobs: FileBlobStore) -> None:
    key = "frames/cam-a/2026-07-24/000001.jpg"
    await blobs.put(key, b"\xff\xd8jpegbytes", content_type="image/jpeg")
    assert await blobs.exists(key)
    assert await blobs.get(key) == b"\xff\xd8jpegbytes"
    assert blobs.url_for(key) == f"/media/{key}"
    await blobs.delete(key)
    assert not await blobs.exists(key)


async def test_blob_get_missing_raises(blobs: FileBlobStore) -> None:
    with pytest.raises(NotFound):
        await blobs.get("frames/nope.jpg")


async def test_blob_list_by_prefix(blobs: FileBlobStore) -> None:
    for key in ("frames/a.jpg", "frames/b.jpg", "masks/c.png"):
        await blobs.put(key, b"x")
    assert sorted(await blobs.list("frames/")) == ["frames/a.jpg", "frames/b.jpg"]
    assert len(await blobs.list()) == 3
    assert len(await blobs.list(limit=1)) == 1


async def test_blob_key_cannot_escape_the_store(blobs: FileBlobStore) -> None:
    """A connector-supplied key must not be able to write outside the media root."""
    with pytest.raises(StoreError, match="escapes"):
        await blobs.put("../../etc/passwd", b"nope")


async def test_blob_delete_is_idempotent(blobs: FileBlobStore) -> None:
    await blobs.delete("frames/never-existed.jpg")
    assert await blobs.ping() is True
    await blobs.close()
