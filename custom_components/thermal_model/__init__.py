"""The Thermal Model integration.

Generic indoor thermal model identification and indoor temperature forecasting:
three entities in (heating entity, indoor sensor, outdoor sensor), a fitted
1R1C model and simulated indoor temperature trajectories out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CAPACITY,
    CONF_DT_MINUTES,
    CONF_DT_STORAGE_MINUTES,
    CONF_HEATING_ENTITY,
    CONF_HISTORY_DAYS,
    CONF_HORIZON_HOURS,
    CONF_INDOOR_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_RETENTION_DAYS,
    CONF_SCENARIO_SETPOINT,
    CONF_SCENARIOS,
    CONF_SMOOTH_HOURS,
    CONF_TAU_GRACE_MINUTES,
    DEFAULT_CAPACITY,
    DEFAULT_DT_MINUTES,
    DEFAULT_DT_STORAGE_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_PARAM_A,
    DEFAULT_PARAM_C_S,
    DEFAULT_PARAM_D,
    DEFAULT_PARAM_K,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCENARIO_SETPOINT,
    DEFAULT_SCENARIOS,
    DEFAULT_SMOOTH_HOURS,
    DEFAULT_TAU_GRACE_MINUTES,
    DOMAIN_KEY,
    PLATFORMS,
    STORAGE_SUBDIR,
    ModelScenario,
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
    slug: str = "thermal_model"
    panel_title: str = "Thermal Model"
    missing_entities: set[str] = field(default_factory=set)
    # Runtime-tunable numbers: the number platform owns their persistence
    # (RestoreEntity), the coordinator only reads them.
    tunables: dict[str, float] = field(default_factory=dict)
    params_source: str = "manual_default"
    options: dict[str, Any] = field(default_factory=dict)
    entry_data: dict[str, Any] = field(default_factory=dict)
    identification: Any = None  # IdentificationOutcome, imported lazily
    scenarios: tuple[ModelScenario, ...] = ()
    storage_path: Path | None = None


def build_tunables(data: dict, options: dict) -> dict[str, float]:
    """Default tunable values, overridable by the entry options."""
    return {
        "a": float(options.get("a", DEFAULT_PARAM_A)),
        "c_s": float(options.get("c_s", DEFAULT_PARAM_C_S)),
        "d": float(options.get("d", DEFAULT_PARAM_D)),
        "k": float(options.get("k", DEFAULT_PARAM_K)),
        "setpoint": float(
            options.get(
                CONF_SCENARIO_SETPOINT, data.get(CONF_SCENARIO_SETPOINT, DEFAULT_SCENARIO_SETPOINT)
            )
        ),
        "capacity": float(options.get(CONF_CAPACITY, data.get(CONF_CAPACITY, DEFAULT_CAPACITY))),
        "horizon": float(
            options.get(CONF_HORIZON_HOURS, data.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS))
        ),
    }


def build_scenarios(data: dict, options: dict) -> tuple[ModelScenario, ...]:
    """Enabled forecast scenarios, in a stable order."""
    raw = options.get(CONF_SCENARIOS, data.get(CONF_SCENARIOS, DEFAULT_SCENARIOS))
    scenarios: list[ModelScenario] = []
    for name in raw or DEFAULT_SCENARIOS:
        try:
            scenarios.append(ModelScenario(name))
        except ValueError:
            _LOGGER.warning("Thermal Model: unknown scenario %r ignored", name)
    return tuple(scenarios) or (ModelScenario.FREE_FLOAT,)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Thermal Model from a config entry."""
    from .coordinator import ThermalModelCoordinator
    from .db import Database

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
        slug=f"thermal_model_{entry.entry_id[:6]}",
        panel_title=entry.title or "Thermal Model",
        options=dict(options),
        entry_data=dict(data),
        tunables=build_tunables(data, options),
        scenarios=build_scenarios(data, options),
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

    storage_dir = Path(hass.config.path(".storage", STORAGE_SUBDIR))
    runtime.storage_path = storage_dir / f"{entry.entry_id}.db"
    database = Database(runtime.storage_path)

    coordinator = ThermalModelCoordinator(hass, entry, runtime, database)
    runtime.coordinator = coordinator  # type: ignore[assignment]

    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Not async_config_entry_first_refresh: a missing sensor must not put the
    # entry in retry (it can appear minutes later), so failures are published by
    # the entities instead of blocking setup.
    await coordinator.async_refresh()

    # Follow the indoor sensor between cycles: after a restart it is often still
    # unknown when the first refresh runs, and a moving temperature must not be
    # plotted against a stale starting point.
    coordinator.async_start()

    # Identify the model from the recorder history: without this the published
    # parameters are the reference house's, which makes the forecast decorative.
    hass.async_create_task(coordinator.async_identify(), "thermal_model identify")

    # The panel needs the entity ids to exist, so it is registered after the
    # platforms: an entity id is the registry's business, not a string we guess.
    from .panel import async_register_panel

    await async_register_panel(hass, entry.entry_id, runtime)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    runtime: ThermalModelRuntimeData | None = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)

    from .panel import async_unregister_panel

    await async_unregister_panel(hass)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and coordinator is not None:
        await coordinator.async_shutdown()
    return unloaded


__all__ = [
    "DOMAIN_KEY",
    "ModelSettings",
    "SourceBinding",
    "ThermalModelRuntimeData",
    "async_setup_entry",
    "async_unload_entry",
    "build_scenarios",
    "build_tunables",
]
