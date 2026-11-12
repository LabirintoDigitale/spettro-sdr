"""Radio di Spettro in Music Assistant, senza provider da installare.

Ogni preferito che è una radio (FM 87,5–108 MHz o una radio DAB+) diventa una radio «per indirizzo»
nella libreria di Music Assistant (provider builtin), tramite le sue API (POST /api, token Bearer).
L'indirizzo punta a uno stream di Spettro dentro Home Assistant (RadioStreamView): quando Music
Assistant lo apre, Spettro sintonizza la chiavetta dell'Ascolto e manda l'audio come WAV, con il nome
della radio e il testo RDS/DLS come metadati ICY. Togliendo il preferito, la radio sparisce anche da
Music Assistant.

Music Assistant apre l'indirizzo anche solo per capirne il formato (ffprobe, e una richiesta per le
intestazioni): all'inizio si manda silenzio e si sintonizza solo se la connessione resta aperta.

DAB+: le access unit AAC (blocchi da 960 campioni) si impacchettano in LOAS/LATM, l'unico formato che
descrive i 960 campioni, e le decodifica l'ffmpeg di Home Assistant. ffmpeg non ha l'SBR a 960
campioni: si decodifica il solo nucleo AAC (meno acuti, niente stereo PS).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import contextlib
import hashlib
import hmac
import logging
import secrets
import shutil
import struct
import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .demod import AUDIO_RATE

if TYPE_CHECKING:
    from .scanner import SpectrumScanner

_LOGGER = logging.getLogger(__name__)

STREAM_PATH = "/api/spettro/radio"
MARKER = "/api/spettro/radio/"  # riconosce nella libreria di MA le radio messe da Spettro
LIVE_TITLE = "🎵 Music Assistant"  # titolo della diretta avviata da uno stream (lo mostra il pannello)
TUNE_DELAY_S = 1.5  # silenzio prima di sintonizzare: le connessioni di prova si chiudono prima
STOP_GRACE_S = 6.0  # attesa prima di liberare la chiavetta quando MA chiude lo stream
PREEMPT_S = 10.0  # dopo che un'altra funzione ha preso la chiavetta, MA non la riprende subito
ICY_METAINT = 16000
DAB_OUT_RATE = 48000
SYNC_DEBOUNCE_S = 3.0
FM_MIN, FM_MAX = 87.5, 108.0


def fav_key(fav: dict[str, Any]) -> str:
    """Identificativo stabile di un preferito negli indirizzi: frequenza in centinaia di Hz (+ SId)."""
    key = str(int(round(float(fav["freq_mhz"]) * 1e4)))
    return f"{key}-{fav['sid'].upper()}" if fav.get("sid") else key


def is_radio(fav: dict[str, Any]) -> bool:
    """Va in Music Assistant: una radio DAB+, oppure una FM in banda broadcast demodulata in FM larga."""
    if fav.get("sid"):
        return True
    return FM_MIN <= float(fav["freq_mhz"]) <= FM_MAX and fav.get("mode") in (None, "", "wfm") and not fav.get("decoder")


def wav_header(rate: int, channels: int) -> bytes:
    """Intestazione WAV per uno stream senza fine (dimensioni al massimo)."""
    block = channels * 2
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class _Bits:
    def __init__(self) -> None:
        self.bits: list[str] = []

    def put(self, value: int, n: int) -> None:
        self.bits.append(format(value, f"0{n}b") if n else "")

    def raw(self, bitstring: str) -> None:
        self.bits.append(bitstring)


def asc_bits(asc_hex: str) -> str:
    """AudioSpecificConfig AAC-LC senza estensioni: 5+4+4+3 = 16 bit esatti (niente riempimento)."""
    return format(int(asc_hex[:4], 16), "016b")


def loas_frame(au: bytes, asc: str) -> bytes:
    """Una access unit AAC in un AudioSyncStream LOAS (AudioMuxElement con StreamMuxConfig ogni volta)."""
    b = _Bits()
    b.put(0, 1)        # useSameStreamMux = 0: la configurazione è in ogni trama
    b.put(0, 1)        # audioMuxVersion
    b.put(1, 1)        # allStreamsSameTimeFraming
    b.put(0, 6)        # numSubFrames
    b.put(0, 4)        # numProgram
    b.put(0, 3)        # numLayer
    b.raw(asc)         # AudioSpecificConfig
    b.put(0, 3)        # frameLengthType = 0 (lunghezza variabile)
    b.put(0xFF, 8)     # latmBufferFullness
    b.put(0, 1)        # otherDataPresent
    b.put(0, 1)        # crcCheckPresent
    n = len(au)
    while n >= 255:    # PayloadLengthInfo
        b.put(255, 8)
        n -= 255
    b.put(n, 8)
    # la access unit non cade sul confine di byte (la configurazione è di 50+ bit): si accoda bit per bit
    s = "".join(b.bits) + "".join(format(x, "08b") for x in au)
    s += "0" * (-len(s) % 8)
    element = int(s, 2).to_bytes(len(s) // 8, "big")
    head = (0x2B7 << 13) | len(element)  # syncword 11 bit + audioMuxLengthBytes 13 bit
    return head.to_bytes(3, "big") + element


class MusicAssistantSync:
    """Tiene allineate le radio della libreria di Music Assistant con i preferiti radio di Spettro."""

    def __init__(self, hass: HomeAssistant, scanner: SpectrumScanner, save_key: Callable[[str], None]) -> None:
        self.hass = hass
        self.scanner = scanner
        self._save_key = save_key
        self._handle: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()
        self.status: dict[str, Any] = {"ok": None, "error": None, "count": 0, "time": None}
        # preferito -> istante in cui un'altra funzione gli ha tolto la chiavetta durante lo stream
        self.preempted: dict[str, float] = {}
        self.last_access: dict[str, Any] | None = None

    # --- impostazioni -------------------------------------------------------

    @property
    def settings(self) -> Any:
        return self.scanner.settings

    def key(self) -> str:
        """Segreto negli indirizzi degli stream (generato e salvato alla prima attivazione)."""
        if not self.settings.ma_key:
            new = secrets.token_urlsafe(18)
            self.settings.ma_key = new
            self._save_key(new)
        return self.settings.ma_key

    def _ma_entry_data(self) -> dict[str, Any]:
        entries = self.hass.config_entries.async_entries("music_assistant")
        return dict(entries[0].data) if entries else {}

    def ma_url(self) -> str:
        """Indirizzo di Music Assistant: quello scritto, altrimenti quello dell'integrazione Music Assistant."""
        url = (self.settings.ma_url or "").strip() or str(self._ma_entry_data().get("url") or "")
        return url.rstrip("/")

    def token(self) -> str:
        if self.settings.ma_token:
            return self.settings.ma_token
        data = self._ma_entry_data()
        return str(data.get("token") or data.get("access_token") or "")

    def stream_base(self) -> str:
        """Come Music Assistant raggiunge Home Assistant per aprire gli stream."""
        url = (self.settings.ma_stream_url or "").strip().rstrip("/")
        if url:
            return url
        api = self.hass.config.api
        port = api.port if api else 8123
        scheme = "https" if api and api.use_ssl else "http"
        if "hassio" in self.hass.config.components:
            # add-on (HA OS / supervised): nella rete interna Home Assistant si chiama «homeassistant»
            return f"{scheme}://homeassistant:{port}"
        from homeassistant.helpers.network import NoURLAvailableError, get_url  # noqa: PLC0415
        try:
            return get_url(self.hass, allow_external=False, allow_cloud=False, prefer_external=False).rstrip("/")
        except NoURLAvailableError:
            return f"{scheme}://127.0.0.1:{port}"

    def radio_url(self, fav: dict[str, Any]) -> str:
        # il nome nell'indirizzo (come impronta): rinominando il preferito cambia l'indirizzo, così MA
        # non usa le informazioni (nome ICY) che ha in cache per quello vecchio
        n = hashlib.sha1((fav.get("name") or "").encode()).hexdigest()[:8]
        return f"{self.stream_base()}{STREAM_PATH}/{self.key()}/{fav_key(fav)}.wav?n={n}"

    # --- API di Music Assistant -------------------------------------------------

    async def _call(self, command: str, **args: Any) -> Any:
        url = self.ma_url()
        if not url:
            raise RuntimeError("indirizzo di Music Assistant mancante")
        token = self.token()
        if not token:
            raise RuntimeError("token di Music Assistant mancante")
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{url}/api",
                json={"command": command, "args": args, "message_id": secrets.token_hex(4)},
                headers={"Authorization": f"Bearer {token}"},
                timeout=ClientTimeout(total=60),
            ) as resp:
                if resp.status in (401, 403):
                    raise RuntimeError(f"Music Assistant rifiuta il token ({resp.status}): creane uno nuovo nel profilo")
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    raise RuntimeError(f"Music Assistant ha risposto {resp.status}: {text}")
                return await resp.json(content_type=None)
        except (ClientError, TimeoutError) as err:
            raise RuntimeError(f"Music Assistant non raggiungibile su {url}: {err or type(err).__name__}") from err

    async def _library_radios(self) -> dict[str, Any]:
        """Radio messe da Spettro nella libreria: indirizzo -> id della libreria."""
        out: dict[str, Any] = {}
        offset = 0
        while True:
            items = await self._call("music/radios/library_items", limit=500, offset=offset, summary=False)
            if isinstance(items, dict):  # alcune versioni rispondono con {"items": [...]}
                items = items.get("items", [])
            for it in items or []:
                for m in it.get("provider_mappings") or []:
                    iid = str(m.get("item_id") or "")
                    if MARKER in iid:
                        out[iid] = it.get("item_id")
            if not items or len(items) < 500:
                return out
            offset += 500

    @callback
    def schedule(self, delay: float = SYNC_DEBOUNCE_S) -> None:
        """Sincronizza tra poco (più modifiche ravvicinate ai preferiti = una sola sincronizzazione)."""
        if self._handle is not None:
            self._handle.cancel()
        self._handle = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self.sync(), "spettro_ma_sync"))

    async def sync(self) -> dict[str, Any]:
        """Aggiunge a Music Assistant i preferiti radio mancanti e toglie quelli non più preferiti."""
        self._handle = None
        if not self.settings.ma_enabled:
            self.status = {"ok": None, "error": None, "count": 0, "time": None}
            self._emit()
            return self.status
        async with self._lock:
            try:
                favs = [f for f in (self.scanner.favorites.items if self.scanner.favorites else []) if is_radio(f)]
                want = {self.radio_url(f): f for f in favs}
                have = await self._library_radios()
                removed = added = 0
                for url, lib_id in have.items():
                    if url not in want:
                        await self._call("music/library/remove_item", media_type="radio", library_item_id=lib_id)
                        removed += 1
                errors = []
                for url, fav in want.items():
                    if url in have:
                        continue
                    try:
                        await self._add(url, fav)
                        added += 1
                    except RuntimeError as err:
                        errors.append(f"{fav.get('name')}: {err}")
                self.status = {"ok": not errors, "error": "; ".join(errors) or None, "count": len(want),
                               "added": added, "removed": removed, "time": time.time()}
                _LOGGER.info("Music Assistant: %d radio (+%d, -%d)", len(want), added, removed)
            except RuntimeError as err:
                self.status = {"ok": False, "error": str(err), "count": 0, "time": time.time()}
                _LOGGER.warning("Sincronizzazione con Music Assistant non riuscita: %s", err)
        self._emit()
        return self.status

    async def _add(self, url: str, fav: dict[str, Any]) -> None:
        """Aggiunge la radio come oggetto completo del provider builtin: MA la salva così com'è, senza
        aprire lo stream per esaminarlo (che farebbe sintonizzare la chiavetta per niente, radio per radio).
        Se la versione di MA non accetta l'oggetto, si passa l'indirizzo (MA lo esamina da sé)."""
        radio = {
            "item_id": url, "provider": "builtin", "name": fav.get("name") or "Spettro",
            "media_type": "radio",
            "provider_mappings": [{"item_id": url, "provider_domain": "builtin",
                                   "provider_instance": "builtin", "available": True}],
        }
        try:
            await self._call("music/library/add_item", item=radio)
        except RuntimeError as err:
            _LOGGER.debug("Radio come oggetto rifiutata (%s): la passo come indirizzo", err)
            await self._call("music/library/add_item", item=url)

    async def clear(self) -> None:
        """Toglie da Music Assistant tutte le radio di Spettro (quando si spegne il collegamento)."""
        async with self._lock:
            with contextlib.suppress(RuntimeError):
                for lib_id in (await self._library_radios()).values():
                    await self._call("music/library/remove_item", media_type="radio", library_item_id=lib_id)

    def access(self, fav: dict[str, Any], esito: str) -> None:
        """Ultimo accesso di Music Assistant a uno stream (per capire dal pannello se ci arriva)."""
        self.last_access = {"time": time.time(), "name": fav.get("name"), "esito": esito}
        _LOGGER.info("Music Assistant → %s: %s", fav.get("name"), esito)
        self._emit()

    def event(self) -> dict[str, Any]:
        """Stato per il pannello: esito dell'ultima sincronizzazione e indirizzi usati."""
        return {"kind": "ma", **self.status, "url": self.ma_url(),
                "stream_base": self.stream_base(),
                "token_from_ha": not self.settings.ma_token and bool(self.token()),
                "last_access": self.last_access}

    def _emit(self) -> None:
        self.scanner._emit(self.event())  # noqa: SLF001


