#!/usr/bin/env python3
"""Apply the Postgres schema, idempotently.

    uv run python scripts/init_db.py            # apply anything not yet applied
    uv run python scripts/init_db.py --check    # report status, change nothing
    uv run python scripts/init_db.py --reset    # DROP the schema and rebuild (destructive)

Each file in ``infra/postgres/*.sql`` is applied once and recorded in ``schema_migrations``
with a checksum. If a file changes after it has been applied, that is reported rather than
silently re-run: re-running an edited migration is how two developers end up with different
databases and identical git histories.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "infra" / "postgres"

sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def connect(dsn: str):  # type: ignore[no-untyped-def]
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        print("psycopg is not installed; run: just setup", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        return psycopg.connect(dsn, autocommit=True)
    except Exception as exc:
        print(f"cannot connect to postgres: {exc}", file=sys.stderr)
        print("\nis it running?  just services", file=sys.stderr)
        raise SystemExit(2) from None


def ensure_bookkeeping(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    text PRIMARY KEY,
            checksum    text NOT NULL,
            applied_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied_state(conn) -> dict[str, str]:  # type: ignore[no-untyped-def]
    rows = conn.execute("SELECT filename, checksum FROM schema_migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def reset_schema(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop everything SIO owns. Triggers block DELETE, but DROP TABLE is a different verb —
    which is why this is an explicit, clearly labelled flag rather than a default."""
    print("!! dropping the public schema (all SIO data)")
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the SIO Postgres schema")
    parser.add_argument("--check", action="store_true", help="report status without applying")
    parser.add_argument("--reset", action="store_true", help="drop and rebuild (destructive)")
    parser.add_argument("--dsn", default=None, help="override the connection string")
    args = parser.parse_args(argv)

    from sio_core.config import get_settings

    dsn = args.dsn or get_settings().pg_dsn
    files = migrations()
    if not files:
        print(f"no migrations found in {MIGRATIONS_DIR}")
        return 1

    with connect(dsn) as conn:
        if args.reset:
            reset_schema(conn)
        ensure_bookkeeping(conn)
        state = applied_state(conn)

        pending: list[Path] = []
        drifted: list[str] = []
        for path in files:
            digest = checksum(path.read_text())
            previous = state.get(path.name)
            if previous is None:
                pending.append(path)
            elif previous != digest:
                drifted.append(path.name)

        if args.check:
            print(f"postgres: {dsn.rsplit('@', 1)[-1]}")
            print(f"  applied: {len(state)}  pending: {len(pending)}  drifted: {len(drifted)}")
            for path in pending:
                print(f"  pending  {path.name}")
            for name in drifted:
                print(f"  DRIFTED  {name} (already applied with different contents)")
            return 1 if (pending or drifted) else 0

        if drifted:
            print("these migrations changed after being applied:", file=sys.stderr)
            for name in drifted:
                print(f"  {name}", file=sys.stderr)
            print(
                "\nadd a new numbered file instead of editing an applied one, "
                "or rebuild with --reset (destructive).",
                file=sys.stderr,
            )
            return 1

        if not pending:
            print(f"schema up to date ({len(state)} migrations applied)")
        for path in pending:
            print(f"applying {path.name}")
            try:
                conn.execute(path.read_text())
            except Exception as exc:
                print(f"  failed: {exc}", file=sys.stderr)
                if "postgis" in str(exc).lower() or "vector" in str(exc).lower():
                    print(
                        "  hint: PostGIS/pgvector extensions are missing. "
                        "macOS: brew install postgis pgvector. "
                        "Linux: apt-get install postgresql-16-postgis-3 postgresql-16-pgvector",
                        file=sys.stderr,
                    )
                return 1
            conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s) "
                "ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum",
                (path.name, checksum(path.read_text())),
            )
            print(f"  ok {path.name}")

        extensions = {row[0] for row in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        for required in ("postgis", "vector"):
            marker = "ok" if required in extensions else "MISSING"
            print(f"  extension {required}: {marker}")
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchone()
        print(f"schema ready: {tables[0]} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
