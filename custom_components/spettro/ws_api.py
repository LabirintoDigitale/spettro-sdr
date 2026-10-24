"""Comandi websocket usati dal pannello Spettro."""

from __future__ import annotations

import base64
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .analysis import describe
from .download import FORMATS, IqDownloadView, iq_url
from .const import DATA_WS_REGISTERED, DOMAIN, OPT_ENABLED
from .scanner import ANALYSIS_MAX_S, FFT_SIZES, SAMPLE_RATES, ScanSettings

_LOGGER = logging.getLogger(__name__)

AI_INSTRUCTIONS = """Sei un esperto di radiofrequenza e SDR. Un ricevitore RTL-SDR in Italia ha registrato \
il segnale descritto qui sotto. Identifica che tipo di segnale è con più probabilità (servizio, modulazione, \
protocollo), indica quanto sei sicuro e su quali indizi ti basi (banda, forma nella cascata, durata e ripetizione \
dei burst, andamento della frequenza, banda di appartenenza), e proponi alternative plausibili.

Se è un segnale pubblico e in chiaro che si può decodificare legalmente (es. radio FM e RDS, ADS-B, AIS, APRS, \
sensori meteo e domotici con rtl_433, satelliti meteo, banda aerea in ascolto), indica il programma o il metodo \
per decodificarlo. Se invece è probabilmente una comunicazione privata, cifrata o riservata (telefonia cellulare, \
TETRA, reti dati private), dillo chiaramente e non dare indicazioni per intercettarla o decifrarla: in Italia \
è vietato. Suggerisci infine cosa misurare o registrare per confermare l'ipotesi.

Rispondi in italiano, in modo conciso, con brevi paragrafi o elenchi.

Misure:
"""


@callback
def async_setup(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_WS_REGISTERED):
        return
    hass.data[DATA_WS_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_set_config)
    websocket_api.async_register_command(hass, ws_scan_now)
    websocket_api.async_register_command(hass, ws_dab_scan)
    websocket_api.async_register_command(hass, ws_adsb)
    websocket_api.async_register_command(hass, ws_set_enabled)
    websocket_api.async_register_command(hass, ws_analyze)
    websocket_api.async_register_command(hass, ws_ask_ai)
    websocket_api.async_register_command(hass, ws_archive_list)
    websocket_api.async_register_command(hass, ws_archive_get)
    websocket_api.async_register_command(hass, ws_archive_delete)
    websocket_api.async_register_command(hass, ws_favorite_set)
    websocket_api.async_register_command(hass, ws_favorite_remove)
    websocket_api.async_register_command(hass, ws_live_start)
    websocket_api.async_register_command(hass, ws_live_stop)
    websocket_api.async_register_command(hass, ws_iq_url)
    hass.http.register_view(IqDownloadView())
    websocket_api.async_register_command(hass, ws_meteo_list)
    websocket_api.async_register_command(hass, ws_meteo_get)
    websocket_api.async_register_command(hass, ws_meteo_delete)
    websocket_api.async_register_command(hass, ws_meteo_config)
    websocket_api.async_register_command(hass, ws_meteo_receive)
    websocket_api.async_register_command(hass, ws_meteo_stop)


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
        vol.Optional("analysis_s"): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=ANALYSIS_MAX_S)),
        vol.Optional("auto_analyze"): bool,
        vol.Optional("lnb"): bool,
        vol.Optional("save_iq_auto"): bool,
        vol.Optional("hf_sampling"): vol.All(vol.Coerce(int), vol.In([0, 1, 2])),
        vol.Optional("archive_hours"): vol.All(vol.Coerce(float), vol.Range(min=1, max=24 * 90)),
        vol.Optional("reanalyze_min"): vol.All(vol.Coerce(float), vol.Range(min=0, max=7 * 24 * 60)),
        vol.Optional("auto_limit_step"): bool,
        vol.Optional("auto_max_per_step"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
        vol.Optional("auto_limit_sweep"): bool,
        vol.Optional("auto_max_per_sweep"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100000)),
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
    hass.async_create_task(scanner.prune_archive())  # le ore di conservazione possono essere cambiate
    connection.send_result(msg["id"], merged)


