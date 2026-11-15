"""Pick the power meter that feeds the heating.

Automatic detection is a *guess the data supports*, not a certainty: a house publishes
a dozen power meters and the heat pump is not always the one that varies most or even
the one that follows the indoor-outdoor gap best (measured on a real installation: the
grid meter tracked the gap better than the heat pump's own meter, because the pump
modulates instead of switching).

So the choice is exposed as a list the user can correct in one click, and the result of
the identification always says which meter it used.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from . import ThermalModelRuntimeData
from .coordinator import ThermalModelCoordinator
from .entity import ThermalModelEntity

__all__ = ["async_setup_entry"]

_LOGGER = logging.getLogger(__name__)

AUTO = "auto"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any
) -> None:
    """Set up the power-meter selector."""
    runtime: ThermalModelRuntimeData = entry.runtime_data
    coordinator: ThermalModelCoordinator = runtime.coordinator  # type: ignore[assignment]
    async_add_entities([HeatingPowerSelect(coordinator, entry, runtime)])


class HeatingPowerSelect(ThermalModelEntity, SelectEntity, RestoreEntity):
    """Which power sensor represents the heating."""

    _attr_icon = "mdi:heat-pump"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
    ) -> None:
        super().__init__(
            coordinator, entry, runtime, key="heating_power", name="Heating power meter"
        )
        self._attr_options = [AUTO]

    async def async_added_to_hass(self) -> None:
        """Restore the user's choice; 'auto' means let the identification decide."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", "", AUTO):
            self.runtime.selected_power_sensor = last.state

    @property
    def options(self) -> list[str]:
        """Auto, then the meters the last identification considered."""
        discovered = [
            entity_id
            for entity_id in (self.runtime.power_options or [])
            if entity_id in self.hass.states.async_entity_ids()
        ]
        return [AUTO, *discovered]

    @property
    def current_option(self) -> str:
        """The stored choice, or auto."""
        choice = self.runtime.selected_power_sensor or AUTO
        return choice if choice in self.options else AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the last identification actually used."""
        identification = self.runtime.identification
        return {
            "used_by_identification": getattr(identification, "heating_power_sensor", None),
            "candidates": list(self.runtime.power_options or []),
        }

    async def async_select_option(self, option: str) -> None:
        """Store the choice and re-identify with it."""
        self.runtime.selected_power_sensor = None if option == AUTO else option
        self.async_write_ha_state()
        _LOGGER.info("Thermal Model: heating power meter set to %s", option)
        await self.coordinator.async_identify()
