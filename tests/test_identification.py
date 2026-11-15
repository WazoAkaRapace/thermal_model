"""Automatic identification: the integration must fit its own model from history.

The boundary mocked here is the recorder's statistics API — reading it is Home
Assistant's job, everything after it is ours. In particular these tests pin the two
choices the user should never have to make: which thermometers represent the house
when the bound sensor is too young, and which of a dozen power meters is the heating.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.thermal_model.const import DOMAIN_KEY
from custom_components.thermal_model.sources.statistics import StatisticPoint

HOUR = timedelta(hours=1)
DAYS = 12
HEATING = "climate.living_room"
INDOOR = "sensor.indoor_temperature"
OUTDOOR = "sensor.outdoor_temperature"
PAC = "sensor.pac_power"
OTHER = "sensor.other_heater_power"
JUNK_POWER = "sensor.lamp_power"
ROOM = "sensor.room_thermometer"
HOT_DEVICE = "sensor.contact_device_temperature"
FORECAST = "sensor.tempo_forecast_pool"


def _now() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def _synthetic(days: int = DAYS) -> dict[str, dict[datetime, float]]:
    """A house with hysteresis, an outdoor trend and several power meters.

    The outdoor temperature falls over the window on purpose: without a real trend in
    the indoor-outdoor gap nothing can be identified, which is what the identifier
    says when it refuses.
    """
    end = _now()
    hours = [end - HOUR * (days * 24 - index) for index in range(days * 24)]
    inside = 19.5
    heating_on = True
    indoor: dict[datetime, float] = {}
    outdoor: dict[datetime, float] = {}
    pac: dict[datetime, float] = {}
    junk: dict[datetime, float] = {}
    other: dict[datetime, float] = {}
    for index, moment in enumerate(hours):
        fraction = index / len(hours)
        outside = 11.0 - 8.0 * fraction + 1.5 * ((index % 24) / 24.0 - 0.5)
        if heating_on and inside >= 20.6:
            heating_on = False
        elif not heating_on and inside <= 19.4:
            heating_on = True
        if heating_on:
            inside += 0.09
        else:
            inside += (outside - inside) * 0.012
        indoor[moment] = inside
        outdoor[moment] = outside
        pac[moment] = 2400.0 if heating_on else 8.0
        # A meter that misses one heating hour in eleven: still identifiable when the
        # user picks it by hand, but a worse fit than the real one, so auto-detection
        # has something to get right.
        other[moment] = 2400.0 if (heating_on and index % 11) else 8.0
        junk[moment] = 30.0 + 15.0 * ((index % 5) / 5.0)
    return {
        INDOOR: indoor,
        OUTDOOR: outdoor,
        PAC: pac,
        OTHER: other,
        JUNK_POWER: junk,
    }


@pytest.fixture
def power_states(hass: HomeAssistant) -> None:
    """Power meters must exist as entities: discovery lists real states."""
    for entity_id, unit, peak in (
        (PAC, "W", 2400),
        (JUNK_POWER, "W", 45),
        (OTHER, "W", 1500),
        ("sensor.giant_meter", "W", 40000),
    ):
        hass.states.async_set(
            entity_id,
            str(peak),
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": unit,
            },
        )


@pytest.fixture
def statistics_reader(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[datetime, float]]:
    """Serve synthetic statistics instead of the recorder, and stub past weather."""
    data = _synthetic()

    async def fake_read_statistics(
        hass: Any, statistic_ids: Any, days: int, *, prefer: str = "mean"
    ) -> dict[str, list[StatisticPoint]]:
        return {
            statistic_id: [
                StatisticPoint(moment, value)
                for moment, value in sorted(data[statistic_id].items())
            ]
            for statistic_id in statistic_ids
            if statistic_id in data
        }

    async def fake_past_weather(
        session: Any, *, latitude: float, longitude: float, days: int
    ) -> list[Any]:
        class Step:
            def __init__(self, time: datetime) -> None:
                self.time = time
                self.text = 5.0
                self.solar = 0.0

        end = _now()
        return [Step(end - HOUR * offset) for offset in range(days * 24, -1, -1)]

    monkeypatch.setattr(
        "custom_components.thermal_model.model_inputs.async_read_statistics",
        fake_read_statistics,
    )
    monkeypatch.setattr(
        "custom_components.thermal_model.model_inputs.fetch_past_weather",
        fake_past_weather,
    )
    return data


async def test_identification_publishes_identified_parameters(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """The numbers must stop being the reference house's defaults."""
    setup_entry.runtime_data.options["heating_power_sensor"] = PAC
    coordinator = setup_entry.runtime_data.coordinator
    assert (
        hass.states.get("number.home_wall_loss_a").attributes["params_source"] == "manual_default"
    )

    await coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    identification = state.attributes["identification"]
    assert state.attributes["params_source"] == "identified", json.dumps(identification, indent=1)

    assert identification["window_days"] >= 5
    assert identification["samples"] >= 24 * 5
    assert identification["heating_power_sensor"] == PAC
    assert identification["indoor_sources"] == [INDOOR]
    assert identification["identified_at"] is not None

    wall_loss = float(state.state)
    assert 0.0005 < wall_loss < 0.2
    assert float(hass.states.get("number.home_heating_gain_k").state) > 0

    forecast = hass.states.get("sensor.home_forecast_free_float")
    assert forecast.attributes["params_source"] == "identified"


