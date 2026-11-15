"""Identify the heating gain from daily energy, not from hourly power.

Hourly identification of ``k`` needs two things a real house often refuses to provide:
a power meter on the heating, and enough hours where the house free-floats so that the
other coefficients are pinned without it. A daily energy balance needs neither.

Per day, with ``a``, ``c_s`` and ``d`` already identified:

    ΔTin(day) = -a·Σ(Tin - Tout) + c_s·ΣS + 24·d + k·E(day)

Everything but ``k`` is known, so one day gives one equation and three weeks of days
give a one-parameter regression. Daily aggregation is not a compromise here: it removes
the hourly noise that makes the hourly estimate fragile, and it matches what a heating
meter actually measures — kilowatt-hours per day.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .linalg import least_squares
from .simulate import ThermalParams

__all__ = ["DailyKResult", "DailyModelResult", "fit_daily_model", "identify_k_from_daily_energy"]

_LOGGER = logging.getLogger(__name__)

MIN_DAYS = 8
MIN_DAILY_KWH = 0.5


@dataclass
class DailyKResult:
    """The heating gain and how well the days agree with it."""

    k: float | None = None
    stderr: float | None = None
    days: int = 0
    total_kwh: float = 0.0
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether k came out with a usable uncertainty."""
        return self.k is not None and self.message is None

    @property
    def relative_error(self) -> float | None:
        """Standard error relative to the estimate."""
        if self.k is None or self.stderr is None or self.k == 0:
            return None
        return abs(self.stderr / self.k)


def identify_k_from_daily_energy(
    times: Sequence[datetime],
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    energy_kwh: Sequence[float | None],
    params: ThermalParams,
    *,
    dt_hours: float = 1.0,
    min_days: int = MIN_DAYS,
) -> DailyKResult:
    """Fit ``k`` on daily energy and daily temperature change."""
    length = len(times)
    if not (len(tin) == len(tout) == len(solar) == len(energy_kwh) == length):
        return DailyKResult(message="séries de longueurs différentes")
    if length < 24:
        return DailyKResult(message="moins d'une journée de données")

    passive: dict[object, float] = defaultdict(float)
    observed: dict[object, float] = defaultdict(float)
    energy: dict[object, float] = defaultdict(float)
    hours: dict[object, int] = defaultdict(int)

    for index in range(length - 1):
        before, after = tin[index], tin[index + 1]
        if before is None or after is None:
            continue
        day = times[index].date()
        mean_inside = (before + after) / 2.0
        passive[day] += (
            -params.a * (mean_inside - tout[index]) + params.c_s * solar[index] + params.d
        ) * dt_hours
        observed[day] += after - before
        energy[day] += float(energy_kwh[index] or 0.0)
        hours[day] += 1

    usable: list[tuple[float, float]] = []
    for day in sorted(passive, key=str):
        if hours[day] < 12:  # a partial day would bias the balance
            continue
        if energy[day] < MIN_DAILY_KWH:
            continue
        residual = observed[day] - passive[day]
        usable.append((energy[day], residual))

    if len(usable) < min_days:
        return DailyKResult(
            days=len(usable),
            total_kwh=sum(day_energy for day_energy, _ in usable),
            message=(
                f"{len(usable)} jour(s) avec chauffage mesuré, il en faut {min_days} "
                "(journées complètes, au moins "
                f"{MIN_DAILY_KWH} kWh de chauffe)"
            ),
        )

    numerator = sum(day_energy * residual for day_energy, residual in usable)
    denominator = sum(day_energy * day_energy for day_energy, _ in usable)
    if denominator <= 0:
        return DailyKResult(days=len(usable), message="aucune énergie de chauffe exploitable")
    k = numerator / denominator

    # Standard error of a one-parameter least squares fit.
    residuals = [residual - k * day_energy for day_energy, residual in usable]
    variance = sum(value * value for value in residuals) / max(1, len(residuals) - 1)
    stderr = (variance / denominator) ** 0.5
    total_kwh = sum(day_energy for day_energy, _ in usable)

    if k <= 0:
        return DailyKResult(
            k=k,
            stderr=stderr,
            days=len(usable),
            total_kwh=total_kwh,
            message=(
                "le gain de chauffe ressort négatif : l'énergie mesurée n'explique pas la montée"
            ),
        )
    if k is not None and stderr > 0.5 * k:
        return DailyKResult(
            k=k,
            stderr=stderr,
            days=len(usable),
            total_kwh=total_kwh,
            message=(
                f"k = {k:.3f} ± {stderr:.3f} K/kWh : l'incertitude dépasse la moitié de "
                "la valeur, il faut plus de journées"
            ),
        )

    _LOGGER.debug(
        "Thermal Model: k = %.4f ± %.4f K/kWh on %s days (%.1f kWh)",
        k,
        stderr,
        len(usable),
        total_kwh,
    )
    return DailyKResult(k=k, stderr=stderr, days=len(usable), total_kwh=total_kwh)


