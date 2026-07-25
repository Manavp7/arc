"""Enterprise data: CSV files and SQL databases (PRD M1, Phase 7).

The least glamorous connector and often the first one a deployment actually needs. A yard's WMS knows which
trailer is booked into which dock, and no amount of computer vision recovers that — it is a fact in somebody's
Oracle instance, and the platform is worth much less without it.

Two kinds in one module because they are the same problem with different transports: a table of rows, polled,
turned into observations. Sharing the row-to-observation mapping means a deployment can prototype against a CSV
export and switch to the live database by changing `kind` and adding a DSN — which is how these integrations
actually go, since getting a read-only database credential takes three weeks and getting a CSV takes an hour.

**Both are read-only by construction.** `csv_enterprise` never writes, and `sql_enterprise` refuses any statement
that is not a `SELECT` or a `WITH`. A connector that could write to a customer's system of record is a connector
nobody will be allowed to install, and "we only run the query you configured" is not a guarantee anybody accepts
when the query comes from a config file.
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from sio_schemas import Geo, Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: Statements a read-only connector may run.
#:
#: Checked as a prefix on the stripped, lowercased query. Deliberately a whitelist rather than a blacklist of
#: `DROP`/`DELETE`/`UPDATE`: a blacklist has to anticipate every way to write, including the ones a specific
#: dialect invents, and it only takes one gap. A whitelist is wrong in the safe direction.
READ_ONLY_PREFIXES = ("select", "with")


def _refuse_writes(query: str) -> None:
    """Raise unless the query only reads.

    Stated as its own function because it is the single most important line in this module. A connector pointed
    at a customer's system of record must not be able to change it, and the check has to happen at configuration
    time — a deployment that discovers this at 2am has already run the statement.
    """
    stripped = query.strip().lstrip("(").lower()
    if not stripped.startswith(READ_ONLY_PREFIXES):
        first_word = stripped.split()[0] if stripped.split() else "(empty)"
        raise ValueError(
            f"this connector is read-only and will not run a {first_word.upper()} statement. "
            f"Only SELECT and WITH are permitted — a connector that can write to a system of record "
            f"is one nobody will let you install."
        )
    # A trailing statement after a semicolon is the classic way to smuggle a write past a prefix check.
    body = stripped.rstrip(";")
    if ";" in body:
        raise ValueError(
            "the query contains more than one statement; only a single SELECT is permitted. "
            "A prefix check cannot vouch for what follows a semicolon."
        )


def _rows_to_observations(
    rows: Iterable[dict[str, Any]],
    *,
    source_id: str,
    modality: Modality,
    mapping: dict[str, str],
) -> list[Observation]:
    """Turn rows into observations using a declared column mapping.

    A mapping rather than convention, because nobody's warehouse names a column `entity_id`. Requiring one is the
    difference between a connector that works against the customer's schema and a connector that works against
    the schema we wish they had.

    Unmapped columns are kept in the payload rather than dropped. The platform does not know which of a
    customer's forty columns matters, and discarding them means a later question — "was this trailer flagged
    hazmat?" — cannot be answered without re-ingesting history this platform deliberately never deletes.
    """
    observations: list[Observation] = []
    for row in rows:
        payload = dict(row)
        latitude = _number(row.get(mapping.get("lat", "lat")))
        longitude = _number(row.get(mapping.get("lon", "lon")))
        geo = (
            Geo(lat=latitude, lon=longitude)
            if latitude is not None and longitude is not None
            else None
        )
        observations.append(
            Observation(
                source_id=source_id,
                modality=modality,
                ts=utc_now(),
                geo=geo,
                # Everything goes in the payload, including the label. `Observation` has no `label` field
                # and no `entity_id` — which is the schema enforcing the right thing: fusion decides identity,
                # and a connector able to assert it would let a WMS trailer number silently overrule a track
                # the perception stack has been following for ten minutes.
                payload={
                    **payload,
                    "label": str(row.get(mapping.get("label", "label"), "") or "") or None,
                },
            )
        )
    return observations


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@register_connector
class CsvEnterpriseConnector(Connector):
    """Reads a CSV file, re-reading it when it changes.

    Watches by modification time rather than polling the contents, so a 200MB nightly export costs one `stat`
    per interval instead of a full parse. The whole file is re-read when it does change, deliberately: an export
    is a snapshot, and diffing snapshots to find "new" rows guesses at a primary key the file may not have.
    """

    kind = "csv_enterprise"
    modality = Modality.ENTERPRISE

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.path = Path(str(options.get("path", "")))
        self.interval_s = float(options.get("interval_s", 60))
        self.mapping = dict(options.get("mapping") or {})
        self.modality_override = options.get("modality")
        self.once = bool(options.get("once", False))
        self._last_mtime: float | None = None
        self._rows_seen = 0
        self._error: str | None = None

    async def start(self) -> None:
        if not self.path:
            raise ValueError(f"{self.kind} needs options.path (the CSV to read)")
        if not self.path.exists():
            # Refused at startup rather than logged every minute. A connector configured against a path that
            # does not exist is a deployment mistake, and discovering it in a log an hour later wastes the hour.
            raise FileNotFoundError(
                f"no such file: {self.path}. The path is resolved relative to the working directory "
                f"({Path.cwd()}), so an absolute path is usually what you want."
            )

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            for observation in self._read_if_changed():
                yield observation
            if self.once:
                return
            await asyncio.sleep(self.interval_s)

    def _read_if_changed(self) -> list[Observation]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as error:
            self._error = f"cannot stat {self.path}: {error}"
            return []
        if self._last_mtime is not None and mtime <= self._last_mtime:
            return []
        self._last_mtime = mtime

        try:
            text = self.path.read_text()
        except OSError as error:
            self._error = f"cannot read {self.path}: {error}"
            return []

        rows = list(csv.DictReader(io.StringIO(text)))
        observations = _rows_to_observations(
            rows,
            source_id=self.source_id,
            modality=Modality(self.modality_override) if self.modality_override else self.modality,
            mapping=self.mapping,
        )
        self._rows_seen += len(observations)
        self._error = None
        self.log.info("csv.read", path=str(self.path), rows=len(observations))
        return observations

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        return f"ok ({self._rows_seen} rows read)"


@register_connector
class SqlEnterpriseConnector(Connector):
    """Polls a SQL query against any database SQLAlchemy can reach.

    SQLAlchemy rather than a driver per database, because "generic JDBC/CSV enterprise connector" means a
    deployment points it at Oracle, SQL Server, MySQL or Postgres and it works. An optional dependency: a
    platform whose default install pulls in database drivers nobody uses is one that takes four minutes to
    `pip install`.
    """

    kind = "sql_enterprise"
    modality = Modality.ENTERPRISE

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.dsn = str(options.get("dsn", ""))
        self.query = str(options.get("query", ""))
        self.interval_s = float(options.get("interval_s", 300))
        self.mapping = dict(options.get("mapping") or {})
        self.modality_override = options.get("modality")
        self._engine: Any = None
        self._error: str | None = None
        self._rows_seen = 0

    async def start(self) -> None:
        if not self.dsn or not self.query:
            raise ValueError(f"{self.kind} needs options.dsn and options.query")
        # Before anything connects. A read-only guarantee checked after the connection is open is a guarantee
        # checked after the statement could have run.
        _refuse_writes(self.query)
        try:
            from sqlalchemy import create_engine
        except ImportError as error:
            raise RuntimeError(
                "sql_enterprise needs SQLAlchemy: `uv pip install 'sio-ingest[enterprise]'`. "
                "It is optional because most deployments do not use it, and a default install that "
                "pulls in database drivers nobody needs is one nobody enjoys."
            ) from error
        self._engine = create_engine(self.dsn, pool_pre_ping=True)

    async def stop(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            for observation in await asyncio.to_thread(self._poll):
                yield observation
            await asyncio.sleep(self.interval_s)

    def _poll(self) -> list[Observation]:
        """Run the query on a thread.

        SQLAlchemy's sync engine blocks, and blocking the event loop would stall every other connector in the
        service — a slow enterprise database becoming a platform-wide outage. `to_thread` keeps somebody else's
        query planner out of our scheduler.
        """
        from sqlalchemy import text as sql_text

        try:
            with self._engine.connect() as connection:  # type: ignore[union-attr]
                result = connection.execute(sql_text(self.query))
                rows = [dict(row._mapping) for row in result]
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            self.log.warning("sql.query_failed", error=self._error)
            return []

        self._error = None
        self._rows_seen += len(rows)
        return _rows_to_observations(
            rows,
            source_id=self.source_id,
            modality=Modality(self.modality_override) if self.modality_override else self.modality,
            mapping=self.mapping,
        )

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        return f"ok ({self._rows_seen} rows read)"


__all__ = [
    "READ_ONLY_PREFIXES",
    "CsvEnterpriseConnector",
    "SqlEnterpriseConnector",
]
