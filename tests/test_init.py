"""Setup and unload tests."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_model.const import (
    CONF_DT_MINUTES,
    CONF_DT_STORAGE_MINUTES,
    CONF_HEATING_ENTITY,
    CONF_HISTORY_DAYS,
    CONF_INDOOR_SENSOR,
    CONF_NAME,
    CONF_OUTDOOR_SENSOR,
    CONF_RETENTION_DAYS,
    CONF_SMOOTH_HOURS,
    CONF_TAU_GRACE_MINUTES,
    DOMAIN_KEY,
)

ENTRY_DATA = {
    CONF_NAME: "Home",
    CONF_HEATING_ENTITY: "climate.living_room",
    CONF_INDOOR_SENSOR: "sensor.indoor_temperature",
    CONF_OUTDOOR_SENSOR: "sensor.outdoor_temperature",
    CONF_HISTORY_DAYS: 21,
    CONF_DT_MINUTES: 15,
    CONF_DT_STORAGE_MINUTES: 5,
    CONF_SMOOTH_HOURS: 2.0,
    CONF_TAU_GRACE_MINUTES: 30,
    CONF_RETENTION_DAYS: 400,
}


async def test_setup_and_unload_entry(hass: HomeAssistant, temperature_entities: None) -> None:
    """A config entry with the three entities loads and unloads cleanly."""
    entry = MockConfigEntry(domain=DOMAIN_KEY, data=ENTRY_DATA, title="Home")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.sources.indoor_sensor == "sensor.indoor_temperature"
    assert entry.runtime_data.settings.smooth_hours == 2.0
    assert entry.runtime_data.missing_entities == set()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_tolerates_missing_entities(hass: HomeAssistant) -> None:
    """Entities can be missing right after a restart: never fail setup."""
    entry = MockConfigEntry(domain=DOMAIN_KEY, data=ENTRY_DATA, title="Home")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.missing_entities == {
        "sensor.indoor_temperature",
        "sensor.outdoor_temperature",
    }


async def test_options_override_data(hass: HomeAssistant, temperature_entities: None) -> None:
    """Options win over the initial data for the identification window."""
    entry = MockConfigEntry(
        domain=DOMAIN_KEY,
        data=ENTRY_DATA,
        options={CONF_SMOOTH_HOURS: 4.0, CONF_HISTORY_DAYS: 90},
        title="Home",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.settings.smooth_hours == 4.0
    assert entry.runtime_data.settings.history_days == 90


@pytest.mark.parametrize(
    ("missing_entity", "replacement"),
    [
        ("sensor.missing", {"indoor_sensor": "sensor.missing"}),
    ],
)
async def test_missing_entity_is_recorded_not_guessed(
    hass: HomeAssistant, missing_entity: str, replacement: dict[str, str]
) -> None:
    """A mapped entity that does not exist is recorded, never inferred."""
    data = {**ENTRY_DATA, **replacement}

    entry = MockConfigEntry(domain=DOMAIN_KEY, data=data, title="Home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert missing_entity in entry.runtime_data.missing_entities
