"""Spettro: analizzatore di spettro per server rtl_tcp con pannello laterale."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from . import ws_api
from .const import (
    DATA_STATIC_REGISTERED,
    DOMAIN,
    OPT_ENABLED,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_URL,
    VERSION,
)
from .scanner import ScanSettings, SpectrumScanner

PLATFORMS = [Platform.IMAGE, Platform.SENSOR, Platform.SWITCH]

type SpettroConfigEntry = ConfigEntry[SpectrumScanner]


async def async_setup_entry(hass: HomeAssistant, entry: SpettroConfigEntry) -> bool:
    """Avvia lo scanner, registra il pannello e i comandi websocket."""
    settings = ScanSettings.from_dict({**ScanSettings().as_dict(), **entry.options})
    scanner = SpectrumScanner(
        entry.data[CONF_HOST], entry.data[CONF_PORT], settings, enabled=entry.options.get(OPT_ENABLED, True)
    )
    entry.runtime_data = scanner
    hass.data[DOMAIN] = entry

    if not hass.data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
        )
        hass.data[DATA_STATIC_REGISTERED] = True

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/spettro-panel.js?v={VERSION}",
        require_admin=False,
        config={},
    )
    ws_api.async_setup(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    scanner.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpettroConfigEntry) -> bool:
    """Ferma lo scanner e rimuove il pannello."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.stop()
        frontend.async_remove_panel(hass, PANEL_URL)
        hass.data.pop(DOMAIN, None)
    return unload_ok
