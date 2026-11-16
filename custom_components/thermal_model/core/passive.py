"""The passive fit: the house measured while nothing heats it.

This is the part of the model the user actually relies on — the free-float trajectory
that answers "what is the lowest temperature the house will reach, and when" — so its
coefficients come from free-float data only:

- a passive step is one where the heating meter sits at standby, on the hour *and* on
  the next hour (a burst starting mid-step would contaminate the rate);
- daytime passive steps are included on purpose: the solar rise they contain is what
  separates ``c_s`` from ``d``. Fitting ``d`` on night steps alone is degenerate
  (the indoor-outdoor gap barely moves at night) and is exactly how ``d`` came to
  absorb the constant ECS share of a dual-service heat pump — measured on a real
  installation: published d = +0.061 K/h (a free lift of +4.8 K over the outdoor
  temperature) against +0.021 ± 0.044 on passive steps, i.e. indistinguishable from
  zero.

Measured on the same real data, the joint fit over day and night passive steps gives
a = 0.0111 ± 0.003 1/h, c_s = 0.00213 ± 0.00025 (significant), and predicts the
observed hourly rates with a median error of 0.08 K/h.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .linalg import least_squares

__all__ = ["PassiveFit", "fit_passive"]

_LOGGER = logging.getLogger(__name__)

# Below this the heating is at standby. Measured on a real installation: the heat
# pump sits at 2 W for 75-80 % of the hours and runs in ~0.9 kW bursts, so anything
# under 50 W is unambiguously "not heating" — and "not producing hot water either",
# which is the point.
STANDBY_KW = 0.05
MIN_PASSIVE_STEPS = 48


@dataclass(frozen=True)
class PassiveFit:
    """The house as it behaves when nothing heats it."""

    a: float
    c_s: float
    d: float
    stderrs: tuple[float | None, ...] = ()
    steps: int = 0
    rmse_k_per_h: float | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the three coefficients are usable."""
        return self.message is None and self.steps >= MIN_PASSIVE_STEPS

    @property
    def free_float_lift_k(self) -> float:
        """How far above the outdoor temperature the house settles without heating."""
        return self.d / self.a


def fit_passive(
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float],
    *,
    standby_kw: float = STANDBY_KW,
) -> PassiveFit:
    """Fit a, c_s and d on the steps where the heating meter sits at standby."""
    length = len(tin)
    if not (len(tout) == len(solar) == len(heating_kw) == length):
        return PassiveFit(a=0.0, c_s=0.0, d=0.0, message="séries de longueurs différentes")

    rows: list[list[float]] = []
    targets: list[float] = []
    for index in range(length - 1):
        before, after = tin[index], tin[index + 1]
        if before is None or after is None:
            continue
        if heating_kw[index] > standby_kw or heating_kw[index + 1] > standby_kw:
            continue
        rows.append([-((before + after) / 2.0 - tout[index]), solar[index], 1.0])
        targets.append(after - before)

    if len(rows) < MIN_PASSIVE_STEPS:
        return PassiveFit(
            a=0.0,
            c_s=0.0,
            d=0.0,
            steps=len(rows),
            message=(
                f"seulement {len(rows)} étapes sans chauffe (il en faut "
                f"{MIN_PASSIVE_STEPS}) : impossible d'ajuster le régime libre"
            ),
        )

    # Without irradiance the daytime solar rise lands in d (measured: d = +0.33 K/h
    # on a synthetic house with a dead solar column) and a follows it up. Refusing is
    # honest; publishing those numbers is not.
    if not any(abs(row[1]) > 1e-9 for row in rows):
        return PassiveFit(
            a=0.0,
            c_s=0.0,
            d=0.0,
            steps=len(rows),
            message=(
                "aucune irradiance exploitable : impossible de séparer le soleil "
                "des gains internes sur le régime libre"
            ),
        )

    fit = least_squares(rows, targets)
    a, c_s, d = fit.params
    if a <= 0:
        return PassiveFit(
            a=a,
            c_s=c_s,
            d=d,
            stderrs=fit.stderr,
            steps=len(rows),
            message=f"déperditions non physiques (a = {a:.4f})",
        )
    rmse = (fit.residual_sum_squares / len(rows)) ** 0.5
    _LOGGER.debug(
        "Thermal Model: passive fit a=%.5f c_s=%.5f d=%+.4f on %s steps (rmse %.3f K/h)",
        a,
        c_s,
        d,
        len(rows),
        rmse,
    )
    return PassiveFit(a=a, c_s=c_s, d=d, stderrs=fit.stderr, steps=len(rows), rmse_k_per_h=rmse)
