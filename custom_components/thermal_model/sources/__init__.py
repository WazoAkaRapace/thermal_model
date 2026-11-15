"""Weather source abstraction (multiple providers land here later)."""

from __future__ import annotations

from .openmeteo import WeatherError, fetch_past_weather, fetch_weather

__all__ = ["WeatherError", "fetch_past_weather", "fetch_weather"]
