"""Identify the model from the installation's own history.

This is the job that makes the integration useful: read what Home Assistant kept,
build the four input series the identifier needs, and publish the result — or the
reason it could not be identified, which is a result too.

The inputs come from long-term statistics (see ``model_inputs``), so the window is
weeks rather than the recorder's ~10-day purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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
    energy_sensor: str | None = None
    k_stderr: float | None = None
    k_days: int = 0
    k_energy_kwh: float = 0.0
    k_alpha: float | None = None
    k_alpha_stderr: float | None = None
    k_reference_temperature: float | None = None
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
            "energy_sensor": self.energy_sensor,
            "k_standard_error": (None if self.k_stderr is None else round(self.k_stderr, 4)),
            "k_days": self.k_days,
            "k_energy_kwh": round(self.k_energy_kwh, 2),
            "k_alpha": (None if self.k_alpha is None else round(self.k_alpha, 4)),
            "k_alpha_stderr": (
                None if self.k_alpha_stderr is None else round(self.k_alpha_stderr, 4)
            ),
            "k_reference_temperature": self.k_reference_temperature,
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

    # The heating meter is a configuration decision, never a guess.
    chosen_by_user = options.get(CONF_HEATING_POWER_SENSOR) or entry_data.get(
        CONF_HEATING_POWER_SENSOR
    )

    inputs, result = await build_inputs(
        hass,
        indoor_sensor=runtime.sources.indoor_sensor,
        outdoor_sensor=runtime.sources.outdoor_sensor,
        days=days,
        power_sensor=chosen_by_user,
        irradiance_sensor=options.get(CONF_IRRADIANCE_SENSOR)
        or entry_data.get(CONF_IRRADIANCE_SENSOR),
        latitude=latitude,
        longitude=longitude,
        settings=ident_settings,
        runtime_tunables=runtime.tunables,
    )

    notes = tuple(inputs.reasons)
    if result is not None and getattr(result, "params", None) is not None:
        pass  # alpha note handled below via inputs.daily_k
    _daily = inputs.daily_k
    if _daily is not None and _daily.alpha_note and result is not None:
        notes = (*notes, _daily.alpha_note)

    daily_model = inputs.daily_model
    if daily_model is not None and daily_model.ok and daily_model.params is not None:
        # No power meter at all: the four coefficients come from the daily balance.
        _LOGGER.info(
            "Thermal Model: daily model a=%.5f c_s=%.5f d=%+.4f k=%.3f on %s days",
            daily_model.params.a,
            daily_model.params.c_s,
            daily_model.params.d,
            daily_model.params.k or 0.0,
            daily_model.days,
        )
        return IdentificationOutcome(
            params=daily_model.params,
            identifiability=Identifiability.IDENTIFIED,
            coverage=1.0,
            samples=len(inputs.times),
            days=daily_model.days,
            k_source="daily_model",
            energy_sensor=inputs.energy_sensor,
            k_stderr=daily_model.stderrs[3] if len(daily_model.stderrs) > 3 else None,
            k_days=daily_model.days,
            k_energy_kwh=daily_model.total_kwh,
            identified_at=datetime.now(UTC),
            indoor_sources=tuple(inputs.indoor_sources),
            solar_source=inputs.solar_source,
            notes=notes,
        )

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

    daily = inputs.daily_k
    k_source = "daily_energy" if (daily is not None and daily.ok) else "hourly"
    # A k that implies an impossible rise at full power is not a parameter, it is a
    # bad fit: publishing it makes the thermostat scenario diverge (measured: 5.25
    # K/kWh × 2 kW = 10.5 K/h). Refuse it instead of publishing a broken model.
    capacity = float(runtime.tunables.get("capacity", 2.0) or 2.0)
    if result.params is not None and result.params.k is not None:
        implied = result.params.k * capacity
        if not 0.02 <= implied <= 3.0:
            notes = (
                *notes,
                f"k = {result.params.k:.3f} K/kWh impliquerait {implied:.1f} K/h à "
                f"{capacity:.1f} kW : rejeté",
            )
            result = replace(result, params=replace(result.params, k=None))

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
    # The hourly fit reports K_NOT_IDENTIFIED when its own regression could not
    # separate k; once the daily balance has provided it, keeping that reason makes
    # the entities say "k_not_identified" over a published k. Confusing on a real
    # installation — drop it.
    reasons = tuple(result.reasons)
    if inputs.daily_k is not None and inputs.daily_k.ok and ReasonCode.K_NOT_IDENTIFIED in reasons:
        reasons = tuple(code for code in reasons if code != ReasonCode.K_NOT_IDENTIFIED)

    return IdentificationOutcome(
        params=result.params,
        identifiability=result.identifiability,
        reasons=reasons,
        driving_spread=result.driving_spread,
        rmse_observed=result.rmse_observed,
        rmse_free_float=result.rmse_free_float,
        coverage=coverage,
        samples=len(inputs.times),
        days=identified_days,
        k_source=k_source,
        energy_sensor=inputs.energy_sensor,
        k_stderr=daily.stderr if daily is not None else None,
        k_days=daily.days if daily is not None else 0,
        k_energy_kwh=daily.total_kwh if daily is not None else 0.0,
        k_alpha=daily.alpha if daily is not None else None,
        k_alpha_stderr=daily.alpha_stderr if daily is not None else None,
        k_reference_temperature=(daily.reference_temperature if daily is not None else None),
        identified_at=datetime.now(UTC),
        indoor_sources=tuple(inputs.indoor_sources),
        heating_power_sensor=inputs.heating_power_sensor,
        solar_source=inputs.solar_source,
        notes=notes,
    )
