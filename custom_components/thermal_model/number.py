"""Model parameters as numbers, so the dashboard can show and tune them.

They are the only writable surface in this milestone: identification will fill
them automatically later, but a forecast needs four numbers and refusing to
start without them would make the dashboard empty on a fresh install. Each one
restores its last value across restarts, and changing one re-runs the forecast
immediately.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from . import ThermalModelRuntimeData
from .const import (
    DEFAULT_CAPACITY,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_PARAM_A,
    DEFAULT_PARAM_C_S,
    DEFAULT_PARAM_D,
    DEFAULT_PARAM_K,
    DEFAULT_SCENARIO_SETPOINT,
    MAX_CAPACITY,
    MAX_HORIZON_HOURS,
    MAX_PARAM_A,
    MAX_PARAM_C_S,
    MAX_PARAM_D,
    MAX_PARAM_K,
    MAX_SETPOINT,
    MIN_CAPACITY,
    MIN_HORIZON_HOURS,
    MIN_PARAM_A,
    MIN_PARAM_C_S,
    MIN_PARAM_D,
    MIN_PARAM_K,
    MIN_SETPOINT,
)
from .coordinator import (
    KEY_A,
    KEY_C_S,
    KEY_CAPACITY,
    KEY_D,
    KEY_HORIZON,
    KEY_K,
    KEY_SETPOINT,
    ThermalModelCoordinator,
)
from .entity import ThermalModelEntity

__all__ = ["async_setup_entry"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    """Set up the tunable numbers."""
    runtime: ThermalModelRuntimeData = entry.runtime_data
    coordinator: ThermalModelCoordinator = runtime.coordinator  # type: ignore[assignment]

    async_add_entities(
        [
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_A,
                name="Wall loss (a)",
                minimum=MIN_PARAM_A,
                maximum=MAX_PARAM_A,
                step=0.0001,
                default=DEFAULT_PARAM_A,
                category=EntityCategory.CONFIG,
                icon="mdi:home-thermometer-outline",
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_C_S,
                name="Solar gain (c_s)",
                minimum=MIN_PARAM_C_S,
                maximum=MAX_PARAM_C_S,
                step=0.0001,
                default=DEFAULT_PARAM_C_S,
                category=EntityCategory.CONFIG,
                icon="mdi:white-balance-sunny",
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_D,
                name="Internal gain (d)",
                minimum=MIN_PARAM_D,
                maximum=MAX_PARAM_D,
                step=0.01,
                default=DEFAULT_PARAM_D,
                category=EntityCategory.CONFIG,
                icon="mdi:account-group",
                unit=UnitOfTemperature.CELSIUS,
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_K,
                name="Heating gain (k)",
                minimum=MIN_PARAM_K,
                maximum=MAX_PARAM_K,
                step=0.01,
                default=DEFAULT_PARAM_K,
                category=EntityCategory.CONFIG,
                icon="mdi:radiator",
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_SETPOINT,
                name="Scenario setpoint",
                minimum=MIN_SETPOINT,
                maximum=MAX_SETPOINT,
                step=0.1,
                default=DEFAULT_SCENARIO_SETPOINT,
                category=EntityCategory.CONFIG,
                icon="mdi:thermostat",
                unit=UnitOfTemperature.CELSIUS,
                device_class=NumberDeviceClass.TEMPERATURE,
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_CAPACITY,
                name="Heating capacity",
                minimum=MIN_CAPACITY,
                maximum=MAX_CAPACITY,
                step=0.1,
                default=DEFAULT_CAPACITY,
                category=EntityCategory.CONFIG,
                icon="mdi:fire",
                unit=UnitOfPower.KILO_WATT,
                device_class=NumberDeviceClass.POWER,
            ),
            TunableNumber(
                coordinator,
                entry,
                runtime,
                key=KEY_HORIZON,
                name="Forecast horizon",
                minimum=MIN_HORIZON_HOURS,
                maximum=MAX_HORIZON_HOURS,
                step=1,
                default=DEFAULT_HORIZON_HOURS,
                category=EntityCategory.CONFIG,
                icon="mdi:clock-outline",
                unit=UnitOfTime.HOURS,
                mode=NumberMode.BOX,
            ),
        ]
    )


class TunableNumber(ThermalModelEntity, NumberEntity, RestoreEntity):
    """One runtime-tunable number, persisted by restore-on-start."""

    _attr_mode = NumberMode.AUTO

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
        *,
        key: str,
        name: str,
        minimum: float,
        maximum: float,
        step: float,
        default: float,
        category: EntityCategory,
        icon: str,
        unit: str | None = None,
        device_class: NumberDeviceClass | None = None,
        mode: NumberMode = NumberMode.AUTO,
    ) -> None:
        super().__init__(coordinator, entry, runtime, key=f"number_{key}", name=name)
        self.key = key
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_entity_category = category
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_mode = mode
        self._default = default

    @property
    def native_value(self) -> float:
        """Always read the live value: identification writes into the same dict."""
        return float(self.runtime.tunables.get(self.key, self._default))

    async def async_added_to_hass(self) -> None:
        """Restore the last value, and register ourselves as the truth."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", ""):
            try:
                # Do not overwrite an identified value with a stale restored state.
                if self.runtime.params_source != "identified" or self.key not in (
                    KEY_A,
                    KEY_C_S,
                    KEY_D,
                    KEY_K,
                ):
                    self.runtime.tunables[self.key] = float(last.state)
            except (TypeError, ValueError):
                _LOGGER.debug("Thermal Model: could not restore %s from %r", self.key, last.state)

    async def async_set_native_value(self, value: float) -> None:
        """Store, publish and recompute at once."""
        self.runtime.tunables[self.key] = float(value)
        if self.key in (KEY_A, KEY_C_S, KEY_D, KEY_K):
            self.runtime.params_source = "manual"
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain what the number does, in the dashboard's own language."""
        explanations = {
            KEY_A: "Fraction of the indoor-outdoor gap lost per hour (1/h)",
            KEY_C_S: "How much solar irradiance heats the house",
            KEY_D: "Internal gains (occupants, appliances), K/h",
            KEY_K: "Temperature rise per kWh delivered by the heating",
            KEY_SETPOINT: "Setpoint used by the thermostat scenario",
            KEY_CAPACITY: "Power delivered by the heating when on, kW",
            KEY_HORIZON: "How many hours ahead the scenarios are simulated",
        }
        identification = self.runtime.identification
        return {
            "explanation": explanations.get(self.key, ""),
            "params_source": self.runtime.params_source,
            "default": self._default,
            # Why this value: on the number itself, where the user looks when the
            # model says something surprising.
            "identification": (
                identification.as_attributes()
                if identification is not None
                else {"identifiability": "not_run"}
            ),
        }
