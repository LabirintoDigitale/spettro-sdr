"""Spettro: analizzatore di spettro per server rtl_tcp con pannello laterale."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import ws_api
from .const import (
    ARCHIVE_DIR,
    DATA_STATIC_REGISTERED,
    DOMAIN,
    METEO_DIR,
    OPT_ENABLED,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_URL,
    VERSION,
)
from .archive import SpikeArchive
from .favorites import Favorites
from .scanner import ScanSettings, SpectrumScanner
from .weather import Meteo, MeteoArchive

PLATFORMS = [Platform.IMAGE, Platform.SENSOR, Platform.SWITCH]

type SpettroConfigEntry = ConfigEntry[SpectrumScanner]


async def async_setup_entry(hass: HomeAssistant, entry: SpettroConfigEntry) -> bool:
    """Avvia lo scanner, registra il pannello e i comandi websocket."""
    settings = ScanSettings.from_dict({**ScanSettings().as_dict(), **entry.options})
    scanner = SpectrumScanner(
        entry.data[CONF_HOST], entry.data[CONF_PORT], settings, enabled=entry.options.get(OPT_ENABLED, True)
    )
    archive = SpikeArchive(Path(hass.config.path(ARCHIVE_DIR)))
    await hass.async_add_executor_job(archive.load)
    scanner.archive = archive
    scanner.favorites = Favorites(hass)
    await scanner.favorites.async_load()
    await scanner.prune_archive()
    await _setup_meteo(hass, scanner)
    scanner.status["version"] = VERSION
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


async def _setup_meteo(hass: HomeAssistant, scanner: SpectrumScanner) -> None:
    """Satelliti meteo: posizione della stazione, TLE salvati (e scaricati da CelesTrak), archivio immagini."""
    session = async_get_clientsession(hass)

    async def fetch(url: str) -> str:
        async with session.get(url, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.text()

    meteo = Meteo(hass.config.latitude, hass.config.longitude, hass.config.elevation or 0, fetch)
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}.tle")
    saved = await store.async_load()
    if saved and saved.get("tles"):
        meteo.set_tles({int(k): tuple(v) for k, v in saved["tles"].items()}, saved.get("time", 0))

    def save() -> None:
        hass.async_create_task(store.async_save({"tles": {str(k): list(v) for k, v in meteo.tle_text.items()},
                                                 "time": meteo.tle_time}))

    archive = MeteoArchive(Path(hass.config.path(METEO_DIR)))
    await hass.async_add_executor_job(archive.load)
    scanner.meteo, scanner.meteo_save, scanner.meteo_archive = meteo, save, archive
    if archive.entries:
        scanner.last_meteo_png = await hass.async_add_executor_job(archive.get, archive.entries[0]["id"])
    await scanner.prune_meteo()


async def async_unload_entry(hass: HomeAssistant, entry: SpettroConfigEntry) -> bool:
    """Ferma lo scanner e rimuove il pannello."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.stop()
        frontend.async_remove_panel(hass, PANEL_URL)
        hass.data.pop(DOMAIN, None)
    return unload_ok
