"""Store tests: what the dashboard reads back must be what was written."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.thermal_model.const import ModelScenario
from custom_components.thermal_model.core.forecast import ForecastRun
from custom_components.thermal_model.db import SCHEMA_VERSION, Database

START = datetime(2026, 1, 10, 12, tzinfo=UTC)


def _run(issued: datetime, scenario: ModelScenario = ModelScenario.FREE_FLOAT) -> ForecastRun:
    times = tuple(issued + timedelta(hours=hour) for hour in range(3))
    return ForecastRun(
        issued_at=issued,
        scenario=scenario,
        source="test",
        times=times,
        values=(19.0, 18.5, 18.0),
        params_digest="0.01600/0.00230/+0.0400/0.4900",
    )


def test_roundtrip_keeps_times_values_and_metadata(tmp_path: Path) -> None:
    """A run read back must be identical, otherwise the comparison is a lie."""
    store = Database(tmp_path / "runs.db")
    store.record_run(_run(START))

    (stored,) = store.recent_runs()

    assert stored.issued_at == START
    assert stored.scenario is ModelScenario.FREE_FLOAT
    assert stored.source == "test"
    assert stored.params_digest == "0.01600/0.00230/+0.0400/0.4900"
    assert stored.times == (START, START + timedelta(hours=1), START + timedelta(hours=2))
    assert stored.values == (19.0, 18.5, 18.0)
    assert stored.as_run().minimum == 18.0


def test_runs_come_back_newest_first(tmp_path: Path) -> None:
    """The dashboard shows the latest run: ordering is part of the contract."""
    store = Database(tmp_path / "runs.db")
    for hour in (0, 6, 3):
        store.record_run(_run(START + timedelta(hours=hour)))

    issued = [run.issued_at for run in store.recent_runs()]

    assert issued == sorted(issued, reverse=True)


def test_limit_and_scenario_filter(tmp_path: Path) -> None:
    """Comparisons are per scenario: filtering must not mix them."""
    store = Database(tmp_path / "runs.db")
    store.record_run(_run(START, ModelScenario.FREE_FLOAT))
    store.record_run(_run(START + timedelta(hours=1), ModelScenario.THERMOSTAT))
    store.record_run(_run(START + timedelta(hours=2), ModelScenario.THERMOSTAT))

    assert len(store.recent_runs(limit=2)) == 2
    thermostats = store.recent_runs(scenario=ModelScenario.THERMOSTAT)
    assert len(thermostats) == 2
    assert all(run.scenario is ModelScenario.THERMOSTAT for run in thermostats)


def test_before_excludes_the_run_itself(tmp_path: Path) -> None:
    """'What did I predict 6 h ago' must not return the run issued now."""
    store = Database(tmp_path / "runs.db")
    store.record_run(_run(START))
    store.record_run(_run(START - timedelta(hours=6)))

    older = store.recent_runs(before=START)

    assert len(older) == 1
    assert older[0].issued_at == START - timedelta(hours=6)


def test_prune_keeps_recent_runs_and_thins_old_days(tmp_path: Path) -> None:
    """Retention must bound the file without emptying the comparison window."""
    store = Database(tmp_path / "runs.db")
    now = datetime.now(UTC)
    for hour in range(10):  # 10 runs today: all kept
        store.record_run(_run(now - timedelta(minutes=hour * 10)))
    for hour in range(12):  # 12 runs 30 days ago: thinned
        store.record_run(_run(now - timedelta(days=30, hours=hour)))

    deleted = store.prune(keep_days=14, keep_per_day=4)

    assert deleted == 8
    assert store.count() == 14


def test_count_and_size_are_reported(tmp_path: Path) -> None:
    """Diagnostics need a cheap way to see the store is alive."""
    store = Database(tmp_path / "runs.db")
    assert store.count() == 0
    store.record_run(_run(START))
    assert store.count() == 1
    assert store.size_bytes() > 0


def test_schema_version_is_stamped(tmp_path: Path) -> None:
    """Upgrades must be explicit: a newer file is refused, not silently used."""
    store = Database(tmp_path / "runs.db")
    store.record_run(_run(START))

    import sqlite3

    with sqlite3.connect(tmp_path / "runs.db") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        connection.execute("PRAGMA user_version=999")

    with pytest.raises(RuntimeError, match="newer"):
        store.recent_runs()


def test_clear_empties_the_store(tmp_path: Path) -> None:
    """Changing the model or the sources invalidates the archived runs."""
    store = Database(tmp_path / "runs.db")
    store.record_run(_run(START))

    store.clear()

    assert store.count() == 0
