"""The bundled panel: a real frontend, served by the integration itself.

Why a panel and not a Lovelace dashboard: a dashboard is a YAML document full of
entity ids that the user has to import, keep in sync and adapt; it can only show
what existing cards can draw. A panel is shipped *by* the integration, opens from
the sidebar on every installation, receives the Home Assistant object directly
and renders exactly what this integration computes — a measured line, the current
forecast per scenario and the forecasts issued hours ago, on one time axis.

The shape follows what Home Assistant itself does for panels:

- ``panel_custom.async_register_panel`` registers the sidebar entry and points it
  at an ES module;
- that module is served by this integration through an authenticated view, with
  the integration version in the path so a browser never mixes an old module with
  new ones after an update;
- ``requirements`` stays empty: the panel is plain DOM and SVG, no framework, no
  build step, nothing fetched from a CDN.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components import panel_custom
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import ThermalModelRuntimeData
from .const import DOMAIN_KEY

__all__ = [
    "PANEL_URL_PATH",
    "WEBCOMPONENT_NAME",
    "async_register_panel",
    "async_unregister_panel",
]

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "thermal-model"
WEBCOMPONENT_NAME = "thermal-model-panel"
FRONTEND_DIR = Path(__file__).parent / "frontend"
VIEW_NAME = "api:thermal_model:frontend"
VIEW_URL = f"/{DOMAIN_KEY}/frontend/{{version}}/{{file_name}}"

_ALLOWED_SUFFIXES = {".js", ".mjs", ".css", ".svg", ".png"}


class PanelFilesView(HomeAssistantView):
    """Serve the panel's static files, cache-safe.

    ``requires_auth = False`` on purpose: the frontend loads a panel module with a
    plain ``import()``, which carries no bearer token (Home Assistant keeps its
    tokens in localStorage, not in a cookie), so an authenticated view answers 401
    and the browser reports "Unable to load custom panel". These files hold no user
    data — the panel reads every value over the authenticated websocket — so
    serving them like Home Assistant serves ``/frontend_latest/*`` is the correct
    trade.
    """

    url = VIEW_URL
    name = VIEW_NAME
    requires_auth = False

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def get(self, request: web.Request, version: str, file_name: str) -> web.StreamResponse:
        """Return one file from the frontend directory."""
        # No traversal: the name is pinned to the folder and the extension is known
        candidate = (self.directory / file_name).resolve()
        if (
            candidate.parent != self.directory.resolve()
            or candidate.suffix not in _ALLOWED_SUFFIXES
            or not candidate.is_file()
        ):
            return web.Response(status=404, text="not found")

        response = web.FileResponse(path=str(candidate))
        # The panel changes on every integration update: revalidate instead of
        # letting the browser serve a stale module.
        response.headers["Cache-Control"] = "no-cache"
        if candidate.suffix in (".js", ".mjs"):
            # An ES module served with a non-JS MIME type is refused by browsers,
            # and some hosts map .js to text/plain.
            response.headers["Content-Type"] = "text/javascript"
        else:
            guessed, _ = mimetypes.guess_type(candidate.name)
            if guessed:
                response.headers["Content-Type"] = guessed
        return response


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    """Resolve an entity id from its unique id (ids are the registry's business)."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN_KEY, unique_id)


def panel_config(
    hass: HomeAssistant, entry_id: str, runtime: ThermalModelRuntimeData
) -> dict[str, Any]:
    """What the frontend needs to know: entity ids, labels and scenarios.

    Resolved from the entity registry rather than guessed from the device name,
    so renaming the device or the entities does not break the panel.
    """
    scenarios: dict[str, str] = {}
    for scenario in runtime.scenarios:
        entity_id = _entity_id(hass, "sensor", f"{DOMAIN_KEY}_{entry_id}_forecast_{scenario}")
        if entity_id:
            scenarios[str(scenario)] = entity_id

    numbers: dict[str, str] = {}
    for key in ("a", "c_s", "d", "k", "setpoint", "capacity", "horizon"):
        entity_id = _entity_id(hass, "number", f"{DOMAIN_KEY}_{entry_id}_number_{key}")
        if entity_id:
            numbers[key] = entity_id

    return {
        "entry_id": entry_id,
        "indoor_entity": _entity_id(hass, "sensor", f"{DOMAIN_KEY}_{entry_id}_indoor_temperature"),
        "source_indoor_entity": runtime.sources.indoor_sensor,
        "source_outdoor_entity": runtime.sources.outdoor_sensor,
        "heating_entity": runtime.sources.heating_entity,
        "scenarios": scenarios,
        "numbers": numbers,
    }


async def async_register_panel(
    hass: HomeAssistant,
    entry_id: str,
    runtime: ThermalModelRuntimeData,
) -> None:
    """Register the sidebar panel and the view that serves it."""
    if hass.data.get(f"{DOMAIN_KEY}_view_registered") is not True:
        hass.http.register_view(PanelFilesView(FRONTEND_DIR))
        hass.data[f"{DOMAIN_KEY}_view_registered"] = True

    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN_KEY)
    version = str(integration.version or "0")
    module_url = f"/{DOMAIN_KEY}/frontend/{version}/{WEBCOMPONENT_NAME}.js"

    # A panel left over from an earlier load would keep the old query config.
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        async_remove_panel(hass, PANEL_URL_PATH)

    config = panel_config(hass, entry_id, runtime)
    if not config["indoor_entity"] or not config["scenarios"]:
        _LOGGER.warning(
            "Thermal Model: panel not registered yet (entities missing from the registry)"
        )
        return

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT_NAME,
        module_url=module_url,
        sidebar_title=runtime.panel_title,
        sidebar_icon="mdi:home-thermometer",
        require_admin=False,
        embed_iframe=False,
        config=config,
    )
    _LOGGER.debug("Thermal Model: panel registered at /%s (module %s)", PANEL_URL_PATH, module_url)


async def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar entry when the last config entry unloads."""
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        async_remove_panel(hass, PANEL_URL_PATH)


def default_panel_title(entry_data: dict[str, Any]) -> str:
    """Human title for the sidebar entry."""
    return str(entry_data.get(CONF_NAME) or "Thermal Model")
