#!/usr/bin/env python3
"""Bootstrap Neo4j: set the initial password, then apply the schema.

    uv run python scripts/init_neo4j.py            # detect state, fix it, apply constraints
    uv run python scripts/init_neo4j.py --check    # report only
    uv run python scripts/init_neo4j.py --reset    # wipe the auth store, then re-bootstrap

Why this script exists
----------------------
Neo4j Community ships with the credentials ``neo4j/neo4j`` and *requires* a password change
before it will accept any query. A naive ``cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -f
constraints.cypher`` therefore fails on every fresh install — usually silently, because the
error looks like an ordinary auth failure and the next service to start reports "connection
refused"-shaped noise instead.

So this script *detects* which of four states the database is in and does the right thing:

1. already using the configured password  → just apply the schema;
2. uninitialised (still ``neo4j/neo4j``)  → change the password over Bolt, then apply;
3. auth store absent (never started)      → ``neo4j-admin dbms set-initial-password``;
4. initialised with an unknown password   → explain precisely how to recover, and exit non-zero
   rather than pretending to have succeeded.

State 3 needs the server *stopped*, because ``set-initial-password`` writes the auth store
directly; that is handled and reported rather than assumed.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO_ROOT / "infra" / "neo4j" / "constraints.cypher"
DEFAULT_PASSWORD = "neo4j"  # Neo4j's factory credential

sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))


def log(message: str) -> None:
    print(f"  {message}")


def neo4j_home() -> Path | None:
    """Where Neo4j lives: the repo-local tarball (Linux) or the Homebrew prefix (macOS)."""
    local = REPO_ROOT / ".sio" / "neo4j"
    if (local / "bin" / "neo4j").exists():
        return local
    for candidate in (
        Path("/opt/homebrew/opt/neo4j/libexec"),
        Path("/usr/local/opt/neo4j/libexec"),
        Path("/var/lib/neo4j"),
    ):
        if (candidate / "bin" / "neo4j-admin").exists() or (candidate / "bin" / "neo4j").exists():
            return candidate
    return None


def neo4j_admin_command() -> list[str] | None:
    home = neo4j_home()
    if home and (home / "bin" / "neo4j-admin").exists():
        return [str(home / "bin" / "neo4j-admin")]
    found = shutil.which("neo4j-admin")
    return [found] if found else None


def auth_store_paths() -> list[Path]:
    home = neo4j_home()
    if not home:
        return []
    base = home / "data" / "dbms"
    return [base / "auth", base / "auth.ini"]


def auth_store_initialised() -> bool:
    return any(path.exists() for path in auth_store_paths())


def try_connect(uri: str, user: str, password: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for one credential attempt."""
    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import AuthError, Neo4jError
    except ImportError:  # pragma: no cover
        return False, "the neo4j python driver is not installed (run: just setup)"

    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=timeout)
        with driver.session() as session:
            session.run("RETURN 1").consume()
        return True, "ok"
    except AuthError as exc:
        return False, f"auth: {exc}"
    except Neo4jError as exc:
        # A credentials-expired error means the password is *correct* but must be changed —
        # which is state 2, and is progress rather than failure.
        if "credentials" in str(exc).lower() and "expired" in str(exc).lower():
            return False, "credentials_expired"
        return False, f"neo4j: {exc}"
    except Exception as exc:
        return False, f"unreachable: {exc}"
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.close()


def change_password_over_bolt(uri: str, user: str, old: str, new: str) -> bool:
    """Rotate the password using the system database (works while credentials are expired)."""
    try:
        from neo4j import GraphDatabase
    except ImportError:  # pragma: no cover
        return False
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, old))
        with driver.session(database="system") as session:
            session.run(
                "ALTER CURRENT USER SET PASSWORD FROM $old TO $new", old=old, new=new
            ).consume()
        return True
    except Exception as exc:
        log(f"password change over bolt failed: {exc}")
        return False
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.close()


def set_initial_password(password: str) -> bool:
    """Write the auth store directly. Requires the server to be stopped."""
    command = neo4j_admin_command()
    if not command:
        log("neo4j-admin not found; cannot set the initial password")
        return False
    for variant in (
        [*command, "dbms", "set-initial-password", password, "--require-password-change=false"],
        [*command, "dbms", "set-initial-password", password],
        [*command, "set-initial-password", password],  # Neo4j 4.x layout
    ):
        try:
            result = subprocess.run(
                variant,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "NEO4J_HOME": str(neo4j_home() or "")},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"neo4j-admin invocation failed: {exc}")
            continue
        if result.returncode == 0:
            log("initial password set via neo4j-admin")
            return True
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        if detail:
            log(f"neo4j-admin: {detail[-1]}")
    return False


