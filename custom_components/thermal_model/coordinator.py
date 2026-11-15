"""Coordinator: fetch weather, run every enabled scenario, archive the runs.

The dashboard needs two things this class produces:

- the **current** trajectories, over the next ``horizon`` hours;
- the **previous** trajectories, kept in SQLite, so the chart can show what the
  model predicted 6, 12 or 24 hours ago next to what actually happened.

A missing indoor temperature is a hard stop (there is nothing to start from),
but a failed weather call is not: the previous runs stay on screen, flagged
stale, instead of the entities blinking to ``unavailable`` every time the API
hiccups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import ModelSettings, SourceBinding, ThermalModelRuntimeData
from .const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_WEATHER_MODELS,
    DEFAULT_WEATHER_MODEL,
    DOMAIN_KEY,
    FORECAST_REFRESH_MINUTES,
    RUNS_KEPT_PER_DAY,
    RUNS_RETENTION_DAYS,
    ModelScenario,
)
from .core.forecast import ForecastRun, WeatherStep, build_run
from .core.simulate import ThermalParams
from .db import Database, StoredRun
from .sources import WeatherError, fetch_weather

__all__ = ["ForecastBundle", "ThermalModelCoordinator", "Tunable"]

_LOGGER = logging.getLogger(__name__)

# Number keys shared with the number platform (single source of truth for names)
Tunable = str
KEY_A = "a"
KEY_C_S = "c_s"
KEY_D = "d"
KEY_K = "k"
KEY_SETPOINT = "setpoint"
KEY_CAPACITY = "capacity"
KEY_HORIZON = "horizon"


@dataclass(frozen=True, kw_only=True)
class ForecastBundle:
    """Everything the entities publish, computed once per cycle."""

    issued_at: datetime
    runs: dict[str, ForecastRun]
    weather: tuple[WeatherStep, ...]
    params: ThermalParams
    params_source: str
    indoor_temperature: float | None
    outdoor_temperature: float | None
    history: dict[str, tuple[StoredRun, ...]]
    stale: bool = False
    error: str | None = None

    def run(self, scenario: str) -> ForecastRun | None:
        """Trajectory for one scenario, if it was computed."""
        return self.runs.get(scenario)


class ThermalModelCoordinator(DataUpdateCoordinator[ForecastBundle]):
    """Refresh loop: weather -> scenarios -> archive."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
        database: Database,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN_KEY}_{entry.entry_id}",
            update_interval=timedelta(minutes=FORECAST_REFRESH_MINUTES),
        )
        self.entry = entry
        self.runtime: ThermalModelRuntimeData = runtime
        self.database = database
        self._last_bundle: ForecastBundle | None = None

    # -- helpers ---------------------------------------------------------
    @property
    def sources(self) -> SourceBinding:
        """The bound entities."""
        return self.runtime.sources

    @property
    def settings(self) -> ModelSettings:
        """Identification and storage settings."""
        return self.runtime.settings

    def _tunable(self, key: Tunable) -> float:
        return float(self.runtime.tunables[key])

    def _params(self) -> ThermalParams:
        """Current parameter set, from the tunable numbers."""
        gain = self._tunable(KEY_K)
        return ThermalParams(
            a=self._tunable(KEY_A),
            c_s=self._tunable(KEY_C_S),
            d=self._tunable(KEY_D),
            k=gain if gain > 0 else None,
        )

    def _state_float(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _weather_model(self) -> str:
        models = self.entry.options.get(
            CONF_WEATHER_MODELS, self.entry.data.get(CONF_WEATHER_MODELS) or []
        )
        return str(models[0]) if models else DEFAULT_WEATHER_MODEL

    def _coordinates(self) -> tuple[float, float]:
        latitude = self.entry.options.get(
            CONF_LATITUDE, self.entry.data.get(CONF_LATITUDE, self.hass.config.latitude)
        )
        longitude = self.entry.options.get(
            CONF_LONGITUDE, self.entry.data.get(CONF_LONGITUDE, self.hass.config.longitude)
        )
        return float(latitude), float(longitude)

    # -- main loop -------------------------------------------------------
    async def _async_update_data(self) -> ForecastBundle:
        indoor = self._state_float(self.sources.indoor_sensor)
        if indoor is None:
            raise UpdateFailed(f"{self.sources.indoor_sensor} has no usable temperature")

        outdoor = self._state_float(self.sources.outdoor_sensor)
        latitude, longitude = self._coordinates()
        horizon = int(self._tunable(KEY_HORIZON))
        now = datetime.now(UTC)

        try:
            weather = await fetch_weather(
                async_get_clientsession(self.hass),
                latitude=latitude,
                longitude=longitude,
                hours=horizon,
                model=self._weather_model(),
                now=now,
            )
        except WeatherError as err:
            return self._stale_bundle(indoor, outdoor, str(err))

        params = self._params()
        runs: dict[str, ForecastRun] = {}
        for scenario in self.runtime.scenarios:
            try:
                runs[str(scenario)] = build_run(
                    params=params,
                    weather=weather,
                    start_temperature=indoor,
                    scenario=scenario,
                    issued_at=now,
                    source=self._weather_model(),
                    dt_hours=1.0,
                    setpoint=self._tunable(KEY_SETPOINT),
                    capacity_kw=self._tunable(KEY_CAPACITY),
                )
            except ValueError as err:  # pragma: no cover - guarded upstream
                _LOGGER.warning("Thermal Model: scenario %s failed: %s", scenario, err)

        for run in runs.values():
            await self.hass.async_add_executor_job(self.database.record_run, run)
        await self.hass.async_add_executor_job(
            partial(
                self.database.prune,
                keep_days=RUNS_RETENTION_DAYS,
                keep_per_day=RUNS_KEPT_PER_DAY,
            )
        )

        bundle = ForecastBundle(
            issued_at=now,
            runs=runs,
            weather=tuple(weather),
            params=params,
            params_source=self.runtime.params_source,
            indoor_temperature=indoor,
            outdoor_temperature=outdoor,
            history=await self._history(runs),
        )
        self._last_bundle = bundle
        return bundle

    async def _history(self, runs: dict[str, ForecastRun]) -> dict[str, tuple[StoredRun, ...]]:
        """Older runs for the 'forecast issued ago' comparison, per scenario."""
        history: dict[str, tuple[StoredRun, ...]] = {}
        for name in runs:
            scenario = next(s for s in self.runtime.scenarios if str(s) == name)
            stored = await self.hass.async_add_executor_job(
                _recent_runs_for, self.database, scenario
            )
            history[name] = stored
        return history

    def _stale_bundle(self, indoor: float, outdoor: float | None, error: str) -> ForecastBundle:
        """Keep the last trajectories on screen when only the weather is missing."""
        previous = self._last_bundle
        if previous is None:
            raise UpdateFailed(error)
        _LOGGER.warning("Thermal Model: weather unavailable (%s), keeping previous runs", error)
        return ForecastBundle(
            issued_at=previous.issued_at,
            runs=previous.runs,
            weather=previous.weather,
            params=previous.params,
            params_source=previous.params_source,
            indoor_temperature=indoor,
            outdoor_temperature=outdoor,
            history=previous.history,
            stale=True,
            error=error,
        )


def _recent_runs_for(
    database: Database, scenario: ModelScenario, limit: int = 4
) -> tuple[StoredRun, ...]:
    """Executor-side read (SQLite must stay out of the event loop)."""
    return tuple(database.recent_runs(limit=limit, scenario=scenario))
