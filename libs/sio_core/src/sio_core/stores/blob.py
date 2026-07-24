"""Object storage adapters: MinIO (default) and a local filesystem fallback.

Raw media is never deleted or mutated in place (PRD M2 "nothing discarded"), so both adapters
treat keys as immutable: re-putting the same key is allowed only because ingestion is
idempotent under at-least-once delivery, and the bytes are identical by construction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, NotFound, StoreError
from ..telemetry import get_logger

log = get_logger("sio.blob")


class FileBlobStore:
    """Filesystem-backed object store.

    Used when MinIO is unavailable (CI, `--minimal` bootstrap, unit tests). Keys map to paths
    under ``root``, with traversal blocked.
    """

    def __init__(self, root: Path | str, *, url_prefix: str = "/media") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._url_prefix = url_prefix.rstrip("/")

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if not str(candidate).startswith(str(root)):
            raise StoreError(f"blob key escapes the store root: {key!r}")
        return candidate

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFound(f"blob {key!r} not found")
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def list(self, prefix: str = "", *, limit: int = 1000) -> list[str]:
        base = self._root
        out: list[str] = []
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            rel = str(path.relative_to(base))
            if rel.startswith(prefix):
                out.append(rel)
            if len(out) >= limit:
                break
        return out

    def url_for(self, key: str) -> str:
        return f"{self._url_prefix}/{key}"

    async def ping(self) -> bool:
        return self._root.exists()

    async def close(self) -> None:
        return None


class MinioBlobStore:
    """MinIO / S3-compatible object store (PRD §8.2).

    The MinIO SDK is synchronous, so every call is offloaded to a worker thread — otherwise a
    single frame upload would stall the whole event loop of a service that is also consuming
    the bus.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = False,
        url_prefix: str = "/media",
    ) -> None:
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise DependencyMissing("minio", "MinioBlobStore") from exc
        self._client: Any = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self._url_prefix = url_prefix.rstrip("/")

    async def ensure_bucket(self) -> None:
        """Create the bucket when missing. Idempotent; called by ``scripts/init_minio.py``."""

        def _ensure() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await asyncio.to_thread(_ensure)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        import io

        def _put() -> None:
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
                metadata=dict(metadata) if metadata else None,
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"minio put {key!r} failed: {exc}") from exc
        return key

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            response = None
            try:
                response = self._client.get_object(self._bucket, key)
                return bytes(response.read())
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            raise NotFound(f"minio get {key!r} failed: {exc}") from exc

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            try:
                self._client.stat_object(self._bucket, key)
                return True
            except Exception:  # noqa: BLE001
                return False

        return await asyncio.to_thread(_stat)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.remove_object, self._bucket, key)

    async def list(self, prefix: str = "", *, limit: int = 1000) -> list[str]:
        def _list() -> list[str]:
            out: list[str] = []
            for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
                out.append(obj.object_name)
                if len(out) >= limit:
                    break
            return out

        return await asyncio.to_thread(_list)

    def url_for(self, key: str) -> str:
        # Served through the API rather than presigned so that access stays governed by the
        # same authz path as every other read (PRD M21) — a presigned URL bypasses policy.
        return f"{self._url_prefix}/{key}"

    async def ping(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self._client.bucket_exists, self._bucket))
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        return None