def reset_auth_store() -> None:
    for path in auth_store_paths():
        if path.exists():
            path.unlink()
            log(f"removed {path}")


def cypher_statements(text: str) -> list[str]:
    """Split a .cypher file into statements.

    Comments are stripped *before* splitting on ';', because prose in a comment may contain a
    semicolon — which is exactly how the first version of this broke.
    """
    lines = [line for line in text.splitlines() if not line.strip().startswith("//")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def apply_constraints(uri: str, user: str, password: str, database: str) -> int:
    from neo4j import GraphDatabase

    statements = cypher_statements(CONSTRAINTS.read_text())
    applied = 0
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            for statement in statements:
                session.run(statement).consume()
                applied += 1
    finally:
        driver.close()
    return applied


def report(uri: str, user: str, password: str, database: str) -> int:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            constraints = len(list(session.run("SHOW CONSTRAINTS")))
            indexes = len(list(session.run("SHOW INDEXES")))
            entities = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()
        log(f"constraints: {constraints}, indexes: {indexes}, entities: {entities['n']}")
    finally:
        driver.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Neo4j for SIO")
    parser.add_argument("--check", action="store_true", help="report state, change nothing")
    parser.add_argument("--reset", action="store_true", help="wipe the auth store first")
    args = parser.parse_args(argv)

    from sio_core.config import get_settings

    cfg = get_settings()
    uri, user, password, database = (
        cfg.neo4j_uri,
        cfg.neo4j_user,
        cfg.neo4j_password,
        cfg.neo4j_database,
    )
    print(f"neo4j bootstrap: {uri} (user: {user}, database: {database})")

    if len(password) < 8:
        print(
            f"  NEO4J_PASSWORD is {len(password)} characters; Neo4j requires at least 8.",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        log("--reset: removing the auth store (the server must be stopped)")
        reset_auth_store()

    # State 1: the configured password already works.
    ok, detail = try_connect(uri, user, password)
    if ok:
        log("configured password accepted")
        if args.check:
            return report(uri, user, password, database)
        applied = apply_constraints(uri, user, password, database)
        log(f"applied {applied} schema statements")
        return report(uri, user, password, database)

    if args.check:
        log(f"cannot connect with the configured password: {detail}")
        return 1

    reachable = "unreachable" not in detail

    # State 3: never initialised and not running — write the auth store directly.
    if not auth_store_initialised() and not reachable:
        log("auth store absent and server not reachable; setting the initial password")
        if set_initial_password(password):
            log("start the server and re-run: just services && just neo4j-init")
            return 0
        log("could not set the initial password")
        print(
            "\nremedy: start Neo4j (just services), then re-run just neo4j-init — "
            "the password can also be changed over Bolt once the server is up.",
            file=sys.stderr,
        )
        return 1

    if not reachable:
        print(
            f"\nneo4j is not reachable at {uri}: {detail}\n"
            "start it with:  just services\n"
            f"or switch backends for now:  SIO_GRAPH_BACKEND=postgres",
            file=sys.stderr,
        )
        return 2

    # State 2: reachable, still on the factory credential (possibly flagged as expired).
    for candidate in (DEFAULT_PASSWORD,):
        ok_default, detail_default = try_connect(uri, user, candidate)
        if ok_default or detail_default == "credentials_expired":
            log(f"server is using the factory credential ({user}/{candidate}); rotating it")
            if change_password_over_bolt(uri, user, candidate, password):
                log("password rotated to the configured value")
                applied = apply_constraints(uri, user, password, database)
                log(f"applied {applied} schema statements")
                return report(uri, user, password, database)
            print("  password rotation failed", file=sys.stderr)
            return 1

    # State 4: initialised with something we do not know.
    print(
        "\nneo4j is running but rejects both the configured password and the factory default.\n"
        "  either set NEO4J_PASSWORD in .env to the real password,\n"
        "  or reset it:  just services-stop neo4j && "
        "uv run python scripts/init_neo4j.py --reset && just services\n",
        file=sys.stderr,
    )
    return 1


def wait_for_bolt(uri: str, attempts: int = 30) -> bool:
    """Poll Bolt until it answers. Used by callers that have just started the server."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(uri)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 7687
    for _ in range(attempts):
        with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=1):
            return True
        time.sleep(1)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
