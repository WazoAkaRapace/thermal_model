"""Fixtures for the Thermal Model test suite."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_model.const import (
    CONF_HEATING_ENTITY,
    CONF_INDOOR_SENSOR,
    CONF_NAME,
    CONF_OUTDOOR_SENSOR,
    DOMAIN_KEY,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]

ENTRY_DATA: dict[str, Any] = {
    CONF_NAME: "Home",
    CONF_HEATING_ENTITY: "climate.living_room",
    CONF_INDOOR_SENSOR: "sensor.indoor_temperature",
    CONF_OUTDOOR_SENSOR: "sensor.outdoor_temperature",
}

# Fixed instant for the forecast tests: weather mocks and freezer agree on it.
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


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


def open_meteo_payload(hours: int = 60) -> dict[str, Any]:
    """An hourly weather block starting one hour before NOW, like the real API."""
    start = NOW - timedelta(hours=1)
    times = [(start + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M") for hour in range(hours)]
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [4.0 + (hour % 6) - 2.0 for hour in range(hours)],
            "shortwave_radiation": [0.0 if hour % 24 < 8 else 300.0 for hour in range(hours)],
        }
    }


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, temperature_entities: None, aioclient_mock: Any, freezer: Any
) -> MockConfigEntry:
    """A loaded entry with a mocked weather provider, frozen at NOW."""
    freezer.move_to(NOW)
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=open_meteo_payload())
    entry = MockConfigEntry(domain=DOMAIN_KEY, data=ENTRY_DATA, title="Home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
