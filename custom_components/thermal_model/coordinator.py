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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
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
from .core.simulate import ModelDivergedError, ThermalParams
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

# A cycle that produced nothing (sensor not ready yet) must not wait 30 minutes,
# and a moving temperature must not leave a stale trajectory on screen.
RETRY_INTERVAL = timedelta(minutes=2)

# Lead times compared on the 'past forecast vs reality' chart. Runs are archived
# every cycle (30 min), so picking the three most recent would show three
# near-identical curves; these are the ages that actually test the model.
ARCHIVE_AGES_HOURS = (6.0, 12.0, 24.0)
ARCHIVE_TOLERANCE_HOURS = 2.5
# While the archive is young, keep runs at least this far apart so the comparison
# curves are actually distinguishable.
MIN_ARCHIVE_SEPARATION_SECONDS = 45 * 60
LIVE_REFRESH_COOLDOWN = timedelta(minutes=2)
# The model is re-identified daily: the parameters drift with the season, and one
# identification per day is cheap (a few seconds of recorder reads).
IDENTIFY_INTERVAL = timedelta(hours=24)
# Wait for the recorder and the weather API before the startup identification.
IDENTIFY_STARTUP_DELAY = timedelta(minutes=2)
IDENTIFY_RETRY_DELAY = timedelta(minutes=15)


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
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_identify: Callable[[], None] | None = None
        self._debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=LIVE_REFRESH_COOLDOWN.total_seconds(),
            immediate=False,
            function=self._async_debounced_refresh,
            background=True,
        )

    # -- lifecycle -------------------------------------------------------
    def async_start(self) -> None:
        """Follow the indoor sensor between scheduled cycles.

        Two problems this solves, both seen on a real installation:

        - at startup the indoor sensor is still `unknown` when the first refresh
          runs, so the entry published nothing and simply waited 30 minutes;
        - a house whose temperature moves should not show a trajectory computed
          from a value hours old.
        """
        if self._unsub_state is not None:  # pragma: no cover - defensive
            return
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.sources.indoor_sensor], self._handle_indoor_change
        )
        self._unsub_identify = async_track_time_interval(
            self.hass, self._handle_identify_tick, IDENTIFY_INTERVAL
        )

    async def async_shutdown(self) -> None:
        """Stop following the sensor when the entry unloads."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_identify is not None:
            self._unsub_identify()
            self._unsub_identify = None
        self._debouncer.async_cancel()
        await super().async_shutdown()

    @callback
    def _handle_identify_tick(self, _now: datetime) -> None:
        """Daily re-identification."""
        self.hass.async_create_task(self.async_identify(), "thermal_model identify (daily)")

    async def async_identify_if_incomplete(self) -> None:
        """Retry once when the first attempt ran on incomplete inputs.

        Right after a start the statistics may not cover the window yet; a second
        attempt a quarter of an hour later is cheap and usually enough.
        """
        from homeassistant.helpers.event import async_call_later

        outcome = self.runtime.identification
        if outcome is not None and outcome.ok and len(outcome.indoor_sources) >= 1:
            return

        async def _retry(_now: Any) -> None:
            await self.async_identify()

        async_call_later(self.hass, IDENTIFY_RETRY_DELAY, _retry)

    async def async_identify(self) -> None:
        """Fit the model on the recorded history and publish the parameters.

        This is what makes the forecasts mean something: without it the numbers are
        the reference house's defaults. When the data cannot support an
        identification, the reason is published instead of a plausible number.
        """
        from .identify_job import async_identify_model

        outcome = await async_identify_model(
            self.hass, self.runtime, days=self.settings.history_days
        )
        self.runtime.identification = outcome

        if outcome.ok and outcome.params is not None:
            params = outcome.params
            self.runtime.tunables[KEY_A] = params.a
            self.runtime.tunables[KEY_C_S] = params.c_s
            self.runtime.tunables[KEY_D] = params.d
            if params.k is not None:
                self.runtime.tunables[KEY_K] = params.k
            self.runtime.params_source = "identified"
            _LOGGER.info(
                "Thermal Model: identified a=%.5f c_s=%.5f d=%+.4f k=%s (%s, %s days, %s samples)",
                params.a,
                params.c_s,
                params.d,
                "none" if params.k is None else f"{params.k:.3f}",
                outcome.identifiability,
                outcome.days,
                outcome.samples,
            )
        else:
            _LOGGER.warning(
                "Thermal Model: identification impossible (%s), keeping %s parameters",
                outcome.message,
                self.runtime.params_source,
            )

        # Not async_request_refresh: that one is debounced by ~10 s, and the new
        # parameters must reach the entities as soon as they are known.
        await self.async_refresh()

    @callback
    def _handle_indoor_change(self, _event: Event[Any]) -> None:
        """Refresh shortly after the measured temperature settles."""
        self._debouncer.async_schedule_call()

    async def _async_debounced_refresh(self) -> None:
        await self.async_request_refresh()

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
            # The sensor is often still `unknown` seconds after a restart: retry
            # soon instead of leaving every entity unavailable for half an hour.
            self.update_interval = RETRY_INTERVAL
            raise UpdateFailed(f"{self.sources.indoor_sensor} has no usable temperature")

        self.update_interval = timedelta(minutes=FORECAST_REFRESH_MINUTES)
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
            except (ValueError, ModelDivergedError) as err:
                # A scenario that diverges must not take the whole update down: the
                # other scenario, the measurements and the parameters are still good,
                # and the previous run for this one stays visible until the next cycle.
                _LOGGER.warning("Thermal Model: scenario %s failed: %s", scenario, err)
                previous = self.runtime.last_runs.get(str(scenario))
                if previous is not None:
                    runs[str(scenario)] = previous

        self.runtime.last_runs = dict(runs)
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
        """Older runs for the 'forecast issued ago' comparison, per scenario.

        One run per useful lead time (see ARCHIVE_AGES_HOURS): the point is to put
        a trajectory announced hours ago next to what actually happened, not three
        runs issued half an hour apart that look identical.
        """
        history: dict[str, tuple[StoredRun, ...]] = {}
        for name in runs:
            scenario = next(s for s in self.runtime.scenarios if str(s) == name)
            stored = await self.hass.async_add_executor_job(_archived_for, self.database, scenario)
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


def _archived_for(database: Database, scenario: ModelScenario) -> tuple[StoredRun, ...]:
    """Executor-side read: one stored run per comparison lead time.

    Falls back to the most recent runs while the archive is still young, so a
    fresh installation is not left with an empty comparison panel.
    """
    picked = [
        run
        for run in (
            database.run_near(
                age_hours=age, scenario=scenario, tolerance_hours=ARCHIVE_TOLERANCE_HOURS
            )
            for age in ARCHIVE_AGES_HOURS
        )
        if run is not None
    ]
    if picked:
        return tuple(picked)
    return tuple(database.recent_runs(limit=len(ARCHIVE_AGES_HOURS), scenario=scenario))
