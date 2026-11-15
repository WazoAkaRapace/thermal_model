"""The bundled panel: registered by the integration, opened from the sidebar.

These tests check the parts that fail silently in production: the sidebar entry
existing at all, the module being served with a JS content type, the entity ids
handed to the frontend being the real ones, and the JavaScript actually parsing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_model import panel as panel_module
from custom_components.thermal_model.const import DOMAIN_KEY

FRONTEND = Path(panel_module.__file__).parent / "frontend"
PANEL_JS = FRONTEND / f"{panel_module.WEBCOMPONENT_NAME}.js"


def _panel(hass: HomeAssistant) -> Any | None:
    """The registered panel entry, if any (a ``Panel`` object on recent HA)."""
    return hass.data.get("frontend_panels", {}).get(panel_module.PANEL_URL_PATH)


async def test_panel_is_registered_in_the_sidebar(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Without this, the integration ships a dashboard nobody can open."""
    registered = _panel(hass)

    assert registered is not None
    assert registered.component_name == "custom"
    assert registered.sidebar_title == "Home"
    assert registered.require_admin is False

    config = registered.config or {}
    webcomponent = config.get("_panel_custom") or {}
    # Our own keys sit next to the panel_custom block: the frontend reads
    # `panel.config.scenarios`, not a nested dict.
    assert "scenarios" in config and "entry_id" in config
    assert webcomponent.get("name") == panel_module.WEBCOMPONENT_NAME
    module_url = webcomponent.get("module_url", "")
    assert module_url.endswith(f"{panel_module.WEBCOMPONENT_NAME}.js")
    # The version is a path segment, so an update invalidates browser caches
    assert "/frontend/" in module_url
    assert webcomponent.get("embed_iframe") is False


async def test_panel_receives_the_real_entity_ids(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The frontend cannot guess entity ids: they must be resolved server-side."""
    registered = _panel(hass)
    assert registered is not None
    config = registered.config or {}

    assert config["entry_id"] == setup_entry.entry_id
    assert config["indoor_entity"] == "sensor.home_indoor_temperature"
    assert set(config["scenarios"]) == {"free_float", "thermostat"}
    assert config["scenarios"]["free_float"] == "sensor.home_forecast_free_float"
    assert set(config["numbers"]) == {"a", "c_s", "d", "k", "setpoint", "capacity", "horizon"}

    for entity_id in [config["indoor_entity"], *config["scenarios"].values()]:
        assert hass.states.get(entity_id) is not None, entity_id
    for entity_id in config["numbers"].values():
        assert hass.states.get(entity_id) is not None, entity_id


async def test_the_module_is_served_with_a_javascript_content_type(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client: Any
) -> None:
    """A module served as text/plain blanks the panel with no error in HA."""
    registered = _panel(hass)
    assert registered is not None
    module_url = (registered.config or {})["_panel_custom"]["module_url"]

    client = await hass_client()
    response = await client.get(module_url)

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/javascript")
    assert response.headers["Cache-Control"] == "no-cache"
    body = await response.text()
    assert "customElements.define" in body


async def test_the_view_refuses_unknown_files(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client: Any
) -> None:
    """No traversal, no serving arbitrary files out of the package."""
    client = await hass_client()

    for name in ("../__init__.py", "missing.js", "manifest.json"):
        response = await client.get(f"/{DOMAIN_KEY}/frontend/1.0.0/{name}")
        assert response.status == 404, name


async def test_unload_removes_the_panel(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A panel left behind points at entities that no longer exist."""
    assert _panel(hass) is not None

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert _panel(hass) is None


def test_the_panel_module_parses() -> None:
    """A syntax error in the JS blanks the panel: check it without a browser."""
    assert PANEL_JS.exists(), f"missing panel module: {PANEL_JS}"
    source = PANEL_JS.read_text(encoding="utf-8")
    assert f'customElements.define("{panel_module.WEBCOMPONENT_NAME}"' in source

    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in CI
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--check", str(PANEL_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_panel_renders_with_a_stubbed_dom() -> None:
    """Run the real render path in node: `node --check` cannot catch runtime errors."""
    script = Path(__file__).parent / "js" / "panel_smoke.mjs"
    assert script.exists(), f"missing smoke test: {script}"

    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in CI
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_the_panel_has_no_external_dependency() -> None:
    """No CDN, no bundler: the module must run from the integration alone."""
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "http://" not in source.replace("http://www.w3.org", "")
    assert "https://" not in source
    assert "import " not in source.split("*/", 1)[-1]
