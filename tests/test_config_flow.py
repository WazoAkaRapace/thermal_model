"""Config flow tests: the three mandatory entities are the whole contract."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_model.const import (
    CONF_DT_MINUTES,
    CONF_DT_STORAGE_MINUTES,
    CONF_HEATING_POWER_SENSOR,
    CONF_HISTORY_DAYS,
    CONF_INDOOR_SENSOR,
    CONF_IRRADIANCE_SENSOR,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_OUTDOOR_SENSOR,
    CONF_RETENTION_DAYS,
    CONF_SMOOTH_HOURS,
    CONF_TAU_GRACE_MINUTES,
    DOMAIN_KEY,
)

USER_INPUT = {CONF_NAME: "Home", CONF_LATITUDE: 45.92, CONF_LONGITUDE: 0.92}
SOURCES_INPUT = {
    CONF_INDOOR_SENSOR: "sensor.indoor_temperature",
    CONF_OUTDOOR_SENSOR: "sensor.outdoor_temperature",
}
WINDOW_INPUT = {
    CONF_HISTORY_DAYS: 21,
    CONF_DT_MINUTES: 15,
    CONF_DT_STORAGE_MINUTES: 5,
    CONF_SMOOTH_HOURS: 2.0,
    CONF_TAU_GRACE_MINUTES: 30,
    CONF_RETENTION_DAYS: 400,
}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN_KEY, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(hass: HomeAssistant, temperature_entities: None) -> None:
    """Three entities plus a name and a location: that is all it takes."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "window"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], WINDOW_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_SMOOTH_HOURS] == 2.0


async def test_rejects_non_temperature_sensor(
    hass: HomeAssistant, temperature_entities: None
) -> None:
    """A sensor without temperature device class or unit is refused, explicitly."""
    hass.states.async_set(
        "sensor.humidity", "55", {"device_class": "humidity", "unit_of_measurement": "%"}
    )

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES_INPUT, CONF_INDOOR_SENSOR: "sensor.humidity"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert result["errors"] == {CONF_INDOOR_SENSOR: "not_a_temperature_sensor"}


async def test_rejects_unknown_entity(hass: HomeAssistant, temperature_entities: None) -> None:
    """A typo is reported as a missing entity, not swallowed."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES_INPUT, CONF_OUTDOOR_SENSOR: "sensor.nope"}
    )

    assert result["errors"] == {CONF_OUTDOOR_SENSOR: "entity_not_found"}


async def test_rejects_identical_sensors(hass: HomeAssistant, temperature_entities: None) -> None:
    """Indoor and outdoor cannot be the same entity."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SOURCES_INPUT, CONF_OUTDOOR_SENSOR: "sensor.indoor_temperature"},
    )

    assert result["errors"] == {CONF_OUTDOOR_SENSOR: "same_entity"}


async def test_accepts_sensor_that_is_not_reporting_yet(
    hass: HomeAssistant, temperature_entities: None
) -> None:
    """An unavailable sensor is accepted: attribute presence is never a gate."""
    hass.states.async_set("sensor.pending", "unavailable", {})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES_INPUT, CONF_OUTDOOR_SENSOR: "sensor.pending"}
    )

    assert result["step_id"] == "window"


async def test_duplicate_name_aborts(hass: HomeAssistant, temperature_entities: None) -> None:
    """One config entry per model name."""
    MockConfigEntry(
        domain=DOMAIN_KEY,
        data={CONF_NAME: "Home"},
        title="Home",
        unique_id="home",
    ).add_to_hass(hass)
    await hass.async_block_till_done()

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_window(hass: HomeAssistant, temperature_entities: None) -> None:
    """The options flow is the only place for the window/storage settings."""
    entry = MockConfigEntry(
        domain=DOMAIN_KEY,
        data={**USER_INPUT, **SOURCES_INPUT, **WINDOW_INPUT},
        title="Home",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**WINDOW_INPUT, CONF_HISTORY_DAYS: 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HISTORY_DAYS] == 60


async def test_reconfigure_remaps_sources(hass: HomeAssistant, temperature_entities: None) -> None:
    """Re-mapping an entity does not require reinstalling the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN_KEY,
        data={**USER_INPUT, **SOURCES_INPUT, **WINDOW_INPUT},
        title="Home",
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.new_outdoor", "4.0", {"device_class": "temperature", "unit_of_measurement": "°C"}
    )

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SOURCES_INPUT, CONF_OUTDOOR_SENSOR: "sensor.new_outdoor"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_OUTDOOR_SENSOR] == "sensor.new_outdoor"


async def test_sources_step_offers_the_two_optional_measurements(
    hass: HomeAssistant, temperature_entities: None
) -> None:
    """A user who has a heating meter and a solar sensor must be able to say so.

    Without these fields the integration guesses both, and on a house with a dozen
    power meters and an inverter the guess lands on the wrong meter.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN_KEY, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    keys = [str(key) for key in result["data_schema"].schema]
    assert CONF_HEATING_POWER_SENSOR in keys
    assert CONF_IRRADIANCE_SENSOR in keys


async def test_the_optional_measurements_are_stored(
    hass: HomeAssistant, temperature_entities: None
) -> None:
    """They must reach the entry data: the identification reads them from there."""
    hass.states.async_set(
        "sensor.pac_power", "1200", {"device_class": "power", "unit_of_measurement": "W"}
    )
    hass.states.async_set(
        "sensor.roof_irradiance",
        "320",
        {"device_class": "irradiance", "unit_of_measurement": "W/m²"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN_KEY, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **SOURCES_INPUT,
            CONF_HEATING_POWER_SENSOR: "sensor.pac_power",
            CONF_IRRADIANCE_SENSOR: "sensor.roof_irradiance",
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], WINDOW_INPUT)

    entry = result["result"]
    assert entry.data[CONF_HEATING_POWER_SENSOR] == "sensor.pac_power"
    assert entry.data[CONF_IRRADIANCE_SENSOR] == "sensor.roof_irradiance"


async def test_an_unknown_optional_measurement_is_rejected(
    hass: HomeAssistant, temperature_entities: None
) -> None:
    """A typo in an optional field must not silently disable the input."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN_KEY, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SOURCES_INPUT, CONF_HEATING_POWER_SENSOR: "sensor.does_not_exist"},
    )

    assert result["errors"][CONF_HEATING_POWER_SENSOR] == "unknown_entity"
    assert result["step_id"] == "sources"
