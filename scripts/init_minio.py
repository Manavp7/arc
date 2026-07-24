#!/usr/bin/env python3
"""Create the media bucket, idempotently.

    uv run python scripts/init_minio.py           # create the bucket if missing
    uv run python scripts/init_minio.py --check   # report only

The bucket referenced by ``SIO_MINIO_BUCKET`` has to exist before the first frame is written,
and nothing else creates it — so this ran nowhere in the previous scaffold and every media
write would have failed at runtime.

Uses the Python ``minio`` client rather than the ``mc`` CLI on purpose: ``mc`` is an extra
binary to install on two platforms, and it would be the only part of setup that could not run
from the same virtualenv as everything else.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))

# Media tiering (PRD §17 R7 "retention/tiering from day one"). Frames are the bulk of the
# data and the least valuable after an incident is closed; masks and reports are small and
# worth keeping.
LIFECYCLE_RULES = [
    ("frames/", "SIO_RETAIN_FRAMES_DAYS"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the SIO MinIO bucket")
    parser.add_argument("--check", action="store_true", help="report only, create nothing")
    parser.add_argument("--no-lifecycle", action="store_true", help="skip retention rules")
    args = parser.parse_args(argv)

    from sio_core.config import get_settings

    cfg = get_settings()

    if cfg.blob_backend != "minio":
        print(f"SIO_BLOB_BACKEND={cfg.blob_backend}: MinIO not in use, nothing to do")
        return 0

    try:
        from minio import Minio
        from minio.commonconfig import Filter
        from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
    except ImportError:
        print("the minio package is not installed; run: just setup", file=sys.stderr)
        return 2

    client = Minio(
        cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        secure=cfg.minio_secure,
    )
    print(f"minio: {cfg.minio_endpoint} bucket={cfg.minio_bucket}")

    try:
        exists = client.bucket_exists(cfg.minio_bucket)
    except Exception as exc:
        print(f"  cannot reach minio: {exc}", file=sys.stderr)
        print("\nstart it with:  just services", file=sys.stderr)
        print("or switch backends:  SIO_BLOB_BACKEND=file", file=sys.stderr)
        return 2

    if args.check:
        print(f"  bucket exists: {exists}")
        return 0 if exists else 1

    if exists:
        print("  bucket already present")
    else:
        client.make_bucket(cfg.minio_bucket)
        print("  bucket created")

    if not args.no_lifecycle:
        try:
            rules = [
                Rule(
                    rule_id=f"expire-{prefix.strip('/')}",
                    status="Enabled",
                    rule_filter=Filter(prefix=prefix),
                    expiration=Expiration(days=int(getattr(cfg, attr.lower()[4:], 7))),
                )
                for prefix, attr in LIFECYCLE_RULES
            ]
            client.set_bucket_lifecycle(cfg.minio_bucket, LifecycleConfig(rules))
            print(f"  lifecycle rules applied: {len(rules)}")
        except Exception as exc:
            # Lifecycle support varies across MinIO builds; a missing retention rule is a
            # housekeeping gap, not a reason to fail setup.
            print(f"  lifecycle rules skipped: {exc}")

    # Prove the bucket is actually writable, rather than assuming creation implies access.
    probe_key = ".sio-init-probe"
    import io

    client.put_object(cfg.minio_bucket, probe_key, io.BytesIO(b"ok"), length=2)
    client.remove_object(cfg.minio_bucket, probe_key)
    print("  write probe: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
