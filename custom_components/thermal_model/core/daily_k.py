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

from .simulate import ThermalParams

__all__ = ["DailyKResult", "identify_k_from_daily_energy"]

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
