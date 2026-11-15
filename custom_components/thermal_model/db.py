"""Local SQLite store for forecast runs.

Why a database at all: the whole point of the dashboard is to show *what the
model predicted ago* next to *what actually happened*. Home Assistant's recorder
keeps entity states, not the trajectories we computed, and its retention is the
user's business. So each run is written here, with its scenario, its issue time
and its parameter fingerprint.

Access rules (matching the integration's standing decisions):

- never touch SQLite from the event loop: the caller passes work through the
  Home Assistant executor;
- WAL + ``synchronous=NORMAL`` so a batch write per cycle is cheap on an SD card;
- a schema version in ``PRAGMA user_version`` so upgrades are explicit;
- retention in tiers: recent runs are kept individually, older ones are thinned.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .const import ModelScenario
from .core.forecast import ForecastRun

__all__ = ["SCHEMA_VERSION", "Database", "StoredRun"]

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issued_at TEXT NOT NULL,
    scenario TEXT NOT NULL,
    source TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    points TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_issued ON forecast_runs (issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON forecast_runs (scenario, issued_at DESC);
"""


@dataclass(frozen=True, kw_only=True)
class StoredRun:
    """A run as read back from the store."""

    issued_at: datetime
    scenario: ModelScenario
    source: str
    params_digest: str
    times: tuple[datetime, ...]
    values: tuple[float, ...]

    def as_run(self) -> ForecastRun:
        """Convert back to the core dataclass."""
        return ForecastRun(
            issued_at=self.issued_at,
            scenario=self.scenario,
            source=self.source,
            times=self.times,
            values=self.values,
            params_digest=self.params_digest,
        )


def _parse_timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Database:
    """Thin, synchronous wrapper around the run store (call it in an executor)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- connection ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SCHEMA)
        self._migrate(connection)
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current} is newer than this integration ({SCHEMA_VERSION})"
            )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()

    # -- writes ----------------------------------------------------------
    def record_run(self, run: ForecastRun) -> None:
        """Append one run."""
        payload = {
            "times": [moment.isoformat() for moment in run.times],
            "values": [round(value, 4) for value in run.values],
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO forecast_runs (issued_at, scenario, source, params_digest, points)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    run.issued_at.isoformat(),
                    str(run.scenario),
                    run.source,
                    run.params_digest,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

    def prune(self, *, keep_days: int = 14, keep_per_day: int = 4) -> int:
        """Thin old runs: keep the newest ``keep_per_day`` of each past day.

        Returns the number of rows deleted. Recent rows (today and yesterday) are
        never thinned, so the dashboard's "what did I predict a few hours ago"
        comparison always has data.
        """
        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        deleted = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, issued_at FROM forecast_runs WHERE issued_at < ?"
                " ORDER BY issued_at DESC",
                (cutoff.isoformat(),),
            ).fetchall()
            keep: dict[str, int] = {}
            for row_id, issued in rows:
                day = issued[:10]
                seen = keep.get(day, 0)
                if seen >= keep_per_day:
                    connection.execute("DELETE FROM forecast_runs WHERE id = ?", (row_id,))
                    deleted += 1
                else:
                    keep[day] = seen + 1
        return deleted

    def clear(self) -> None:
        """Drop every run (used when the model or the sources change)."""
        with self._connect() as connection:
            connection.execute("DELETE FROM forecast_runs")

    # -- reads -----------------------------------------------------------
    def recent_runs(
        self,
        *,
        limit: int = 8,
        scenario: ModelScenario | None = None,
        before: datetime | None = None,
    ) -> list[StoredRun]:
        """Most recent runs, newest first."""
        query = "SELECT issued_at, scenario, source, params_digest, points FROM forecast_runs"
        conditions: list[str] = []
        parameters: list[str] = []
        if scenario is not None:
            conditions.append("scenario = ?")
            parameters.append(str(scenario))
        if before is not None:
            conditions.append("issued_at < ?")
            parameters.append(before.isoformat())
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY issued_at DESC LIMIT ?"
        parameters.append(str(limit))

        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()

        stored: list[StoredRun] = []
        for issued, scenario_name, source, digest, points in rows:
            payload = json.loads(points)
            stored.append(
                StoredRun(
                    issued_at=_parse_timestamp(issued),
                    scenario=ModelScenario(scenario_name),
                    source=source,
                    params_digest=digest,
                    times=tuple(_parse_timestamp(value) for value in payload["times"]),
                    values=tuple(float(value) for value in payload["values"]),
                )
            )
        return stored

    def run_near(
        self,
        *,
        age_hours: float,
        scenario: ModelScenario,
        tolerance_hours: float = 3.0,
        now: datetime | None = None,
    ) -> StoredRun | None:
        """The run issued closest to ``age_hours`` ago, if one is close enough.

        The dashboard compares the trajectory of *that* moment with what happened
        since. Taking the three most recent runs instead would compare three
        curves issued minutes apart — visually identical, and useless as a check.
        """
        target = (now or datetime.now(UTC)) - timedelta(hours=age_hours)
        best: tuple[float, StoredRun] | None = None
        for run in self.recent_runs(limit=500, scenario=scenario):
            delta = abs((run.issued_at - target).total_seconds()) / 3600.0
            if delta > tolerance_hours:
                continue
            if best is None or delta < best[0]:
                best = (delta, run)
        return best[1] if best else None

    def count(self) -> int:
        """Number of stored runs."""
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0])

    def size_bytes(self) -> int:
        """Size of the database file, for diagnostics."""
        return self.path.stat().st_size if self.path.exists() else 0
