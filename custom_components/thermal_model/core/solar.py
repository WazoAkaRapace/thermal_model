"""The sun as the house sees it: on its glazed facade, not on the ground.

Measured on the real installation that motivated this module: the house's
daytime response to identical horizontal irradiance varied by a factor of ~20
between days (effective c_s from 0.0002 to 0.0059 K/h per W/m²), and neither
wind, nor the direct/diffuse split, nor smoothing the outdoor temperature
explained it. Reconstructing the irradiance on a vertical south facade did:
with outlier steps trimmed, the day-to-day spread collapsed from ~290 % to
~80 % and the hourly RMSE halved. In November the sun culminates near 24° at
this latitude, so a vertical south surface catches up to ~3× the direct beam
of a horizontal one — exactly what large south glazing turns into indoor
temperature.

Pure Python: solar position from the standard declination/equation-of-time
formulas (good to a fraction of a degree, plenty for a wall), beam projection
on the facade, isotropic half-sky diffuse. Works for any timestamp — past for
the identification, future for the forecast — from latitude, longitude and
time alone.
"""

from __future__ import annotations

import math
from datetime import datetime

__all__ = ["facade_irradiance", "solar_position"]

# Isotropic sky: a vertical wall sees half of the sky dome.
_SKY_VIEW = 0.5
# Ground in front of the facade bounces some of the horizontal irradiance back.
# 20 % albedo × half of the ground view.
_GROUND_REFLECT = 0.1


def solar_position(moment: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """Sun elevation and azimuth (degrees from south, positive west).

    The NOAA-lite formulas: declination of the day, equation of time, hour
    angle from apparent solar time. Accurate to a fraction of a degree, which
    is far beyond what a wall orientation is known to.
    """
    day_of_year = moment.timetuple().tm_yday
    declination = math.radians(23.45) * math.sin(math.radians(360 * (284 + day_of_year) / 365))
    cycle = math.radians(360 * day_of_year / 365)
    equation_of_time = 229.2 * (
        0.000075
        + 0.001868 * math.cos(cycle)
        - 0.032077 * math.sin(cycle)
        - 0.014615 * math.cos(2 * cycle)
        - 0.04089 * math.sin(2 * cycle)
    )
    solar_time = moment.hour + moment.minute / 60.0 + equation_of_time / 60.0 + longitude / 15.0
    hour_angle = math.radians(15.0 * (solar_time - 12.0))
    lat = math.radians(latitude)
    sin_elevation = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    sin_elevation = max(-1.0, min(1.0, sin_elevation))
    elevation = math.asin(sin_elevation)
    denominator = math.cos(elevation) * math.cos(lat)
    if abs(denominator) < 1e-9:  # sun at zenith or pole: azimuth ill-defined
        return math.degrees(elevation), 0.0
    cos_azimuth = (math.sin(declination) - sin_elevation * math.sin(lat)) / denominator
    cos_azimuth = max(-1.0, min(1.0, cos_azimuth))
    # The classic formula yields the azimuth from NORTH (solar noon ~= 180).
    from_north = math.degrees(math.acos(cos_azimuth))
    if hour_angle > 0:  # afternoon: the acos branch folds it back north
        from_north = 360.0 - from_north
    return math.degrees(elevation), from_north - 180.0


def facade_irradiance(
    moment: datetime,
    *,
    direct_normal: float,
    diffuse_horizontal: float,
    latitude: float,
    longitude: float,
    facade_azimuth_deg: float = 0.0,
) -> float:
    """Irradiance on a vertical facade (W/m²), default facing due south.

    Beam: DNI projected on the wall (zero when the sun is behind it or below
    the horizon). Diffuse: half the sky, isotropic. Ground: a tenth of the
    horizontal total, reflected.
    """
    elevation, azimuth = solar_position(moment, latitude, longitude)
    diffuse = _SKY_VIEW * diffuse_horizontal
    if elevation <= 0.0:
        return max(0.0, diffuse)
    incidence = math.cos(math.radians(elevation)) * math.cos(
        math.radians(azimuth - facade_azimuth_deg)
    )
    beam = direct_normal * incidence if incidence > 0.0 else 0.0
    horizontal = direct_normal * math.sin(math.radians(elevation)) + diffuse_horizontal
    return max(0.0, beam + diffuse + _GROUND_REFLECT * horizontal)
