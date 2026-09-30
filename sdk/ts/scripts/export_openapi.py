#!/usr/bin/env python3
"""Export the API schema without starting services or connecting to datastores."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from sio_api.app import ApiService

from sio_core.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).parents[1] / "openapi.json")
    parser.add_argument("--check", action="store_true", help="Fail if the saved schema is stale")
    args = parser.parse_args()
    # Building routes does not enter FastAPI's lifespan. Any local adapter paths point
    # into this temporary directory, never an operator's recordings or configuration.
    with tempfile.TemporaryDirectory(prefix="sio-openapi-") as directory:
        service = ApiService(
            Settings(
                _env_file=None,
                bus_backend="memory",
                blob_backend="file",
                data_dir=Path(directory),
                log_level="ERROR",
            )
        )
        rendered = json.dumps(service.app.openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.out.exists() or args.out.read_text() != rendered:
            print(f"OpenAPI schema is stale: {args.out}; run this script without --check")
            return 1
        print(f"OpenAPI schema is current: {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered)
    print(f"Exported OpenAPI schema: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
