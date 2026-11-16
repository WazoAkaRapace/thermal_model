"""The second capacity: an ARX(2) view of the same house.

Why this module exists is written in the measurements, not in a paper: on the real
installation the passive hours lose -0.203 K/h between 16:00 and 18:00 (whole-house
mean of eight room sensors, so not a sensor artefact) against -0.163 K/h at night,
and smoothing the outdoor temperature over 2 to 6 hours does not absorb any of it
(RMSE flat at 0.195 K/h, evening residual pinned at -0.115 K/h). A single-capacity
model cannot hold two rhythms; a second mass (the envelope the sun charges by day
and the house gives back in the evening) can.

The discrete second-order form stays a linear regression, so the pure-Python
solver carries it without ceremony:

    Tin(t+1) = a1·Tin(t) + a2·Tin(t-1) + b·Text(t) + g1·S(t) + g2·S(t-1) + d + e·P(t)

The two time constants of the building are read from the roots of
x² - a1·x - a2: a fast one (the air) and a slow one (the mass). ``a2`` significantly
different from zero is the statistical signature of the second capacity; without it
this model has nothing over the 1R1C and says so.

Free-float coefficients (a1, a2, b, g1, g2, d) are fitted on passive steps only —
same discipline as the 1R1C passive fit, so the hot-water share of a dual-service
meter never enters. The heating coefficient e is estimated in a second stage on the
heating steps; if the meter carries hot water too, the contamination stops there,
in a coefficient the free-float prediction does not use.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .linalg import least_squares
from .passive import STANDBY_KW

__all__ = ["Arx2Fit", "fit_arx2", "simulate_arx2"]

MIN_STEPS = 100


@dataclass(frozen=True)
class Arx2Fit:
    """A house with an air capacity and a mass capacity."""

    a1: float
    a2: float
    b: float
    g1: float
    g2: float
    d: float
    e: float = 0.0
    stderrs: tuple[float | None, ...] = ()
    steps: int = 0
    heating_steps: int = 0
    rmse_k: float | None = None
    tau_fast_h: float | None = None
    tau_slow_h: float | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.message is None and self.steps >= MIN_STEPS

    @property
    def has_second_capacity(self) -> bool:
        """Whether a2 is more than two standard errors from zero."""
        if not self.ok or len(self.stderrs) < 2 or self.stderrs[1] is None:
            return False
        return abs(self.a2) > 2.0 * self.stderrs[1]


def _eigenvalue_check(a1: float, a2: float) -> tuple[float, float] | None:
    """The two discrete poles, as (fast, slow) time constants in hours — or None.

    Refused: complex poles (an RC network cannot oscillate — that is a fit on
    noise) and poles outside the unit circle (an unstable house). A small
    negative fast pole is ordinary for a discretised two-capacity network (the
    alternation of the fast mode); its time constant reads on the modulus.
    """
    discriminant = a1 * a1 + 4.0 * a2
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    lambdas = sorted(((a1 + root) / 2.0, (a1 - root) / 2.0), key=abs)
    taus = []
    for value in lambdas:
        if not abs(value) < 1.0:
            return None
        taus.append(-1.0 / math.log(abs(value)))
    return taus[0], taus[1]


def fit_arx2(
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float],
    *,
    standby_kw: float = STANDBY_KW,
) -> Arx2Fit:
    """Fit the six free-float coefficients on passive steps, then e on the rest."""
    length = len(tin)
    if not (len(tout) == len(solar) == len(heating_kw) == length):
        return Arx2Fit(0, 0, 0, 0, 0, 0, message="séries de longueurs différentes")

    rows: list[list[float]] = []
    targets: list[float] = []
    for index in range(1, length - 1):
        now, before, after = tin[index], tin[index - 1], tin[index + 1]
        if now is None or before is None or after is None:
            continue
        if heating_kw[index] > standby_kw or heating_kw[index + 1] > standby_kw:
            continue
        rows.append([now, before, tout[index], solar[index], solar[index - 1], 1.0])
        targets.append(after)

    if len(rows) < MIN_STEPS:
        return Arx2Fit(
            0,
            0,
            0,
            0,
            0,
            0,
            steps=len(rows),
            message=(f"seulement {len(rows)} étapes sans chauffe (il en faut {MIN_STEPS})"),
        )
    if not any(abs(row[3]) > 1e-9 for row in rows):
        return Arx2Fit(
            0,
            0,
            0,
            0,
            0,
            0,
            steps=len(rows),
            message="aucune irradiance exploitable : le soleil et la masse sont indissociables",
        )

    fit = least_squares(rows, targets)
    a1, a2, b, g1, g2, d = fit.params
    taus = _eigenvalue_check(a1, a2)
    if taus is None:
        return Arx2Fit(
            a1,
            a2,
            b,
            g1,
            g2,
            d,
            stderrs=fit.stderr,
            steps=len(rows),
            message="pôles non physiques (masses qui oscillent ou divergent)",
        )
    rmse = (fit.residual_sum_squares / len(rows)) ** 0.5

    # Second stage: the heating coefficient, on the steps where the meter runs.
    # Contamination of the meter (a dual-service heat pump's hot water) is
    # confined to e — the free-float prediction never reads it.
    heating_rows: list[tuple[float, float]] = []  # (power, residual)
    for index in range(1, length - 1):
        now, before, after = tin[index], tin[index - 1], tin[index + 1]
        if now is None or before is None or after is None:
            continue
        if heating_kw[index] <= standby_kw:
            continue
        predicted = (
            a1 * now + a2 * before + b * tout[index] + g1 * solar[index] + g2 * solar[index - 1] + d
        )
        heating_rows.append((heating_kw[index], after - predicted))
    e_value = 0.0
    if heating_rows:
        weight = sum(power for power, _ in heating_rows)
        if weight > 0:
            e_value = sum(power * resid for power, resid in heating_rows) / weight

    return Arx2Fit(
        a1=a1,
        a2=a2,
        b=b,
        g1=g1,
        g2=g2,
        d=d,
        e=e_value,
        stderrs=fit.stderr,
        steps=len(rows),
        heating_steps=len(heating_rows),
        rmse_k=rmse,
        tau_fast_h=taus[0],
        tau_slow_h=taus[1],
    )


def simulate_arx2(
    model: Arx2Fit,
    tin_now: float,
    tin_previous: float,
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float] | None = None,
) -> list[float]:
    """Roll the two-capacity house forward; heating_kw defaults to free float."""
    power = heating_kw or [0.0] * len(tout)
    trajectory = [tin_now]
    now, previous = tin_now, tin_previous
    for index in range(len(tout)):
        now, previous = (
            model.a1 * now
            + model.a2 * previous
            + model.b * tout[index]
            + model.g1 * solar[index]
            + model.g2 * (solar[index - 1] if index else 0.0)
            + model.d
            + model.e * power[index],
            now,
        )
        trajectory.append(now)
    return trajectory
