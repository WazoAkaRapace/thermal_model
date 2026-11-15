"""Shared entity plumbing: one device, stable unique ids, one coordinator."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ThermalModelRuntimeData
from .const import DOMAIN_KEY
from .coordinator import ForecastBundle, ThermalModelCoordinator

__all__ = ["ThermalModelEntity"]


class ThermalModelEntity(CoordinatorEntity[ThermalModelCoordinator]):
    """Base class: attaches every entity to the entry's device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ThermalModelCoordinator,
        entry: ConfigEntry,
        runtime: ThermalModelRuntimeData,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.runtime = runtime
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN_KEY}_{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN_KEY, entry.entry_id)},
            name=entry.title,
            manufacturer="Thermal Model",
            model="1R1C grey-box model",
            entry_type=None,
        )

    @property
    def bundle(self) -> ForecastBundle | None:
        """Last computed bundle, or None before the first successful refresh."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Available as soon as one cycle produced trajectories."""
        return self.bundle is not None and bool(self.bundle.runs)
