"""Comandi websocket usati dal pannello Spettro."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DATA_WS_REGISTERED, DOMAIN, OPT_ENABLED
from .scanner import FFT_SIZES, SAMPLE_RATES, ScanSettings


@callback
def async_setup(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_WS_REGISTERED):
        return
    hass.data[DATA_WS_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_set_config)
    websocket_api.async_register_command(hass, ws_scan_now)
    websocket_api.async_register_command(hass, ws_set_enabled)


@callback
def set_scan_enabled(hass: HomeAssistant, entry: ConfigEntry, enabled: bool) -> None:
    """Attiva o mette in pausa la scansione e lo ricorda tra un riavvio e l'altro."""
    entry.runtime_data.set_enabled(enabled)
    hass.config_entries.async_update_entry(entry, options={**entry.options, OPT_ENABLED: enabled})


def _scanner(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]):
    entry = hass.data.get(DOMAIN)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "L'integrazione Spettro non è caricata")
        return None, None
    return entry, entry.runtime_data


@websocket_api.websocket_command({vol.Required("type"): "spettro/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Invia stato e spettri man mano che arrivano."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return

    @callback
    def forward(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    # il pannello conta come "spettatore": serve all'opzione "solo con il pannello aperto"
    connection.subscriptions[msg["id"]] = scanner.add_listener(forward, viewer=True)
    connection.send_result(msg["id"])
    for payload in scanner.snapshot():
        forward(payload)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/set_config",
        vol.Required("start_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Required("stop_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Required("interval_s"): vol.All(vol.Coerce(float), vol.Range(min=0, max=86400)),
        vol.Optional("gain_db"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0, max=60))),
        vol.Optional("sample_rate"): vol.All(vol.Coerce(int), vol.In(SAMPLE_RATES)),
        vol.Optional("fft_size"): vol.All(vol.Coerce(int), vol.In(FFT_SIZES)),
        vol.Optional("threshold_db"): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
        vol.Optional("settle_ms"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1000)),
        vol.Optional("ppm"): vol.All(vol.Coerce(int), vol.Range(min=-200, max=200)),
        vol.Optional("only_when_viewed"): bool,
    }
)
@websocket_api.require_admin
@callback
def ws_set_config(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Salva i parametri nelle opzioni dell'integrazione e riparte con la scansione."""
    entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    if msg["stop_mhz"] - msg["start_mhz"] < 0.1:
        connection.send_error(msg["id"], "invalid_range", "La frequenza finale deve superare quella iniziale")
        return
    changes = {k: v for k, v in msg.items() if k not in ("id", "type")}
    merged = {**ScanSettings().as_dict(), **entry.options, **changes}
    hass.config_entries.async_update_entry(entry, options=merged)
    scanner.update_settings(ScanSettings.from_dict(merged))
    connection.send_result(msg["id"], merged)


@websocket_api.websocket_command({vol.Required("type"): "spettro/scan_now"})
@callback
def ws_scan_now(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    scanner.scan_now()
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "spettro/set_enabled", vol.Required("enabled"): bool})
@websocket_api.require_admin
@callback
def ws_set_enabled(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    set_scan_enabled(hass, entry, msg["enabled"])
    connection.send_result(msg["id"])
