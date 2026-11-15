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

from .core.daily_k import DailyKResult, identify_k_from_daily_energy
from .core.identify import IdentificationError, IdentificationSettings, identify
from .sources import WeatherError, fetch_past_weather
from .sources.statistics import StatisticPoint, async_read_statistics

__all__ = ["ModelInputs", "build_inputs", "indoor_candidates", "power_candidates"]

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
    power_options: list[str] = field(default_factory=list)
    energy_sensor: str | None = None
    daily_k: DailyKResult | None = None
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


def indoor_candidates(values: dict[str, dict[datetime, float]], exclude: set[str]) -> list[str]:
    """Indoor thermometers among the given statistic series.

    Statistical, not nominal: a room temperature stays in a narrow band over a month.
    """
    selected: list[tuple[float, str]] = []
    for statistic_id, points in values.items():
        if statistic_id in exclude:
            continue
        lowered = statistic_id.lower()
        if any(token in lowered for token in _NOT_A_ROOM):
            continue
        readings = list(points.values())
        if len(readings) < 24 * MIN_INDOOR_DAYS:
            continue
        mean = statistics.fmean(readings)
        spread = statistics.pstdev(readings)
        if not (ROOM_BAND[0] <= mean <= ROOM_BAND[1]):
            continue
        if spread > ROOM_MAX_SPREAD:
            continue
        selected.append((spread, statistic_id))
    # the most stable first: a room follows the house, a device follows its own heat
    selected.sort()
    return [statistic_id for _spread, statistic_id in selected[:MAX_INDOOR_SENSORS]]


def _correlation(left: list[float], right: list[float]) -> float:
    """Pearson correlation, 0 when either series is flat."""
    if len(left) != len(right) or len(left) < 3:
        return 0.0
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=False))
    spread_left = sum((a - mean_left) ** 2 for a in left) ** 0.5
    spread_right = sum((b - mean_right) ** 2 for b in right) ** 0.5
    if spread_left == 0 or spread_right == 0:
        return 0.0
    return covariance / (spread_left * spread_right)


def looks_like_a_sun_meter(values: dict[datetime, float], solar: dict[datetime, float]) -> bool:
    """Whether a power series is production, not heating.

    A heating meter never follows the sun: when irradiance is high the house needs
    less heat, not more. Measured on a real installation, the meter the fit preferred
    was an inverter's PV string — the solar regressor was already in the model, so the
    fit found the same signal twice and looked excellent.
    """
    both = [(moment, value) for moment, value in sorted(values.items()) if moment in solar]
    if len(both) < 24 * 2:
        return False
    daytime = [value for moment, value in both if solar[moment] > 50.0]
    night = [value for moment, value in both if solar[moment] <= 50.0]
    if not daytime or not night:
        return False
    day_mean = statistics.fmean(daytime)
    night_mean = statistics.fmean(night)
    # produces when the sun is up and stops at night: that is a solar meter
    return day_mean > 0.2 and day_mean > 2.5 * max(night_mean, 0.05)


