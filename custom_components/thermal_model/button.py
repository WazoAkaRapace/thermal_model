"""Ask the integration to re-identify the model now.

The coordinator identifies at startup and once a day. This button exists for the
case where the user has just changed a sensor, freed the recorder or edited the
window: they should not have to restart Home Assistant to see the model catch up.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import ThermalModelRuntimeData
from .coordinator import ThermalModelCoordinator
from .entity import ThermalModelEntity

__all__ = ["async_setup_entry"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any
) -> None:
    """Set up the identification button."""
    runtime: ThermalModelRuntimeData = entry.runtime_data
    coordinator: ThermalModelCoordinator = runtime.coordinator  # type: ignore[assignment]
    async_add_entities([IdentifyButton(coordinator, entry, runtime)])


class IdentifyButton(ThermalModelEntity, ButtonEntity):
    """Recompute the model parameters from the recorded history."""

    _attr_icon = "mdi:calculator-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
    ) -> None:
        super().__init__(coordinator, entry, runtime, key="identify", name="Recalculate model")

    async def async_press(self) -> None:
        """Run an identification cycle."""
        _LOGGER.debug("Thermal Model: identification requested from the button")
        await self.coordinator.async_identify()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Publish what the last identification produced."""
        outcome = self.runtime.identification
        if outcome is None:
            return {"identifiability": "not_run"}
        return outcome.as_attributes()

    @property
    def available(self) -> bool:
        """The button works even when the forecast entities are unavailable."""
        return True
