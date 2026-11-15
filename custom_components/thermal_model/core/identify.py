"""Identify the 1R1C parameters from measured history (pure, no Home Assistant).

The estimator is the one that survived scrutiny: overlapping 1-hour differences
paired with the *window mean* of the temperature difference, because the naive
version — differencing a noisy series and regressing on that same series — biases
``a`` upward by 60 % to 200 % depending on the lag.

Nothing is invented here. When a parameter cannot be identified, it comes back
as ``None`` with the reason attached, and the caller is expected to publish that
honesty instead of a plausible number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..const import Identifiability, ReasonCode
from .linalg import SingularMatrixError, instrumental_variables
from .simulate import ModelDivergedError, SimulationStep, ThermalParams, simulate

__all__ = [
    "DEFAULT_SETTINGS",
    "IdentificationError",
    "IdentificationResult",
    "IdentificationSettings",
    "identify",
]

MIN_A = 0.001
MAX_A = 0.5
MAX_C_S = 0.05
MAX_D = 1.0
MAX_K = 10.0
DEGENERACY_RATIO = 0.5


class IdentificationError(ValueError):
    """Raised when the data cannot support an identification at all."""


@dataclass(frozen=True, kw_only=True)
class IdentificationSettings:
    """Tunables, all expressed in real time so the sampling can change."""

    dt_hours: float = 0.25
    # Smoothing before differencing was originally used to fight measurement
    # noise, but it biases the slope of a curved signal (~+10 % on a 30-day
    # noise-free house) and the lagged-instrument fit below already removes that
    # noise bias. Measured: no smoothing gives a within 2 % on 60-90 days, a 2 h
    # smoothing window gives 0 to -12 %. Kept as an option for very noisy sensors.
    smooth_hours: float = 0.0
    window_hours: float = 1.0
    grace_hours: float = 0.5
    solar_threshold: float = 10.0  # W/m² below which a step counts as "low solar"
    max_rate_k_per_hour: float = 3.0
    min_passive_windows: int = 24
    min_heating_windows: int = 6
    min_coverage: float = 0.25
    # Below this relative spread of the driving temperature, `a` and `d` trade off
    # against each other: the residuals look tiny (smoothed data makes any fit look
    # precise) while the extrapolation is wrong. Calibrated on synthetic houses:
    # 30 days without an outdoor trend gives 0.03 (refused), 60-90 days with a
    # realistic trend gives 0.10-0.12 (accepted).
    min_driving_spread: float = 0.08

    def steps(self, hours: float) -> int:
        """Convert a duration into a number of samples."""
        return max(1, round(hours / self.dt_hours))


DEFAULT_SETTINGS = IdentificationSettings()


@dataclass(frozen=True, kw_only=True)
class IdentificationResult:
    """Fitted parameters plus everything needed to judge them."""

    params: ThermalParams
    stderr: dict[str, float | None] = field(default_factory=dict)
    identifiability: Identifiability = Identifiability.IDENTIFIED
    rmse_free_float: float | None = None
    rmse_observed: float | None = None
    passive_windows: int = 0
    heating_windows: int = 0
    coverage: float = 0.0
    driving_spread: float | None = None
    reasons: tuple[ReasonCode, ...] = ()

    def stderr_ratios(self) -> dict[str, float | None]:
        """SE/|value| per parameter: the published identifiability evidence."""
        ratios: dict[str, float | None] = {}
        values = {"a": self.params.a, "c_s": self.params.c_s, "d": self.params.d}
        if self.params.k is not None:
            values["k"] = self.params.k
        for name, value in values.items():
            error = self.stderr.get(name)
            ratios[name] = None if error is None or value == 0 else abs(error / value)
        return ratios


def _smooth(values: Sequence[float | None], half_window: int) -> list[float | None]:
    """Centred moving average that keeps gaps as gaps."""
    smoothed: list[float | None] = []
    for index in range(len(values)):
        window = [
            values[position]
            for position in range(
                max(0, index - half_window), min(len(values), index + half_window + 1)
            )
            if values[position] is not None
        ]
        smoothed.append(sum(window) / len(window) if len(window) >= half_window else None)  # type: ignore[arg-type]
    return smoothed


def _heating_and_grace(heating_kw: Sequence[float], grace_steps: int) -> list[bool]:
    """Mark the heating steps and the emitter's post-heating restitution window."""
    busy = [power > 0 for power in heating_kw]
    marked = list(busy)
    for index, is_heating in enumerate(busy):
        if is_heating:
            for offset in range(1, grace_steps + 1):
                if index + offset < len(marked):
                    marked[index + offset] = True
    return marked


