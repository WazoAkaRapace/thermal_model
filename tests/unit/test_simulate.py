"""Tests for the forward simulator (the goldens come from the plan, corrected)."""

from __future__ import annotations

import itertools

import pytest

from custom_components.thermal_model.core.simulate import (
    ModelDivergedError,
    SimulationStep,
    ThermalParams,
    analytic_passive,
    simulate,
    step,
)

PARAMS = ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=0.49)


def test_step_without_heating_matches_the_plan_golden() -> None:
    """T=20 °C, Text=5 °C, a=0.016, d=0.04, S=0, dt=1 h -> 19.80 °C.

    The value 19.76 appears in an early draft of the plan: it silently drops
    ``d_int`` and would have made the implementation "fix" correct code.
    """
    result = step(20.0, SimulationStep(text=5.0), PARAMS, dt_hours=1.0)
    assert result == pytest.approx(19.80)
    # and the internal-gain term is worth exactly +0.04 K on that step
    no_internal = ThermalParams(a=0.016, c_s=0.0023, d=0.0, k=0.49)
    assert step(20.0, SimulationStep(text=5.0), no_internal, dt_hours=1.0) == pytest.approx(19.76)


def test_step_with_heating_adds_the_heating_gain() -> None:
    """The same step with 1 kW gives 19.80 + 0.49 = 20.29 °C."""
    result = step(20.0, SimulationStep(text=5.0, heating_kw=1.0), PARAMS, dt_hours=1.0)
    assert result == pytest.approx(20.29)


def test_step_with_solar_gain() -> None:
    """Solar acts through c_s, in K per W/m² per hour."""
    result = step(20.0, SimulationStep(text=20.0, solar=500.0), PARAMS, dt_hours=1.0)
    assert result == pytest.approx(20.0 + 0.0023 * 500.0 + 0.04)


def test_every_term_scales_with_dt() -> None:
    """A half step is not the same number of kelvin."""
    one_hour = step(20.0, SimulationStep(text=5.0, solar=200.0), PARAMS, dt_hours=1.0)
    half_hour = step(20.0, SimulationStep(text=5.0, solar=200.0), PARAMS, dt_hours=0.5)
    assert (20.0 - half_hour) == pytest.approx((20.0 - one_hour) / 2.0)


def test_simulation_converges_towards_the_analytic_solution() -> None:
    """Euler at 1-minute steps reproduces the exact ODE solution."""
    dt = 1.0 / 60.0
    steps = [SimulationStep(text=5.0) for _ in range(120)]
    trajectory = simulate(20.0, steps, PARAMS, dt_hours=dt)
    expected = analytic_passive(20.0, 5.0, PARAMS, hours=2.0)
    assert trajectory[-1] == pytest.approx(expected, abs=0.005)


def test_finer_dt_does_not_change_the_outcome() -> None:
    """Coarse and fine sampling must agree within the Euler error."""
    coarse = simulate(20.0, [SimulationStep(text=5.0) for _ in range(6)], PARAMS, dt_hours=1.0)
    fine = simulate(20.0, [SimulationStep(text=5.0) for _ in range(24)], PARAMS, dt_hours=0.25)
    assert fine[-1] == pytest.approx(coarse[-1], abs=0.02)


def test_free_float_never_heats() -> None:
    """Without heating the temperature only decays towards equilibrium."""
    steps = [SimulationStep(text=5.0) for _ in range(24)]
    trajectory = simulate(21.0, steps, PARAMS, dt_hours=1.0)
    assert all(later < earlier for earlier, later in itertools.pairwise(trajectory))
    assert trajectory[-1] == pytest.approx(analytic_passive(21.0, 5.0, PARAMS, hours=24.0), abs=0.2)


def test_parameters_without_k_ignore_the_heating_input() -> None:
    """When k is not identified the model must not invent a heating effect."""
    params = ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=None)
    without = step(20.0, SimulationStep(text=5.0, heating_kw=0.0), params, dt_hours=1.0)
    with_heat = step(20.0, SimulationStep(text=5.0, heating_kw=3.0), params, dt_hours=1.0)
    assert without == pytest.approx(with_heat)


def test_divergence_is_reported_not_published() -> None:
    """A nonsense model raises instead of returning a plausible-looking curve."""
    absurd = ThermalParams(a=0.9, c_s=0.0023, d=0.0, k=0.49)
    with pytest.raises(ModelDivergedError):
        simulate(20.0, [SimulationStep(text=-25.0) for _ in range(3)], absurd, dt_hours=1.0)


def test_trajectory_is_clamped_to_the_physical_domain() -> None:
    """Even a wild step stays inside the domain instead of returning -400 °C."""
    trajectory = simulate(
        20.0,
        [SimulationStep(text=-29.0) for _ in range(10)],
        ThermalParams(a=0.05, c_s=0.0, d=-0.5, k=None),
        dt_hours=1.0,
    )
    assert min(trajectory) >= -30.0


def test_time_constant_is_the_inverse_of_a() -> None:
    """1/a is the number operators reason with (62.5 h here, not 44 h)."""
    assert PARAMS.time_constant_hours == pytest.approx(62.5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"a": 0.0, "c_s": 0.002, "d": 0.0},
        {"a": 1.5, "c_s": 0.002, "d": 0.0},
        {"a": 0.016, "c_s": -1.0, "d": 0.0},
        {"a": 0.016, "c_s": 0.002, "d": 2.0},
        {"a": 0.016, "c_s": 0.002, "d": 0.0, "k": -1.0},
    ],
)
def test_parameters_out_of_range_are_refused(kwargs: dict[str, float]) -> None:
    """A parameter outside the plausible domain is a bug upstream."""
    with pytest.raises(ValueError):
        ThermalParams(**kwargs)


def test_positive_dt_is_enforced() -> None:
    """A zero-length step is a programming error."""
    with pytest.raises(ValueError):
        step(20.0, SimulationStep(text=5.0), PARAMS, dt_hours=0.0)
