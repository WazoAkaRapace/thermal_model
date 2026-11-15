"""Automatic identification: the integration must fit its own model from history.

The boundary mocked here is the recorder's statistics API — reading it is Home
Assistant's job, everything after it is ours. In particular these tests pin the two
choices the user should never have to make: which thermometers represent the house
when the bound sensor is too young, and which of a dozen power meters is the heating.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.thermal_model.const import DOMAIN_KEY
from custom_components.thermal_model.model_inputs import (
    indoor_candidates,
    power_candidates,
)
from custom_components.thermal_model.sources.recorder import state_is_heating
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
        hass: Any, statistic_ids: Any, days: int
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


def test_indoor_candidates_reject_what_is_not_a_room() -> None:
    """A device's own hardware temperature is not the house temperature."""
    values = {
        "sensor.thermometre_salon_temperature": {_now(): 20.0},
        HOT_DEVICE: {_now(): 27.0},
        FORECAST: {_now(): 22.5},
        "sensor.pool_sensor": {_now(): 4.0},
    }
    # enough points to pass the length floor
    for key in values:
        moment = _now()
        values[key] = {
            moment - HOUR * index: value
            for index, value in enumerate([values[key][moment]] * (24 * 6))
        }

    selected = indoor_candidates(values, exclude={OUTDOOR})

    assert "sensor.thermometre_salon_temperature" in selected
    assert HOT_DEVICE not in selected, "a device hardware temperature is not a room"
    assert FORECAST not in selected, "our own projection is not a measurement"
    assert "sensor.pool_sensor" not in selected, "4 °C is not a living room"


def test_power_candidates_rank_by_variance() -> None:
    """The heating meter is the one that actually moves."""
    moments = [_now() - HOUR * index for index in range(24 * 5)]
    values = {
        PAC: {moment: (2500.0 if index % 3 else 5.0) for index, moment in enumerate(moments)},
        JUNK_POWER: dict.fromkeys(moments, 30.0),
        "sensor.giant_meter": dict.fromkeys(moments, 40000.0),  # 40 kW: industrial
    }

    units = {PAC: "W", JUNK_POWER: "W", "sensor.giant_meter": "W"}
    ranked = power_candidates(values, exclude={OUTDOOR}, unit_of=units)

    assert ranked[0] == PAC
    assert "sensor.giant_meter" not in ranked, "40 kW is not a house heating load"


async def test_identification_publishes_identified_parameters(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """The numbers must stop being the reference house's defaults."""
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
    assert identification["heating_power_sensor"] == PAC, (
        "model selection must pick the meter that explains the temperature, not the first one"
    )
    assert identification["indoor_sources"] == [INDOOR]
    assert identification["identified_at"] is not None

    wall_loss = float(state.state)
    assert 0.0005 < wall_loss < 0.2
    assert float(hass.states.get("number.home_heating_gain_k").state) > 0

    forecast = hass.states.get("sensor.home_forecast_free_float")
    assert forecast.attributes["params_source"] == "identified"


async def test_identification_falls_back_to_room_thermometers(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A freshly created mean helper has four hours of history; the rooms have weeks."""
    data = statistics_reader
    young = {moment: data[INDOOR][moment] for moment in sorted(data[INDOOR])[-4:]}
    data[INDOOR] = young
    data[ROOM] = (
        dict(data[INDOOR])
        if False
        else {moment: value + 0.4 for moment, value in _synthetic()[INDOOR].items()}
    )
    hass.states.async_set(
        ROOM,
        "20.0",
        {"device_class": "temperature", "state_class": "measurement", "unit_of_measurement": "°C"},
    )

    coordinator = setup_entry.runtime_data.coordinator
    await coordinator.async_identify()
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    identification = state.attributes["identification"]
    assert state.attributes["params_source"] == "identified"
    assert identification["indoor_sources"] == [ROOM]
    assert any("thermomètre" in note for note in identification["notes"])


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
    entry = MockConfigEntry(domain=DOMAIN_KEY, data=ENTRY_DATA, title="Home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # the startup identification waits for the recorder and the weather API
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get("number.home_wall_loss_a")
    assert state.attributes["params_source"] == "identified"


def test_state_is_heating_per_family() -> None:
    """A climate in heat mode may be idle: the state is the mode, not the action."""
    now = datetime.now(UTC)
    assert state_is_heating(State(HEATING, "heat", {"hvac_action": "heating"}, now, now)) is True
    assert state_is_heating(State(HEATING, "heat", {"hvac_action": "idle"}, now, now)) is False
    assert state_is_heating(State("switch.boiler", "on", {}, now, now)) is True
    assert (
        state_is_heating(State("water_heater.ecs", "off", {"current_operation": "off"}, now, now))
        is False
    )
    assert state_is_heating(None) is False


def test_a_mean_helper_is_not_its_own_history(
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """Guard the statistical filter against picking a flat series by accident."""
    flat = {_now() - HOUR * index: 21.0 for index in range(24 * 6)}
    assert statistics.pstdev(list(flat.values())) == 0
    assert "sensor.flat" in indoor_candidates({"sensor.flat": flat}, exclude=set())


async def test_the_power_meter_can_be_chosen_by_the_user(
    hass: HomeAssistant,
    temperature_entities: None,
    power_states: None,
    setup_entry: MockConfigEntry,
    statistics_reader: dict[str, dict[datetime, float]],
) -> None:
    """Detection is a supported guess; the user must be able to overrule it."""
    await setup_entry.runtime_data.coordinator.async_identify()
    await hass.async_block_till_done()

    selector = hass.states.get("select.home_heating_power_meter")
    assert selector is not None
    assert "auto" in selector.attributes["options"]
    assert PAC in selector.attributes["options"], "the detected candidates must be offered"
    assert selector.state == "auto"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.home_heating_power_meter", "option": OTHER},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("select.home_heating_power_meter").state == OTHER
    identification = hass.states.get("number.home_wall_loss_a").attributes["identification"]
    assert identification["heating_power_sensor"] == OTHER, (
        "an explicit choice must win over auto-detection"
    )


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
    setup_entry.runtime_data.selected_power_sensor = "sensor.pac_energy"
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
