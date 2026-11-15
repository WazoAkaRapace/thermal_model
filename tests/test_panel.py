"""The bundled panel: registration, serving, and the chart library contract.

These tests cover what fails silently in production: no sidebar entry, a module
served with a content type browsers refuse, entity ids handed to the frontend that
do not exist, a vendored chart library whose version and licence drifted, and a
JavaScript file that does not even parse.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_model import panel as panel_module
from custom_components.thermal_model.const import DOMAIN_KEY

FRONTEND = Path(panel_module.__file__).parent / "frontend"
PANEL_JS = FRONTEND / f"{panel_module.WEBCOMPONENT_NAME}.js"
CHART_JS = FRONTEND / "thermal-model-chart.mjs"
UPLOT_JS = FRONTEND / "uplot.min.js"
UPLOT_CSS = FRONTEND / "uplot.min.css"
UPLOT_LICENSE = FRONTEND / "uplot.LICENSE"
VENDOR_DOC = FRONTEND / "VENDOR.md"
CHART_TEST = Path(__file__).parent / "js" / "chart_test.mjs"

NODE = shutil.which("node")


def _panel(hass: HomeAssistant) -> Any | None:
    """The registered panel entry, if any (a ``Panel`` object on recent HA)."""
    return hass.data.get("frontend_panels", {}).get(panel_module.PANEL_URL_PATH)


def _panel_config(hass: HomeAssistant) -> dict[str, Any]:
    registered = _panel(hass)
    assert registered is not None, "no panel registered"
    return registered.config or {}


# -- registration ---------------------------------------------------------
async def test_panel_is_registered_in_the_sidebar(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Without this, the integration ships a frontend nobody can open."""
    registered = _panel(hass)

    assert registered is not None
    assert registered.component_name == "custom"
    assert registered.sidebar_title == "Home"
    assert registered.require_admin is False

    webcomponent = (registered.config or {}).get("_panel_custom") or {}
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
    config = _panel_config(hass)

    assert config["entry_id"] == setup_entry.entry_id
    assert config["indoor_entity"] == "sensor.home_indoor_temperature"
    assert set(config["scenarios"]) == {"free_float", "thermostat"}
    assert config["scenarios"]["free_float"] == "sensor.home_forecast_free_float"
    assert set(config["numbers"]) == {"a", "c_s", "d", "k", "setpoint", "capacity", "horizon"}

    for entity_id in [config["indoor_entity"], *config["scenarios"].values()]:
        assert hass.states.get(entity_id) is not None, entity_id
    for entity_id in config["numbers"].values():
        assert hass.states.get(entity_id) is not None, entity_id


async def test_unload_removes_the_panel(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A panel left behind points at entities that no longer exist."""
    assert _panel(hass) is not None

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert _panel(hass) is None


# -- serving --------------------------------------------------------------
async def test_every_frontend_file_is_served_as_javascript(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client: Any
) -> None:
    """A module served as text/plain blanks the panel with no error in Home Assistant."""
    module_url = (_panel_config(hass)["_panel_custom"])["module_url"]
    base = module_url.rsplit("/", 1)[0]
    client = await hass_client()

    for name, content_type in (
        (f"{panel_module.WEBCOMPONENT_NAME}.js", "text/javascript"),
        ("thermal-model-chart.mjs", "text/javascript"),
        ("uplot.min.js", "text/javascript"),
    ):
        response = await client.get(f"{base}/{name}")
        assert response.status == 200, name
        assert response.headers["Content-Type"].startswith(content_type), name
        assert response.headers["Cache-Control"] == "no-cache", name

    css = await client.get(f"{base}/uplot.min.css")
    assert css.status == 200
    assert "text/css" in css.headers["Content-Type"]


async def test_the_module_is_reachable_without_a_token(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Regression: the frontend imports the module with no bearer token.

    An authenticated view answers 401 here and Home Assistant shows "Unable to load
    custom panel", which is exactly how this broke the first time on a real
    installation. The module must be fetchable like ``/frontend_latest/*``.
    """
    module_url = (_panel_config(hass)["_panel_custom"])["module_url"]
    base = module_url.rsplit("/", 1)[0]
    client = await hass_client_no_auth()

    for name in (
        f"{panel_module.WEBCOMPONENT_NAME}.js",
        "thermal-model-chart.mjs",
        "uplot.min.js",
        "uplot.min.css",
    ):
        response = await client.get(f"{base}/{name}")
        assert response.status == 200, f"{name} is not reachable without a token"


async def test_the_view_refuses_unknown_files(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """No traversal, no serving arbitrary files out of the package."""
    client = await hass_client_no_auth()

    for name in ("../__init__.py", "missing.js", "manifest.json", "panel.py", "uplot.LICENSE"):
        response = await client.get(f"/{DOMAIN_KEY}/frontend/1.0.0/{name}")
        assert response.status == 404, name


# -- the chart library ----------------------------------------------------
def test_the_chart_library_is_vendored_with_its_licence() -> None:
    """A pinned copy, with its licence and a written reason for the choice."""
    for path in (UPLOT_JS, UPLOT_CSS, UPLOT_LICENSE, VENDOR_DOC):
        assert path.exists(), f"missing vendored file: {path}"

    license_text = UPLOT_LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Leon Sorokin" in license_text

    # the version must be declared in exactly one place, and agree with the doc
    version_match = re.search(r'UPLOT_VERSION = "([0-9.]+)"', CHART_JS.read_text(encoding="utf-8"))
    assert version_match, "UPLOT_VERSION is not declared in the chart module"
    declared = version_match.group(1)
    assert declared in VENDOR_DOC.read_text(encoding="utf-8"), (
        f"VENDOR.md does not mention the vendored version {declared}"
    )


def test_the_frontend_never_loads_anything_remote() -> None:
    """No CDN, no external fetch: the panel must work on an offline installation."""
    for path in (PANEL_JS, CHART_JS):
        source = path.read_text(encoding="utf-8")
        for match in re.findall(r"https?://[^\s\"')]+", source):
            assert "w3.org" in match or "github.com" in match, (
                f"{path.name} references a remote URL: {match}"
            )
        assert "cdn." not in source, f"{path.name} references a CDN"


def test_the_modules_parse() -> None:
    """A syntax error blanks the panel: check both modules without a browser."""
    assert NODE is not None, "node is required to check the frontend modules"
    for path in (PANEL_JS, CHART_JS):
        result = subprocess.run(
            [NODE, "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_the_panel_imports_the_chart_module() -> None:
    """The element must not re-implement the data layer it is tested through."""
    source = PANEL_JS.read_text(encoding="utf-8")
    assert 'from "./thermal-model-chart.mjs"' in source


def test_the_data_layer_passes_its_node_tests() -> None:
    """Alignment, gaps, dashes, scales and ranges, checked outside the browser."""
    assert CHART_TEST.exists(), f"missing chart test: {CHART_TEST}"
    assert NODE is not None, "node is required"
    result = subprocess.run([NODE, str(CHART_TEST)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