class RadioStreamView(HomeAssistantView):
    """GET /api/spettro/radio/<segreto>/<preferito>.wav: la radio in diretta, per Music Assistant."""

    url = STREAM_PATH + "/{key}/{fav}"
    name = "api:spettro:radio"
    requires_auth = False  # Music Assistant non ha un token di HA: l'accesso è col segreto nell'indirizzo

    async def get(self, request: web.Request, key: str, fav: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        entry = hass.data.get(DOMAIN)
        scanner: SpectrumScanner | None = entry.runtime_data if entry else None
        ma: MusicAssistantSync | None = getattr(scanner, "ma", None)
        if scanner is None or ma is None or not scanner.settings.ma_enabled or not scanner.settings.ma_key:
            return web.Response(status=404)
        if not hmac.compare_digest(key, scanner.settings.ma_key):
            return web.Response(status=403)
        fkey = fav.removesuffix(".wav")
        item = next((f for f in (scanner.favorites.items if scanner.favorites else [])
                     if fav_key(f) == fkey and is_radio(f)), None)
        if item is None:
            return web.Response(status=404, text="Radio non più nei preferiti di Spettro")
        if time.monotonic() - ma.preempted.get(fkey, -1e9) < PREEMPT_S:
            ma.access(item, "rifiutato: chiavetta presa da un'altra funzione")
            return web.Response(status=503, text="Chiavetta presa da un'altra funzione di Spettro")
        ma.access(item, "collegato")
        return await _RadioStream(hass, scanner, ma, item, fkey, request).run()


class _RadioStream:
    """Una connessione di Music Assistant: silenzio, sintonia, audio (con metadati ICY)."""

    def __init__(self, hass: HomeAssistant, scanner: SpectrumScanner, ma: MusicAssistantSync,
                 fav: dict[str, Any], fkey: str, request: web.Request) -> None:
        self.hass, self.scanner, self.ma, self.fav, self.fkey, self.request = hass, scanner, ma, fav, fkey, request
        self.dab = bool(fav.get("sid"))
        self.rate, self.channels = (DAB_OUT_RATE, 2) if self.dab else (AUDIO_RATE, 1)
        self.icy = request.headers.get("Icy-MetaData") == "1"
        self.title = fav.get("name") or "Spettro"
        self._sent_title: str | None = None
        self._icy_left = ICY_METAINT
        self.resp = web.StreamResponse()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.started = False  # la diretta l'ha avviata questo stream

    # --- uscita -------------------------------------------------------------

    def _icy_block(self) -> bytes:
        if self.title == self._sent_title:
            return b"\x00"
        self._sent_title = self.title
        text = self.title.replace("'", "’")[:200]
        meta = f"StreamTitle='{text}';".encode()
        meta += b"\x00" * (-len(meta) % 16)
        return bytes([len(meta) // 16]) + meta

    async def _write(self, data: bytes) -> None:
        if not self.icy:
            await self.resp.write(data)
            return
        out = bytearray()
        while data:
            take = data[: self._icy_left]
            out += take
            data = data[len(take):]
            self._icy_left -= len(take)
            if self._icy_left == 0:
                out += self._icy_block()
                self._icy_left = ICY_METAINT
        await self.resp.write(bytes(out))

    async def _silence(self, seconds: float, paced: bool = False) -> None:
        """Silenzio; paced=True al ritmo reale, a pezzi, fermandosi se il client se ne va (prove di MA)."""
        if not paced:
            await self._write(bytes(int(self.rate * seconds) * 2 * self.channels))
            return
        step = 0.25
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._gone():
                raise ConnectionResetError
            await self._write(bytes(int(self.rate * step) * 2 * self.channels))
            await asyncio.sleep(step)
        if self._gone():
            raise ConnectionResetError

    def _gone(self) -> bool:
        tr = self.request.transport
        return tr is None or tr.is_closing()

    # --- diretta --------------------------------------------------------------

    def _is_ours(self) -> bool:
        live = self.scanner.status.get("live") or {}
        if not live:
            return False
        if self.dab:
            return live.get("mode") == "dab" and (live.get("sid") or "").upper() == self.fav["sid"].upper() \
                and abs(float(live.get("freq_mhz") or 0) - float(self.fav["freq_mhz"])) < 0.05
        return live.get("mode") == "wfm" and abs(float(live.get("freq_mhz") or 0) - float(self.fav["freq_mhz"])) < 0.03

    def _on_event(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind in ("live", "live_dab", "live_rds", "live_error") or (kind == "status"):
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(payload)

    async def run(self) -> web.StreamResponse:
        hdr = {"Content-Type": "audio/wav", "Cache-Control": "no-cache", "icy-name": self.title,
               "icy-description": "Spettro (RTL-SDR)", "icy-pub": "0"}
        if self.icy:
            hdr["icy-metaint"] = str(ICY_METAINT)
        self.resp.headers.update(hdr)
        await self.resp.prepare(self.request)
        await self._write(wav_header(self.rate, self.channels))  # anche l'intestazione conta per l'ICY
        self.scanner.stream_clients += 1
        remove = self.scanner.add_listener(self._on_event)
        try:
            if not self._is_ours():
                # le connessioni di prova (intestazioni, esame del formato) si chiudono durante il silenzio.
                # Non si distingue per User-Agent: l'ffmpeg con cui MA suona si presenta come «Lavf»
                # proprio come un ffprobe, e l'ffprobe di MA a volte con lo user agent di MA
                await self._silence(TUNE_DELAY_S, paced=True)
                self.ma.access(self.fav, "sintonia in corso")
                await self._start_live()
                self.ma.access(self.fav, "in onda")
            if self.dab:
                await self._pump_dab()
            else:
                await self._pump_fm()
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass  # Music Assistant ha chiuso (stop o cambio radio)
        except RuntimeError as err:
            _LOGGER.warning("Stream per Music Assistant (%s) interrotto: %s", self.title, err)
            self.ma.access(self.fav, f"errore: {err}")
        finally:
            remove()
            self.scanner.stream_clients -= 1
            # anche se si era agganciato a una diretta già avviata da un altro stream di MA
            if self.started or (self.scanner.status.get("live") or {}).get("title") == LIVE_TITLE:
                self._schedule_stop()
        return self.resp

    def _schedule_stop(self) -> None:
        """Libera la chiavetta poco dopo l'ultima connessione: MA spesso esamina lo stream (ffprobe) e lo
        riapre subito per suonarlo; in quel caso la diretta continua senza risintonizzare."""

        async def later() -> None:
            await asyncio.sleep(STOP_GRACE_S)
            if self._is_ours() and self.scanner.stream_clients <= 0 and self.scanner.viewers <= 0:
                await self.scanner.live_stop()  # nessuno ascolta più: si libera la chiavetta

        self.hass.async_create_task(later(), "spettro_ma_stop")

    async def _start_live(self) -> None:
        f = float(self.fav["freq_mhz"]) * 1e6
        if self.dab:
            await self.scanner.live_start(f, "dab", sid=self.fav["sid"], title=LIVE_TITLE)
        else:
            await self.scanner.live_start(f, "wfm", title=LIVE_TITLE, bw=self.fav.get("bw"))
        self.started = True

    def _check_preempted(self) -> None:
        if not self._is_ours():
            self.ma.preempted[self.fkey] = time.monotonic()
            raise RuntimeError("la chiavetta è passata a un'altra funzione di Spettro")

    async def _next(self, timeout: float) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout(timeout):
                return await self.queue.get()
        except TimeoutError:
            return None

    async def _pump_fm(self) -> None:
        got_audio = False
        while True:
            m = await self._next(0.5)
            if m is None:
                self._check_preempted()
                if not got_audio:
                    await self._silence(0.5)  # in attesa dei primi campioni
                continue
            kind = m.get("kind")
            if kind == "live_error":
                raise RuntimeError(m.get("error") or "errore della chiavetta")
            if kind == "status":
                self._check_preempted()
            elif kind == "live_rds":
                rds = m.get("rds") or {}
                text = rds.get("rt") or rds.get("ps")
                if text:
                    self.title = f"{self.fav.get('name')} — {text}" if rds.get("rt") else self.fav.get("name") or text
            elif kind == "live" and m.get("audio") and m.get("mode") == "wfm":
                got_audio = True
                await self._write(base64.b64decode(m["audio"]))

    async def _pump_dab(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg non trovato in Home Assistant: serve per il DAB+")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "loas", "-i", "pipe:0",
            "-f", "s16le", "-ar", str(DAB_OUT_RATE), "-ac", "2", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        got_audio = asyncio.Event()

        async def reader() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(16384):
                got_audio.set()
                await self._write(chunk)

        rtask = asyncio.create_task(reader())
        try:
            while not rtask.done():
                m = await self._next(0.5)
                if m is None:
                    self._check_preempted()
                    if not got_audio.is_set():
                        await self._silence(0.5)  # aggancio del multiplex: qualche secondo
                    continue
                kind = m.get("kind")
                if kind == "live_error":
                    raise RuntimeError(m.get("error") or "errore della chiavetta")
                if kind == "status":
                    self._check_preempted()
                    continue
                if kind != "live_dab":
                    continue
                if m.get("error"):
                    raise RuntimeError(m["error"])
                if m.get("dls"):
                    self.title = f"{self.fav.get('name')} — {m['dls']}"
                cfg = m.get("config")
                if not m.get("aus") or not cfg:
                    continue
                # ffmpeg non ha l'SBR a 960 campioni: si passa il nucleo AAC (piano B già calcolato)
                asc = asc_bits((cfg.get("fallback") or cfg)["asc"])
                assert proc.stdin is not None
                for au in m["aus"]:
                    proc.stdin.write(loas_frame(base64.b64decode(au), asc))
                await proc.stdin.drain()
        finally:
            rtask.cancel()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rtask

