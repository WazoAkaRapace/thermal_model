"""Forward simulation of the 1R1C model (pure, dt-explicit).

The same stepper is used to validate an identification and to produce the
indoor temperature forecast: a model validated by a simulation must be simulated
by the same code that produced the forecast, or the validation proves nothing.

```
T[k+1] = T[k] + dt·(−a·(T[k] − Text[k]) + c_s·S[k] + d + k·P[k])
```

Every term is multiplied by ``dt``; a coefficient is never expressed "per step",
which is the usual way a discrete model silently stops matching its own equation
when the sampling changes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "ModelDivergedError",
    "SimulationStep",
    "ThermalParams",
    "analytic_passive",
    "simulate",
    "step",
]

# A building cannot gain or lose more than a few kelvin per hour; a bigger rate
# means the parameters are nonsense or the inputs are broken.
MAX_PHYSICAL_RATE_K_PER_HOUR = 5.0
MIN_TEMPERATURE_C = -30.0
MAX_TEMPERATURE_C = 60.0


class ModelDivergedError(RuntimeError):
    """Raised when a simulation leaves the physically plausible domain."""


def effective_heating_k(
    k: float,
    alpha: float | None,
    reference: float | None,
    text: float,
) -> float:
    """The heating gain at this outdoor temperature.

    k is identified at the mean outdoor temperature of the window; when it is colder
    the measured energy delivers less heat (a heat pump's COP drops), so the gain is
    scaled up by alpha per degree below the reference — and down when milder. With no
    alpha the gain is constant, which is correct for resistive heating.
    """
    if not alpha or reference is None:
        return k
    return max(0.0, k * (1.0 + alpha * (reference - text)))


@dataclass(frozen=True, kw_only=True)
class ThermalParams:
    """Identified parameters of the 1R1C model."""

    a: float  # 1/h, normalised heat loss
    c_s: float  # K/(W/m²)/h, solar gain
    d: float  # K/h, net internal gains
    k: float | None = None  # K/(h·kW), heating gain; None when not identified
    # Heating systems convert their measured energy less efficiently when it is cold
    # (a heat pump's COP, mostly). k is then k0 at ``k_reference_temperature`` and
    # grows by k0*alpha for every degree below it. alpha = 0 means a constant k, which
    # is what resistive heating and a narrow identification window give.
    alpha: float | None = None  # 1/K
    k_reference_temperature: float | None = None  # °C, mean outdoor of the window

    def __post_init__(self) -> None:
        if not math.isfinite(self.a) or not 0.0 < self.a < 1.0:
            raise ValueError(f"a must be in ]0, 1[ 1/h, got {self.a!r}")
        if not math.isfinite(self.c_s) or not 0.0 <= self.c_s < 0.1:
            raise ValueError(f"c_s must be in [0, 0.1], got {self.c_s!r}")
        if not math.isfinite(self.d) or not -1.0 < self.d < 1.0:
            raise ValueError(f"d must be in ]-1, 1[ K/h, got {self.d!r}")
        if self.k is not None and (not math.isfinite(self.k) or not 0.0 < self.k < 10.0):
            raise ValueError(f"k must be in ]0, 10[ K/(h·kW) or None, got {self.k!r}")
        if self.alpha is not None and self.k is None:
            raise ValueError("alpha needs k")
        if self.alpha is not None and (
            not math.isfinite(self.alpha) or not -0.05 < self.alpha < 0.30
        ):
            raise ValueError(f"alpha must be in ]-0.05, 0.30[ 1/K or None, got {self.alpha!r}")

    @property
    def time_constant_hours(self) -> float:
        """Free-float time constant (1/a): how slowly the house answers."""
        return 1.0 / self.a


@dataclass(frozen=True, kw_only=True)
class SimulationStep:
    """Exogenous inputs of one step."""

    text: float  # outdoor temperature, °C
    solar: float = 0.0  # irradiance, W/m²
    heating_kw: float = 0.0  # heating power actually delivered, kW


def step(
    tin: float,
    inputs: SimulationStep,
    params: ThermalParams,
    *,
    dt_hours: float,
) -> float:
    """Advance the model by one step."""
    if dt_hours <= 0:
        raise ValueError("dt_hours must be positive")
    heating_k = params.k or 0.0
    rate = (
        -params.a * (tin - inputs.text)
        + params.c_s * inputs.solar
        + params.d
        + effective_heating_k(heating_k, params.alpha, params.k_reference_temperature, inputs.text)
        * inputs.heating_kw
    )
    return tin + dt_hours * rate


def simulate(
    tin: float,
    steps: Sequence[SimulationStep],
    params: ThermalParams,
    *,
    dt_hours: float,
    max_rate_k_per_hour: float = MAX_PHYSICAL_RATE_K_PER_HOUR,
    clamp: bool = True,
) -> tuple[float, ...]:
    """Simulate the indoor temperature over ``steps``.

    Returns the trajectory including the initial sample. Raises
    ``ModelDivergedError`` when a step exceeds the physically plausible rate, so
    a broken model is reported as broken instead of being published as a result.
    """
    if dt_hours <= 0:
        raise ValueError("dt_hours must be positive")
    heating_k = params.k or 0.0
    reference = params.k_reference_temperature

    trajectory: list[float] = [tin]
    current = tin
    for inputs in steps:
        rate = (
            -params.a * (current - inputs.text)
            + params.c_s * inputs.solar
            + params.d
            + effective_heating_k(heating_k, params.alpha, reference, inputs.text)
            * inputs.heating_kw
        )
        if abs(rate) > max_rate_k_per_hour:
            raise ModelDivergedError(
                f"implausible rate {rate:.2f} K/h at step {len(trajectory) - 1} "
                f"(T={current:.2f} °C, Text={inputs.text:.2f} °C, P={inputs.heating_kw:.2f} kW)"
            )
        current = current + dt_hours * rate
        if clamp:
            current = min(max(current, MIN_TEMPERATURE_C), MAX_TEMPERATURE_C)
        trajectory.append(current)
    return tuple(trajectory)


def analytic_passive(
    tin: float,
    text: float,
    params: ThermalParams,
    *,
    hours: float,
) -> float:
    """Exact solution of the linear ODE without heating (test reference).

    ``T(t) = T_eq + (T0 − T_eq)·exp(−a·t)`` with ``T_eq = Text + d/a``.
    """
    if hours < 0:
        raise ValueError("hours must be non-negative")
    equilibrium = text + params.d / params.a
    return equilibrium + (tin - equilibrium) * math.exp(-params.a * hours)