async def test_identification_reports_a_refusal_instead_of_a_number(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable history must leave the parameters alone and say why."""
    monkeypatch.setattr(
        "custom_components.thermal_model.model_inputs.async_read_statistics",
        lambda *args, **kwargs: __import__("asyncio").sleep(0, result={}),
    )
    coordinator = setup_entry.runtime_data.coordinator

    await coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    assert state.attributes["params_source"] == "manual_default"
    identification = state.attributes["identification"]
    assert identification["identifiability"] == "insufficient_data"
    assert identification["identification_error"]


async def test_the_button_identifies_and_publishes_diagnostics(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """A user who just changed a sensor must not restart Home Assistant."""
    setup_entry.runtime_data.options["heating_power_sensor"] = PAC
    assert hass.states.get("button.home_recalculate_model") is not None

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.home_recalculate_model"}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get("button.home_recalculate_model")
    assert state.attributes["identifiability"] != "not_run"
    assert state.attributes["samples"] > 0
    assert state.attributes["heating_power_sensor"] == PAC


async def test_identify_runs_at_startup(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    aioclient_mock: Any,
    freezer: Any,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """A fresh install must not stay 'manual' until someone presses a button."""
    from tests.conftest import ENTRY_DATA, NOW, open_meteo_payload

    freezer.move_to(NOW)
    aioclient_mock.get("https://api.open-meteo.com/v1/forecast", json=open_meteo_payload())
    entry = MockConfigEntry(
        domain=DOMAIN_KEY,
        data={**ENTRY_DATA, "heating_power_sensor": PAC},
        title="Home",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # the startup identification waits for the recorder and the weather API
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    assert state.attributes["params_source"] == "identified"


async def test_a_kwh_counter_alone_identifies_the_whole_model(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """No power meter at all — only a kWh counter, which is what a radiator house has.

    The hourly fit cannot separate the wall loss from the heating when the heating is
    never observed. Three weeks of daily balances can: four coefficients, one equation
    per day.
    """
    from tests.unit.test_detection import _house

    house = _house(days=21)
    moments = house["times"]
    setup_entry.runtime_data.options["heating_power_sensor"] = "sensor.pac_energy"
    hass.states.async_set(
        "sensor.pac_energy",
        "0",
        {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total"},
    )
    statistics_reader[INDOOR] = dict(zip(moments, house["tin"], strict=True))
    statistics_reader[OUTDOOR] = dict(zip(moments, house["tout"], strict=True))
    statistics_reader["sensor.pac_energy"] = dict(zip(moments, house["energy"], strict=True))

    await setup_entry.runtime_data.coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    identification = state.attributes["identification"]
    assert state.attributes["params_source"] == "identified", json.dumps(identification, indent=1)
    assert identification["k_source"] == "daily_model"
    assert identification["energy_sensor"] == "sensor.pac_energy"
    assert identification["k_days"] >= 12
    assert identification["k_energy_kwh"] > 100

    # the four coefficients must land near the house that generated the data
    assert abs(float(hass.states.get("number.home_heating_gain_k").state) - 0.30) < 0.1
    assert 0.005 < float(state.state) < 0.06


async def test_an_implausible_k_is_refused_instead_of_breaking_the_forecast(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """5 K/kWh at 2 kW is 10 K/h: the scenario diverges and every entity goes away."""
    setup_entry.runtime_data.options["heating_power_sensor"] = PAC
    data = statistics_reader
    for moment in list(data[PAC]):
        data[PAC][moment] = 500.0
    await setup_entry.runtime_data.coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_heating_gain_k")
    identification = state.attributes["identification"]
    k = float(state.state)
    capacity = float(hass.states.get("number.home_heating_capacity").state)
    assert 0.02 <= k * capacity <= 3.0, (
        f"k = {k} K/kWh with {capacity} kW implies {k * capacity} K/h: "
        f"{identification.get('notes')}"
    )
    # and the forecast entities must still be there
    assert hass.states.get("sensor.home_forecast_free_float").state not in (
        "unavailable",
        "unknown",
    )


async def test_the_bound_sensor_is_read_from_raw_history_when_it_has_no_statistics(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """949 states, 0 statistics: the user's sensor, not a house-wide mean."""
    data = statistics_reader
    indoor_series = data.pop("sensor.indoor_temperature")  # no statistics at all

    setup_entry.runtime_data.options["heating_power_sensor"] = PAC
    points = sorted(indoor_series.items())  # its raw history, hourly
    import custom_components.thermal_model.model_inputs as mi

    async def fake_series_for(hass_, sensor, days):
        if sensor == "sensor.indoor_temperature":
            return dict(points)
        return statistics_reader.get(sensor) or {}

    monkeypatch.setattr(mi, "_series_for_sensor", fake_series_for)

    await setup_entry.runtime_data.coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    identification = state.attributes["identification"]
    assert state.attributes["params_source"] == "identified", identification
    assert identification["indoor_sources"] == ["sensor.indoor_temperature"], (
        "the bound sensor must be used, not replaced by a mean of every thermometer"
    )
    assert any("historique brut" in str(note) for note in identification["notes"])


async def test_daily_k_finishing_an_hourly_fit_returns_an_identification_result(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """k from the daily balance must arrive wrapped, not as a bare ThermalParams.

    The button crashed on the real installation with "'ThermalParams' object has no
    attribute 'params'": the path "hourly fit without k, finished by the daily
    balance" returned the parameters directly.
    """
    import custom_components.thermal_model.model_inputs as mi
    from custom_components.thermal_model.core.identify import (
        Identifiability,
        IdentificationResult,
    )
    from custom_components.thermal_model.core.simulate import ThermalParams

    setup_entry.runtime_data.options["heating_power_sensor"] = PAC

    def fake_identify(times, tin, tout, solar, heating, settings=None):
        return IdentificationResult(
            params=ThermalParams(a=0.02, c_s=0.001, d=0.015, k=None),
            identifiability=Identifiability.CONSTRAINED,
            driving_spread=0.3,
            rmse_observed=1.0,
            rmse_free_float=1.5,
            coverage=1.0,
        )

    monkeypatch.setattr(mi, "identify", fake_identify)
    monkeypatch.setattr(mi, "_energy_sensor_for", lambda hass_, power: "sensor.pac_energy")

    data = statistics_reader
    data["sensor.pac_energy"] = {
        moment: (2.0 if data[PAC][moment] > 1000 else 0.0) for moment in data[PAC]
    }

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.home_recalculate_model"}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    identification = state.attributes["identification"]
    assert state.attributes["params_source"] == "identified", json.dumps(identification, indent=1)
    assert identification["k_days"] > 0
    assert identification["energy_sensor"] == "sensor.pac_energy"
    assert float(hass.states.get("number.home_heating_gain_k").state) > 0
    # the hourly fit said k was not identifiable; the daily balance filled it, so the
    # published reasons must not keep saying so over a published k
    assert "k_not_identified" not in identification["reasons"], identification["reasons"]
