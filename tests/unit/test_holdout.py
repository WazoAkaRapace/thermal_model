"""The model grades itself: hold-out error on the free-float minimum."""

from __future__ import annotations

import math
import random

from custom_components.thermal_model.core.holdout import evaluate_holdout

A_TRUE = 0.011
C_S_TRUE = 0.0015
D_TRUE = 0.02


def _house(days: int = 10, seed: int = 3, evening_sag: float = 0.0) -> dict[str, list]:
    """A passive-heavy house; ``evening_sag`` adds the 16-18h extra cooling."""
    random.seed(seed)
    hours = days * 24
    temperature, weather = 20.0, 7.0
    tin, tout, solar, power = [], [], [], []
    for index in range(hours):
        hour = index % 24
        weather += random.gauss(0, 0.2)
        outside = weather + 4.0 * math.sin(2 * math.pi * (hour - 14) / 24)
        sun = max(0.0, 350.0 * math.sin(math.pi * (hour - 7.5) / 9)) if 7.5 <= hour <= 16.5 else 0.0
        heating = 5 <= hour < 8 and temperature < 20.3  # morning boost only
        sag = evening_sag if 16 <= hour <= 18 else 0.0
        tin.append(round(temperature, 3))
        tout.append(round(outside, 3))
        solar.append(sun)
        power.append(0.9 if heating else 0.0)
        temperature += (
            -A_TRUE * (temperature - outside)
            + C_S_TRUE * sun
            + D_TRUE
            + (1.8 if heating else 0.0)
            - sag
            + random.gauss(0, 0.01)
        )
    return {"tin": tin, "tout": tout, "solar": solar, "power": power}


def test_a_perfect_model_reports_a_small_error() -> None:
    house = _house()
    result = evaluate_holdout(house["tin"], house["tout"], house["solar"], house["power"])

    assert result.ok, result.message
    assert result.stretches >= 1
    assert result.min_bias_k is not None and abs(result.min_bias_k) < 0.3, (
        f"biais {result.min_bias_k}"
    )
    assert result.min_mae_k is not None and result.min_mae_k < 0.3, f"mae {result.min_mae_k}"


def test_the_evening_sag_is_reported_as_a_warm_bias() -> None:
    """One capacity cannot hold the 16-18h sag: the grade must show it.

    This is the measured behaviour of the real installation: the hold-out
    replay on ten days of data reported +0.9 K of warm bias.
    """
    house = _house(evening_sag=0.14)
    result = evaluate_holdout(house["tin"], house["tout"], house["solar"], house["power"])

    assert result.ok, result.message
    # On the synthetic house the sag costs +0.11 K of warm bias and ~6 h of
    # timing error; on the real installation the same protocol measured +0.90 K.
    assert result.min_bias_k is not None and result.min_bias_k > 0.05, (
        f"le biais chaud attendu n'est pas rapporté: {result.min_bias_k}"
    )


def test_no_passive_stretch_means_no_grade_not_a_fake_one() -> None:
    house = _house()
    always_on = [0.6] * len(house["power"])  # heating never stops
    result = evaluate_holdout(house["tin"], house["tout"], house["solar"], always_on)

    assert not result.ok
    assert result.message is not None


def test_a_short_history_refuses_instead_of_grading_itself() -> None:
    house = _house(days=4)
    result = evaluate_holdout(house["tin"], house["tout"], house["solar"], house["power"])

    assert not result.ok
    assert result.message is not None