def identify(
    times: Sequence[datetime],
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float],
    *,
    settings: IdentificationSettings = DEFAULT_SETTINGS,
) -> IdentificationResult:
    """Fit ``a``, ``c_s``, ``d`` and (when possible) ``k``."""
    length = len(times)
    if not (len(tin) == len(tout) == len(solar) == len(heating_kw) == length):
        raise IdentificationError("all series must have the same length")
    if length < 2 * settings.steps(24):
        raise IdentificationError("at least two days of data are required")

    present = sum(1 for value in tin if value is not None)
    coverage = present / length
    if coverage < settings.min_coverage:
        raise IdentificationError(
            f"temperature coverage too low: {coverage:.0%} < {settings.min_coverage:.0%}"
        )

    smooth_half = max(1, settings.steps(settings.smooth_hours) // 2)
    tin_smooth = _smooth(tin, smooth_half)
    busy = _heating_and_grace(heating_kw, settings.steps(settings.grace_hours))
    span = settings.steps(settings.window_hours)
    dt = settings.dt_hours

    passive_rows: list[list[float]] = []
    passive_instruments: list[list[float]] = []
    passive_targets: list[float] = []
    passive_driving: list[float] = []
    sunny_rows: list[list[float]] = []
    sunny_targets: list[float] = []
    heating_rows: list[list[float]] = []
    heating_targets: list[float] = []
    heating_powers: list[float] = []

    for start in range(0, length - span):
        stop = start + span
        window = range(start, stop + 1)

        if tin_smooth[start] is None or tin_smooth[stop] is None:
            continue
        if any(tin_smooth[position] is None for position in window):
            continue

        delta = (tin_smooth[stop] - tin_smooth[start]) / settings.window_hours
        # The discrete identity sums the driving term over the *steps* of the
        # window (left endpoints only): using the mean of span+1 points instead
        # shifts the regressor and biases `a` by ~7 % on a pure Euler signal.
        step_window = range(start, stop)
        mean_driving = sum(
            (tin_smooth[position] or 0.0) - tout[position] for position in step_window
        ) / len(step_window)
        mean_solar = sum(solar[position] for position in step_window) / len(step_window)
        mean_power = sum(heating_kw[position] for position in step_window) / len(step_window)
        is_busy = any(busy[position] for position in window)

        if not is_busy and mean_solar < settings.solar_threshold:
            # The instrument is the *lagged* driving mean: correlated with the
            # true driving temperature, uncorrelated with the noise of this
            # window. Without it, a is biased by 60 % to 200 % on noisy data.
            if start < span:
                continue
            lag_window = range(start - span, start)
            if any(tin_smooth[position] is None for position in lag_window):
                continue
            lagged = sum(
                (tin_smooth[position] or 0.0) - tout[position] for position in lag_window
            ) / len(lag_window)
            passive_rows.append([-mean_driving, 1.0])
            passive_instruments.append([-lagged, 1.0])
            passive_targets.append(delta)
            passive_driving.append(mean_driving)
        elif not is_busy and mean_solar >= settings.solar_threshold:
            sunny_rows.append([mean_driving, 1.0, mean_solar])
            sunny_targets.append(delta)
        elif all(heating_kw[position] > 0 for position in window):
            heating_rows.append([-mean_driving, 1.0, mean_solar])
            heating_targets.append(delta)
            heating_powers.append(mean_power)

    if len(passive_rows) < settings.min_passive_windows:
        raise IdentificationError(
            f"only {len(passive_rows)} passive low-solar windows "
            f"(need {settings.min_passive_windows}): the house never free-floats"
        )

    try:
        passive_fit = instrumental_variables(passive_rows, passive_instruments, passive_targets)
    except (SingularMatrixError, ValueError) as error:
        raise IdentificationError(f"passive fit is degenerate: {error}") from error

    a = passive_fit.params[0]
    d = passive_fit.params[1]
    if not MIN_A <= a <= MAX_A:
        raise IdentificationError(f"implausible heat loss a={a:.5f} 1/h")
    if abs(d) > MAX_D:
        raise IdentificationError(f"implausible internal gains d={d:.3f} K/h")

    driving_spread = _relative_spread(passive_driving)

    c_s, c_s_stderr = _fit_solar(sunny_rows, sunny_targets, a=a, d=d)
    k, k_stderr, heating_windows = _fit_heating(
        heating_rows,
        heating_targets,
        heating_powers,
        a=a,
        d=d,
        c_s=c_s,
        min_windows=settings.min_heating_windows,
    )

    try:
        params = ThermalParams(a=a, c_s=c_s, d=d, k=k)
    except ValueError as error:
        raise IdentificationError(f"fitted parameters are out of range: {error}") from error

    stderr = {
        "a": passive_fit.stderr[0],
        "d": passive_fit.stderr[1],
        "c_s": c_s_stderr,
        "k": k_stderr,
    }

    reasons: list[ReasonCode] = []
    ratios = {
        name: (None if error is None or value == 0 else abs(error / value))
        for name, value, error in (
            ("a", a, stderr["a"]),
            ("c_s", c_s, c_s_stderr),
            ("d", d, stderr["d"]),
        )
    }
    constrained = any(ratio is not None and ratio > DEGENERACY_RATIO for ratio in ratios.values())
    # A narrow driving range makes a and d trade off against each other: the
    # residuals look tiny (so the standard errors look reassuring) while the
    # extrapolation is nonsense. This is the degeneracy that a pure SE criterion
    # cannot see, so it is checked explicitly.
    if driving_spread is not None and driving_spread < settings.min_driving_spread:
        constrained = True

    identifiability = Identifiability.IDENTIFIED
    if constrained:
        identifiability = Identifiability.CONSTRAINED
        reasons.append(ReasonCode.MODEL_NOT_IDENTIFIABLE)
    if k is None:
        if identifiability is Identifiability.IDENTIFIED:
            identifiability = Identifiability.K_NOT_IDENTIFIED
        reasons.append(ReasonCode.K_NOT_IDENTIFIED)

    rmse_observed = _rmse_observed(
        tin=tin, tout=tout, solar=solar, heating_kw=heating_kw, params=params, dt=dt
    )
    rmse_free_float = _rmse_free_float(
        tin=tin,
        tout=tout,
        solar=solar,
        params=params,
        dt=dt,
        busy=busy,
        solar_threshold=settings.solar_threshold,
        solar_series=solar,
    )

    return IdentificationResult(
        params=params,
        stderr=stderr,
        identifiability=identifiability,
        rmse_free_float=rmse_free_float,
        rmse_observed=rmse_observed,
        passive_windows=len(passive_rows),
        heating_windows=heating_windows,
        coverage=coverage,
        driving_spread=driving_spread,
        reasons=tuple(reasons),
    )


def _relative_spread(values: Sequence[float]) -> float | None:
    """Standard deviation over absolute mean: how much the regressor really varies."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean == 0:
        return None
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance) / abs(mean)


def _fit_solar(
    rows: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    a: float,
    d: float,
) -> tuple[float, float | None]:
    """Fit ``c_s`` on daylight passive windows, with ``a`` and ``d`` held fixed."""
    residual_targets: list[float] = []
    solar_values: list[float] = []
    for row, target in zip(rows, targets, strict=True):
        driving, _, mean_solar = row
        # model: y = -a·driving + c_s·solar + d  ->  y + a·driving − d = c_s·solar
        residual_targets.append(target + a * driving - d)
        solar_values.append(mean_solar)

    denominator = sum(value * value for value in solar_values)
    if denominator <= 0.0:
        return 0.0, None

    c_s = (
        sum(value * target for value, target in zip(solar_values, residual_targets, strict=True))
        / denominator
    )
    c_s = max(0.0, min(c_s, MAX_C_S))

    if len(solar_values) > 2:
        residuals = [
            target - c_s * value
            for value, target in zip(solar_values, residual_targets, strict=True)
        ]
        variance = sum(residual * residual for residual in residuals) / (len(residuals) - 1)
        stderr = math.sqrt(variance / denominator)
    else:
        stderr = None
    return c_s, stderr


def _fit_heating(
    rows: Sequence[Sequence[float]],
    targets: Sequence[float],
    powers: Sequence[float],
    *,
    a: float,
    d: float,
    c_s: float,
    min_windows: int,
) -> tuple[float | None, float | None, int]:
    """Estimate ``k`` by algebraic balance on heating windows (no regression)."""
    if len(rows) < min_windows:
        return None, None, len(rows)

    per_window: list[float] = []
    for row, target, power in zip(rows, targets, powers, strict=True):
        driving, _, mean_solar = row
        if power <= 0:
            continue
        per_window.append((target + a * (-driving) - c_s * mean_solar - d) / power)

    if not per_window:
        return None, None, 0

    k = sum(per_window) / len(per_window)
    if not 0.0 < k <= MAX_K:
        return None, None, len(per_window)

    if len(per_window) > 2:
        variance = sum((value - k) ** 2 for value in per_window) / (len(per_window) - 1)
        stderr: float | None = math.sqrt(variance / len(per_window))
    else:
        stderr = None
    return k, stderr, len(per_window)


def _step_inputs(
    tout: Sequence[float], solar: Sequence[float], heating_kw: Sequence[float], *, free_float: bool
) -> list[SimulationStep]:
    return [
        SimulationStep(
            text=tout[index],
            solar=solar[index],
            heating_kw=0.0 if free_float else heating_kw[index],
        )
        for index in range(len(tout) - 1)
    ]


def _first_present(tin: Sequence[float | None]) -> int | None:
    for index, value in enumerate(tin):
        if value is not None:
            return index
    return None


def _rmse_observed(
    *,
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float],
    params: ThermalParams,
    dt: float,
) -> float | None:
    """Full model with the observed power imposed: the real quality metric."""
    start = _first_present(tin)
    if start is None or start >= len(tin) - 1:
        return None

    steps = [
        SimulationStep(text=tout[index], solar=solar[index], heating_kw=heating_kw[index])
        for index in range(start, len(tin) - 1)
    ]
    try:
        trajectory = simulate(tin[start] or 0.0, steps, params, dt_hours=dt)  # type: ignore[arg-type]
    except ModelDivergedError:
        return None

    errors: list[float] = []
    for offset, predicted in enumerate(trajectory[1:]):
        measured = tin[start + offset + 1]
        if measured is not None:
            errors.append(predicted - measured)
    if not errors:
        return None
    return math.sqrt(sum(error * error for error in errors) / len(errors))


def _rmse_free_float(
    *,
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    params: ThermalParams,
    dt: float,
    busy: Sequence[bool],
    solar_threshold: float,
    solar_series: Sequence[float],
) -> float | None:
    """Free-float simulation compared only to genuinely passive steps.

    Comparing a no-heating simulation to a heated reality would produce a
    meaningless (and discouraging) error.
    """
    free = ThermalParams(a=params.a, c_s=params.c_s, d=params.d, k=None)
    errors: list[float] = []
    current: float | None = None
    for index in range(len(tin)):
        measured = tin[index]
        passive = not busy[index] and solar_series[index] < solar_threshold
        if measured is None or not passive:
            current = None
            continue
        if current is None:
            current = measured
            continue
        try:
            predicted = simulate(
                current,
                [SimulationStep(text=tout[index - 1], solar=solar[index - 1], heating_kw=0.0)],
                free,
                dt_hours=dt,
            )[-1]
        except ModelDivergedError:
            return None
        errors.append(predicted - measured)
        current = predicted
    if not errors:
        return None
    return math.sqrt(sum(error * error for error in errors) / len(errors))
