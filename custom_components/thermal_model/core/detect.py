"""Detect heating episodes without a power meter.

Radiators almost never publish their power as an entity — that is the common case,
not the exception. But the house itself is a sensor: when the heating runs at night,
the indoor temperature rises faster than the passive model allows. So instead of
reading the heating, we *infer* it and let the identifier work on the inferred series.

The scheme alternates between "given the heating, fit the house" and "given the house,
find the heating":

1. start with no heating assumed and fit ``a``, ``c_s``, ``d`` on every low-solar
   window (biased, because the real heating is inside them);
2. predict the passive rate step by step and mark the steps that rise above that
   prediction *at night*, where a rise has one plausible cause: no sun, occupants
   still;
3. refit with those steps marked as heating — the identifier already keeps windows
   containing heating out of the passive fit, so the fit stops being contaminated;
4. repeat until the mask stops moving.

The emitters are assumed to deliver ``capacity_kw`` while they run. That is the one
assumption the method needs, the user knows that number (nameplate), and it is
published with the result: ``k`` then reads "kelvin per kWh at that power".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .identify import (
    DEFAULT_SETTINGS,
    IdentificationError,
    IdentificationResult,
    IdentificationSettings,
    identify,
)

__all__ = ["HeatingDetection", "detect_heating"]

_LOGGER = logging.getLogger(__name__)

MIN_HEATING_STEPS = 6
MIN_PASSIVE_STEPS = 48
# How far above the passive prediction a step must rise to count, in K/h.
# A 2 kW emitter raises a house by roughly half a kelvin per hour; a tenth of that is
# already suspicious and two tenths is a real episode. Below this, model error and
# sensor noise are enough to invent heating that does not exist.
RESIDUAL_FLOOR = 0.15
RESIDUAL_SIGMAS = 2.0
# A night step must warm the house by this much before the detection considers it.
SEED_RISE = 0.02  # K/h
# What a real heating episode does to the house, and what it implies for k.
MIN_DETECTED_RATE = 0.1  # K/h
MIN_IMPLIED_K = 0.03  # K/kWh
MAX_IMPLIED_K = 3.0


@dataclass
class HeatingDetection:
    """The inferred heating series, and what the house looks like without it."""

    mask: list[float] = field(default_factory=list)  # 1.0 when heating, else 0.0
    result: IdentificationResult | None = None
    heating_steps: int = 0
    passive_steps: int = 0
    detected_rate: float = 0.0  # mean injected rate while heating, K/h
    rounds: int = 0
    capacity_kw: float = 0.0
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether heating episodes were found and the house fitted around them."""
        return self.result is not None and self.result.params is not None and not self.message

    @property
    def implied_k(self) -> float | None:
        """Heating gain implied by the detected rises, K/kWh."""
        if not self.capacity_kw:
            return None
        return self.detected_rate / self.capacity_kw

    def as_attributes(self) -> dict[str, object]:
        """Flatten for the entities that publish it."""
        return {
            "heating_hours_detected": self.heating_steps,
            "passive_hours": self.passive_steps,
            "assumed_capacity_kw": self.capacity_kw,
            "detected_rate_k_per_h": round(self.detected_rate, 4),
            "implied_k": None if self.implied_k is None else round(self.implied_k, 4),
            "detection_rounds": self.rounds,
            "detection_error": self.message,
        }


def _seed_mask(
    tin: Sequence[float | None],
    solar: Sequence[float],
    dt_hours: float,
    solar_threshold: float,
) -> list[float]:
    """First guess: the night steps that rise well above the falling ones."""
    rates: list[float] = []
    for index in range(len(tin) - 1):
        before, after = tin[index], tin[index + 1]
        rates.append(
            (after - before) / dt_hours if before is not None and after is not None else 0.0
        )
    night = [solar[index] <= solar_threshold for index in range(len(tin))]
    mask = [0.0] * len(tin)
    # At night, a free-floating house cools: a rise can only come from heating (or from
    # a house so airtight that its own gains beat its losses, which the refinement pass
    # then rejects because the passive fit stops making sense).
    for index in range(len(rates)):
        if night[index] and rates[index] > SEED_RISE:
            mask[index] = 1.0
    return mask


