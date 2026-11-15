"""Generate synthetic houses with known parameters.

A generic identification service cannot be validated on one house. This module
builds virtual houses whose parameters are known by construction, with the same
physics as the production simulator, a thermostat that opens and closes the loop
the way a real one does, measurement noise, and optional data holes.

The thermostat matters more than it looks: with the heating permanently on, or
with a setpoint never reached, the house never free-floats and ``a`` is not
identifiable. The default profile therefore includes a night setback, which is
what makes a real house identifiable too.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from custom_components.thermal_model.core.simulate import SimulationStep, ThermalParams, step

__all__ = ["SyntheticHouse", "generate"]


@dataclass(frozen=True, kw_only=True)
class SyntheticHouse:
    """A generated dataset: the truth and what a sensor would have measured."""

    times: tuple[datetime, ...]
    tin_measured: tuple[float | None, ...]  # noisy: what the integration sees
    tin_truth: tuple[float, ...]  # noise-free, for diagnostics only
    tout: tuple[float, ...]
    solar: tuple[float, ...]
    heating_kw: tuple[float, ...]
    params: ThermalParams
    dt_hours: float

    @property
    def length(self) -> int:
        """Number of samples."""
        return len(self.times)

    @property
    def heating_fraction(self) -> float:
        """Share of samples where the heater was on (duty cycle)."""
        if not self.heating_kw:
            return 0.0
        return sum(1 for power in self.heating_kw if power > 0) / len(self.heating_kw)

    def with_gaps(self, ranges: Sequence[tuple[int, int]]) -> SyntheticHouse:
        """Blank the measured temperature on the given index ranges."""
        if not ranges:
            return self
        values: list[float | None] = list(self.tin_measured)
        for first, stop in ranges:
            for index in range(max(0, first), min(len(values), stop)):
                values[index] = None
        return replace(self, tin_measured=tuple(values))


def _outdoor(
    hour_of_year: float, hour_of_day: float, mean: float, amplitude: float, trend_per_day: float
) -> float:
    """Seasonal drift, a slow trend and a daily swing, like a real weather series."""
    seasonal = amplitude * math.sin(2.0 * math.pi * (hour_of_year / 8760.0) - math.pi / 2.0)
    trend = trend_per_day * (hour_of_year / 24.0)
    daily = 0.35 * amplitude * math.sin(2.0 * math.pi * (hour_of_day - 9.0) / 24.0)
    return mean + seasonal + trend + daily


def _solar(hour_of_day: float, peak: float) -> float:
    """A smooth daylight bell between 08:00 and 18:00."""
    if not 8.0 <= hour_of_day <= 18.0:
        return 0.0
    return max(0.0, peak * math.sin(math.pi * (hour_of_day - 8.0) / 10.0))


def generate(
    params: ThermalParams,
    *,
    start: datetime,
    days: int = 30,
    dt_hours: float = 0.25,
    text_mean: float = 8.0,
    text_amplitude: float = 7.0,
    text_trend_per_day: float = -0.25,
    solar_peak: float = 420.0,
    day_setpoint: float = 20.5,
    night_setpoint: float = 19.0,
    hysteresis: float = 0.4,
    heating_capacity_kw: float = 2.0,
    initial_tin: float = 19.5,
    noise_c: float = 0.1,
    seed: int = 0,
) -> SyntheticHouse:
    """Simulate a house with a thermostatic heater and noisy measurements."""
    if dt_hours <= 0:
        raise ValueError("dt_hours must be positive")
    if days <= 0:
        raise ValueError("days must be positive")

    rng = random.Random(seed)
    steps_per_hour = 1.0 / dt_hours
    capacity = heating_capacity_kw if params.k is not None else 0.0

    times: list[datetime] = []
    tin_measured: list[float | None] = []
    tin_truth: list[float] = []
    tout_series: list[float] = []
    solar_series: list[float] = []
    power_series: list[float] = []

    tin = initial_tin
    heating_on = False
    total_steps = int(days * 24 * steps_per_hour)

    for index in range(total_steps):
        moment = start + timedelta(hours=index * dt_hours)
        hour_of_day = (index / steps_per_hour) % 24.0
        hour_of_year = moment.timetuple().tm_yday * 24.0 + hour_of_day

        tout = _outdoor(hour_of_year, hour_of_day, text_mean, text_amplitude, text_trend_per_day)
        solar = _solar(hour_of_day, solar_peak)

        setpoint = night_setpoint if (hour_of_day < 6.0 or hour_of_day >= 23.0) else day_setpoint
        if tin < setpoint - hysteresis / 2.0:
            heating_on = True
        elif tin > setpoint + hysteresis / 2.0:
            heating_on = False
        power = capacity if heating_on else 0.0

        times.append(moment)
        tin_truth.append(tin)
        tin_measured.append(tin + rng.gauss(0.0, noise_c))
        tout_series.append(tout)
        solar_series.append(solar)
        power_series.append(power)

        tin = step(
            tin,
            SimulationStep(text=tout, solar=solar, heating_kw=power),
            params,
            dt_hours=dt_hours,
        )

    return SyntheticHouse(
        times=tuple(times),
        tin_measured=tuple(tin_measured),
        tin_truth=tuple(tin_truth),
        tout=tuple(tout_series),
        solar=tuple(solar_series),
        heating_kw=tuple(power_series),
        params=params,
        dt_hours=dt_hours,
    )