@dataclass
class DailyModelResult:
    """A model fitted on daily aggregates alone."""

    params: ThermalParams | None = None
    stderrs: tuple[float, ...] = ()
    days: int = 0
    total_kwh: float = 0.0
    r_squared: float | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether all four coefficients came out of the balance."""
        return self.params is not None and self.message is None


def fit_daily_model(
    times: Sequence[datetime],
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    energy_kwh: Sequence[float | None],
    *,
    dt_hours: float = 1.0,
    min_days: int = 12,
) -> DailyModelResult:
    """Fit a, c_s, d AND k on daily aggregates.

    Four unknowns, one equation per day. This is what an installation with no power
    meter at all needs: the hourly fit cannot separate the wall loss from the heating
    when the heating is never observed, but three weeks of daily balances can, because
    the days differ in weather and in energy.
    """
    length = len(times)
    if not (len(tin) == len(tout) == len(solar) == len(energy_kwh) == length):
        return DailyModelResult(message="séries de longueurs différentes")

    sums_gap: dict[object, float] = defaultdict(float)
    sums_solar: dict[object, float] = defaultdict(float)
    sums_energy: dict[object, float] = defaultdict(float)
    observed: dict[object, float] = defaultdict(float)
    hours: dict[object, int] = defaultdict(int)

    for index in range(length - 1):
        before, after = tin[index], tin[index + 1]
        if before is None or after is None:
            continue
        day = times[index].date()
        sums_gap[day] += ((before + after) / 2.0 - tout[index]) * dt_hours
        sums_solar[day] += solar[index] * dt_hours
        sums_energy[day] += float(energy_kwh[index] or 0.0)
        observed[day] += after - before
        hours[day] += 1

    rows: list[list[float]] = []
    targets: list[float] = []
    total_kwh = 0.0
    for day in sorted(sums_gap, key=str):
        if hours[day] < 12:
            continue
        rows.append(
            [-sums_gap[day], sums_solar[day], float(hours[day]) * dt_hours, sums_energy[day]]
        )
        targets.append(observed[day])
        total_kwh += sums_energy[day]

    if len(rows) < min_days:
        return DailyModelResult(
            days=len(rows),
            total_kwh=total_kwh,
            message=(
                f"{len(rows)} journée(s) complète(s), il en faut {min_days} pour ajuster "
                "les quatre coefficients"
            ),
        )

    # A column that is identically zero (no irradiance sensor, no sun in the window)
    # makes the normal equations singular. Drop it and say so instead of failing: the
    # solar coefficient is then simply not identified by this data.
    active = [column for column in range(4) if any(abs(row[column]) > 1e-9 for row in rows)]
    kept = 3 in active
    reduced = [[row[column] for column in active] for row in rows]
    try:
        fit = least_squares(reduced, targets) if active else None
    except (ValueError, ZeroDivisionError) as err:
        return DailyModelResult(days=len(rows), total_kwh=total_kwh, message=str(err))
    if fit is None:
        return DailyModelResult(
            days=len(rows), total_kwh=total_kwh, message="aucune donnée exploitable"
        )

    solution = [0.0] * 4
    errors = [0.0] * 4
    for position, column in enumerate(active):
        solution[column] = fit.params[position]
        errors[column] = fit.stderr[position] or 0.0
    a, c_s, d, k = solution
    if not kept:
        return DailyModelResult(
            days=len(rows),
            total_kwh=total_kwh,
            message="aucune énergie de chauffe dans la fenêtre",
        )
    fit_stderr = tuple(errors)
    fit_r2 = fit.r_squared
    k_err = errors[3]
    if a <= 0:
        return DailyModelResult(
            days=len(rows),
            total_kwh=total_kwh,
            message=f"déperdition non identifiable au pas journalier (a = {a:.4f})",
        )
    if k <= 0:
        return DailyModelResult(
            days=len(rows),
            total_kwh=total_kwh,
            message=(
                "gain de chauffe négatif ou nul : l'énergie mesurée n'explique pas "
                "les variations quotidiennes"
            ),
        )
    stderrs = fit_stderr
    if k_err > 0.5 * k:
        return DailyModelResult(
            params=ThermalParams(a=a, c_s=max(0.0, c_s), d=d, k=k),
            stderrs=stderrs,
            days=len(rows),
            total_kwh=total_kwh,
            r_squared=fit_r2,
            message=(
                f"k = {k:.3f} ± {k_err:.3f} K/kWh : incertitude trop grande, "
                "il faut plus de journées"
            ),
        )

    return DailyModelResult(
        params=ThermalParams(a=a, c_s=max(0.0, c_s), d=d, k=k),
        stderrs=stderrs,
        days=len(rows),
        total_kwh=total_kwh,
        r_squared=fit_r2,
    )
