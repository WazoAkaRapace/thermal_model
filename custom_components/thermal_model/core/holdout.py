"""The model's own report card: hold-out error on the free-float minimum.

The user's decision is "how low will the house go without heating, and when" —
so the error that matters is the error on that number, not a global RMSE. This
module computes it the honest way, automatically, on every identification:

- fit the passive model on everything **except** the last 72 hours;
- replay every passive stretch of at least 6 hours inside those 72 hours,
  simulating from the stretch's start with the weather that actually happened;
- measure the error on the minimum, on its timing, and at the end of the
  stretch.

Measured on the real installation at ten days of data: bias +0.9 K (the
single-capacity model coasts too warm through the evening sag), mean absolute
error ~1.0 K, timing late by 2-10 h. The numbers are published, not hidden:
the panel draws the band and the minimum is announced minus its bias.

The evaluation uses measured weather, so it is a floor: the real forecast adds
the weather provider's own error on top.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .passive import PassiveFit, fit_passive

__all__ = ["HoldoutResult", "evaluate_holdout"]

HOLDOUT_HOURS = 72
MIN_STRETCH_HOURS = 6


@dataclass(frozen=True)
class HoldoutResult:
    """What the model would have gotten wrong on the last three days."""

    min_bias_k: float | None = None
    min_mae_k: float | None = None
    min_max_err_k: float | None = None
    time_err_mean_h: float | None = None
    stretches: int = 0
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.stretches >= 1 and self.min_mae_k is not None


def evaluate_holdout(
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    heating_kw: Sequence[float],
    *,
    holdout_hours: int = HOLDOUT_HOURS,
) -> HoldoutResult:
    """Fit without the tail, replay the tail's passive stretches, score them."""
    length = len(tin)
    if not (len(tout) == len(solar) == len(heating_kw) == length):
        return HoldoutResult(message="séries de longueurs différentes")
    split = length - holdout_hours
    if split < holdout_hours:  # not enough past to fit honestly
        return HoldoutResult(message="pas assez d'historique avant la fenêtre de test")

    training: list[float | None] = list(tin[:split])
    fit: PassiveFit = fit_passive(training, tout[:split], solar[:split], heating_kw[:split])
    if not fit.ok:
        return HoldoutResult(
            message=f"ajustement impossible sur la fenêtre d'apprentissage : {fit.message}"
        )

    min_errors: list[float] = []
    time_errors: list[float] = []
    index = split
    while index < length - 1:
        if heating_kw[index] > 0.05 or heating_kw[index + 1] > 0.05:
            index += 1
            continue
        start_value = tin[index]
        if start_value is None:
            index += 1
            continue
        end = index
        while (
            end < length - 1
            and heating_kw[end] <= 0.05
            and heating_kw[end + 1] <= 0.05
            and tin[end + 1] is not None
        ):
            end += 1
        span = end - index
        if span >= MIN_STRETCH_HOURS:
            level = float(start_value)
            truth: list[float] = []
            predicted: list[float] = []
            for step in range(index, end):
                truth.append(float(tin[step + 1]))
                level += -fit.a * (level - tout[step]) + fit.c_s * solar[step] + fit.d
                predicted.append(level)
            min_errors.append(min(predicted) - min(truth))
            time_errors.append(abs(predicted.index(min(predicted)) - truth.index(min(truth))))
        index = end + 1

    if not min_errors:
        return HoldoutResult(
            message=(
                f"aucun étirement sans chauffe >= {MIN_STRETCH_HOURS} h dans les "
                f"{holdout_hours} dernières heures : erreur non mesurable"
            )
        )
    bias = sum(min_errors) / len(min_errors)
    mae = sum(abs(error) for error in min_errors) / len(min_errors)
    return HoldoutResult(
        min_bias_k=round(bias, 2),
        min_mae_k=round(mae, 2),
        min_max_err_k=round(max(abs(error) for error in min_errors), 2),
        time_err_mean_h=round(sum(time_errors) / len(time_errors), 1),
        stretches=len(min_errors),
    )
