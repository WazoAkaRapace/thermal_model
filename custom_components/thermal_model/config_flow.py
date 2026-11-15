"""Config flow for the Thermal Model integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_DT_MINUTES,
    CONF_DT_STORAGE_MINUTES,
    CONF_HEATING_ENTITY,
    CONF_HISTORY_DAYS,
    CONF_INDOOR_SENSOR,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_OUTDOOR_SENSOR,
    CONF_RETENTION_DAYS,
    CONF_SMOOTH_HOURS,
    CONF_TAU_GRACE_MINUTES,
    DEFAULT_DT_MINUTES,
    DEFAULT_DT_STORAGE_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_NAME,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SMOOTH_HOURS,
    DEFAULT_TAU_GRACE_MINUTES,
    DOMAIN_KEY,
    HEATING_DOMAINS,
    MAX_DT_MINUTES,
    MAX_HISTORY_DAYS,
    MAX_RETENTION_DAYS,
    MAX_SMOOTH_HOURS,
    MAX_TAU_GRACE_MINUTES,
    MIN_DT_MINUTES,
    MIN_HISTORY_DAYS,
    MIN_RETENTION_DAYS,
    MIN_SMOOTH_HOURS,
    MIN_TAU_GRACE_MINUTES,
    TEMPERATURE_UNITS,
)

_LOGGER = logging.getLogger(__name__)

PENDING_STATE = ("unknown", "unavailable")


def _looks_like_temperature(hass: HomeAssistant, entity_id: str) -> bool | None:
    """Return True/False, or None when the state cannot be judged yet."""
    state = hass.states.get(entity_id)
    if state is None:
        return False
    if state.state in PENDING_STATE:
        return None
    if state.attributes.get("device_class") == "temperature":
        return True
    return state.attributes.get("unit_of_measurement") in TEMPERATURE_UNITS


def _validate_sources(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """Validate the three mandatory entities (genericity rules live here)."""
    errors: dict[str, str] = {}

    heating = str(user_input.get(CONF_HEATING_ENTITY, ""))
    domain = heating.split(".", 1)[0] if "." in heating else ""
    if domain not in HEATING_DOMAINS:
        errors[CONF_HEATING_ENTITY] = "unsupported_heating_entity"

    for key in (CONF_INDOOR_SENSOR, CONF_OUTDOOR_SENSOR):
        entity_id = str(user_input.get(key, ""))
        verdict = _looks_like_temperature(hass, entity_id)
        if verdict is False:
            state = hass.states.get(entity_id)
            errors[key] = "entity_not_found" if state is None else "not_a_temperature_sensor"
        # verdict is None -> the entity exists but is not reporting yet: accept.

    if user_input.get(CONF_INDOOR_SENSOR) and user_input.get(CONF_INDOOR_SENSOR) == user_input.get(
        CONF_OUTDOOR_SENSOR
    ):
        errors[CONF_OUTDOOR_SENSOR] = "same_entity"

    return errors


def _window_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Identification/storage settings, shared by the flow and the options."""
    return vol.Schema(
        {
            vol.Required(
                CONF_HISTORY_DAYS, default=defaults.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_HISTORY_DAYS,
                    max=MAX_HISTORY_DAYS,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="d",
                )
            ),
            vol.Required(
                CONF_DT_MINUTES, default=defaults.get(CONF_DT_MINUTES, DEFAULT_DT_MINUTES)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_DT_MINUTES,
                    max=MAX_DT_MINUTES,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_DT_STORAGE_MINUTES,
                default=defaults.get(CONF_DT_STORAGE_MINUTES, DEFAULT_DT_STORAGE_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=MAX_DT_MINUTES,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_SMOOTH_HOURS,
                default=defaults.get(CONF_SMOOTH_HOURS, DEFAULT_SMOOTH_HOURS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_SMOOTH_HOURS,
                    max=MAX_SMOOTH_HOURS,
                    step=0.5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="h",
                )
            ),
            vol.Required(
                CONF_TAU_GRACE_MINUTES,
                default=defaults.get(CONF_TAU_GRACE_MINUTES, DEFAULT_TAU_GRACE_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_TAU_GRACE_MINUTES,
                    max=MAX_TAU_GRACE_MINUTES,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_RETENTION_DAYS,
                default=defaults.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_RETENTION_DAYS,
                    max=MAX_RETENTION_DAYS,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="d",
                )
            ),
        }
    )


def _sources_schema(defaults: dict[str, Any]) -> vol.Schema:
    """The three mandatory entities."""
    return vol.Schema(
        {
            vol.Required(
                CONF_HEATING_ENTITY, default=defaults.get(CONF_HEATING_ENTITY, vol.UNDEFINED)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=list(HEATING_DOMAINS))),
            vol.Required(
                CONF_INDOOR_SENSOR, default=defaults.get(CONF_INDOOR_SENSOR, vol.UNDEFINED)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_OUTDOOR_SENSOR, default=defaults.get(CONF_OUTDOOR_SENSOR, vol.UNDEFINED)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        }
    )


class ThermalModelConfigFlow(ConfigFlow, domain=DOMAIN_KEY):
    """Handle the initial setup: three entities are enough to build a model."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the model name and the location (for weather and archive)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip() or DEFAULT_NAME
            await self.async_set_unique_id(slugify(name))
            self._abort_if_unique_id_configured()
            self._data.update(user_input)
            self._data[CONF_NAME] = name
            return await self.async_step_sources()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
                vol.Required(
                    CONF_LATITUDE, default=self.hass.config.latitude
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-90,
                        max=90,
                        step="any",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_LONGITUDE, default=self.hass.config.longitude
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-180,
                        max=180,
                        step="any",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bind the heating entity, the indoor sensor and the outdoor sensor."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self.hass, user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_window()
            return self.async_show_form(
                step_id="sources",
                data_schema=_sources_schema(user_input),
                errors=errors,
            )

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema(self._data),
            errors=errors,
        )

    async def async_step_window(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Identification window and storage settings."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title=str(self._data[CONF_NAME]), data=self._data)

        return self.async_show_form(step_id="window", data_schema=_window_schema(self._data))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-map the three entities without reinstalling the integration."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            errors = _validate_sources(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={**entry.data, **user_input}
                )
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_sources_schema(user_input),
                errors=errors,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_sources_schema(defaults),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return ThermalModelOptionsFlow()


class ThermalModelOptionsFlow(OptionsFlow):
    """Runtime-tunable settings.

    Only connection/source-like settings belong here; tunables that the user
    changes often are exposed as `number` entities once entities exist.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the identification window and storage settings."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_window_schema(defaults))
