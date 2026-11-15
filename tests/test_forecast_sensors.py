"""End-to-end forecast tests: the dashboard's data must actually exist.

These tests mock Open-Meteo instead of trusting it: they check that a config
entry produces the trajectories, the archived runs and the metadata the Lovelace
dashboard reads, and that a weather outage degrades gracefully.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_entry_loads_and_publishes_forecasts(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """One cycle produces a trajectory per scenario, plus the measured mirror."""
    assert setup_entry.state is ConfigEntryState.LOADED

    measured = hass.states.get("sensor.home_indoor_temperature")
    assert measured is not None and measured.state == "20.4"

    free_float = hass.states.get("sensor.home_forecast_free_float")
    assert free_float is not None
    assert free_float.state not in ("unknown", "unavailable")

    attributes = free_float.attributes
    assert attributes["scenario"] == "free_float"
    assert attributes["horizon_hours"] == 48
    assert len(attributes["times"]) == len(attributes["values"]) == 49
    assert len(attributes["weather_outdoor"]) > 0
    assert attributes["weather_model"] == "best_match"
    assert attributes["issued_at"].startswith("2026-01-10T12:00")
    assert attributes["minimum"] == min(attributes["values"])
    assert attributes["params_source"] == "manual_default"
    assert attributes["stale"] is False
    # The first value is the starting temperature: 'now', not 'now + 1 h'
    assert attributes["values"][0] == pytest.approx(20.4, abs=0.01)


async def test_thermostat_scenario_holds_more_heat_than_free_float(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Two scenarios must produce genuinely different trajectories."""
    free_float = hass.states.get("sensor.home_forecast_free_float")
    thermostat = hass.states.get("sensor.home_forecast_thermostat")
    assert free_float is not None and thermostat is not None

    assert thermostat.attributes["values"][-1] > free_float.attributes["values"][-1]


async def test_runs_are_archived_for_the_past_forecast_chart(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A second cycle must see the first one as an archived run."""
    from custom_components.thermal_model.coordinator import ThermalModelCoordinator

    coordinator: ThermalModelCoordinator = setup_entry.runtime_data.coordinator  # type: ignore[assignment]

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.home_forecast_free_float")
    assert state is not None
    archived = state.attributes["archived_runs"]
    assert len(archived) >= 1
    assert set(archived[0]) >= {"issued_at", "age_hours", "times", "values"}
    assert len(archived[0]["values"]) == len(archived[0]["times"])
    # The stored run is the *previous* one, so it cannot be newer than now
    assert archived[0]["age_hours"] >= 0


async def test_parameters_are_exposed_as_numbers(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The model must be inspectable and adjustable from the UI."""
    wall_loss = hass.states.get("number.home_wall_loss_a")
    assert wall_loss is not None
    assert float(wall_loss.state) == pytest.approx(0.016, abs=1e-6)
    assert wall_loss.attributes["params_source"] == "manual_default"

    for entity_id in (
        "number.home_solar_gain_c_s",
        "number.home_internal_gain_d",
        "number.home_heating_gain_k",
        "number.home_scenario_setpoint",
        "number.home_heating_capacity",
        "number.home_forecast_horizon",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_startup_with_the_indoor_sensor_not_ready_recovers_fast(
    hass: HomeAssistant, temperature_entities: None, aioclient_mock: Any, freezer: Any
) -> None:
    """A restart often finds the indoor sensor still unknown: retry in minutes.

    Seen on a real installation: the first cycle failed, every forecast entity went
    unavailable, and nothing retried for the configured 30 minutes.
    """
    from datetime import timedelta

    from tests.conftest import ENTRY_DATA, NOW, open_meteo_payload

    from custom_components.thermal_model.coordinator import RETRY_INTERVAL

    freezer.move_to(NOW)
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=open_meteo_payload())

    hass.states.async_set("climate.living_room", "heat", {"hvac_action": "heating"})
    hass.states.async_set("sensor.indoor_temperature", "unknown")
    hass.states.async_set("sensor.outdoor_temperature", "6.1")

    entry = MockConfigEntry(domain="thermal_model", data=ENTRY_DATA, title="Home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.update_interval == RETRY_INTERVAL
    assert hass.states.get("sensor.home_forecast_free_float").state == "unavailable"

    # The sensor comes back, as it does a few seconds after a restart.
    hass.states.async_set("sensor.indoor_temperature", "20.4")
    await hass.async_block_till_done()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.home_forecast_free_float")
    assert state.state not in ("unavailable", "unknown")
    assert coordinator.update_interval == timedelta(minutes=30)


async def test_the_forecast_follows_the_indoor_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A moving temperature must not be plotted against an old starting point."""
    before = hass.states.get("sensor.home_forecast_free_float")
    assert before is not None

    hass.states.async_set(
        "sensor.indoor_temperature",
        "17.2",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    await hass.async_block_till_done()

    coordinator = setup_entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    after = hass.states.get("sensor.home_forecast_free_float")
    # values[0] is the starting point of the trajectory: it must be the new reading
    assert after.attributes["values"][0] == pytest.approx(17.2, abs=0.05)


async def test_a_weather_outage_keeps_the_previous_runs(
    hass: HomeAssistant, setup_entry: MockConfigEntry, aioclient_mock: Any
) -> None:
    """The chart must not blink to empty when the API is down."""
    from custom_components.thermal_model.coordinator import ThermalModelCoordinator

    coordinator: ThermalModelCoordinator = setup_entry.runtime_data.coordinator  # type: ignore[assignment]

    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", status=503)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.home_forecast_free_float")
    assert state is not None
    assert state.attributes["stale"] is True
    assert state.attributes["error"] is not None
    assert len(state.attributes["values"]) == 49  # previous run still there


async def test_storage_file_is_created_under_the_config_directory(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Archived runs live next to the config, not in the recorder database."""
    runtime = setup_entry.runtime_data
    assert runtime.storage_path is not None
    assert runtime.storage_path.exists()
    assert runtime.storage_path.suffix == ".db"


async def test_unload_is_clean(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Unloading must stop the coordinator without leaving listeners behind."""
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
