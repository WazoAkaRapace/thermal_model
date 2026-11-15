"""Identify the model from the installation's own history.

This is the job that makes the integration useful: read what Home Assistant kept,
build the four input series the identifier needs, and publish the result — or the
reason it could not be identified, which is a result too.

The inputs come from long-term statistics (see ``model_inputs``), so the window is
weeks rather than the recorder's ~10-day purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from . import ThermalModelRuntimeData
from .const import (
    CONF_HEATING_POWER_SENSOR,
    CONF_IRRADIANCE_SENSOR,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    Identifiability,
    ReasonCode,
)
from .core.identify import IdentificationSettings
from .core.simulate import ThermalParams
from .model_inputs import build_inputs

__all__ = ["IdentificationOutcome", "async_identify_model"]

_LOGGER = logging.getLogger(__name__)

MIN_DAYS = 5


@dataclass(frozen=True, kw_only=True)
class IdentificationOutcome:
    """What the identification produced, including what it refused."""

    params: ThermalParams | None = None
    identifiability: Identifiability = Identifiability.INSUFFICIENT_DATA
    reasons: tuple[ReasonCode, ...] = ()
    driving_spread: float | None = None
    rmse_observed: float | None = None
    rmse_free_float: float | None = None
    coverage: float = 0.0
    samples: int = 0
    days: int = 0
    k_source: str = "statistics"
    message: str | None = None
    identified_at: datetime | None = None
    indoor_sources: tuple[str, ...] = ()
    heating_power_sensor: str | None = None
    solar_source: str = "open_meteo_past"
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether a usable parameter set came out."""
        return self.params is not None

    def as_attributes(self) -> dict[str, Any]:
        """Flatten for the entities that publish it."""
        return {
            "identifiability": str(self.identifiability),
            "reasons": [str(reason) for reason in self.reasons],
            "driving_spread": (
                None if self.driving_spread is None else round(self.driving_spread, 4)
            ),
            "rmse_observed": (None if self.rmse_observed is None else round(self.rmse_observed, 3)),
            "rmse_free_float": (
                None if self.rmse_free_float is None else round(self.rmse_free_float, 3)
            ),
            "coverage": round(self.coverage, 3),
            "samples": self.samples,
            "window_days": self.days,
            "k_source": self.k_source,
            "indoor_sources": list(self.indoor_sources),
            "heating_power_sensor": self.heating_power_sensor,
            "solar_source": self.solar_source,
            "notes": list(self.notes),
            "identification_error": self.message,
            "identified_at": self.identified_at.isoformat() if self.identified_at else None,
        }


async def async_identify_model(
    hass: HomeAssistant,
    runtime: ThermalModelRuntimeData,
    *,
    days: int | None = None,
) -> IdentificationOutcome:
    """Read the statistics and fit the 1R1C parameters."""
    settings = runtime.settings
    days = max(MIN_DAYS, days or settings.history_days)
    options = runtime.options or {}
    entry_data = runtime.entry_data or {}

    ident_settings = IdentificationSettings(
        dt_hours=1.0,
        window_hours=1.0,
        smooth_hours=settings.smooth_hours,
        grace_hours=max(0.0, settings.tau_grace_minutes / 60.0),
    )

    latitude = float(
        options.get(CONF_LATITUDE, entry_data.get(CONF_LATITUDE, hass.config.latitude))
    )
    longitude = float(
        options.get(CONF_LONGITUDE, entry_data.get(CONF_LONGITUDE, hass.config.longitude))
    )

    inputs, result = await build_inputs(
        hass,
        indoor_sensor=runtime.sources.indoor_sensor,
        outdoor_sensor=runtime.sources.outdoor_sensor,
        heating_entity=runtime.sources.heating_entity,
        days=days,
        power_sensor=options.get(CONF_HEATING_POWER_SENSOR),
        irradiance_sensor=options.get(CONF_IRRADIANCE_SENSOR),
        latitude=latitude,
        longitude=longitude,
        settings=ident_settings,
    )

    notes = tuple(inputs.reasons)
    if result is None:
        return IdentificationOutcome(
            message="; ".join(notes) or "identification impossible",
            days=days,
            samples=len(inputs.times),
            indoor_sources=tuple(inputs.indoor_sources),
            heating_power_sensor=inputs.heating_power_sensor,
            solar_source=inputs.solar_source,
            notes=notes,
            identified_at=datetime.now(UTC),
        )

    identified_days = max(1, round(len(inputs.times) / 24))
    coverage = (
        sum(1 for value in inputs.tin if value is not None) / len(inputs.tin) if inputs.tin else 0.0
    )
    _LOGGER.info(
        "Thermal Model: identified over %s days with %s indoor source(s) and %s",
        identified_days,
        len(inputs.indoor_sources),
        inputs.heating_power_sensor,
    )
    return IdentificationOutcome(
        params=result.params,
        identifiability=result.identifiability,
        reasons=tuple(result.reasons),
        driving_spread=result.driving_spread,
        rmse_observed=result.rmse_observed,
        rmse_free_float=result.rmse_free_float,
        coverage=coverage,
        samples=len(inputs.times),
        days=identified_days,
        k_source="statistics",
        identified_at=datetime.now(UTC),
        indoor_sources=tuple(inputs.indoor_sources),
        heating_power_sensor=inputs.heating_power_sensor,
        solar_source=inputs.solar_source,
        notes=notes,
    )
