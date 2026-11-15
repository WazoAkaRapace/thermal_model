"""The Thermal Model integration.

Generic indoor thermal model identification and indoor temperature forecasting:
three entitities in (heating entity, indoor sensor, outdoor sensor), a fitted
1R1C model and simulated indoor temperature trajectories out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DT_MINUTES,
    CONF_DT_STORAGE_MINUTES,
    CONF_HEATING_ENTITY,
    CONF_HISTORY_DAYS,
    CONF_INDOOR_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_RETENTION_DAYS,
    CONF_SMOOTH_HOURS,
    CONF_TAU_GRACE_MINUTES,
    DEFAULT_DT_MINUTES,
    DEFAULT_DT_STORAGE_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SMOOTH_HOURS,
    DEFAULT_TAU_GRACE_MINUTES,
    DOMAIN_KEY,
    PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SourceBinding:
    """The entities the model is built from."""

    heating_entity: str
    indoor_sensor: str
    outdoor_sensor: str


@dataclass(frozen=True, kw_only=True)
class ModelSettings:
    """Identification and storage settings (tunable at runtime later on)."""

    history_days: int = DEFAULT_HISTORY_DAYS
    dt_minutes: int = DEFAULT_DT_MINUTES
    dt_storage_minutes: int = DEFAULT_DT_STORAGE_MINUTES
    smooth_hours: float = DEFAULT_SMOOTH_HOURS
    tau_grace_minutes: int = DEFAULT_TAU_GRACE_MINUTES
    retention_days: int = DEFAULT_RETENTION_DAYS


@dataclass
class ThermalModelRuntimeData:
    """Runtime data attached to the config entry."""

    sources: SourceBinding
    settings: ModelSettings
    missing_entities: set[str] = field(default_factory=set)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Thermal Model from a config entry."""
    data = entry.data
    options = entry.options

    runtime = ThermalModelRuntimeData(
        sources=SourceBinding(
            heating_entity=data[CONF_HEATING_ENTITY],
            indoor_sensor=data[CONF_INDOOR_SENSOR],
            outdoor_sensor=data[CONF_OUTDOOR_SENSOR],
        ),
        settings=ModelSettings(
            history_days=int(
                options.get(CONF_HISTORY_DAYS, data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
            ),
            dt_minutes=int(
                options.get(CONF_DT_MINUTES, data.get(CONF_DT_MINUTES, DEFAULT_DT_MINUTES))
            ),
            dt_storage_minutes=int(
                options.get(
                    CONF_DT_STORAGE_MINUTES,
                    data.get(CONF_DT_STORAGE_MINUTES, DEFAULT_DT_STORAGE_MINUTES),
                )
            ),
            smooth_hours=float(
                options.get(CONF_SMOOTH_HOURS, data.get(CONF_SMOOTH_HOURS, DEFAULT_SMOOTH_HOURS))
            ),
            tau_grace_minutes=int(
                options.get(
                    CONF_TAU_GRACE_MINUTES,
                    data.get(CONF_TAU_GRACE_MINUTES, DEFAULT_TAU_GRACE_MINUTES),
                )
            ),
            retention_days=int(
                options.get(
                    CONF_RETENTION_DAYS, data.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
                )
            ),
        ),
    )

    runtime.missing_entities = {
        entity_id
        for entity_id in (
            runtime.sources.heating_entity,
            runtime.sources.indoor_sensor,
            runtime.sources.outdoor_sensor,
        )
        if hass.states.get(entity_id) is None
    }
    if runtime.missing_entities:
        # Missing entities are normal right after a restart of the providing
        # integration: never fail setup, publish the fact instead.
        _LOGGER.warning(
            "Thermal Model: %s not available yet, model computation will wait for them",
            ", ".join(sorted(runtime.missing_entities)),
        )

    entry.runtime_data = runtime

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not PLATFORMS:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


__all__ = [
    "DOMAIN_KEY",
    "ModelSettings",
    "SourceBinding",
    "ThermalModelRuntimeData",
    "async_setup_entry",
    "async_unload_entry",
]