def power_candidates(
    values: dict[str, dict[datetime, float]],
    exclude: set[str],
    unit_of: dict[str, str | None] | None = None,
    indoor: dict[datetime, float] | None = None,
    outdoor: dict[datetime, float] | None = None,
    solar: dict[datetime, float] | None = None,
) -> list[str]:
    """Power meters worth trying as the heating input, most variable first.

    Values are normalised to kW before the plausibility filter, because the recorder
    stores what the sensor reported: a 2400 W meter is a 2.4 kW house load, not a
    2400 kW one.
    """
    unit_of = unit_of or {}
    scored: list[tuple[float, str]] = []
    for statistic_id, points in values.items():
        if statistic_id in exclude:
            continue
        readings = list(_kw(points, unit_of.get(statistic_id)).values())
        if len(readings) < 24 * 3:
            continue
        peak = max(readings)
        # kW: a house heating draws at least a few hundred watts when it runs, and
        # nothing above 20 kW. Without this floor the fit picked a 50 W ventilation
        # meter, which cannot heat anything but happened to track the weather.
        if peak < 0.8 or peak > 20.0:
            continue
        if solar and looks_like_a_sun_meter(dict(points), solar):
            continue
        scored.append((statistics.pvariance(readings), statistic_id))
    scored.sort(reverse=True)
    chosen = [statistic_id for _variance, statistic_id in scored[:MAX_POWER_CANDIDATES]]

    # A heating meter rises when the house cools. Variance alone is a bad ranking: on
    # a real installation the most variable meters were solar inverters and grid
    # meters, and the heat pump was not among them.
    if indoor:
        inverse: list[tuple[float, str]] = []
        for statistic_id, points in values.items():
            if statistic_id in exclude or statistic_id in chosen:
                continue
            both = [(moment, value) for moment, value in sorted(points.items()) if moment in indoor]
            if len(both) < 24 * 3:
                continue
            correlation = _correlation(
                [
                    _kw({moment: value}, unit_of.get(statistic_id)).get(moment, value)
                    for moment, value in both
                ],
                [indoor[moment] for moment, _value in both],
            )
            if correlation < -0.1:
                inverse.append((correlation, statistic_id))
        inverse.sort()
        chosen.extend(
            statistic_id for _correlation_value, statistic_id in inverse[:MAX_INVERSE_CANDIDATES]
        )
    return chosen


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
    heating_entity: str,
    days: int,
    power_sensor: str | None = None,
    irradiance_sensor: str | None = None,
    latitude: float = 0.0,
    longitude: float = 0.0,
    settings: IdentificationSettings | None = None,
) -> tuple[ModelInputs, Any]:
    """Assemble the series and pick the best heating-power candidate.

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
        inputs.reasons.append(f"aucune statistique horaire pour {outdoor_sensor}")
        return inputs, None

    indoor_points = first_points.get(indoor_sensor) or {}
    indoor_sources: list[str] = []
    indoor_series: dict[str, dict[datetime, float]] = {}
    if len(indoor_points) >= 24 * MIN_INDOOR_DAYS:
        indoor_series[indoor_sensor] = indoor_points
        indoor_sources = [indoor_sensor]
    else:
        # The bound sensor is too young (a freshly created mean helper, typically):
        # use the thermometers the recorder already knows.
        all_ids = sorted(
            {
                state.entity_id
                for state in hass.states.async_all("sensor")
                if state.attributes.get("device_class") == "temperature"
            }
            - wanted
        )
        discovered = await async_read_statistics(hass, all_ids, days)
        candidates = indoor_candidates(_points(discovered), exclude=wanted)
        indoor_series = {
            candidate: _points(discovered)[candidate]
            for candidate in candidates
            if candidate in discovered
        }
        indoor_sources = list(indoor_series)
        if not indoor_series:
            inputs.reasons.append(
                "le capteur intérieur n'a pas d'historique et aucun thermomètre "
                "exploitable n'a été trouvé (minimum 5 jours de statistiques horaires)"
            )
            return inputs, None
        if indoor_points:
            history = (
                f"{len(indoor_points) // 24} jour(s)"
                if len(indoor_points) >= 24
                else f"{len(indoor_points)} heure(s)"
            )
            inputs.reasons.append(
                f"le capteur intérieur {indoor_sensor} n'a que {history} d'historique : "
                f"moyenne de {len(indoor_series)} thermomètre(s) à la place"
            )

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
        every_power = sorted(
            {
                state.entity_id
                for state in hass.states.async_all("sensor")
                if state.attributes.get("device_class") == "power"
            }
            - wanted
        )
        discovered_power = await async_read_statistics(hass, every_power, days)
        units = {
            entity_id: (
                hass.states.get(entity_id).attributes.get("unit_of_measurement")
                if hass.states.get(entity_id) is not None
                else None
            )
            for entity_id in every_power
        }
        power_options = power_candidates(_points(discovered_power), exclude=wanted, unit_of=units)

    inputs.power_options = list(power_options)

    # Phase 2: let the identifier choose the heating input. A house has a dozen power
    # meters; the one that explains the measured temperature is the heating.
    best: tuple[float, str, Any, list[float], Any] | None = None
    best_without_k: tuple[float, str, Any, list[float]] | None = None
    failures: list[str] = []
    # One read for every candidate: a query per candidate would be dozens of database
    # round trips for a job that runs daily.
    every_series = await async_read_statistics(hass, power_options, days) if power_options else {}
    every_points = _points(every_series)
    for candidate in power_options:
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
            return inputs, finished
        if daily is not None and daily.message:
            inputs.reasons.append(daily.message)
        inputs.reasons.extend(failures[:3])
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
    candidates.sort(
        key=lambda entity_id: (
            str(hass.states[entity_id].attributes.get("unit_of_measurement")) == "Wh",
            entity_id,
        )
    )
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

    series = await async_read_statistics(hass, [energy_sensor], days)
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
    finished = replace(params, k=daily.k)
    _LOGGER.info(
        "Thermal Model: k = %.4f ± %.4f K/kWh from %s days of %s",
        daily.k,
        daily.stderr or 0.0,
        daily.days,
        energy_sensor,
    )
    return finished, energy_sensor, daily
