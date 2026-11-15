"""Build indoor temperature forecasts from weather and a fitted model.

Without a control loop, a forecast is a *scenario*: what will happen depends on
what the heating does. Three explicit scenarios are offered rather than one
opaque number:

- ``free_float``   : heating never runs — the reference trajectory, and the one
  used to validate the model;
- ``thermostat``   : hysteresis emulation bounded by the heating capacity;
- ``observed_duty``: replay of the duty cycle observed over the last 24 hours,
  hour by hour.

Every run is timestamped and identified by its scenario so past runs can be
compared with what actually happened.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..const import ModelScenario
from .simulate import SimulationStep, ThermalParams, simulate

__all__ = [
    "ForecastRun",
    "WeatherStep",
    "build_run",
    "observed_duty_profile",
    "params_digest",
]


@dataclass(frozen=True, kw_only=True)
class WeatherStep:
    """One forecast step: what the weather is expected to do."""

    time: datetime
    text: float  # outdoor temperature, °C
    solar: float = 0.0  # irradiance, W/m²


@dataclass(frozen=True, kw_only=True)
class ForecastRun:
    """A forecast trajectory, stamped so it can be compared later."""

    issued_at: datetime
    scenario: ModelScenario
    source: str
    times: tuple[datetime, ...]
    values: tuple[float, ...]
    params_digest: str

    @property
    def minimum(self) -> float:
        """Coldest predicted value."""
        return min(self.values)

    @property
    def minimum_at(self) -> datetime:
        """When the coldest value is expected."""
        return self.times[self.values.index(self.minimum)]

    def at(self, moment: datetime) -> float | None:
        """Predicted value at ``moment``, if the run covers it."""
        for time, value in zip(self.times, self.values, strict=True):
            if time == moment:
                return value
        return None


def params_digest(params: ThermalParams) -> str:
    """Short fingerprint of a parameter set, to spot a model change in history."""
    parts = [f"{params.a:.5f}", f"{params.c_s:.5f}", f"{params.d:+.4f}"]
    parts.append("none" if params.k is None else f"{params.k:.4f}")
    return "/".join(parts)


def observed_duty_profile(
    past: Sequence[tuple[datetime, float]], *, hours: int = 24
) -> dict[int, float]:
    """Average heating power per hour of the day, from the recent past.

    Used by the ``observed_duty`` scenario: it replays the boiler's actual
    cycling instead of pretending to know the setpoint.
    """
    buckets: dict[int, list[float]] = {}
    for moment, power in past:
        buckets.setdefault(moment.hour, []).append(power)
    return {hour: sum(values) / len(values) for hour, values in buckets.items() if values}


def _thermostat_steps(
    steps: Sequence[WeatherStep],
    *,
    setpoint: float,
    hysteresis: float,
    capacity_kw: float,
    start_temperature: float,
    params: ThermalParams,
    dt_hours: float,
) -> list[SimulationStep]:
    """Emulate a thermostat: on below setpoint − h/2, off above setpoint + h/2.

    The loop is evaluated on the *simulated* temperature, not on the current one:
    freezing the decision makes the trajectory diverge from the physical result.
    """
    if capacity_kw <= 0 or params.k is None:
        return [SimulationStep(text=step.text, solar=step.solar) for step in steps]

    heating_on = start_temperature < setpoint
    current = start_temperature
    planned: list[SimulationStep] = []
    for step in steps:
        if current < setpoint - hysteresis / 2.0:
            heating_on = True
        elif current > setpoint + hysteresis / 2.0:
            heating_on = False
        power = capacity_kw if heating_on else 0.0
        planned.append(SimulationStep(text=step.text, solar=step.solar, heating_kw=power))
        current = simulate(current, [planned[-1]], params, dt_hours=dt_hours)[-1]
    return planned


def build_run(
    *,
    params: ThermalParams,
    weather: Sequence[WeatherStep],
    start_temperature: float,
    scenario: ModelScenario,
    issued_at: datetime,
    source: str,
    dt_hours: float,
    setpoint: float = 20.5,
    hysteresis: float = 0.4,
    capacity_kw: float = 2.0,
    duty_profile: dict[int, float] | None = None,
) -> ForecastRun:
    """Simulate one scenario over the given weather steps."""
    if not weather:
        raise ValueError("no weather steps to forecast on")
    if dt_hours <= 0:
        raise ValueError("dt_hours must be positive")

    if scenario is ModelScenario.FREE_FLOAT:
        steps = [SimulationStep(text=step.text, solar=step.solar) for step in weather]
    elif scenario is ModelScenario.THERMOSTAT:
        steps = _thermostat_steps(
            weather,
            setpoint=setpoint,
            hysteresis=hysteresis,
            capacity_kw=capacity_kw,
            start_temperature=start_temperature,
            params=params,
            dt_hours=dt_hours,
        )
    elif scenario is ModelScenario.OBSERVED_DUTY:
        profile = duty_profile or {}
        steps = [
            SimulationStep(
                text=step.text, solar=step.solar, heating_kw=profile.get(step.time.hour, 0.0)
            )
            for step in weather
        ]
    else:  # pragma: no cover - StrEnum is exhaustive
        raise ValueError(f"unknown scenario: {scenario}")

    times = tuple(step.time for step in weather)
    values = simulate(start_temperature, steps, params, dt_hours=dt_hours)
    return ForecastRun(
        issued_at=issued_at,
        scenario=scenario,
        source=source,
        times=(times[0] - timedelta(hours=dt_hours), *times),
        values=values,
        params_digest=params_digest(params),
    )
