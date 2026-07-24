"""Export every contract as JSON Schema, so non-Python consumers share one source of truth.

    uv run python -m sio_schemas.export --out docs/schemas

The generated files are committed: a schema diff in a pull request is the clearest possible
signal that a wire format changed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import (
    Alert,
    AuditRecord,
    BusMessage,
    Decision,
    Detection,
    Entity,
    Event,
    Explanation,
    Forecast,
    HealthStatus,
    Mission,
    Observation,
    Principal,
    Relationship,
    SimulationRun,
    SioModel,
    Track,
    WorkflowRun,
)
from .base import SCHEMA_VERSION

EXPORTED: tuple[type[SioModel], ...] = (
    BusMessage,
    Observation,
    Detection,
    Track,
    Entity,
    Relationship,
    Event,
    Forecast,
    Decision,
    Alert,
    Explanation,
    Mission,
    WorkflowRun,
    SimulationRun,
    AuditRecord,
    Principal,
    HealthStatus,
)


def schema_for(model: type[SioModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True, mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://sio.dev/schemas/{SCHEMA_VERSION}/{model.__name__}.json"
    schema["x-sio-schema-version"] = SCHEMA_VERSION
    return schema


def write_all(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    index: dict[str, str] = {}
    for model in EXPORTED:
        path = out_dir / f"{model.__name__}.json"
        path.write_text(json.dumps(schema_for(model), indent=2, sort_keys=True) + "\n")
        written.append(path)
        index[model.__name__] = path.name
    manifest = out_dir / "index.json"
    manifest.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "schemas": index}, indent=2, sort_keys=True)
        + "\n"
    )
    written.append(manifest)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export SIO contracts as JSON Schema")
    parser.add_argument("--out", default="docs/schemas", type=Path, help="Output directory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the on-disk schemas differ from the models (for CI)",
    )
    args = parser.parse_args(argv)

    if args.check:
        stale: list[str] = []
        for model in EXPORTED:
            path = args.out / f"{model.__name__}.json"
            expected = json.dumps(schema_for(model), indent=2, sort_keys=True) + "\n"
            if not path.exists() or path.read_text() != expected:
                stale.append(model.__name__)
        if stale:
            print(f"stale schemas: {', '.join(stale)}\nrun: just schemas")
            return 1
        print(f"schemas up to date ({len(EXPORTED)} contracts, v{SCHEMA_VERSION})")
        return 0

    written = write_all(args.out)
    print(f"wrote {len(written)} files to {args.out} (schema v{SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
