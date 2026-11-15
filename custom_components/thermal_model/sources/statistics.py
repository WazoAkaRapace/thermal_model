"""Long-term statistics: the only history that survives the recorder purge.

The recorder keeps raw states for ~10 days by default and the model needs weeks to be
worth anything, so identification reads the *hourly statistics* Home Assistant keeps
forever. They come from the same database, through the recorder's own API, in the
executor.

Hourly is also the resolution the identifier wants: it fits one-hour windows, so a
finer grid only adds noise and rows.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

__all__ = ["StatisticPoint", "async_list_statistic_ids", "async_read_statistics"]

_LOGGER = logging.getLogger(__name__)


class StatisticPoint:
    """One hourly bucket."""

    __slots__ = ("moment", "value")

    def __init__(self, moment: datetime, value: float) -> None:
        self.moment = moment
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StatisticPoint({self.moment.isoformat()}, {self.value})"


def _rows_to_points(rows: Sequence[dict[str, Any]], prefer: str) -> list[StatisticPoint]:
    """Turn statistics rows into points, taking `mean` or `change`."""
    points: list[StatisticPoint] = []
    for row in rows:
        value = row.get(prefer)
        if value is None:
            value = row.get("mean") if prefer != "mean" else row.get("change")
        if value is None:
            continue
        start = row.get("start")
        moment = (
            datetime.fromtimestamp(start / 1000, UTC) if isinstance(start, (int, float)) else start
        )
        if not isinstance(moment, datetime):
            continue
        points.append(StatisticPoint(moment, float(value)))
    return points


def _read(
    hass: HomeAssistant,
    statistic_ids: set[str],
    start: datetime,
    end: datetime,
    prefer: str = "mean",
) -> dict[str, list[StatisticPoint]]:
    """Read hourly statistics (sync: touches the recorder database)."""
    import homeassistant.components.recorder.statistics as recorder_statistics

    rows = recorder_statistics.statistics_during_period(
        hass,
        start,
        end,
        statistic_ids,
        "hour",
        None,  # units: take what the recorder stored
        {"mean", "change"},
    )
    return {
        statistic_id: _rows_to_points(values, prefer=prefer)
        for statistic_id, values in (rows or {}).items()
    }


def _list_ids(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Statistics the recorder knows about (sync)."""
    import homeassistant.components.recorder.statistics as recorder_statistics

    return recorder_statistics.list_statistic_ids(hass)


async def async_read_statistics(
    hass: HomeAssistant,
    statistic_ids: Sequence[str],
    days: int,
) -> dict[str, list[StatisticPoint]]:
    """Hourly statistics for the given ids, over the last ``days``."""
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    try:
        return await hass.async_add_executor_job(_read, hass, set(statistic_ids), start, end)
    except Exception as err:  # recorder not set up, database locked, …
        _LOGGER.debug("Thermal Model: statistics unavailable: %s", err)
        return {}


async def async_list_statistic_ids(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every statistic id with its metadata: name, unit, state_class."""
    try:
        return await hass.async_add_executor_job(_list_ids, hass)
    except Exception as err:  # pragma: no cover - recorder not ready
        _LOGGER.debug("Thermal Model: statistics list unavailable: %s", err)
        return []
