"""The passive fit, on a house whose meter mixes heating and hot water.

The regression that motivated it: on a real dual-service heat pump, fitting the house
on night steps alone published d = +0.061 K/h — a free-float lift of +4.8 K the house
never had — because the constant hot-water share of the meter is indistinguishable
from internal gains on night data. The passive fit measures the house only where the
meter sits at standby, day and night, so the hot water never enters and the daytime
solar rise separates c_s from d.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from custom_components.thermal_model.core.passive import fit_passive

A_TRUE = 0.011
C_S_TRUE = 0.002
D_TRUE = 0.02
CAPACITY = 1.8  # kW electrical when the heat pump runs
ECS_KW = 0.35  # constant hot-water share, on the same meter


def _dual_service_house(days: int = 21, seed: int = 5) -> dict[str, list]:
    """A house heated in bursts, whose meter also carries a hot-water load."""
    random.seed(seed)
    hours = days * 24
    temperature, weather = 20.0, 6.0
    tin: list[float] = []
    tout: list[float] = []
    solar: list[float] = []
    meter: list[float] = []
    for index in range(hours):
        hour = index % 24
        weather += random.gauss(0, 0.2)
        outside = weather + 4.0 * math.sin(2 * math.pi * hour / 24)
        sun = (
            max(0.0, 450.0 * random.uniform(0.3, 1.0) * math.sin(math.pi * (hour - 7.5) / 9))
            if 7.5 <= hour <= 16.5
            else 0.0
        )
        heating = (hour >= 22 or hour < 6) and temperature < 20.4
        ecs = hour in (5, 6, 7, 20, 21)  # morning and evening hot water
        rate = (
            -A_TRUE * (temperature - outside)
            + C_S_TRUE * sun
            + D_TRUE
            + (0.28 if heating else 0.0)  # k_true * CAPACITY
            + random.gauss(0, 0.01)
        )
        tin.append(round(temperature, 3))
        tout.append(round(outside, 3))
        solar.append(sun)
        meter.append(round((CAPACITY if heating else 0.0) + (ECS_KW if ecs else 0.0), 3))
        temperature += rate
    return {"tin": tin, "tout": tout, "solar": solar, "meter": meter}


def test_the_passive_fit_recovers_the_house_despite_the_hot_water_share() -> None:
    """Standby steps contain no hot water, so nothing is left to absorb it."""
    house = _dual_service_house()
    fit = fit_passive(house["tin"], house["tout"], house["solar"], house["meter"])

    assert fit.ok, fit.message
    assert fit.steps >= 200, "three weeks must leave hundreds of standby steps"
    assert abs(fit.a - A_TRUE) < 0.002, f"a = {fit.a}"
    assert abs(fit.c_s - C_S_TRUE) < 0.0004, f"c_s = {fit.c_s}"
    assert abs(fit.d - D_TRUE) < 0.015, f"d = {fit.d} (hot water must not land here)"
    assert abs(fit.free_float_lift_k - D_TRUE / A_TRUE) < 1.5


def test_the_passive_fit_refuses_when_the_house_never_stands_by() -> None:
    """A meter that never rests cannot yield a free float: say so, publish nothing."""
    house = _dual_service_house()
    always_on = [max(value, 0.4) for value in house["meter"]]
    fit = fit_passive(house["tin"], house["tout"], house["solar"], always_on)

    assert not fit.ok
    assert fit.message is not None
    assert "sans chauffe" in fit.message


def test_a_window_without_sun_refuses_instead_of_blaming_internal_gains() -> None:
    """A dead solar column must refuse: fitted anyway, the daytime rise lands in d.

    Measured on the synthetic house: d jumps to +0.33 K/h and a follows, because the
    unmodelled sun is indistinguishable from free warmth.
    """
    house = _dual_service_house()
    fit = fit_passive(house["tin"], house["tout"], [0.0] * len(house["tin"]), house["meter"])

    assert not fit.ok
    assert fit.message is not None
    assert "irradiance" in fit.message


def test_the_observed_rates_need_no_model() -> None:
    """Sanity check on the generator itself: standby nights cool, middays warm."""
    house = _dual_service_house()
    night = [
        house["tin"][i + 1] - house["tin"][i]
        for i in range(len(house["tin"]) - 1)
        if house["meter"][i] == 0.0 and house["meter"][i + 1] == 0.0 and i % 24 in (0, 1, 2, 3)
    ]
    midday = [
        house["tin"][i + 1] - house["tin"][i]
        for i in range(len(house["tin"]) - 1)
        if house["meter"][i] == 0.0 and house["meter"][i + 1] == 0.0 and i % 24 in (12, 13, 14)
    ]
    assert sum(night) / len(night) < -0.05, "nights cool down without heating"
    assert sum(midday) / len(midday) > 0.1, "midday warms up on solar alone"
    _ = datetime.now(UTC)  # timedelta imported for the module's other users
