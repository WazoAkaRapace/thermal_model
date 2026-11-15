"""The heating gain from daily energy, and the night-time heating detection.

Two paths that exist because a radiator never publishes its power as an entity:

- the daily energy balance, which needs a kWh counter (usually present on a meter
  even when the power entity is not) and no free-float at all;
- the night-time detection, which infers the heating from the temperature response and
  needs the house to cool down between cycles.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from custom_components.thermal_model.core.daily_k import identify_k_from_daily_energy
from custom_components.thermal_model.core.detect import detect_heating
from custom_components.thermal_model.core.simulate import ThermalParams

START = datetime(2026, 10, 26)


def _house(
    *,
    days: int = 21,
    a: float = 0.02,
    c_s: float = 0.001,
    d: float = 0.015,
    k: float = 0.30,
    capacity_kw: float = 2.0,
    night_heating: bool = True,
    weather_start: float = 2.0,
    seed: int = 3,
) -> dict[str, list]:
    """A house with a drifting weather and a night heating schedule."""
    random.seed(seed)
    hours = days * 24
    temperature, weather = 20.0, weather_start
    tin: list[float] = []
    tout: list[float] = []
    solar: list[float] = []
    energy: list[float] = []
    pattern: list[bool] = []
    for index in range(hours):
        hour = index % 24
        weather += random.gauss(0, 0.15)
        outside = weather + 4.0 * math.sin(2 * math.pi * hour / 24)
        sun = (
            max(0.0, 500.0 * random.uniform(0.3, 1.0) * math.sin(math.pi * (hour - 7.5) / 9))
            if 7.5 <= hour <= 16.5
            else 0.0
        )
        on = night_heating and (hour >= 22 or hour < 6) and temperature < 20.8
        rate = (
            -a * (temperature - outside)
            + c_s * sun
            + d
            + (k * capacity_kw if on else 0.0)
            + random.gauss(0, 0.008)
        )
        tin.append(round(temperature, 3))
        tout.append(round(outside, 3))
        solar.append(sun)
        energy.append(capacity_kw if on else 0.0)
        pattern.append(on)
        temperature += rate
    return {
        "times": [START + timedelta(hours=index) for index in range(hours)],
        "tin": tin,
        "tout": tout,
        "solar": solar,
        "energy": energy,
        "heating": pattern,
    }


def test_daily_energy_recovers_k_without_any_free_float() -> None:
    """Three weeks of kWh per day are enough — and this is the path a radiator allows."""
    house = _house()
    result = identify_k_from_daily_energy(
        house["times"],
        house["tin"],
        house["tout"],
        house["solar"],
        house["energy"],
        ThermalParams(a=0.02, c_s=0.001, d=0.015, k=None),
    )

    assert result.ok
    assert result.days >= 18
    assert result.k is not None
    assert abs(result.k - 0.30) < 0.03, f"k = {result.k}"
    assert result.stderr is not None and result.stderr < 0.03
    assert result.total_kwh > 100


def test_daily_energy_refuses_without_energy() -> None:
    """No counter means no k, and the reason must say so."""
    house = _house()
    result = identify_k_from_daily_energy(
        house["times"],
        house["tin"],
        house["tout"],
        house["solar"],
        [0.0] * len(house["times"]),
        ThermalParams(a=0.02, c_s=0.001, d=0.015, k=None),
    )

    assert not result.ok
    assert result.message is not None
    assert "kWh" in result.message or "jour" in result.message


def test_daily_energy_refuses_a_nonsense_energy_series() -> None:
    """Energy that does not follow the temperature must not produce a k."""
    house = _house()
    # a house heated during the day only, while the temperature rises at night:
    # the balance cannot explain it and k must come out non-positive
    inverted = [
        value if index % 24 >= 8 and index % 24 < 18 else 0.0
        for index, value in enumerate(house["energy"])
    ]
    result = identify_k_from_daily_energy(
        house["times"],
        house["tin"],
        house["tout"],
        house["solar"],
        inverted,
        ThermalParams(a=0.02, c_s=0.001, d=0.015, k=None),
    )

    assert not result.ok or result.k is None or result.k <= 0


def test_night_detection_finds_the_heating_episodes() -> None:
    """The seed that makes the detection possible must work without any parameter."""
    house = _house()
    detection = detect_heating(
        house["times"],
        house["tin"],
        house["tout"],
        house["solar"],
        capacity_kw=2.0,
        dt_hours=1.0,
    )

    expected = sum(1 for flag in house["heating"] if flag)
    assert expected > 100
    # the detection must find the episodes, not the exact hour-by-hour alignment
    assert detection.heating_steps > 0
    assert abs(detection.heating_steps - expected) <= 0.25 * expected, (
        f"{detection.heating_steps} detected against {expected} real heating hours"
    )


def test_night_detection_finds_nothing_in_a_house_that_never_heats() -> None:
    """A house with no heating at all must not be given invented episodes."""
    # a genuinely cold week: the house cools at night, nothing warms it
    house = _house(night_heating=False, k=0.0, weather_start=-5.0)
    detection = detect_heating(
        house["times"],
        house["tin"],
        house["tout"],
        house["solar"],
        capacity_kw=2.0,
        dt_hours=1.0,
    )

    assert not detection.ok
    assert detection.heating_steps < 6
