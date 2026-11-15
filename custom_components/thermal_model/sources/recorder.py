"""Read the bound entities' history out of the recorder.

The integration identifies its own model, so it needs the past the way Home
Assistant recorded it — not a series the user exports by hand. Everything here runs
in the executor: the recorder is a SQLite database and blocking the event loop on it
is the classic integration mistake.

Three inputs are needed and only two are sensors:

- indoor temperature: the bound indoor sensor;
- outdoor temperature: the bound outdoor sensor;
- heating: either a power sensor (``CONF_HEATING_POWER_SENSOR``, kW or W) or the
  heating entity itself, where "on" is read per family (``climate`` needs
  ``hvac_action == heating``) and multiplied by the configured capacity. That
  proxy is published as such: ``k`` identified from it is not the same quantity as
  ``k`` identified from a real meter, and the result says which one it is.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

__all__ = ["HistorySeries", "async_read_states"]

_LOGGER = logging.getLogger(__name__)


class HistorySeries:
    """One entity's past as ``[(timestamp, value|None)]`` at a fixed step."""

    def __init__(self, points: Sequence[tuple[datetime, float | None]], step_minutes: int) -> None:
        self.points = list(points)
        self.step_minutes = step_minutes

    def __len__(self) -> int:
        return len(self.points)

    @property
    def values(self) -> list[float | None]:
        """Just the values, in order."""
        return [value for _moment, value in self.points]

    @property
    def coverage(self) -> float:
        """Share of steps that carry a value."""
        if not self.points:
            return 0.0
        present = sum(1 for _moment, value in self.points if value is not None)
        return present / len(self.points)


def _numeric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN guard without numpy


def async_read_states(
    hass: HomeAssistant,
    entity_ids: Sequence[str],
    start: datetime,
    end: datetime,
    step_minutes: int,
) -> dict[str, HistorySeries]:
    """Fetch the significant states of several entities, synchronously.

    Call it through the recorder's executor (``get_instance(hass).async_add_executor_job``):
    the generic one works but HA warns that database access anywhere else slows the
    instance down.
    """
    # Import the submodule itself: `from homeassistant.components.recorder import
    # history` can bind None, because that attribute is populated lazily by the
    # recorder's own setup.
    import homeassistant.components.recorder.history as recorder_history

    step = timedelta(minutes=step_minutes)
    # The grid must land on step boundaries: hourly statistics sit exactly on :00,
    # and a grid starting "now" (14:37, 15:37, …) never matches one of them —
    # measured as "0 heures communes entre l'intérieur et l'extérieur".
    start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    steps = int((end - start) / step) + 1

    try:
        recorded = recorder_history.get_significant_states(
            hass,
            start,
            end,
            list(entity_ids),
            None,
            True,
            False,
            False,
            False,
        )
    except Exception as err:  # pragma: no cover - recorder unavailable
        _LOGGER.warning("Thermal Model: recorder history unavailable: %s", err)
        return {}

    result: dict[str, HistorySeries] = {}
    for entity_id in entity_ids:
        states = recorded.get(entity_id) or []
        points: list[tuple[datetime, float | None]] = []
        index = 0
        last_value: float | None = None
        for step_index in range(steps):
            moment = start + step * step_index
            # carry the last known value forward (a state is held until it changes)
            while index < len(states):
                changed = states[index].last_updated
                if isinstance(changed, str):
                    # minimal/compressed history rows carry strings, the recorder's
                    # own State objects carry datetimes
                    changed = dt_util.parse_datetime(changed)
                if changed is None:
                    index += 1
                    continue
                if changed > moment:
                    break
                last_value = _numeric(states[index].state)
                index += 1
            points.append((moment, last_value))
        result[entity_id] = HistorySeries(points, step_minutes)
    return result


async def async_collect(
    hass: HomeAssistant,
    entity_ids: Sequence[str],
    days: int,
    step_minutes: int,
) -> dict[str, HistorySeries]:
    """Async wrapper: read the recorder in its own executor."""
    from homeassistant.components.recorder import get_instance

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    # Align the grid on step boundaries: statistics hours sit exactly on :00, and a
    # grid starting "now" (14:37, 15:37, …) never matches one of them — measured as
    # "0 heures communes entre l'intérieur et l'extérieur" on a real installation.
    end = end.replace(minute=0, second=0, microsecond=0)
    start = start.replace(minute=0, second=0, microsecond=0)
    try:
        return await get_instance(hass).async_add_executor_job(
            async_read_states, hass, list(entity_ids), start, end, step_minutes
        )
    except Exception as err:  # recorder not set up, database locked, …
        _LOGGER.debug("Thermal Model: recorder history unavailable: %s", err)
        return {}