def _spread(values: Sequence[float]) -> float:
    """Standard deviation, without numpy."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def detect_heating(
    times: Sequence[datetime],
    tin: Sequence[float | None],
    tout: Sequence[float],
    solar: Sequence[float],
    *,
    capacity_kw: float,
    dt_hours: float = 1.0,
    settings: IdentificationSettings = DEFAULT_SETTINGS,
    rounds: int = 4,
    night_only: bool = True,
) -> HeatingDetection:
    """Infer the heating series from the temperature response itself."""
    length = len(times)
    if not (len(tin) == len(tout) == len(solar) == length):
        return HeatingDetection(message="séries de longueurs différentes")
    if length < MIN_PASSIVE_STEPS + MIN_HEATING_STEPS:
        return HeatingDetection(message="série trop courte pour détecter la chauffe")
    if capacity_kw <= 0:
        return HeatingDetection(message="capacité de chauffe invalide")

    # one entry per timestamp: mask[i] means "heating between i and i+1"
    mask = _seed_mask(tin, solar, dt_hours, settings.solar_threshold)
    result: IdentificationResult | None = None
    completed = 0
    injected: list[float] = []

    for round_index in range(rounds):
        completed = round_index + 1
        heating = [value * capacity_kw for value in mask]
        try:
            result = identify(times, tin, tout, solar, heating, settings=settings)
        except IdentificationError as err:
            return HeatingDetection(
                mask=mask,
                result=result,
                heating_steps=int(sum(mask)),
                capacity_kw=capacity_kw,
                rounds=completed,
                message=f"le régime libre ne se laisse pas ajuster : {err}",
            )

        params = result.params
        if params is None:
            return HeatingDetection(
                mask=mask,
                result=result,
                capacity_kw=capacity_kw,
                rounds=completed,
                message="aucun paramètre exploitable",
            )

        residuals: list[float | None] = []
        for index in range(length - 1):
            before, after = tin[index], tin[index + 1]
            if before is None or after is None:
                residuals.append(None)
                continue
            predicted = (
                -params.a * ((before + after) / 2.0 - tout[index])
                + params.c_s * solar[index]
                + params.d
            )
            residuals.append((after - before) / dt_hours - predicted)
        residuals.append(None)  # no step after the last sample

        calm = [value for value in residuals if value is not None and value <= RESIDUAL_FLOOR]
        threshold = max(RESIDUAL_FLOOR, RESIDUAL_SIGMAS * _spread(calm))

        updated: list[float] = []
        injected: list[float] = []
        for index in range(length):
            residual_value = residuals[index]
            night = solar[index] <= settings.solar_threshold
            # Two conditions, not one: above the passive prediction AND actually
            # warming. A step that merely cools slower than predicted is model error,
            # not heating — it is what produced phantom episodes in a house that only
            # ever loses heat.
            rising = (
                residual_value is not None
                and residual_value > threshold
                and tin[index] is not None
                and tin[index + 1] is not None
                and (tin[index + 1] - tin[index]) / dt_hours > SEED_RISE  # type: ignore[operator]
            )
            if rising and residual_value is not None and (night or not night_only):
                updated.append(1.0)
                injected.append(float(residual_value))
            else:
                updated.append(0.0)

        if updated == mask:
            break
        mask = updated

    heating_steps = int(sum(mask))
    passive_steps = length - heating_steps
    base = {
        "mask": mask,
        "result": result,
        "heating_steps": heating_steps,
        "passive_steps": passive_steps,
        "capacity_kw": capacity_kw,
        "rounds": completed,
    }
    if heating_steps < MIN_HEATING_STEPS:
        return HeatingDetection(
            **base,
            message=(
                f"{heating_steps} heure(s) de chauffe détectée(s), il en faut "
                f"{MIN_HEATING_STEPS} : aucune montée nocturne au-dessus du régime libre"
            ),
        )
    if passive_steps < MIN_PASSIVE_STEPS:
        return HeatingDetection(
            **base,
            message=(
                f"seulement {passive_steps} heure(s) sans chauffe, il en faut "
                f"{MIN_PASSIVE_STEPS} pour ajuster la maison"
            ),
        )

    detected_rate = (sum(injected) / len(injected)) if injected else 0.0
    implied = detected_rate / capacity_kw
    if detected_rate < MIN_DETECTED_RATE:
        return HeatingDetection(
            **base,
            detected_rate=detected_rate,
            message=(
                f"montée détectée trop faible ({detected_rate:.3f} K/h) pour être une "
                "chauffe : le bruit du modèle suffit à l'expliquer"
            ),
        )
    if not MIN_IMPLIED_K <= implied <= MAX_IMPLIED_K:
        return HeatingDetection(
            **base,
            detected_rate=detected_rate,
            message=(
                f"gain de chauffe implicite {implied:.2f} K/kWh hors de la plage "
                f"physique [{MIN_IMPLIED_K}, {MAX_IMPLIED_K}] avec "
                f"{capacity_kw:.1f} kW supposés"
            ),
        )

    return HeatingDetection(**base, detected_rate=detected_rate)
