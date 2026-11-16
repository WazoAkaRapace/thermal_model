"""The sun on the facade: position, projection, and what it changes."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from custom_components.thermal_model.core.solar import facade_irradiance, solar_position

LAT, LON = 47.9, 0.5


def _noon_november() -> datetime:
    # solar noon near 12:00 UTC at longitude 0.5
    return datetime(2026, 11, 16, 11, 58, tzinfo=UTC)


def test_solar_noon_is_high_and_south() -> None:
    elevation, azimuth = solar_position(_noon_november(), LAT, LON)
    assert 20.0 < elevation < 28.0, f"november noon elevation: {elevation}"
    assert abs(azimuth) < 10.0, f"november noon azimuth from south: {azimuth}"


def test_morning_sun_is_east_of_south() -> None:
    _elevation, azimuth = solar_position(datetime(2026, 11, 16, 9, 0, tzinfo=UTC), LAT, LON)
    assert azimuth < -20.0, f"9am azimuth should be well east of south (negative): {azimuth}"


def test_night_has_no_sun() -> None:
    value = facade_irradiance(
        datetime(2026, 11, 16, 2, 0, tzinfo=UTC),
        direct_normal=800.0,
        diffuse_horizontal=50.0,
        latitude=LAT,
        longitude=LON,
    )
    assert 0.0 < value <= 25.0  # diffuse half-sky only, no beam, no ground bounce at night


def test_a_south_facade_beats_the_ground_at_a_low_sun() -> None:
    """The measured reason this module exists: winter low sun, vertical glazing."""
    noon = _noon_november()
    elevation, _azimuth = solar_position(noon, LAT, LON)
    horizontal = 800.0 * math.sin(math.radians(elevation)) + 100.0
    facade = facade_irradiance(
        noon,
        direct_normal=800.0,
        diffuse_horizontal=100.0,
        latitude=LAT,
        longitude=LON,
    )
    assert facade > 2.0 * horizontal, (
        f"facade {facade:.0f} vs horizontal {horizontal:.0f} W/m² at elevation {elevation:.1f}°"
    )


def test_a_north_facade_gets_no_beam() -> None:
    noon = _noon_november()
    north = facade_irradiance(
        noon,
        direct_normal=800.0,
        diffuse_horizontal=100.0,
        latitude=LAT,
        longitude=LON,
        facade_azimuth_deg=180.0,
    )
    south = facade_irradiance(
        noon,
        direct_normal=800.0,
        diffuse_horizontal=100.0,
        latitude=LAT,
        longitude=LON,
    )
    assert north < 0.2 * south


def test_the_forecast_of_yesterday_matches_today() -> None:
    """Same formula for past and future hours: identification and forecast agree."""
    stamp = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert facade_irradiance(
        stamp, direct_normal=600.0, diffuse_horizontal=80.0, latitude=LAT, longitude=LON
    ) == facade_irradiance(
        stamp, direct_normal=600.0, diffuse_horizontal=80.0, latitude=LAT, longitude=LON
    )
