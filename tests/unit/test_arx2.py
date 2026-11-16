"""The ARX(2) fit on a house that really has two capacities.

The generator integrates a two-capacity network (air + envelope mass, the sun
charging the mass by day) so the test asserts recovery of physical behaviour, not
of the generator's algebra: a fast pole for the air, a slow pole for the mass, an
a2 significantly away from zero, and a free-float simulation that beats the 1R1C
on the same synthetic house.
"""

from __future__ import annotations

import math
import random

from custom_components.thermal_model.core.arx2 import fit_arx2, simulate_arx2
from custom_components.thermal_model.core.passive import fit_passive

# A mid-weight house: air exchanges with the mass in ~5 h, the mass with the
# outside in ~60 h, the air with the outside in ~30 h.
RI_M_H = 5.0  # tau air<->mass
RE_H = 30.0  # tau air<->outside
REM_H = 90.0  # tau mass<->outside
D_TRUE = 0.02  # constant internal gains, K/h
E_TRUE = 2.4  # K per kW of metered heating


def _two_capacity_house(days: int = 30, seed: int = 7) -> dict[str, list]:
    random.seed(seed)
    hours = days * 24
    ti, tm, weather = 20.0, 19.5, 7.0
    tin: list[float] = []
    tout: list[float] = []
    solar: list[float] = []
    meter: list[float] = []
    for index in range(hours):
        hour = index % 24
        weather += random.gauss(0, 0.15)
        outside = weather + 4.0 * math.sin(2 * math.pi * (hour - 14) / 24)
        sun = (
            max(0.0, 400.0 * random.uniform(0.4, 1.0) * math.sin(math.pi * (hour - 7.5) / 9))
            if 7.5 <= hour <= 16.5
            else 0.0
        )
        heating = (hour >= 22 or hour < 6) and ti < 20.3
        meter.append(0.8 if heating else 0.0)
        tin.append(round(ti, 4))
        tout.append(round(outside, 4))
        solar.append(sun)
        # sub-stepped integration of the two coupled capacities
        for _ in range(12):
            flow_air_mass = (tm - ti) / RI_M_H
            flow_air_out = (outside - ti) / RE_H
            flow_mass_out = (outside - tm) / REM_H
            gain_air = 0.0035 * sun + D_TRUE + (E_TRUE * 0.8 if heating else 0.0)
            gain_mass = 0.0020 * sun
            ti += (flow_air_mass + flow_air_out + gain_air + random.gauss(0, 0.004)) / 12
            tm += (-flow_air_mass + flow_mass_out + gain_mass) / 12
    return {"tin": tin, "tout": tout, "solar": solar, "meter": meter}


def test_the_second_capacity_is_detected_and_the_poles_are_physical() -> None:
    house = _two_capacity_house()
    fit = fit_arx2(house["tin"], house["tout"], house["solar"], house["meter"])

    assert fit.ok, fit.message
    assert fit.has_second_capacity, f"a2 = {fit.a2} (seconde capacité invisible)"
    assert fit.tau_fast_h is not None and fit.tau_slow_h is not None
    # Hourly sampling resolves one dominant pole plus a fast alternation mode:
    # the slow pole must carry the air-mass dialogue (hours to a day), the fast
    # one must be clearly quicker, and they must be separated.
    assert 4.0 < fit.tau_slow_h < 40.0, f"tau_slow = {fit.tau_slow_h}"
    assert fit.tau_fast_h < 5.0, f"tau_fast = {fit.tau_fast_h}"
    assert fit.tau_slow_h > 3 * fit.tau_fast_h


def test_the_arx2_forecast_beats_the_1r1c_on_a_two_capacity_house() -> None:
    house = _two_capacity_house()
    arx = fit_arx2(house["tin"], house["tout"], house["solar"], house["meter"])
    one = fit_passive(house["tin"], house["tout"], house["solar"], house["meter"])
    assert arx.ok and one.ok

    # Walk the last three days in free float (perfect weather, no heating) and
    # score the daily minimum — the number the integration exists to predict.
    horizon = 72
    start = len(house["tin"]) - horizon - 2
    errors_arx, errors_1r1c = [], []
    for day in range(3):
        t0 = start + day * 24
        truth = house["tin"][t0 + 1 : t0 + 25]
        weather = house["tout"][t0 + 1 : t0 + 25]
        sun = house["solar"][t0 + 1 : t0 + 25]

        sim = simulate_arx2(arx, house["tin"][t0], house["tin"][t0 - 1], weather, sun)
        errors_arx.append(min(sim[1:]) - min(truth))

        level = house["tin"][t0]
        sim1 = [level]
        for index in range(24):
            level += -one.a * (level - weather[index]) + one.c_s * sun[index] + one.d
            sim1.append(level)
        errors_1r1c.append(min(sim1[1:]) - min(truth))

    mean_arx = sum(abs(e) for e in errors_arx) / len(errors_arx)
    mean_1r1c = sum(abs(e) for e in errors_1r1c) / len(errors_1r1c)
    assert mean_arx < mean_1r1c, (
        f"erreur |min| arx2 = {mean_arx:.2f} K contre 1r1c = {mean_1r1c:.2f} K"
    )


def test_a_house_with_one_capacity_reports_no_second_capacity() -> None:
    """On a genuinely 1R1C house a2 must fold back to zero within noise."""
    random.seed(11)
    hours = 30 * 24
    ti, weather = 20.0, 7.0
    tin, tout, solar, meter = [], [], [], []
    for index in range(hours):
        hour = index % 24
        weather += random.gauss(0, 0.15)
        outside = weather + 4.0 * math.sin(2 * math.pi * (hour - 14) / 24)
        sun = max(0.0, 400.0 * math.sin(math.pi * (hour - 7.5) / 9)) if 7.5 <= hour <= 16.5 else 0.0
        heating = (hour >= 22 or hour < 6) and ti < 20.3
        meter.append(0.8 if heating else 0.0)
        tin.append(ti)
        tout.append(outside)
        solar.append(sun)
        ti += (
            -0.011 * (ti - outside)
            + 0.002 * sun
            + 0.02
            + (2.0 * 0.8 if heating else 0.0)
            + random.gauss(0, 0.01)
        )
    fit = fit_arx2(tin, tout, solar, meter)

    assert fit.ok, fit.message
    assert not fit.has_second_capacity, (
        f"a2 = {fit.a2} détectée à tort sur une maison à une seule capacité"
    )


def test_too_few_passive_steps_refuses() -> None:
    house = _two_capacity_house(days=3)
    fit = fit_arx2(house["tin"], house["tout"], house["solar"], house["meter"])

    assert not fit.ok
    assert fit.message is not None
    assert "sans chauffe" in fit.message
