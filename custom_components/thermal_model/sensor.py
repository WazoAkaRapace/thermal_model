"""Sensors: the measured indoor temperature and one trajectory per scenario.

Two data shapes, deliberately different:

- the **measured** series is a ordinary sensor mirroring the bound thermometer,
  so Home Assistant's recorder gives the chart its history for free;
- a **forecast** sensor publishes the future trajectory in its attributes (times
  + values), plus the runs issued earlier for the same scenario, so the dashboard
  can draw "what I predicted 6 h ago" against what was actually measured.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from . import ThermalModelRuntimeData
from .const import DOMAIN_KEY, ModelScenario
from .coordinator import ThermalModelCoordinator
from .entity import ThermalModelEntity

__all__ = ["MeasuredTemperatureSensor", "ScenarioForecastSensor", "async_setup_entry"]

_LOGGER = logging.getLogger(__name__)

# How many archived runs the chart gets per scenario (6 h, 12 h, 24 h ago...)
ARCHIVED_RUNS: int = 3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    """Set up the sensors of one config entry."""
    runtime: ThermalModelRuntimeData = entry.runtime_data
    coordinator: ThermalModelCoordinator = runtime.coordinator  # type: ignore[assignment]

    entities: list[SensorEntity] = [
        MeasuredTemperatureSensor(coordinator, entry, runtime),
    ]
    for scenario in runtime.scenarios:
        entities.append(ScenarioForecastSensor(coordinator, entry, runtime, scenario))

    async_add_entities(entities)


class MeasuredTemperatureSensor(SensorEntity):
    """Mirror of the bound indoor thermometer.

    It exists so the dashboard has one stable entity to plot: the user's own
    sensor may be renamed, and the forecast is only meaningful next to the
    temperature it was started from.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
    ) -> None:
        self.coordinator = coordinator
        self.runtime = runtime
        self._source = runtime.sources.indoor_sensor
        self._attr_name = "Indoor temperature"
        self._attr_unique_id = f"thermal_model_{entry.entry_id}_indoor_temperature"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN_KEY, entry.entry_id)},
            name=entry.title,
            manufacturer="Thermal Model",
            model="1R1C grey-box model",
        )

    async def async_added_to_hass(self) -> None:
        """Follow the source sensor for live updates."""
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._source], self._handle_source_event)
        )

    @callback
    def _handle_source_event(self, event: Event[Any]) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self._source)
        return state is not None and state.state not in ("unknown", "unavailable", "")

    @property
    def native_value(self) -> float | None:
        state = self.hass.states.get(self._source)
        if state is None:
            return None
        try:
            return round(float(state.state), 2)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say where the number comes from."""
        return {
            "source_entity": self._source,
            "description": "Measured indoor temperature, mirrored for the dashboard",
        }


class ScenarioForecastSensor(ThermalModelEntity, SensorEntity):
    """Predicted indoor temperature for one scenario."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    _LABELS: ClassVar[dict[ModelScenario, str]] = {
        ModelScenario.FREE_FLOAT: "Forecast free float",
        ModelScenario.THERMOSTAT: "Forecast thermostat",
        ModelScenario.OBSERVED_DUTY: "Forecast observed duty",
    }

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
        scenario: ModelScenario,
    ) -> None:
        super().__init__(
            coordinator,
            entry,
            runtime,
            key=f"forecast_{scenario}",
            name=self._LABELS.get(scenario, f"Forecast {scenario}"),
        )
        self.scenario = scenario

    @property
    def run(self) -> Any | None:
        """The current trajectory for this scenario."""
        bundle = self.bundle
        return bundle.run(str(self.scenario)) if bundle else None

    @property
    def native_value(self) -> float | None:
        """Temperature expected at the next hour (values[1]; values[0] is now)."""
        run = self.run
        if run is None or len(run.values) < 2:
            return None
        return round(run.values[1], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The trajectory itself, plus the earlier runs and the weather it used."""
        bundle = self.bundle
        run = self.run
        if bundle is None or run is None:
            return {"scenario": str(self.scenario)}

        archived = bundle.history.get(str(self.scenario), ())
        return {
            "scenario": str(self.scenario),
            "issued_at": run.issued_at.isoformat(),
            "weather_model": run.source,
            "horizon_hours": len(run.values) - 1,
            "duration_hours": len(run.values) - 1,
            "params_digest": run.params_digest,
            "params_source": bundle.params_source,
            "indoor_temperature_now": bundle.indoor_temperature,
            "outdoor_temperature_now": bundle.outdoor_temperature,
            "minimum": round(run.minimum, 2),
            "minimum_at": run.minimum_at.isoformat(),
            "stale": bundle.stale,
            "error": bundle.error,
            # Trajectory, ready for apexcharts data_generator
            "times": [moment.isoformat() for moment in run.times],
            "values": [round(value, 2) for value in run.values],
            # Weather used by this run
            "weather_times": [step.time.isoformat() for step in bundle.weather],
            "weather_outdoor": [round(step.text, 1) for step in bundle.weather],
            # Runs issued earlier: the 'forecast vs reality' comparison
            "archived_runs": [
                {
                    "issued_at": stored.issued_at.isoformat(),
                    "age_hours": round(
                        (run.issued_at - stored.issued_at).total_seconds() / 3600.0, 1
                    ),
                    "times": [moment.isoformat() for moment in stored.times],
                    "values": [round(value, 2) for value in stored.values],
                }
                for stored in archived[:ARCHIVED_RUNS]
            ],
        }
