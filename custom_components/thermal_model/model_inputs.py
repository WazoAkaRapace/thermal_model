"""Build the identification inputs from long-term statistics.

Home Assistant keeps raw states for ~10 days but hourly statistics forever, and the
model needs weeks. So the inputs come from statistics, and the code has to answer
three questions on its own, because the user should not have to:

1. **Which indoor temperature?** The bound sensor when its statistics are long
   enough; otherwise the mean of the indoor thermometers the recorder already knows
   about. The selection is statistical (a room thermometer sits in a narrow band
   around the house temperature) plus a short blocklist of things that are *not* a
   room: a device's own hardware temperature, our own forecasts, anything named like
   a projection.
2. **Which heating power?** An explicitly configured sensor wins. Otherwise the
   candidates are tried in the identification itself and the one that fits the
   measured temperature best — with ``k`` actually identified — is kept. That is
   model selection, not name guessing, which matters because a house has a dozen
   power meters and only one of them is the heating.
3. **Which solar input?** Open-Meteo's past irradiance, unless the user bound a
   sensor.

Every choice is published in the outcome, so a surprising parameter can be traced
back to the series it came from.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .core.daily_k import (
    DailyKResult,
    DailyModelResult,
    fit_daily_model,
    identify_k_from_daily_energy,
)
from .core.identify import IdentificationError, IdentificationSettings, identify
from .sources import WeatherError, fetch_past_weather
from .sources.statistics import StatisticPoint, async_read_statistics

__all__ = ["ModelInputs", "build_inputs"]

_LOGGER = logging.getLogger(__name__)

# Not a room: device hardware temperatures, our own forecasts and the projections
# other integrations publish. Token match on the statistic id, which is derived from
# the entity id and therefore stable.
_NOT_A_ROOM = (
    "device_temperature",
    "prevision",
    "forecast",
    "tempo",
    "optimizer",
    "target",
    "consigne",
    "setpoint",
    "dewpoint",
    "rosee",
)

MIN_INDOOR_DAYS = 5
MAX_INDOOR_SENSORS = 16
MAX_POWER_CANDIDATES = 40
# Meters that rise when the house cools are heating candidates even when their
# variance is modest: a heat pump drawing 2.5 kW is outranked by any solar inverter.
MAX_INVERSE_CANDIDATES = 40
# A room temperature does not swing by more than a couple of degrees over a month,
# and it sits inside this band. A door contact's hardware sensor is at 27 °C in a
# 20 °C house; the outdoor probe is below zero. This filter is what removes them.
ROOM_BAND = (10.0, 30.0)
ROOM_MAX_SPREAD = 2.5


@dataclass
class ModelInputs:
    """The four series the identifier consumes, plus where they came from."""

    times: list[datetime] = field(default_factory=list)
    tin: list[float | None] = field(default_factory=list)
    tout: list[float] = field(default_factory=list)
    solar: list[float] = field(default_factory=list)
    heating_kw: list[float] = field(default_factory=list)
    indoor_sources: list[str] = field(default_factory=list)
    heating_power_sensor: str | None = None
    solar_source: str = "open_meteo_past"
    energy_sensor: str | None = None
    daily_k: DailyKResult | None = None
    daily_model: DailyModelResult | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[str]:
        """Why the inputs cannot be used at all."""
        return list(self.reasons)


def _points(series: dict[str, list[StatisticPoint]]) -> dict[str, dict[datetime, float]]:
    return {
        statistic_id: {point.moment: point.value for point in points}
        for statistic_id, points in series.items()
    }


async def _series_for_sensor(hass: HomeAssistant, sensor: str, days: int) -> dict[datetime, float]:
    """Hourly values for a sensor, from statistics or, failing that, from raw history.

    A sensor without ``state_class`` never gets long-term statistics — measured on a
    real installation: 949 states over 10 days, 0 statistics — and the identification
    must not ignore the sensor the user chose for that reason. Raw history only
    survives the recorder purge, but a week of the right sensor beats three weeks of
    the wrong series.
    """
    from .sources.recorder import async_collect

    series = await async_read_statistics(hass, [sensor], days)
    points = _points(series).get(sensor) or {}
    if points:
        return points

    collected = await async_collect(hass, [sensor], days, step_minutes=60)
    history = collected.get(sensor)
    if history is None:
        return {}
    return {moment: value for moment, value in history.points if value is not None}


def _mean_series(series: dict[str, dict[datetime, float]]) -> tuple[list[datetime], list[float]]:
    """Hours present in every series, with the mean across them."""
    if not series:
        return [], []
    common = set.intersection(*[set(points) for points in series.values()])
    hours = sorted(common)
    return hours, [
        statistics.fmean([points[hour] for points in series.values() if hour in points])
        for hour in hours
    ]


def _kw(values: dict[datetime, float], unit: str | None) -> dict[datetime, float]:
    """Normalise a power series to kW."""
    if unit and unit.strip().lower() in ("w", "watt", "watts"):
        return {moment: value / 1000.0 for moment, value in values.items()}
    return dict(values)


async def _solar_series(
    hass: HomeAssistant,
    hours: list[datetime],
    days: int,
    irradiance_sensor: str | None,
    latitude: float,
    longitude: float,
    facade_bearing: float = 180.0,
) -> tuple[list[float], str, str | None]:
    """Hourly irradiance for the given hours."""
    if irradiance_sensor:
        series = await async_read_statistics(hass, [irradiance_sensor], days)
        points = _points(series).get(irradiance_sensor) or {}
        if points:
            return (
                [points.get(hour, 0.0) for hour in hours],
                "sensor",
                None,
            )
    try:
        past = await fetch_past_weather(
            async_get_clientsession(hass),
            latitude=latitude,
            longitude=longitude,
            days=days,
            facade_bearing=facade_bearing,
        )
    except WeatherError as err:
        return [0.0] * len(hours), "unavailable", str(err)
    by_hour = {step.time: step.solar for step in past}
    return (
        [by_hour.get(hour.replace(minute=0, second=0, microsecond=0), 0.0) for hour in hours],
        "open_meteo_past",
        None,
    )


async def build_inputs(
    hass: HomeAssistant,
    *,
    indoor_sensor: str,
    outdoor_sensor: str,
    days: int,
    power_sensor: str | None = None,
    irradiance_sensor: str | None = None,
    latitude: float = 0.0,
    longitude: float = 0.0,
    facade_bearing: float = 180.0,
    settings: IdentificationSettings | None = None,
    runtime_tunables: dict[str, float] | None = None,
) -> tuple[ModelInputs, Any]:
    """Assemble the series from the sensors the user actually configured.

    Returns the inputs and the identification result when one was fitted for the
    winning power candidate (the caller re-runs it to keep this function honest about
    what it returned).
    """
    ident_settings = settings or IdentificationSettings(dt_hours=1.0, window_hours=1)
    inputs = ModelInputs()

    wanted = {indoor_sensor, outdoor_sensor}
    power_options: list[str] = []
    if power_sensor:
        power_options = [power_sensor]
        wanted.add(power_sensor)
    if irradiance_sensor:
        wanted.add(irradiance_sensor)

    # Phase 1: the bound sensors and the power candidates, in one read.
    probe_ids = set(wanted)
    first_pass = await async_read_statistics(hass, sorted(probe_ids), days)
    first_points = _points(first_pass)
    outdoor_points = first_points.get(outdoor_sensor) or {}
    if not outdoor_points:
        # the bound outdoor sensor may lack state_class too
        outdoor_points = await _series_for_sensor(hass, outdoor_sensor, days)
        if not outdoor_points:
            inputs.reasons.append(f"aucune statistique ni aucun historique pour {outdoor_sensor}")
            return inputs, None
        inputs.reasons.append(f"{outdoor_sensor}: pas de statistiques, historique brut utilisé")

    indoor_points = first_points.get(indoor_sensor) or {}
    if not indoor_points:
        raw_indoor = await _series_for_sensor(hass, indoor_sensor, days)
        if len(raw_indoor) >= 24 * MIN_INDOOR_DAYS:
            indoor_points = raw_indoor
            inputs.reasons.append(
                f"{indoor_sensor}: pas de statistiques (state_class manquant ?), "
                "historique brut utilisé"
            )
    indoor_sources: list[str] = []
    indoor_series: dict[str, dict[datetime, float]] = {}
    if len(indoor_points) >= 24 * MIN_INDOOR_DAYS:
        indoor_series[indoor_sensor] = indoor_points
        indoor_sources = [indoor_sensor]
    else:
        inputs.reasons.append(
            f"le capteur intérieur {indoor_sensor} n'a pas assez d'historique "
            f"(minimum {MIN_INDOOR_DAYS} jours)"
        )
        return inputs, None

    hours, tin = _mean_series(indoor_series)
    hours = [hour for hour in hours if hour in outdoor_points]
    if len(hours) < 24 * MIN_INDOOR_DAYS:
        inputs.reasons.append(
            f"seulement {len(hours)} heures communes entre l'intérieur et l'extérieur"
        )
        return inputs, None

    index = {hour: position for position, hour in enumerate(hours)}
    tin = [tin[index[hour]] for hour in hours]
    tout = [outdoor_points[hour] for hour in hours]

    solar, solar_source, solar_error = await _solar_series(
        hass,
        hours,
        days,
        irradiance_sensor,
        latitude,
        longitude,
        facade_bearing,
    )
    if solar_error:
        inputs.reasons.append(f"irradiance indisponible : {solar_error}")

    # Publish what is already known before the power phase: a refusal must still say
    # which indoor sources and which window it refused on.
    inputs.times = hours
    inputs.tin = tin
    inputs.tout = tout
    inputs.solar = solar
    inputs.indoor_sources = indoor_sources
    inputs.solar_source = solar_source

    if not power_options:
        # No meter was configured. The heating is simply unknown: guessing one
        # ("the most variable meter", "the one that follows the indoor-outdoor gap",
        # then "the one that fits best") was tried and measured on a real
        # installation — it picked a solar inverter, and a plausible-looking fit with
        # the wrong physics is worse than an honest refusal.
        inputs.reasons.append(
            "aucun compteur de puissance ou d'énergie de chauffe configuré : "
            "l'apport de chauffe ne peut pas être identifié"
        )
        return inputs, None

    # Phase 2: the configured meter is the heating input. If it is a power meter the
    # hourly fit runs on it; the kWh counter of the same device can still finish k
    # through the daily balance, which survives a modulating heat pump.
    best: tuple[float, str, Any, list[float], Any] | None = None
    best_without_k: tuple[float, str, Any, list[float]] | None = None
    failures: list[str] = []
    # One read for every candidate: a query per candidate would be dozens of database
    # round trips for a job that runs daily.
    every_series = await async_read_statistics(hass, power_options, days) if power_options else {}
    every_points = _points(every_series)
    energy_options: list[str] = []
    for candidate in power_options:
        candidate_state = hass.states.get(candidate)
        candidate_unit = (
            str(candidate_state.attributes.get("unit_of_measurement") or "")
            if candidate_state is not None
            else ""
        )
        if (
            candidate_state is not None
            and candidate_state.attributes.get("device_class") == "energy"
        ) or candidate_unit in ("kWh", "Wh"):
            energy_options.append(candidate)

    for candidate in [option for option in power_options if option not in energy_options]:
        points = every_points.get(candidate) or {}
        if len(points) < len(hours) * 0.6:
            failures.append(f"{candidate}: historique trop court")
            continue
        state = hass.states.get(candidate)
        unit = state.attributes.get("unit_of_measurement") if state is not None else None
        normalised = _kw(points, unit)
        values = [normalised.get(hour, 0.0) for hour in hours]
        try:
            result = identify(hours, tin, tout, solar, values, settings=ident_settings)
        except IdentificationError as err:
            failures.append(f"{candidate}: {err}")
            continue
        score = result.rmse_observed if result.rmse_observed is not None else float("inf")
        # The plausibility guard applies to what we guessed, never to what the user
        # chose: an explicitly selected meter is a decision, not a candidate.
        if result.params is not None and result.params.k is not None and power_sensor is None:
            capacity = float(runtime_tunables.get("capacity", 2.0)) if runtime_tunables else 2.0
            implied = result.params.k * capacity
            if not 0.05 <= implied <= 3.0:
                # k is the temperature rise at full power: a meter that implies 12 K/h
                # or 0.01 K/h is not measuring the heating, however well it fits.
                failures.append(
                    f"{candidate}: gain implicite {implied:.2f} K/h hors plage "
                    f"pour {capacity:.1f} kW"
                )
                continue
        if result.params is None:
            failures.append(f"{candidate}: aucun paramètre")
            continue
        if result.params.k is None:
            # The house coefficients can still be good — a and c_s are what the passive
            # windows give. Daily energy finishes k without needing free-float nights.
            failures.append(f"{candidate}: k non identifiable à l'heure")
            if best_without_k is None or score < best_without_k[0]:
                best_without_k = (score, candidate, result, values)
            continue
        if best is None or score < best[0]:
            best = (score, candidate, result, values, result)

    if best is None and best_without_k is not None:
        _score, candidate, result, values = best_without_k
        finished, energy_sensor, daily = await _finish_k_with_daily_energy(
            hass,
            candidate=candidate,
            hours=hours,
            tin=tin,
            tout=tout,
            solar=solar,
            days=days,
            params=result.params,
        )
        inputs.energy_sensor = energy_sensor
        inputs.daily_k = daily
        if finished is not None:
            inputs.heating_kw = values
            inputs.heating_power_sensor = candidate
            # `finished` is a ThermalParams: wrap it, the caller expects an
            # IdentificationResult (this exact omission crashed the button on the
            # real installation: "'ThermalParams' object has no attribute 'params'")
            return inputs, replace(result, params=finished)
        if daily is not None and daily.message:
            inputs.reasons.append(daily.message)
        inputs.reasons.extend(failures[:3])
        return inputs, None

    if best is None and energy_options and best_without_k is None:
        # Only a kWh counter is available: fit the four coefficients on the daily
        # balance. The hourly fit cannot separate the wall loss from the heating when
        # the heating is never observed, the days can.
        energy_sensor = energy_options[0]
        series = await async_read_statistics(hass, [energy_sensor], days, prefer="change")
        points = _points(series).get(energy_sensor) or {}
        state = hass.states.get(energy_sensor)
        unit = str(state.attributes.get("unit_of_measurement") or "") if state else ""
        scale = 0.001 if unit == "Wh" else 1.0
        energy = [points.get(hour, 0.0) * scale for hour in hours]
        daily_model = fit_daily_model(hours, tin, tout, solar, energy, dt_hours=1.0)
        inputs.daily_model = daily_model
        inputs.energy_sensor = energy_sensor
        if daily_model.ok and daily_model.params is not None:
            inputs.heating_kw = [0.0] * len(hours)
            return inputs, None
        if daily_model.message:
            inputs.reasons.append(daily_model.message)
        return inputs, None

    if best is None:
        if power_options:
            inputs.reasons.append(
                "aucune puissance de chauffe exploitable parmi : " + ", ".join(power_options[:4])
            )
            inputs.reasons.extend(failures[:3])
        else:
            inputs.reasons.append("aucun capteur de puissance de chauffe trouvé")
        _LOGGER.debug("Thermal Model: power candidates rejected: %s", failures[:6])
        return inputs, None

    _score, chosen, result, values, _ = best
    inputs.heating_kw = values
    inputs.heating_power_sensor = chosen

    # A kWh counter on the same device is a measurement, and the daily balance is far
    # more robust than an hourly regression on a modulating heat pump: on the real
    # installation the hourly fit returned k = 5.96 K/kWh (a 2.4 kW pump raising the
    # whole house by 12 K/h: impossible) while the daily balance returned
    # 0.251 ± 0.025 over 20 days and 113 kWh. Prefer the measurement.
    finished, energy_sensor, daily = await _finish_k_with_daily_energy(
        hass,
        candidate=chosen,
        hours=hours,
        tin=tin,
        tout=tout,
        solar=solar,
        days=days,
        params=result.params,
    )
    inputs.energy_sensor = energy_sensor
    inputs.daily_k = daily
    if finished is not None:
        result = replace(result, params=finished) if hasattr(result, "params") else result
        return inputs, result
    return inputs, result


def _energy_sensor_for(hass: HomeAssistant, power_sensor: str) -> str | None:
    """The energy counter of the same device as the power meter.

    A heat pump almost never exposes its power as an entity, but its meter usually
    exposes energy in kWh on the same device — and kWh per day is exactly what the
    daily balance needs.
    """
    state = hass.states.get(power_sensor)
    if state is not None:
        unit = str(state.attributes.get("unit_of_measurement") or "")
        if state.attributes.get("device_class") == "energy" or unit in ("kWh", "Wh"):
            return power_sensor  # the user pointed at the kWh counter itself

    registry = er.async_get(hass)
    meter = registry.async_get(power_sensor)
    device_id = getattr(meter, "device_id", None)
    candidates: list[str] = []
    for entry in registry.entities.values():
        if entry.device_id != device_id or entry.domain != "sensor":
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        unit = str(state.attributes.get("unit_of_measurement") or "")
        if state.attributes.get("device_class") == "energy" or unit in ("kWh", "Wh"):
            candidates.append(entry.entity_id)
    if not candidates:
        return None

    # prefer kWh over Wh so the numbers stay readable, then alphabetical for determinism
    def _kwh_first(entity_id: str) -> tuple[bool, str]:
        state = hass.states.get(entity_id)
        unit = str(state.attributes.get("unit_of_measurement")) if state is not None else ""
        return (unit == "Wh", entity_id)  # kWh before Wh, then alphabetical

    candidates.sort(key=_kwh_first)
    return candidates[0]


async def _finish_k_with_daily_energy(
    hass: HomeAssistant,
    *,
    candidate: str,
    hours: list[datetime],
    tin: list[float],
    tout: list[float],
    solar: list[float],
    days: int,
    params: Any,
) -> tuple[Any, str | None, DailyKResult | None]:
    """Replace the missing ``k`` with the daily energy balance."""
    energy_sensor = _energy_sensor_for(hass, candidate)
    if energy_sensor is None:
        return None, None, None

    series = await async_read_statistics(hass, [energy_sensor], days, prefer="change")
    points = _points(series).get(energy_sensor) or {}
    if not points:
        return None, energy_sensor, None
    state = hass.states.get(energy_sensor)
    unit = str(state.attributes.get("unit_of_measurement") or "") if state is not None else ""
    scale = 0.001 if unit == "Wh" else 1.0
    energy = [points.get(hour, 0.0) * scale for hour in hours]

    daily = identify_k_from_daily_energy(hours, tin, tout, solar, energy, params)
    if not daily.ok or daily.k is None:
        return None, energy_sensor, daily
    finished = replace(
        params,
        k=daily.k,
        alpha=daily.alpha,
        k_reference_temperature=daily.reference_temperature,
    )
    _LOGGER.info(
        "Thermal Model: k = %.4f ± %.4f K/kWh from %s days of %s",
        daily.k,
        daily.stderr or 0.0,
        daily.days,
        energy_sensor,
    )
    return finished, energy_sensor, daily
