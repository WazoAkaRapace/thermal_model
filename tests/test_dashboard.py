"""The shipped dashboard must reference entities and attributes that exist.

A dashboard is a config file full of string identifiers: a renamed entity turns
it into a page of "Entity not available" with no error anywhere. This test walks
the YAML and checks every reference against a live config entry, including the
attribute names used by the apexcharts ``data_generator`` blocks and by the
markdown card.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboards" / "thermal_model.yaml"

# Attributes the dashboard reads through state_attr(...) or entity.attributes
EXPECTED_ATTRIBUTES = (
    "times",
    "values",
    "weather_times",
    "weather_outdoor",
    "archived_runs",
    "issued_at",
    "stale",
    "error",
    "minimum",
    "minimum_at",
    "horizon_hours",
    "weather_model",
    "params_source",
    "params_digest",
)


@pytest.fixture
def dashboard() -> dict[str, Any]:
    """The shipped dashboard, parsed."""
    assert DASHBOARD.exists(), f"missing dashboard: {DASHBOARD}"
    with DASHBOARD.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _walk(node: Any, key: str = "") -> Any:
    """Yield (key, value) pairs anywhere in a nested structure."""
    if isinstance(node, dict):
        for sub_key, value in node.items():
            yield sub_key, value
            yield from _walk(value, sub_key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)


def _referenced_entities(dashboard: dict[str, Any]) -> set[str]:
    """Every entity id mentioned as an ``entity`` value."""
    return {
        str(value)
        for key, value in _walk(dashboard)
        if key == "entity" and isinstance(value, str) and "." in value
    }


def _template_entities(dashboard: dict[str, Any]) -> set[str]:
    """Entity ids hard-coded inside markdown templates."""
    found: set[str] = set()
    for _key, value in _walk(dashboard):
        if isinstance(value, str):
            found.update(re.findall(r"'((?:sensor|number|climate)\.[a-z0-9_]+)'", value))
    return found


def test_dashboard_is_valid_yaml_with_views(dashboard: dict[str, Any]) -> None:
    """A malformed dashboard is only discovered by the user, in the UI."""
    assert dashboard["title"] == "Thermal Model"
    assert len(dashboard["views"]) >= 2
    assert {view["path"] for view in dashboard["views"]} == {"previsions", "repli"}


def test_every_referenced_entity_exists(
    hass: HomeAssistant, setup_entry: MockConfigEntry, dashboard: dict[str, Any]
) -> None:
    """Every entity id in the dashboard must be live after setup."""
    referenced = _referenced_entities(dashboard) | _template_entities(dashboard)
    assert referenced, "the dashboard references no entity at all"

    missing = sorted(entity_id for entity_id in referenced if hass.states.get(entity_id) is None)
    assert not missing, f"dashboard references unknown entities: {missing}"


def test_the_measured_and_forecast_series_are_both_present(
    hass: HomeAssistant, setup_entry: MockConfigEntry, dashboard: dict[str, Any]
) -> None:
    """The point of the dashboard: reality, forecast, and past forecasts."""
    referenced = _referenced_entities(dashboard)

    assert "sensor.home_indoor_temperature" in referenced
    assert "sensor.home_forecast_free_float" in referenced
    assert "sensor.home_forecast_thermostat" in referenced


def test_declared_attributes_exist_on_the_forecast_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry, dashboard: dict[str, Any]
) -> None:
    """The data_generator blocks read attributes by name: they must be there."""
    state = hass.states.get("sensor.home_forecast_free_float")
    assert state is not None

    missing = [name for name in EXPECTED_ATTRIBUTES if name not in state.attributes]
    assert not missing, f"forecast sensor is missing attributes: {missing}"


def test_trajectory_lengths_are_consistent(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A chart with mismatched x/y lengths draws a wrong curve, silently."""
    state = hass.states.get("sensor.home_forecast_free_float")
    assert state is not None
    attributes = state.attributes

    assert len(attributes["times"]) == len(attributes["values"]) == 49
    for run in attributes["archived_runs"]:
        assert len(run["times"]) == len(run["values"])
    assert len(attributes["weather_times"]) == len(attributes["weather_outdoor"])


def test_dashboard_has_a_native_fallback_view(dashboard: dict[str, Any]) -> None:
    """apexcharts-card is a HACS dependency: the dashboard must degrade."""
    views = {view["path"]: view for view in dashboard["views"]}
    card_types = [card["type"] for card in views["repli"]["cards"]]

    assert "custom:apexcharts-card" not in card_types
    assert "history-graph" in card_types
