"""Fixtures for the Thermal Model test suite."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from homeassistant.core import HomeAssistant

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    """Load custom_components in every test."""
    yield


@pytest.fixture
def temperature_entities(hass: HomeAssistant) -> None:
    """Create the three mandatory entities of a minimal installation."""
    hass.states.async_set(
        "climate.living_room",
        "heat",
        {
            "friendly_name": "Living room",
            "hvac_action": "heating",
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
        },
    )
    hass.states.async_set(
        "sensor.indoor_temperature",
        "20.4",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    hass.states.async_set(
        "sensor.outdoor_temperature",
        "6.1",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
