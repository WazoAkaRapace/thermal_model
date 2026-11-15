"""Weather input: hourly outdoor temperature and irradiance from Open-Meteo.

Open-Meteo is used because it needs no API key and exposes several models that
can be compared on the same coordinates (``best_match``, ``arome_france``,
``ecmwf_ifs025``, ``icon_seamless``, ...). The provider is deliberately thin: it
returns plain :class:`WeatherStep` objects so the forecast code never knows where
numbers come from, and the tests can feed hand-built series.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import WEATHER_API_URL, WEATHER_TIMEOUT
from ..core.forecast import WeatherStep

__all__ = ["WeatherError", "fetch_weather"]

_LOGGER = logging.getLogger(__name__)


class WeatherError(Exception):
    """The weather provider could not be used."""


def _parse_hour(value: str) -> datetime:
    """Open-Meteo returns naive timestamps: they are UTC when asked for UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


async def fetch_weather(
    session: aiohttp.ClientSession,
    *,
    latitude: float,
    longitude: float,
    hours: int,
    model: str,
    now: datetime | None = None,
) -> list[WeatherStep]:
    """Fetch the next ``hours`` hourly steps for one weather model."""
    if hours <= 0:
        raise WeatherError("horizon must be positive")

    reference = (now or dt_util.utcnow()).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    forecast_days = min(16, max(2, (hours // 24) + 2))
    params = {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "hourly": "temperature_2m,shortwave_radiation",
        "models": model,
        "forecast_days": str(forecast_days),
        "timezone": "UTC",
    }

    try:
        async with session.get(
            WEATHER_API_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=WEATHER_TIMEOUT),
        ) as response:
            if response.status != 200:
                raise WeatherError(f"Open-Meteo returned HTTP {response.status}")
            payload = await response.json()
    except TimeoutError as err:
        raise WeatherError("Open-Meteo timed out") from err
    except aiohttp.ClientError as err:
        raise WeatherError(f"Open-Meteo unreachable: {err}") from err

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temperatures = hourly.get("temperature_2m") or []
    if not times or len(times) != len(temperatures):
        raise WeatherError("Open-Meteo returned an incomplete hourly block")

    radiation = hourly.get("shortwave_radiation") or []
    steps: list[WeatherStep] = []
    for index, stamp in enumerate(times):
        moment = _parse_hour(stamp)
        if moment < reference:
            continue
        temperature = temperatures[index]
        if temperature is None:
            continue
        steps.append(
            WeatherStep(
                time=moment,
                text=float(temperature),
                solar=float(radiation[index] or 0.0) if index < len(radiation) else 0.0,
            )
        )
        if len(steps) >= hours:
            break

    if not steps:
        raise WeatherError("Open-Meteo returned no usable hour ahead of now")
    _LOGGER.debug(
        "Thermal Model: %s steps from %s to %s (%s)",
        len(steps),
        steps[0].time,
        steps[-1].time + timedelta(hours=1),
        model,
    )
    return steps
