"""Forecast scenario tests: a scenario must behave like its name says."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.thermal_model.const import ModelScenario
from custom_components.thermal_model.core.forecast import (
    ForecastRun,
    WeatherStep,
    build_run,
    observed_duty_profile,
    params_digest,
)
from custom_components.thermal_model.core.simulate import ThermalParams

PARAMS = ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=0.49)
START = datetime(2026, 1, 10, 12, tzinfo=UTC)


def _weather(hours: int, text: float = 2.0, solar: float = 0.0) -> list[WeatherStep]:
    return [
        WeatherStep(time=START + timedelta(hours=hour), text=text, solar=solar)
        for hour in range(hours)
    ]


def _run(
    scenario: ModelScenario,
    *,
    weather: list[WeatherStep] | None = None,
    start_temperature: float = 19.0,
    dt_hours: float = 1.0,
    **kwargs: Any,
) -> ForecastRun:
    return build_run(
        params=PARAMS,
        weather=_weather(48) if weather is None else weather,
        start_temperature=start_temperature,
        scenario=scenario,
        issued_at=START,
        source="test",
        dt_hours=dt_hours,
        **kwargs,
    )


def test_free_float_decays_towards_the_outdoor_temperature() -> None:
    """No heating: the house drifts to the outside temperature, never past it."""
    run = _run(ModelScenario.FREE_FLOAT, weather=_weather(48, text=2.0))

    assert run.values[0] == pytest.approx(19.0, abs=1e-9)
    assert run.values[-1] < run.values[0]
    assert run.values[-1] > 2.0  # approaches, does not overshoot
    assert run.times[-1] - run.times[0] == timedelta(hours=48)


def test_a_cold_snap_cools_faster_than_a_mild_one() -> None:
    """The scenario must react to the weather it is given, not to a constant."""
    mild = _run(ModelScenario.FREE_FLOAT, weather=_weather(24, text=10.0))
    cold = _run(ModelScenario.FREE_FLOAT, weather=_weather(24, text=-5.0))

    assert cold.values[-1] < mild.values[-1] - 2.0


def test_thermostat_holds_the_setpoint() -> None:
    """Hysteresis emulation must keep the house around the setpoint, not run away."""
    run = _run(ModelScenario.THERMOSTAT, setpoint=20.5, capacity_kw=2.0)

    tail = run.values[6:]
    assert min(tail) > 19.5
    assert max(tail) < 22.0


def test_thermostat_without_gain_cannot_heat() -> None:
    """k = None means the model has no heating term: the thermostat is inert."""
    run = build_run(
        params=ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=None),
        weather=_weather(24, text=2.0),
        start_temperature=19.0,
        scenario=ModelScenario.THERMOSTAT,
        issued_at=START,
        source="test",
        dt_hours=1.0,
        capacity_kw=5.0,
    )

    assert run.values[-1] < run.values[0]


def test_observed_duty_replays_the_profile_hour_by_hour() -> None:
    """The profile is per hour of the day, so it must repeat during the horizon."""
    past = [
        (START - timedelta(hours=h), 1.5 if (START - timedelta(hours=h)).hour >= 6 else 0.0)
        for h in range(48)
    ]
    profile = observed_duty_profile(past)

    run = _run(ModelScenario.OBSERVED_DUTY, duty_profile=profile, weather=_weather(24, text=2.0))

    assert set(profile) == {moment.hour for moment, _ in past}
    assert run.values[-1] > run.values[0]  # daytime duty heats the house


def test_observed_duty_without_history_is_free_float() -> None:
    """An empty profile must not invent heating."""
    run = _run(ModelScenario.OBSERVED_DUTY, duty_profile={})

    assert all(isinstance(value, float) for value in run.values)
    assert run.values[-1] < run.values[0]


def test_minimum_is_reported_with_its_time() -> None:
    """The dashboard highlights the coldest moment: it must exist and be coherent."""
    run = _run(ModelScenario.FREE_FLOAT, weather=_weather(48, text=2.0))

    assert run.minimum == min(run.values)
    assert run.minimum_at == run.times[run.values.index(run.minimum)]


def test_at_returns_none_outside_the_horizon() -> None:
    """Looking up a moment the run does not cover must say so, not extrapolate."""
    run = _run(ModelScenario.FREE_FLOAT, weather=_weather(12))

    assert run.at(run.times[3]) == run.values[3]
    assert run.at(START + timedelta(days=3)) is None


def test_no_weather_is_an_error_not_an_empty_curve() -> None:
    """An empty forecast would silently render as 'no data' on the dashboard."""
    with pytest.raises(ValueError, match="no weather"):
        _run(ModelScenario.FREE_FLOAT, weather=[])


def test_params_digest_changes_when_the_model_changes() -> None:
    """The fingerprint is how a past run is traced back to a model version."""
    same = ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=0.49)
    other = ThermalParams(a=0.017, c_s=0.0023, d=0.04, k=0.49)

    assert params_digest(same) == params_digest(PARAMS)
    assert params_digest(other) != params_digest(PARAMS)


def test_rejects_a_non_positive_step() -> None:
    """dt is a physical quantity: zero would divide by zero somewhere."""
    with pytest.raises(ValueError, match="dt_hours"):
        _run(ModelScenario.FREE_FLOAT, dt_hours=0.0)