@websocket_api.websocket_command({vol.Required("type"): "spettro/dab_scan", vol.Optional("stop", default=False): bool})
@websocket_api.async_response
async def ws_dab_scan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Ricerca di tutti i multiplex DAB, canale per canale (o la ferma)."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    try:
        if msg["stop"]:
            await scanner.dab_scan_stop()
        else:
            scanner.dab_scan_start()
    except RuntimeError as err:
        connection.send_error(msg["id"], "dab_scan_failed", str(err))
        return
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "spettro/adsb",
    vol.Optional("action", default="start"): vol.In(["start", "stop"]),
    vol.Optional("continuous", default=False): bool,
})
@websocket_api.async_response
async def ws_adsb(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Avvia o ferma il ricevitore aerei ADS-B (1090 MHz)."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    try:
        if msg["action"] == "stop":
            await scanner.adsb_stop()
            connection.send_result(msg["id"], {})
        else:
            connection.send_result(msg["id"], await scanner.adsb_start(msg["continuous"]))
    except RuntimeError as err:
        connection.send_error(msg["id"], "adsb_failed", str(err))


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


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/analyze",
        vol.Required("freq_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Optional("duration_s"): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=ANALYSIS_MAX_S)),
    }
)
@websocket_api.async_response
async def ws_analyze(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Registra il segnale scelto (la scansione va in pausa) e restituisce misure e cascata."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    try:
        result = await scanner.analyze(msg["freq_mhz"] * 1e6, msg.get("duration_s", scanner.settings.analysis_s))
    except (OSError, TimeoutError, RuntimeError, ValueError) as err:
        connection.send_error(msg["id"], "analysis_failed", str(err) or type(err).__name__)
        return
    connection.send_result(msg["id"], _with_media(result, scanner.last_analysis_png, scanner.last_analysis_audio))


def _with_media(result: dict[str, Any], png: bytes, audio: dict[str, bytes]) -> dict[str, Any]:
    """Analisi con cascata e audio in base64, come le usa il pannello."""
    return {
        **result,
        "png": base64.b64encode(png).decode(),
        "audio": {mode: base64.b64encode(wav).decode() for mode, wav in audio.items()},
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/ask_ai",
        vol.Optional("entity_id"): vol.Any(None, str),
        vol.Optional("question"): vol.Any(None, str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_ask_ai(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Chiede all'entità AI Task di Home Assistant di riconoscere l'ultimo segnale analizzato."""
    entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    if scanner.last_analysis is None:
        connection.send_error(msg["id"], "no_analysis", "Analizza prima un segnale")
        return
    if not hass.services.has_service("ai_task", "generate_data"):
        connection.send_error(
            msg["id"], "no_ai_task",
            "AI Task non è disponibile: serve Home Assistant 2025.8 o successivo e un'integrazione AI configurata",
        )
        return
    instructions = AI_INSTRUCTIONS + describe(scanner.last_analysis)
    if msg.get("question"):
        instructions += f"\n\nDomanda dell'utente: {msg['question']}"
    data: dict[str, Any] = {"task_name": "Spettro: identifica segnale", "instructions": instructions}
    if msg.get("entity_id"):
        data["entity_id"] = msg["entity_id"]
    image_id = er.async_get(hass).async_get_entity_id("image", DOMAIN, f"{entry.entry_id}_analysis")

    async def ask(with_image: bool) -> Any:
        call = dict(data)
        if with_image and image_id:
            call["attachments"] = [{"media_content_id": f"media-source://image/{image_id}", "media_content_type": "image/png"}]
        return await hass.services.async_call("ai_task", "generate_data", call, blocking=True, return_response=True)

    with_image = True
    try:
        try:
            response = await ask(True)
        except HomeAssistantError as err:
            if "attachment" not in str(err).lower():
                raise
            # il modello scelto non accetta immagini: si riprova con le sole misure
            _LOGGER.debug("AI Task senza allegati: %s", err)
            with_image = False
            response = await ask(False)
    except (HomeAssistantError, vol.Invalid) as err:
        connection.send_error(msg["id"], "ai_failed", str(err))
        return
    answer = response.get("data") if isinstance(response, dict) else response
    connection.send_result(msg["id"], {"answer": str(answer), "with_image": with_image})


@websocket_api.websocket_command({vol.Required("type"): "spettro/archive_list"})
@callback
def ws_archive_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Analisi archiviate, dalla più recente."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    entries = scanner.archive.entries if scanner.archive else []
    connection.send_result(msg["id"], {"entries": entries, "hours": scanner.settings.archive_hours})


@websocket_api.websocket_command({vol.Required("type"): "spettro/archive_get", vol.Required("entry_id"): str})
@websocket_api.async_response
async def ws_archive_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Un'analisi dell'archivio completa di cascata e audio."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    found = await hass.async_add_executor_job(scanner.archive.get, msg["entry_id"]) if scanner.archive else None
    if found is None:
        connection.send_error(msg["id"], "not_found", "Analisi non più presente nell'archivio")
        return
    result, png, audio = found
    connection.send_result(msg["id"], _with_media(result, png, audio))


@websocket_api.websocket_command({vol.Required("type"): "spettro/archive_delete", vol.Optional("entry_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_archive_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Cancella un'analisi dall'archivio, o tutto l'archivio se entry_id manca."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    if scanner.archive is None:
        connection.send_result(msg["id"], {"removed": 0})
        return
    if msg.get("entry_id"):
        removed = int(await hass.async_add_executor_job(scanner.archive.delete, msg["entry_id"]))
    else:
        removed = await hass.async_add_executor_job(scanner.archive.clear)
    connection.send_result(msg["id"], {"removed": removed})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/favorite_set",
        vol.Required("freq_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Optional("name"): vol.Any(None, str),
        vol.Optional("source", default="user"): vol.In(["user", "rds", "dab"]),
        vol.Optional("sid"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def ws_favorite_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Aggiunge una frequenza ai preferiti (o ne cambia il nome)."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    await scanner.favorites.async_set(msg["freq_mhz"], msg.get("name"), msg["source"], msg.get("sid"))
    scanner.favorites_changed()
    connection.send_result(msg["id"], {"favorites": scanner.favorites.items})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/favorite_remove",
        vol.Required("freq_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Optional("sid"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def ws_favorite_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    await scanner.favorites.async_remove(msg["freq_mhz"], msg.get("sid"))
    scanner.favorites_changed()
    connection.send_result(msg["id"], {"favorites": scanner.favorites.items})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/live_start",
        vol.Required("freq_mhz"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=2200)),
        vol.Optional("mode"): vol.Any(None, vol.In(["wfm", "nfm", "am", "usb", "lsb", "cw", "dab", "tv", "sat"])),
        vol.Optional("sid"): vol.Any(None, str),
        vol.Optional("decoder"): vol.Any(None, vol.In(["sstv", "fax", "cw", "rtty45", "rtty50", "ft8", "wspr", "dati"])),
        vol.Optional("bw"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=100, max=50000))),
        vol.Optional("title"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def ws_live_start(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Analisi continua: chiavetta fissa sulla frequenza, scansione sospesa."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    try:
        live = await scanner.live_start(msg["freq_mhz"] * 1e6, msg.get("mode"), msg.get("sid"),
                                        decoder=msg.get("decoder"), title=msg.get("title"),
                                        bw=msg.get("bw"))
    except RuntimeError as err:
        connection.send_error(msg["id"], "live_failed", str(err))
        return
    connection.send_result(msg["id"], live)


@websocket_api.websocket_command({vol.Required("type"): "spettro/live_stop"})
@websocket_api.async_response
async def ws_live_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    await scanner.live_stop()
    connection.send_result(msg["id"])


# --- satelliti meteo ----------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "spettro/meteo_list"})
@callback
def ws_meteo_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Immagini ricevute e prossimi passaggi."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    archive = scanner.meteo_archive
    connection.send_result(msg["id"], {
        "entries": archive.entries if archive else [],
        "days": scanner.settings.meteo_days,
        "passes": scanner.meteo.snapshot() if scanner.meteo else None,
    })


@websocket_api.websocket_command(
    {vol.Required("type"): "spettro/meteo_get", vol.Required("entry_id"): str, vol.Optional("ir", default=False): bool}
)
@websocket_api.async_response
async def ws_meteo_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Un'immagine meteo (PNG in base64)."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    archive = scanner.meteo_archive
    png = await hass.async_add_executor_job(archive.get, msg["entry_id"], msg["ir"]) if archive else None
    if png is None:
        connection.send_error(msg["id"], "not_found", "Immagine non trovata")
        return
    meta = next(e for e in archive.entries if e["id"] == msg["entry_id"])
    connection.send_result(msg["id"], {**meta, "png": base64.b64encode(png).decode()})


@websocket_api.websocket_command({vol.Required("type"): "spettro/meteo_delete", vol.Optional("entry_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_meteo_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Cancella un'immagine meteo, o tutte senza entry_id."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None or scanner.meteo_archive is None:
        return
    n = await hass.async_add_executor_job(scanner.meteo_archive.delete, msg.get("entry_id"))
    scanner._emit({"kind": "meteo_pruned"})  # noqa: SLF001 - avvisa i pannelli
    connection.send_result(msg["id"], {"deleted": n})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spettro/meteo_config",
        vol.Optional("meteo_auto"): bool,
        vol.Optional("meteo_min_el"): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
        vol.Optional("meteo_days"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=365)),
        vol.Optional("iss_sstv"): bool,
        vol.Optional("hf_sampling"): vol.All(vol.Coerce(int), vol.In([0, 1, 2])),
    }
)
@websocket_api.require_admin
@callback
def ws_meteo_config(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Impostazioni dei satelliti meteo (senza toccare quelle della scansione)."""
    entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    changes = {k: v for k, v in msg.items() if k not in ("id", "type")}
    merged = {**ScanSettings().as_dict(), **entry.options, **changes}
    hass.config_entries.async_update_entry(entry, options=merged)
    scanner.settings = ScanSettings.from_dict(merged)
    scanner.status["settings"] = scanner.settings.as_dict()
    scanner._emit(dict(scanner.status))  # noqa: SLF001
    hass.async_create_task(scanner.prune_meteo())
    connection.send_result(msg["id"], merged)


@websocket_api.websocket_command({vol.Required("type"): "spettro/meteo_receive", vol.Optional("norad"): int})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_meteo_receive(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Riceve subito il passaggio in corso."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    try:
        p = await scanner.meteo_receive(msg.get("norad"))
    except RuntimeError as err:
        connection.send_error(msg["id"], "meteo_failed", str(err))
        return
    connection.send_result(msg["id"], p)


@websocket_api.websocket_command({vol.Required("type"): "spettro/meteo_stop"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_meteo_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Interrompe la ricezione (l'immagine ricevuta fin lì viene salvata)."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    await scanner.meteo_stop()
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {vol.Required("type"): "spettro/iq_url", vol.Required("entry_id"): str, vol.Required("format"): vol.In(FORMATS)}
)
@callback
def ws_iq_url(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Link firmato (valido 10 minuti) per scaricare la registrazione IQ di un'analisi."""
    _entry, scanner = _scanner(hass, connection, msg)
    if scanner is None:
        return
    url = async_sign_path(hass, iq_url(msg["entry_id"], msg["format"]), timedelta(minutes=10),
                          refresh_token_id=connection.refresh_token_id)
    connection.send_result(msg["id"], {"url": url})
