"""Client rtl_tcp e motore di scansione dello spettro.

Questo modulo non dipende da Home Assistant: si collega a un server rtl_tcp,
sintonizza la chiavetta a passi successivi, calcola la FFT di ogni passo e
ricompone uno spettro unico su tutto l'intervallo richiesto.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
import json
import logging
import math
import socket
import struct
import time
from typing import Any

import numpy as np

from . import adsb as adsb_mod
from . import adsb_db
from . import adsb_online
from . import dab, dvbt, rds, satellite, slots, textdec, verify, weather
from .analysis import encode_png
from .fax import FaxDecoder
from .sstv import SstvDecoder, ToneTracker
from .analysis import analyze_iq
from .dabplus import DabLiveReceiver
from .demod import BANDWIDTHS
from .archive import SpikeArchive
from .live import CHUNK, LIVE_RATE, LiveDemod
from .orbit import passes as orbit_passes

_LOGGER = logging.getLogger(__name__)

# Comandi del protocollo rtl_tcp (1 byte comando + uint32 big endian)
CMD_SET_FREQ = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03
CMD_SET_GAIN = 0x04
CMD_SET_FREQ_CORRECTION = 0x05
CMD_SET_AGC_MODE = 0x08
CMD_SET_DIRECT_SAMPLING = 0x09
HF_MAX_HZ = 24e6  # sotto questa frequenza (onde corte) si usa il direct sampling, se attivo
DIRECT_SETTLE_S = 0.3  # campioni scartati dopo un cambio di direct sampling

TUNER_NAMES = {
    0: "Sconosciuto",
    1: "E4000",
    2: "FC0012",
    3: "FC0013",
    4: "FC2580",
    5: "R820T/R820T2",
    6: "R828D",
}

SAMPLE_RATES = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000, 2_560_000, 2_880_000]
FFT_SIZES = [256, 512, 1024, 2048, 4096]

READ_LIMIT = 64 * 1024  # buffer dello StreamReader
RCV_BUF = 128 * 1024  # buffer socket piccolo: meno campioni "vecchi" dopo un cambio frequenza
USABLE_FRACTION = 0.75  # parte centrale della banda tenuta (i bordi sono attenuati dal filtro)
AVERAGES = 16  # FFT mediate per ogni passo
MAX_POINTS = 6000  # punti massimi inviati al pannello (riduzione con max-hold, i picchi restano)
MAX_PEAKS = 25
HISTORY = 60  # scansioni complete ricordate (cascata del pannello, stesso valore di MAX_HISTORY nel JS)
PARTIAL_EVERY = 1.5  # secondi tra un aggiornamento parziale e l'altro
CONNECT_TIMEOUT = 8
IO_TIMEOUT = 10
MIN_ERROR_RETRY = 10
LIVE_RDS_WINDOW_S = 6.0  # multiplex tenuto per decodificare l'RDS in diretta
LIVE_RDS_EVERY_S = 1.0
LIVE_NO_VIEWER_STOP_S = 30  # senza pannelli aperti l'analisi continua si ferma da sola
IMAGE_DECODERS = ("sstv", "fax")
SLOT_DECODERS = ("ft8", "wspr")  # modi digitali a periodi UTC
KEEP_RUNNING = IMAGE_DECODERS + SLOT_DECODERS  # questi continuano anche a pannello chiuso
DIGI_LOG = 600  # messaggi FT8/WSPR tenuti in memoria
DAB_SCAN_S = 1.2  # registrazione per ogni canale nella ricerca dei multiplex DAB
ADSB_PRUNE_S = 60.0  # un aereo sparisce dalla lista se non si sente da tanto
ADSB_HISTORY_H = 24.0  # ore dopo cui un volo nello storico si cancella da solo
ADSB_HISTORY_MAX = 800  # voli tenuti al massimo nello storico
ADSB_EMIT_S = 1.0  # ogni quanto si aggiorna il pannello
ADSB_CHUNK = int(adsb_mod.RATE * 0.25)  # campioni per lettura (0,25 s)
ADSB_OVERLAP = int(adsb_mod.RATE * 140e-6)  # coda riusata: un messaggio a cavallo non si perde
DATA_WINDOW_S = 2.0  # "Riconosci dati": finestra di IQ esaminata
DATA_EVERY_S = 1.5  # ogni quanto (le finestre si sovrappongono: i pacchetti a cavallo non si perdono)
TEXT_KEEP = 4000  # caratteri di testo decodificato tenuti nello stato (per chi apre il pannello dopo)
ANALYSIS_MAX_S = 5  # durata massima di una registrazione per l'analisi
TV_LIVE_S = 0.4  # campioni di ogni misura del multiplex TV in diretta
SPIKE_TOL_HZ = 100e3  # due picchi così vicini sono lo stesso segnale


@dataclass
class ScanSettings:
    """Parametri di scansione modificabili dal pannello."""

    start_mhz: float = 87.5
    stop_mhz: float = 108.0
    interval_s: float = 30.0
    gain_db: float | None = None  # None = guadagno automatico
    sample_rate: int = 2_048_000
    fft_size: int = 1024
    threshold_db: float = 10.0
    settle_ms: int = 60
    ppm: int = 0
    only_when_viewed: bool = False  # scansiona solo mentre qualcuno ha il pannello aperto
    auto_analyze: bool = False  # analizza subito i picchi trovati durante la scansione
    archive_hours: float = 24.0  # dopo quante ore si cancellano le analisi archiviate
    reanalyze_min: float = 30.0  # la stessa frequenza non si rianalizza in automatico prima di così
    auto_limit_step: bool = False  # limita i picchi analizzati per passo della scansione
    auto_max_per_step: int = 3
    auto_limit_sweep: bool = False  # limita i picchi analizzati per scansione completa
    auto_max_per_sweep: int = 20
    analysis_s: float = 2.0  # durata della registrazione di ogni analisi, manuale o automatica
    save_iq_auto: bool = False  # salva la registrazione grezza (IQ) anche delle analisi automatiche
    lnb: bool = False  # c'è un LNB satellitare sul cavo: 950–2150 MHz sono transponder TV
    meteo_auto: bool = False  # riceve da solo i passaggi dei satelliti meteo Meteor
    meteo_min_el: float = 20.0  # elevazione massima minima di un passaggio da ricevere (gradi)
    meteo_days: float = 7.0  # giorni dopo cui si cancellano le immagini meteo
    iss_sstv: bool = False  # riceve l'SSTV della ISS ai suoi passaggi
    hf_sampling: int = 2  # onde corte: direct sampling sul ramo Q (2), I (1) o spento (0, es. RTL-SDR Blog V4)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanSettings:
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known})
        # nella 1.4.2 lo 0 voleva dire "nessun limite": si torna ai valori predefiniti
        defaults = cls()
        if s.auto_max_per_step < 1:
            s.auto_max_per_step = defaults.auto_max_per_step
        if s.auto_max_per_sweep < 1:
            s.auto_max_per_sweep = defaults.auto_max_per_sweep
        return s

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RtlTcpError(Exception):
    """Il server non risponde come un rtl_tcp."""


class ScanPaused(Exception):
    """La scansione è stata messa in pausa mentre era in corso."""


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    """Apre la connessione, legge l'header 'RTL0' e restituisce il tipo di tuner."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    family, type_, proto, _, addr = infos[0]
    sock = socket.socket(family, type_, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCV_BUF)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        async with asyncio.timeout(CONNECT_TIMEOUT):
            await loop.sock_connect(sock, addr)
    except BaseException:
        sock.close()
        raise
    reader, writer = await asyncio.open_connection(sock=sock, limit=READ_LIMIT)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            header = await reader.readexactly(12)
    except BaseException:
        writer.close()
        raise
    if header[:4] != b"RTL0":
        writer.close()
        raise RtlTcpError("Il server non ha risposto con l'header rtl_tcp")
    tuner, _gains = struct.unpack(">II", header[4:])
    return reader, writer, tuner


async def probe(host: str, port: int) -> str:
    """Verifica che all'indirizzo ci sia un rtl_tcp e restituisce il nome del tuner."""
    _reader, writer, tuner = await _open(host, port)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return TUNER_NAMES.get(tuner, f"Tipo {tuner}")


def compute_psd(raw: bytes, fft_size: int, window: np.ndarray, norm: float) -> np.ndarray:
    """Spettro di potenza in dBFS (fftshift: frequenze negative a sinistra)."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    iq = a[0::2] + 1j * a[1::2]
    iq -= iq.mean()  # toglie l'offset DC
    frames = iq.reshape(-1, fft_size) * window
    spec = np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)
    power = np.mean(spec.real**2 + spec.imag**2, axis=0)
    # il picco DC al centro è un artefatto della chiavetta: lo si sostituisce coi vicini
    c = fft_size // 2
    power[c - 2 : c + 3] = (power[c - 4] + power[c + 4]) / 2
    return (10 * np.log10(power / norm + 1e-20)).astype(np.float32)


FLOOR_SPAN = 11  # passi (~16 MHz) su cui si stima il rumore di una zona


def zone_floor(lows: list[float] | np.ndarray) -> float:
    """Rumore di una zona dai livelli bassi dei suoi passi: il 25° percentile, così un segnale largo
    che occupa qualche passo intero (DAB 1,5 MHz, TV 8 MHz) non diventa lui il "rumore"."""
    return float(np.percentile(lows, 25))


def step_low(seg: np.ndarray) -> float:
    """Livello basso di un passo (20° percentile dei suoi bin)."""
    return float(np.percentile(seg, 20))


def local_floor(values: np.ndarray, block: int) -> np.ndarray:
    """Rumore di fondo zona per zona, raccordato tra i passi.

    Un solo valore per tutta la scansione non va: il rumore cambia molto con la frequenza (onde
    corte, guadagno del sintonizzatore), e dove è più alto della media il rumore stesso sembrava un picco.
    """
    n = len(values)
    if n == 0:
        return np.zeros(0, np.float32)
    block = max(16, block)
    edges = np.append(np.arange(0, n, block), n)  # blocchi = passi della scansione (il fondo cambia tra un passo e l'altro)
    if len(edges) > 2 and edges[-1] - edges[-2] < block // 4:
        edges = np.delete(edges, -2)  # l'ultimo pezzetto si unisce al blocco prima
    nb = len(edges) - 1
    lows = np.array([step_low(values[a:b]) for a, b in zip(edges[:-1], edges[1:], strict=True)])
    # zona a sinistra e a destra di ogni passo: si sceglie quella più vicina al passo stesso, così
    # un salto del fondo (es. al passaggio al direct sampling) viene seguito subito, mentre un segnale
    # largo resta sopra il rumore (le sue zone, da entrambi i lati, cadono nel rumore attorno)
    zone = np.empty(nb)
    for k in range(nb):
        left = zone_floor(lows[max(0, k - FLOOR_SPAN + 1): k + 1])
        right = zone_floor(lows[k: k + FLOOR_SPAN])
        zone[k] = left if abs(left - lows[k]) <= abs(right - lows[k]) else right
    return np.interp(np.arange(n), (edges[:-1] + edges[1:] - 1) / 2, zone).astype(np.float32)


def find_peaks(
    values: np.ndarray, f_start: float, bin_hz: float, threshold_db: float, min_sep_hz: float, block: int = 0
) -> tuple[list[dict[str, float]], np.ndarray]:
    """Massimi locali sopra (rumore di fondo locale + soglia), separati di almeno min_sep_hz."""
    if len(values) < 3:
        return [], np.full(len(values), float(values.max()) if len(values) else 0.0, np.float32)
    floor_arr = local_floor(values, block) if block else np.full(len(values), float(np.median(values)), np.float32)
    mid = values[1:-1]
    mask = (mid > values[:-2]) & (mid >= values[2:]) & (mid > floor_arr[1:-1] + threshold_db)
    idx = np.nonzero(mask)[0] + 1
    if not len(idx):
        return [], floor_arr
    # un segnale largo (DAB, TV, FM) ha tanti massimi: conta uno solo per tratto sopra il rumore
    over = np.convolve(values > floor_arr + threshold_db / 2, np.ones(9), "same") > 0
    region = np.cumsum(np.diff(np.concatenate([[0], over.astype(np.int8)])) == 1)
    snr = values - floor_arr
    best: dict[int, int] = {}
    for i in idx:
        r = int(region[i])
        if r not in best or snr[i] > snr[best[r]]:
            best[r] = int(i)
    idx = np.array(sorted(best.values()))
    order = idx[np.argsort(snr[idx])[::-1]][:3000]  # dal più alto sopra il rumore locale
    min_sep = max(1, int(min_sep_hz / bin_hz))
    chosen: list[int] = []
    for i in order:
        if all(abs(int(i) - j) > min_sep for j in chosen):
            chosen.append(int(i))
            if len(chosen) >= MAX_PEAKS:
                break
    peaks = [
        {
            "freq_mhz": round((f_start + i * bin_hz) / 1e6, 4),
            "db": round(float(values[i]), 1),
            "snr": round(float(values[i] - floor_arr[i]), 1),
        }
        for i in chosen
    ]
    return peaks, floor_arr


class SpectrumScanner:
    """Esegue scansioni periodiche e notifica i risultati agli ascoltatori."""

    def __init__(self, host: str, port: int, settings: ScanSettings, enabled: bool = True) -> None:
        self.host = host
        self.port = port
        self.settings = settings
        self.enabled = enabled
        self.viewers = 0
        self.analyzing = False
        self.last_analysis: dict[str, Any] | None = None
        self.last_analysis_png: bytes | None = None
        self.last_analysis_audio: dict[str, bytes] = {}
        self.archive: SpikeArchive | None = None  # impostato dall'integrazione dopo il caricamento
        self.favorites: Any = None  # preferiti (Favorites), impostati dall'integrazione
        self._live_task: asyncio.Task | None = None
        self.last_slide: dict[str, str] | None = None  # ultima immagine della slideshow DAB+
        self.last_dab: dict[str, Any] = {}  # ultimi dati della radio DAB+ (nome, testo, statistiche)
        self._live_stop_handle: asyncio.TimerHandle | None = None
        self.meteo: weather.Meteo | None = None  # orbite e passaggi, impostati dall'integrazione
        self.meteo_archive: weather.MeteoArchive | None = None
        self.meteo_save: Callable[[], None] | None = None  # salva i TLE scaricati
        self.meteo_rx: dict[str, Any] | None = None  # ricezione di un passaggio in corso
        self.last_meteo_png: bytes | None = None
        self._meteo_task: asyncio.Task | None = None
        self._meteo_loop_task: asyncio.Task | None = None
        self._meteo_done: set[tuple[int, float]] = set()  # passaggi già ricevuti (norad, aos)
        self.meteo_log: deque[dict[str, Any]] = deque(maxlen=15)  # ultime ricezioni dei satelliti
        self.digi_log: deque[dict[str, Any]] = deque(maxlen=DIGI_LOG)  # messaggi FT8/WSPR ricevuti (dal più vecchio)
        self._radio = asyncio.Lock()  # una sola connessione rtl_tcp alla volta
        self._direct: dict[int, int] = {}  # direct sampling attivo per connessione
        self._rate = 2_048_000  # sample rate della connessione in corso
        self._floor_prev: tuple[tuple[float, float, int], np.ndarray] | None = None  # rumore dell'ultima scansione
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._one_shot = False  # un solo giro chiesto a mano mentre la scansione è spenta
        self.dab_list: list[dict[str, Any]] = []  # multiplex DAB trovati dalla ricerca canale per canale
        self.dab_scan_time: float | None = None
        self._dab_task: asyncio.Task | None = None
        self._adsb_task: asyncio.Task | None = None
        self.adsb_tracker: adsb_mod.AircraftTracker | None = None  # aerei visti nella sessione in corso
        self._adsb_resume: dict[str, Any] | None = None  # opzioni ADS-B da riprendere dopo un passaggio satellite
        self.adsb_history: list[dict[str, Any]] = []  # voli intercettati (dal più recente), con la traccia
        self.adsb_record: bool = False  # se True registra i voli nello storico
        self.http_get: Callable[[str], Any] | None = None  # richieste HTTP (per i dettagli online degli aerei)
        self._adsb_ac: dict[str, dict[str, Any]] = {}  # dettagli online per codice ICAO (anche {} = cercato, assente)
        self._adsb_route: dict[str, dict[str, Any]] = {}  # rotte online per sigla del volo
        self._active = asyncio.Event()
        self._sweep_id = 0
        self.last_spectrum: dict[str, Any] | None = None
        self.last_complete: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self.status: dict[str, Any] = {
            "kind": "status",
            "state": "starting",
            "error": None,
            "progress": 0.0,
            "tuner": None,
            "last_scan": None,
            "sweep_s": None,
            "next_scan": None,
            "host": host,
            "port": port,
            "settings": settings.as_dict(),
            "enabled": enabled,
            "paused": None,
            "auto_mhz": None,  # picco in analisi automatica
            "auto_count": None,  # picchi analizzati in automatico nell'ultima scansione
            "version": None,  # versione dell'integrazione, per accorgersi di un riavvio mancato
            "live": None,  # analisi continua in corso: {"freq_mhz", "mode"}
            "adsb": None,  # ricevitore aerei ADS-B in corso: {"continuous", "aircraft", ...}
        }
        self._update_active(emit=False)

    # --- ascoltatori -------------------------------------------------------

    def add_listener(self, cb: Callable[[dict[str, Any]], None], viewer: bool = False) -> Callable[[], None]:
        """Registra un ascoltatore; viewer=True per il pannello (conta chi sta guardando)."""
        self._listeners.append(cb)
        if viewer:
            self.viewers += 1
            self._update_active()

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
                if viewer:
                    self.viewers -= 1
                    self._update_active()
                    if self.viewers <= 0 and self._live_task is not None:
                        self._schedule_live_stop()

        return remove

    def snapshot(self) -> list[dict[str, Any]]:
        """Stato, scansioni complete recenti (dalla più vecchia) ed eventuale parziale in corso.

        Un pannello appena aperto ricostruisce così grafico, max hold e cascata
        invece di ripartire dalla sola parte già scansionata del giro in corso.
        """
        out = [dict(self.status), *self.history]
        if self.last_spectrum is not None and not self.last_spectrum["complete"]:
            out.append(self.last_spectrum)
        if self.favorites is not None:
            out.append({"kind": "favorites", "favorites": self.favorites.items})
        if self.meteo is not None:
            out.append(self.meteo.snapshot())
        if self.meteo_rx:
            out.append({"kind": "meteo_live", **self.meteo_rx})
        if self.meteo_log:
            out.append({"kind": "meteo_log", "log": list(self.meteo_log)})
        if self.digi_log:
            out.append({"kind": "digi_log", "log": list(self.digi_log)})
        if self.dab_list or self._dab_task is not None:
            out.append({"kind": "dab_scan", "done": self._dab_task is None, "found": self.dab_list,
                        "time": self.dab_scan_time})
        if self.status["adsb"]:
            out.append({"kind": "adsb", **self.status["adsb"]})
        if self.adsb_history or self.adsb_record:
            out.append({"kind": "adsb_history", "flights": self._adsb_compact(), "record": self.adsb_record,
                        "hours": ADSB_HISTORY_H, "home": list(self.meteo.location) if self.meteo else None})
        live = self.status["live"]
        if live and live.get("mode") == "dab" and (self.last_slide or self.last_dab):
            # un pannello aperto a diretta già avviata riceve subito nome, testo e immagine
            ev = {"kind": "live_dab", "freq_mhz": live["freq_mhz"], "sid": live["sid"],
                  "synced": "info" in self.last_dab, "error": None, **self.last_dab}
            if self.last_slide:
                ev["slide"] = self.last_slide
            out.append(ev)
        return out

    def favorites_changed(self) -> None:
        """Avvisa i pannelli che i preferiti sono cambiati."""
        if self.favorites is not None:
            self._emit({"kind": "favorites", "favorites": self.favorites.items})

    def _emit(self, payload: dict[str, Any]) -> None:
        for cb in list(self._listeners):
            try:
                cb(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Errore in un ascoltatore dello spettro")

    def _set_status(self, **changes: Any) -> None:
        self.status.update(changes)
        self._emit(dict(self.status))

    # --- pausa -------------------------------------------------------------

    def _pause_reason(self) -> str | None:
        if self.meteo_rx:
            return "satellite"
        if self._adsb_task is not None:
            return "adsb"
        if self.analyzing:
            return "analysis"
        if self.status["live"]:
            return "live"
        if self._one_shot:
            return None  # "Scansiona ora": un giro anche con la scansione spenta
        if not self.enabled:
            return "disabled"
        if self.settings.only_when_viewed and self.viewers <= 0:
            return "no_viewers"
        return None

    def _update_active(self, emit: bool = True) -> None:
        reason = self._pause_reason()
        was_paused = self.status["paused"] is not None or not self._active.is_set()
        self.status["enabled"] = self.enabled
        self.status["paused"] = reason
        if reason is None:
            if not self._active.is_set():
                self._active.set()
                if was_paused:
                    self._wake.set()  # riattivato: scansiona subito invece di aspettare l'intervallo
        else:
            self._active.clear()
            self.status.update(state="paused", next_scan=None)
        if emit:
            self._emit(dict(self.status))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._update_active()

    # --- ciclo di vita -----------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            asyncio.get_running_loop().create_task(self._load_dab())
            asyncio.get_running_loop().create_task(self._load_adsb_history())
            self._task = asyncio.get_running_loop().create_task(self._run(), name="spettro_scanner")
        if self.meteo is not None and (self._meteo_loop_task is None or self._meteo_loop_task.done()):
            self._meteo_loop_task = asyncio.get_running_loop().create_task(self._meteo_loop(), name="spettro_meteo")

    async def stop(self) -> None:
        await self.dab_scan_stop()
        await self.adsb_stop()
        await self.live_stop()
        await self.meteo_stop()
        if self._meteo_loop_task is not None:
            self._meteo_loop_task.cancel()
            self._meteo_loop_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def update_settings(self, settings: ScanSettings) -> None:
        """Applica i nuovi parametri ripartendo subito con una scansione."""
        self.settings = settings
        self.status["settings"] = settings.as_dict()
        self._update_active()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.start()

    def scan_now(self) -> None:
        if self._pause_reason() in ("disabled", "no_viewers"):
            self._one_shot = True
            self._update_active()
        self._wake.set()

    # --- ricerca dei multiplex DAB ------------------------------------------

    def _dab_file(self) -> Any:
        return self.archive.path.parent / "spettro_dab.json" if self.archive is not None else None

    async def _load_dab(self) -> None:
        path = self._dab_file()
        if path is None:
            return

        def read() -> dict[str, Any] | None:
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None

        data = await asyncio.get_running_loop().run_in_executor(None, read)
        if data and not self.dab_list:
            self.dab_list, self.dab_scan_time = data.get("found", []), data.get("time")

    def dab_scan_start(self) -> None:
        """Prova tutti i canali della banda III: per ognuno nome del multiplex e radio (scansione sospesa).

        Usa sempre 2,048 MS/s, qualunque sia il sample rate della scansione.
        """
        if self._dab_task is not None:
            raise RuntimeError("Ricerca DAB già in corso")
        if self.analyzing or self._live_task is not None:
            raise RuntimeError("Ferma prima l'analisi in corso")
        self._dab_task = asyncio.get_running_loop().create_task(self._dab_scan_run(), name="spettro_dab_scan")

    async def dab_scan_stop(self) -> None:
        task = self._dab_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _dab_scan_run(self) -> None:
        loop = asyncio.get_running_loop()
        found: list[dict[str, Any]] = []
        chans = list(dab.CHANNELS.items())
        discard = RCV_BUF + 2 * READ_LIMIT + int(dab.RATE * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        await self._adsb_yield(resume=False)
        self.analyzing = True
        self._update_active()
        error = None
        try:
            async with self._radio:
                reader, writer, _tuner = await _open(self.host, self.port)
                try:
                    await self._configure(writer, dab.RATE)
                    for i, (ch, mhz) in enumerate(chans):
                        self._emit({"kind": "dab_scan", "done": False, "progress": i / len(chans), "channel": ch,
                                    "found": found})
                        await self._tune(writer, mhz * 1e6, reader)
                        async with asyncio.timeout(IO_TIMEOUT + DAB_SCAN_S):
                            await self._discard(reader, discard)
                            raw = await reader.readexactly(int(dab.RATE * DAB_SCAN_S) * 2)
                        # mentre si decodifica si continua a leggere (e buttare) il flusso: altrimenti i campioni
                        # accumulati finirebbero attribuiti al canale successivo
                        job = loop.run_in_executor(None, _dab_probe, raw)
                        while not job.done():
                            try:
                                async with asyncio.timeout(0.1):
                                    await reader.read(READ_LIMIT)
                            except TimeoutError:
                                pass
                        res = await job
                        if res:
                            found.append({"channel": ch, "mhz": mhz, **res})
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            error = str(err) or type(err).__name__
        finally:
            self.analyzing = False
            self._dab_task = None
            self._update_active()
        if error is None or found:
            self.dab_list, self.dab_scan_time = found, time.time()
            path = self._dab_file()
            if path is not None:
                text = json.dumps({"found": found, "time": self.dab_scan_time})
                try:
                    await loop.run_in_executor(None, path.write_text, text)
                except OSError as err:
                    _LOGGER.warning("Impossibile salvare i multiplex DAB: %s", err)
        self._emit({"kind": "dab_scan", "done": True, "progress": 1.0, "found": self.dab_list,
                    "time": self.dab_scan_time, "error": error})

    async def analyze(self, freq_hz: float, duration_s: float) -> dict[str, Any]:
        """Mette in pausa la scansione, registra il segnale a frequenza fissa e lo misura."""
        if self.analyzing:
            raise RuntimeError("C'è già un'analisi in corso")
        if self._live_task is not None:
            raise RuntimeError("Ferma prima l'analisi continua")
        await self._adsb_yield(resume=False)
        rate = int(self.settings.sample_rate)
        capture_bytes = int(rate * min(duration_s, ANALYSIS_MAX_S)) * 2
        discard_bytes = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard_bytes -= discard_bytes % 2
        center = self._tune_center(freq_hz, rate)
        self.analyzing = True
        self._update_active()  # la scansione in corso si ferma al passo successivo e libera la chiavetta
        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    await self._tune(writer, center, reader)
                    async with asyncio.timeout(IO_TIMEOUT + ANALYSIS_MAX_S):
                        await self._discard(reader, discard_bytes)
                        raw = await reader.readexactly(capture_bytes)
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
            result, png, audio = await asyncio.get_running_loop().run_in_executor(
                None, analyze_iq, raw, rate, center, freq_hz, self.meteo.location if self.meteo else None
            )
        finally:
            self.analyzing = False
            self._update_active()  # riparte la scansione
        result["tune_hz"] = center  # frequenza a cui era sintonizzata la chiavetta (centro della registrazione)
        return await self._store_analysis(result, png, audio, auto=False, iq=raw)

    async def _store_analysis(
        self, result: dict[str, Any], png: bytes, audio: dict[str, bytes], auto: bool, iq: bytes | None = None
    ) -> dict[str, Any]:
        """Ultima analisi in memoria, archivio su disco e notifica a pannello ed entità."""
        result["timestamp"] = time.time()
        result["auto"] = auto
        entry = None
        if self.archive is not None:
            try:
                entry = await asyncio.get_running_loop().run_in_executor(
                    None, self.archive.add, result, png, audio, iq)
                result["id"] = entry["id"]
                result["iq_bytes"] = entry.get("iq_bytes")
            except OSError as err:
                _LOGGER.warning("Impossibile salvare l'analisi nell'archivio: %s", err)
        self.last_analysis = result
        self.last_analysis_png = png
        self.last_analysis_audio = audio
        self._emit({"kind": "analysis", "analysis": result, "entry": entry})
        return result

    async def prune_archive(self) -> None:
        if self.archive is None:
            return
        removed = await asyncio.get_running_loop().run_in_executor(
            None, self.archive.prune, self.settings.archive_hours * 3600
        )
        if removed:
            self._emit({"kind": "archive_pruned", "removed": removed})

    @staticmethod
    def _tune_center(freq_hz: float, rate: int) -> float:
        """Frequenza a cui sintonizzare la chiavetta per analizzare freq_hz.

        Di solito spostata di 1/8 della banda, perché il segnale non cada sul picco DC al centro;
        un multiplex DAB (1,5 MHz) invece va centrato sul canale, o finirebbe sul bordo della banda
        (la sua portante centrale è comunque vuota).
        """
        ch = dab.channel_for(freq_hz / 1e6)
        if ch and rate == dab.RATE:
            return ch[1] * 1e6
        tv = dvbt.channel_for(freq_hz / 1e6)
        if tv and rate >= 1_500_000:
            return tv[1] * 1e6  # multiplex TV: tutta la banda ricevuta è dentro il canale
        return float(round(freq_hz + rate / 8))

    def _spike_candidates(self, seg: np.ndarray, f0: float, bin_hz: float, level: float,
                          noise: float) -> list[tuple[float, float]]:
        """Picchi di un passo della scansione sopra `level`, dal più forte, non ancora analizzati di recente."""
        if self.archive is None or len(seg) < 3:
            return []
        mid = seg[1:-1]
        idx = np.nonzero((mid > seg[:-2]) & (mid >= seg[2:]) & (mid > level))[0] + 1
        out: list[tuple[float, float]] = []  # (frequenza, dB sopra il rumore della zona)
        recent = time.time() - self.settings.reanalyze_min * 60
        for i in idx[np.argsort(seg[idx])[::-1]]:
            f = f0 + int(i) * bin_hz
            # un multiplex DAB è un blocco piatto con più "picchi": conta una volta, al centro del canale
            ch = dab.channel_for(f / 1e6)
            tv = dvbt.channel_for(f / 1e6)
            if ch:
                f = ch[1] * 1e6
            elif tv and tv[2] == 8:  # anche un multiplex TV (8 MHz, UHF) conta una volta sola
                f = tv[1] * 1e6
            if any(abs(f - g) <= SPIKE_TOL_HZ for g, _ in out):
                continue
            if self.archive.find(f, SPIKE_TOL_HZ, newer_than=recent):
                continue
            out.append((f, round(float(seg[i]) - noise, 1)))
            if self.settings.auto_limit_step and len(out) >= self.settings.auto_max_per_step:
                break
        return out

    async def _auto_analyze(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, rate: int, freq_hz: float, discard: int,
        scan_snr: float | None = None,
    ) -> None:
        """Analisi di un picco a metà scansione, sulla stessa connessione e con le stesse impostazioni."""
        center = self._tune_center(freq_hz, rate)
        self._set_status(auto_mhz=round(freq_hz / 1e6, 4))
        try:
            await self._tune(writer, center, reader)
            duration = min(self.settings.analysis_s, ANALYSIS_MAX_S)
            async with asyncio.timeout(IO_TIMEOUT + duration):
                await self._discard(reader, discard)
                raw = await reader.readexactly(int(rate * duration) * 2)
            # durante il calcolo rtl_tcp continua a inviare: si scarta, altrimenti il passo
            # successivo della scansione leggerebbe campioni vecchi della frequenza analizzata
            job = asyncio.get_running_loop().run_in_executor(None, analyze_iq, raw, rate, center, freq_hz,
                                                              self.meteo.location if self.meteo else None)
            while True:
                done, _ = await asyncio.wait([job], timeout=0)
                if done:
                    break
                async with asyncio.timeout(IO_TIMEOUT):
                    if not await reader.read(READ_LIMIT):
                        raise ConnectionError("Connessione chiusa dal server")
            result, png, audio = job.result()
            result["scan_snr_db"] = scan_snr  # quanto stava sopra il rumore nella scansione
            result["tune_hz"] = center
            await self._store_analysis(result, png, audio, auto=True, iq=raw if self.settings.save_iq_auto else None)
        finally:
            self._set_status(auto_mhz=None)

    @staticmethod
    async def _discard(reader: asyncio.StreamReader, nbytes: int) -> None:
        remaining = nbytes
        while remaining > 0:
            chunk = await reader.read(min(remaining, READ_LIMIT))
            if not chunk:
                raise ConnectionError("Connessione chiusa dal server")
            remaining -= len(chunk)

    # --- analisi continua (in diretta) --------------------------------------

    def live_mode_for(self, freq_hz: float) -> str:
        """Demodulazione predefinita per la frequenza: FM larga per la radio, AM per la banda aerea."""
        mhz = freq_hz / 1e6
        if self.settings.lnb and satellite.IF_MIN <= mhz <= satellite.IF_MAX:
            return "sat"
        tv = dvbt.channel_for(mhz)
        if tv and tv[2] == 8:
            return "tv"
        if 87.5 <= mhz <= 108:
            return "wfm"
        if 118 <= mhz <= 137:
            return "am"
        return "nfm"

    async def live_start(self, freq_hz: float, mode: str | None = None, sid: str | None = None,
                         decoder: str | None = None, until: datetime | None = None,
                         title: str | None = None, auto: bool = False, bw: float | None = None) -> dict[str, Any]:
        """Sintonizza la chiavetta fissa su freq_hz (scansione sospesa) e demodula di continuo.

        mode "dab" con sid: una radio DAB+ del multiplex (freq_hz = centro del canale).
        decoder "sstv"/"fax": immagini nell'audio, salvate nella galleria; continua anche senza
        pannelli aperti (fino a `until`, se c'è). decoder "cw"/"rtty45"/"rtty50": testo Morse o RTTY.
        bw: larghezza del canale in Hz (None = quella predefinita del modo).
        """
        if self.analyzing:
            raise RuntimeError("C'è un'analisi in corso, riprova tra qualche secondo")
        if mode == "dab" and not sid:
            raise RuntimeError("Scegli una radio del multiplex")
        await self.live_stop()
        await self._adsb_yield(resume=False)
        mode = mode or self.live_mode_for(freq_hz)
        if decoder in IMAGE_DECODERS and mode not in ("nfm", "am", "usb", "lsb"):
            raise RuntimeError("SSTV e radiofax si decodificano in FM stretta, AM o SSB")
        if decoder in textdec.DECODERS and mode not in ("usb", "lsb", "cw"):
            raise RuntimeError("Morse e RTTY si decodificano in CW o SSB (USB/LSB)")
        if decoder in SLOT_DECODERS and mode != "usb":
            raise RuntimeError("FT8 e WSPR si ricevono in USB")
        if decoder == "dati" and mode != "nfm":
            raise RuntimeError("Il riconoscimento dei dati funziona in FM stretta")
        if bw is not None and mode in BANDWIDTHS:
            bw = min(BANDWIDTHS[mode][1], key=lambda b: abs(b - bw))
        else:
            bw = None
        self.status["live"] = {"freq_mhz": round(freq_hz / 1e6, 4), "mode": mode, "sid": sid, "decoder": decoder,
                               "until": until.timestamp() if until else None, "title": title,
                               "bw": bw or (BANDWIDTHS[mode][0] if mode in BANDWIDTHS else None), "text": ""}
        if mode == "dab":
            ch = dab.channel_for(freq_hz / 1e6)
            self.status["live"]["channel"] = ch[0] if ch else None
        if mode == "tv":
            tv = dvbt.channel_for(freq_hz / 1e6)
            if not tv:
                self.status["live"] = None
                raise RuntimeError("La frequenza non è in un canale TV (UHF 21–48 o VHF D–H2)")
            self.status["live"].update(channel=dvbt.channel_label(tv[0]), freq_mhz=tv[1])
        if mode == "sat":
            self.status["live"].update(self._sat_transponder(freq_hz / 1e6))
        self._update_active()  # la scansione si ferma al passo successivo e libera la chiavetta
        run = (self._live_dab_run(freq_hz, sid) if mode == "dab"
               else self._live_tv_run(freq_hz, mode == "sat") if mode in ("tv", "sat")
               else self._live_meter_run(freq_hz) if mode == "meter"
               else self._live_run(freq_hz, mode, decoder, until, title, auto, bw))
        self._live_task = asyncio.get_running_loop().create_task(run, name="spettro_live")
        if self.viewers <= 0 and decoder not in KEEP_RUNNING:
            self._schedule_live_stop()
        return self.status["live"]

    async def live_stop(self) -> None:
        if self._live_stop_handle is not None:
            self._live_stop_handle.cancel()
            self._live_stop_handle = None
        task, self._live_task = self._live_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _schedule_live_stop(self) -> None:
        if self._live_stop_handle is not None:
            self._live_stop_handle.cancel()

        def check() -> None:
            self._live_stop_handle = None
            live = self.status["live"] or {}
            if self.viewers <= 0 and self._live_task is not None and live.get("decoder") not in KEEP_RUNNING:
                _LOGGER.info("Nessun pannello aperto: analisi continua fermata")
                asyncio.get_running_loop().create_task(self.live_stop())

        self._live_stop_handle = asyncio.get_running_loop().call_later(LIVE_NO_VIEWER_STOP_S, check)

    async def _live_run(self, freq_hz: float, mode: str, decoder: str | None = None,
                        until: datetime | None = None, title: str | None = None, auto: bool = False,
                        bw: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        img = _ImageListener(decoder, freq_hz, title) if decoder in IMAGE_DECODERS else None
        text = textdec.DECODERS[decoder]() if decoder in textdec.DECODERS else None
        slot = (slots.SlotDecoder(decoder, freq_hz, self.meteo.location if self.meteo else None)
                if decoder in SLOT_DECODERS else None)
        slot_tasks: set[asyncio.Task] = set()
        data_raw: deque[bytes] = deque()  # ultimi secondi di IQ per "Riconosci dati"
        data_task: asyncio.Task | None = None
        last_data = loop.time()
        log = self._log_start("ISS", "iss", auto) if title == "ISS" else None
        center = float(round(freq_hz + LIVE_RATE / 8))
        discard = RCV_BUF + 2 * READ_LIMIT + int(LIVE_RATE * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        rds_task: asyncio.Task | None = None
        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, LIVE_RATE)
                    await self._tune(writer, center, reader)
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    demod = LiveDemod(LIVE_RATE, freq_hz - center, mode, bw)
                    mpx: list[np.ndarray] = []
                    keep = int(LIVE_RDS_WINDOW_S * demod.mid)
                    last_rds = loop.time()
                    while until is None or datetime.now(UTC) < until:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(CHUNK * 2)
                        out = await loop.run_in_executor(None, _live_chunk, demod, raw, img, text, slot, time.time())
                        if img is not None:
                            await self._image_events(img, out)
                        extra: dict[str, Any] = {}
                        if text is not None:
                            extra = {"text": out["text"], "text_info": text.status()["info"]}
                            if out["text"] and self.status["live"]:
                                self.status["live"]["text"] = (self.status["live"]["text"] + out["text"])[-TEXT_KEEP:]
                        if decoder == "dati":
                            data_raw.append(raw)
                            while len(data_raw) * CHUNK > DATA_WINDOW_S * LIVE_RATE:
                                data_raw.popleft()
                            if loop.time() - last_data >= DATA_EVERY_S and (data_task is None or data_task.done()):
                                last_data = loop.time()
                                data_task = loop.create_task(self._live_data(b"".join(data_raw), freq_hz - center))
                                slot_tasks.add(data_task)
                                data_task.add_done_callback(slot_tasks.discard)
                        if slot is not None:
                            extra = {"digi": slot.status()}
                            for start, seg in out["slots"]:
                                task = loop.create_task(self._decode_slot(slot, start, seg))
                                slot_tasks.add(task)
                                task.add_done_callback(slot_tasks.discard)
                        self._emit({
                            **extra,
                            "kind": "live",
                            "freq_mhz": round(freq_hz / 1e6, 4),
                            "mode": mode,
                            "audio": base64.b64encode(out["pcm"]).decode(),
                            "row": out["row"],
                            "span_hz": out["span_hz"],
                            "level_db": out["level_db"],
                        })
                        if out["mpx"] is None:
                            continue
                        mpx.append(out["mpx"])
                        while sum(len(m) for m in mpx) - len(mpx[0]) >= keep:
                            mpx.pop(0)
                        # l'RDS si decodifica a parte, così la lettura dei campioni non si ferma
                        if loop.time() - last_rds >= LIVE_RDS_EVERY_S and (rds_task is None or rds_task.done()):
                            last_rds = loop.time()
                            rds_task = loop.create_task(self._live_rds(np.concatenate(mpx), demod.mid))
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Analisi continua interrotta: %s", msg)
            self._emit({"kind": "live_error", "error": msg})
        finally:
            if rds_task is not None:
                rds_task.cancel()
            for task in slot_tasks:
                task.cancel()
            if log is not None and img is not None:
                self._log_end(log, ("1 immagine SSTV salvata" if img.saved == 1 else f"{img.saved} immagini SSTV salvate")
                              if img.saved
                              else "nessuna immagine (la ISS non trasmetteva SSTV)")
            if asyncio.current_task() is self._live_task:
                self._live_task = None
            self.status["live"] = None
            self._update_active()  # riparte la scansione, se è attiva
            await self._adsb_maybe_resume()

    async def _live_dab_run(self, freq_hz: float, sid: str) -> None:
        """Radio DAB+ in diretta: chiavetta al centro del canale a 2,048 MS/s, AU AAC al pannello."""
        loop = asyncio.get_running_loop()
        ch = dab.channel_for(freq_hz / 1e6)
        center = ch[1] * 1e6 if ch else freq_hz
        discard = RCV_BUF + 2 * READ_LIMIT + int(dab.RATE * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        rx = DabLiveReceiver(sid)
        self.last_slide = None
        self.last_dab = {}
        chunk = dab.FRAME_LEN * 2  # due trame (192 ms) per lettura
        last_stats = 0.0
        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, dab.RATE)
                    await self._tune(writer, center, reader)
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    while True:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(chunk * 2)
                        out = await loop.run_in_executor(None, rx.process, raw)
                        payload: dict[str, Any] = {"kind": "live_dab", "freq_mhz": round(center / 1e6, 4), "sid": sid,
                                                   "synced": out["synced"], "error": out["error"]}
                        if out["row"] is not None:
                            payload.update(row=out["row"], span_hz=dab.RATE, level_db=out["level_db"])
                        if out["aus"]:
                            payload["aus"] = [base64.b64encode(a).decode() for a in out["aus"]]
                            payload["config"] = out["config"]
                        if "slide" in out:
                            payload["slide"] = out["slide"]
                            self.last_slide = out["slide"]
                        if "info" in out:
                            payload["info"] = self.last_dab["info"] = out["info"]
                            if out["info"].get("service") and self.status["live"]:
                                self.status["live"]["service"] = out["info"]["service"]
                                self._emit(dict(self.status))
                        if loop.time() - last_stats >= 1 and "stats" in out:
                            last_stats = loop.time()
                            self.last_dab.update(stats=out["stats"], dls=out["dls"])
                            # il nome della radio va ripetuto: un pannello aperto dopo l'aggancio non l'ha ricevuto
                            payload.update(self.last_dab)
                        self._emit(payload)
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Analisi continua DAB interrotta: %s", msg)
            self._emit({"kind": "live_error", "error": msg})
        finally:
            if asyncio.current_task() is self._live_task:
                self._live_task = None
            self.status["live"] = None
            self._update_active()

    # --- ricevitore aerei ADS-B (1090 MHz) --------------------------------------

    async def adsb_start(self, continuous: bool = False, online: bool = False) -> dict[str, Any]:
        """Tiene la chiavetta su 1090 MHz e raccoglie gli aerei (scansione sospesa).

        continuous=True: resta sintonizzato anche a pannello chiuso (finché non lo fermi).
        online=True: scarica da internet modello, immatricolazione e rotta (adsbdb.com).
        """
        if self.analyzing or self._dab_task is not None:
            raise RuntimeError("C'è un'altra operazione in corso, riprova tra poco")
        if self.meteo_rx is not None:
            raise RuntimeError("C'è una ricezione satellitare in corso")
        if online and self.http_get is None:
            online = False
        await self.live_stop()
        if self._adsb_task is not None:
            self.status["adsb"]["continuous"] = continuous  # già attivo: cambio solo le opzioni
            self.status["adsb"]["online"] = online and self.http_get is not None
            self._emit(dict(self.status))
            return self.status["adsb"]
        self.adsb_tracker = adsb_mod.AircraftTracker(self.meteo.location if self.meteo else None)
        self.adsb_tracker.record = self.adsb_record
        self.status["adsb"] = {"continuous": continuous, "online": online, "record": self.adsb_record,
                               "aircraft": [], "count": 0, "messages": 0, "history": len(self.adsb_history),
                               "home": list(self.meteo.location) if self.meteo else None}
        self._adsb_task = asyncio.get_running_loop().create_task(self._adsb_run(), name="spettro_adsb")
        self._update_active()  # ora la scansione vede il task e si ferma
        return self.status["adsb"]

    async def adsb_stop(self) -> None:
        task, self._adsb_task = self._adsb_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _adsb_file(self) -> Any:
        return self.archive.path.parent / "spettro_adsb_history.json" if self.archive is not None else None

    async def _load_adsb_history(self) -> None:
        path = self._adsb_file()
        if path is None:
            return

        def read() -> dict[str, Any] | None:
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None

        data = await asyncio.get_running_loop().run_in_executor(None, read)
        if data and not self.adsb_history:
            self.adsb_record = bool(data.get("record"))
            self.adsb_history = data.get("flights", [])
            self._prune_adsb_history()

    def _prune_adsb_history(self) -> None:
        limit = time.time() - ADSB_HISTORY_H * 3600
        self.adsb_history = [f for f in self.adsb_history if f.get("last", 0) > limit][:ADSB_HISTORY_MAX]

    def _record_flights(self, planes: list[dict[str, Any]]) -> None:
        added = False
        for pl in planes:
            if not pl.get("call") and not pl.get("path"):
                continue  # nulla di utile (né sigla né posizione)
            self.adsb_history.insert(0, self._adsb_flight_record(pl))
            added = True
        if added:
            self._prune_adsb_history()
            self._save_adsb_history()
            self._emit_adsb_history()

    def _adsb_flight_record(self, pl: dict[str, Any]) -> dict[str, Any]:
        icao = pl["icao"]
        rec: dict[str, Any] = {"id": f"{icao}-{int(pl['first'])}", "icao": icao, "call": pl.get("call"),
                               "first": round(pl["first"], 1), "last": round(pl["last"], 1), "msgs": pl["msgs"],
                               "country": adsb_db.country(icao), "path": pl.get("path", [])}
        name, flight = adsb_db.airline(pl.get("call"))
        rec["airline"], rec["flight"] = name, flight
        if "cat" in pl:
            rec["category"] = adsb_db.category(*pl["cat"])
        if "alt_ft" in pl:
            rec["alt_m"] = round(pl["alt_ft"] * 0.3048)
        info = self._adsb_ac.get(icao)
        if info:
            rec.update({k: info[k] for k in ("registration", "type", "manufacturer", "owner") if info.get(k)})
        route = self._adsb_route.get(pl.get("call") or "")
        if route:
            rec["route"] = route
        if pl.get("path") and self.meteo:
            kms = [adsb_mod.distance_km(self.meteo.location, (pt[0], pt[1])) for pt in pl["path"]]
            rec["km_min"] = round(min(kms), 1)
        return rec

    def _save_adsb_history(self) -> None:
        path = self._adsb_file()
        if path is None:
            return
        data = json.dumps({"record": self.adsb_record, "flights": self.adsb_history, "time": time.time()})
        asyncio.get_running_loop().run_in_executor(None, path.write_text, data)

    def _adsb_compact(self) -> list[dict[str, Any]]:
        """Lo storico senza le tracce (leggero per la lista); le tracce si chiedono a parte."""
        return [{k: v for k, v in f.items() if k != "path"} | {"points": len(f.get("path", []))}
                for f in self.adsb_history]

    def _emit_adsb_history(self) -> None:
        self._emit({"kind": "adsb_history", "flights": self._adsb_compact(), "record": self.adsb_record,
                    "hours": ADSB_HISTORY_H, "home": list(self.meteo.location) if self.meteo else None})

    async def adsb_set_record(self, on: bool) -> None:
        self.adsb_record = on
        if self.adsb_tracker is not None:
            self.adsb_tracker.record = on
        if self.status["adsb"]:
            self.status["adsb"]["record"] = on
        self._save_adsb_history()
        self._emit_adsb_history()

    async def adsb_clear_history(self) -> None:
        self.adsb_history = []
        self._save_adsb_history()
        self._emit_adsb_history()

    def adsb_history_paths(self, ids: list[str]) -> dict[str, list]:
        want = set(ids)
        return {f["id"]: f.get("path", []) for f in self.adsb_history if f["id"] in want}

    async def _adsb_yield(self, resume: bool) -> None:
        """Libera la chiavetta dall'ADS-B per un'altra operazione. resume=True: la riprende dopo."""
        if self._adsb_task is not None:
            if resume and self.status["adsb"]:
                self._adsb_resume = {"continuous": self.status["adsb"]["continuous"],
                                     "online": self.status["adsb"]["online"]}
            await self.adsb_stop()

    async def _adsb_maybe_resume(self) -> None:
        """Riavvia l'ADS-B dopo un passaggio, se era stato sospeso e la chiavetta è di nuovo libera."""
        opts = self._adsb_resume
        if opts is None or self._adsb_task is not None:
            return
        if self.status["live"] or self.meteo_rx is not None or self.analyzing or self._dab_task is not None:
            return  # qualcos'altro sta ancora usando la chiavetta: si riproverà più tardi
        self._adsb_resume = None
        try:
            await self.adsb_start(opts["continuous"], opts["online"])
        except RuntimeError:
            pass

    async def _adsb_run(self) -> None:
        loop = asyncio.get_running_loop()
        tracker = self.adsb_tracker
        rate = adsb_mod.RATE
        discard = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        tail = b""
        last_emit = 0.0
        no_viewer_since: float | None = None

        def work(raw: bytes) -> list[list[int]]:
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            return adsb_mod.decode_frames((a[0::2] + 1j * a[1::2]).astype(np.complex64), rate)

        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    await self._tune(writer, adsb_mod.FREQ, reader)
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    while True:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(ADSB_CHUNK * 2)
                        frames = await loop.run_in_executor(None, work, tail + raw)
                        tail = raw[-ADSB_OVERLAP * 2:]
                        tracker.feed_frames(frames)
                        gone = tracker.prune(ADSB_PRUNE_S)
                        if self.adsb_record and gone:
                            self._record_flights(gone)
                        if loop.time() - last_emit >= ADSB_EMIT_S:
                            last_emit = loop.time()
                            ac = tracker.aircraft()
                            if self.status["adsb"]["online"]:
                                self._adsb_enrich(ac)
                            self.status["adsb"].update(aircraft=ac, count=len(ac), messages=tracker.messages)
                            self._emit({"kind": "adsb", **self.status["adsb"]})
                        # a pannello chiuso: se non è "continuo", ci si ferma dopo un po'
                        if not self.status["adsb"]["continuous"] and self.viewers <= 0:
                            no_viewer_since = no_viewer_since or loop.time()
                            if loop.time() - no_viewer_since > LIVE_NO_VIEWER_STOP_S:
                                _LOGGER.info("Nessun pannello aperto: ricevitore ADS-B fermato")
                                break
                        else:
                            no_viewer_since = None
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Ricevitore ADS-B interrotto: %s", msg)
            self._emit({"kind": "adsb_error", "error": msg})
        finally:
            if self.adsb_record and self.adsb_tracker is not None:
                self._record_flights(list(self.adsb_tracker.planes.values()))
            if asyncio.current_task() is self._adsb_task:
                self._adsb_task = None
            self.status["adsb"] = None
            self.adsb_tracker = None
            self._update_active()
            self._emit({"kind": "adsb", "done": True})
            await self._adsb_maybe_resume()

    def _adsb_enrich(self, aircraft: list[dict[str, Any]]) -> None:
        """Unisce agli aerei i dettagli online già in cache, e avvia la ricerca di quelli nuovi."""
        loop = asyncio.get_running_loop()
        for a in aircraft:
            info = self._adsb_ac.get(a["icao"])
            if info:
                a.update({k: v for k, v in info.items() if v})
            elif info is None and self.http_get is not None:
                self._adsb_ac[a["icao"]] = {}  # segnaposto: non richiederlo di nuovo mentre arriva
                loop.create_task(self._adsb_fetch_ac(a["icao"]))
            route = self._adsb_route.get(a.get("call") or "")
            if route:
                a["route"] = route
            elif a.get("call") and a["call"] not in self._adsb_route and self.http_get is not None:
                self._adsb_route[a["call"]] = {}
                loop.create_task(self._adsb_fetch_route(a["call"]))

    async def _adsb_fetch_ac(self, icao: str) -> None:
        if self.http_get is None:
            return
        info = await adsb_online.aircraft_info(self.http_get, icao)
        if info is None:
            self._adsb_ac.pop(icao, None)  # errore di rete: si riproverà
        else:
            self._adsb_ac[icao] = info

    async def _adsb_fetch_route(self, callsign: str) -> None:
        if self.http_get is None:
            return
        route = await adsb_online.route_info(self.http_get, callsign)
        if route is None:
            self._adsb_route.pop(callsign, None)
        else:
            self._adsb_route[callsign] = route

    async def _live_meter_run(self, freq_hz: float) -> None:
        """Misuratore di puntamento: tiene la frequenza e mostra di continuo il livello e il picco,
        per orientare l'antenna (satelliti, 137 MHz, ripetitori…). Nessun audio."""
        loop = asyncio.get_running_loop()
        rate = LIVE_RATE
        center = float(round(freq_hz + rate / 8))
        discard = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        chunk = int(rate * 0.2) * 2

        def work(raw: bytes) -> tuple[float, float, list[float]]:
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            iq = a[0::2] + 1j * a[1::2]
            level = 10 * math.log10(float(np.mean(a * a)) * 2 / 127.5**2 + 1e-20)
            row = dvbt.spectrum_row(iq)
            # picco più forte escludendo il centro (residuo DC della chiavetta)
            n = len(row)
            peak = max(row[: n // 2 - 2] + row[n // 2 + 3:]) if n > 8 else level
            return round(level, 1), round(peak, 1), row

        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    await self._tune(writer, center, reader)
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    while True:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(chunk)
                        job = loop.run_in_executor(None, work, raw)
                        while not job.done():
                            async with asyncio.timeout(IO_TIMEOUT):
                                if not await reader.read(READ_LIMIT):
                                    raise ConnectionError("Connessione chiusa dal server")
                            await asyncio.sleep(0)
                        level, peak, row = job.result()
                        self._emit({"kind": "live_meter", "freq_mhz": round(freq_hz / 1e6, 4),
                                    "level_db": level, "peak_db": peak, "row": row, "span_hz": rate})
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Misuratore di puntamento interrotto: %s", msg)
            self._emit({"kind": "live_error", "error": msg})
        finally:
            if asyncio.current_task() is self._live_task:
                self._live_task = None
            self.status["live"] = None
            self._update_active()
            await self._adsb_maybe_resume()

    def _sat_transponder(self, if_mhz: float) -> dict[str, Any]:
        """Transponder riconosciuto nell'ultima scansione più vicino alla frequenza (sul cavo), se c'è."""
        sat = (self.last_complete or {}).get("sat") or {}
        near = [t for t in sat.get("transponders", []) if abs(t["if_mhz"] - if_mhz) <= t["sr"] / 1000 * 0.6]
        if not sat.get("found") or not near:
            return {"satellite": None, "ku_mhz": None}
        t = min(near, key=lambda t: abs(t["if_mhz"] - if_mhz))
        return {"satellite": sat["satellite"], "ku_mhz": t["ku_mhz"], "polarization": sat["polarization"],
                "freq_mhz": t["if_mhz"]}

    async def _live_tv_run(self, freq_hz: float, sat: bool = False) -> None:
        """Misuratore di campo: TV terrestre (livello e qualità del multiplex) o transponder satellitare (livello)."""
        loop = asyncio.get_running_loop()
        if sat:
            live = self.status["live"] or {}
            freq_hz = (live.get("freq_mhz") or freq_hz / 1e6) * 1e6  # centro del transponder, se riconosciuto
            name, center_mhz, bw = "sat", freq_hz / 1e6, 0
        else:
            name, center_mhz, bw = dvbt.channel_for(freq_hz / 1e6) or ("?", freq_hz / 1e6, 8)
        center = center_mhz * 1e6
        rate = dab.RATE
        discard = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        chunk = int(rate * TV_LIVE_S) * 2
        known: dict[str, Any] | None = None
        misses = 0

        def work(raw: bytes) -> tuple[dict[str, Any] | None, list[float], float]:
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            iq = a[0::2] + 1j * a[1::2]
            level = 10 * math.log10(float(np.mean(a * a)) * 2 / 127.5**2 + 1e-20)
            m = None if sat else dvbt.measure(iq, rate, bw, known)
            return m, dvbt.spectrum_row(iq), round(level, 1)

        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    await self._tune(writer, center, reader)
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    while True:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(chunk)
                        job = loop.run_in_executor(None, work, raw)
                        # durante il calcolo si scartano i campioni in arrivo: la misura dopo è fresca
                        while not job.done():
                            async with asyncio.timeout(IO_TIMEOUT):
                                if not await reader.read(READ_LIMIT):
                                    raise ConnectionError("Connessione chiusa dal server")
                            await asyncio.sleep(0)
                        m, row, level = job.result()
                        if m:
                            known, misses = m, 0
                        else:
                            misses += 1
                            if misses >= 3:
                                known = None  # forse è cambiato: si torna a cercare tutte le modalità
                        if sat:
                            self._emit({"kind": "live_sat", "freq_mhz": center_mhz, "row": row, "span_hz": rate,
                                        "level_db": level, "flatness_db": round(float(np.percentile(row, 90)
                                                                                        - np.percentile(row, 10)), 1)})
                            continue
                        self._emit({"kind": "live_tv", "freq_mhz": center_mhz, "channel": dvbt.channel_label(name),
                                    "row": row, "span_hz": rate, "level_db": level,
                                    "tv": m, "quality": dvbt.quality_label(m) if m else None})
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Misura continua TV interrotta: %s", msg)
            self._emit({"kind": "live_error", "error": msg})
        finally:
            if asyncio.current_task() is self._live_task:
                self._live_task = None
            self.status["live"] = None
            self._update_active()

    # --- satelliti meteo --------------------------------------------------------

    async def _meteo_loop(self) -> None:
        """Tiene aggiornati TLE e passaggi; con la ricezione automatica avvia i passaggi da sé."""
        assert self.meteo is not None
        loop = asyncio.get_running_loop()
        min_el = None
        while True:
            try:
                m = self.meteo
                stale = time.time() - m.tle_time > weather.TLE_MAX_AGE_S
                if m.fetch is not None and time.time() - m.tle_try > 600 and (stale or m.missing()):
                    if await m.update_tles() and self.meteo_save is not None:
                        self.meteo_save()
                if m.sats and (time.time() - m.passes_time > 600 or min_el != self.settings.meteo_min_el):
                    min_el = self.settings.meteo_min_el
                    await loop.run_in_executor(None, m.compute_passes, min_el)
                    self._emit(m.snapshot())
                if self.settings.meteo_auto and self._meteo_task is None:
                    now = datetime.now(UTC)
                    for p in m.passes:
                        key = (p["norad"], p["aos"].timestamp())
                        if key in self._meteo_done or p.get("kind") != "meteor":
                            continue
                        if p["aos"] - timedelta(seconds=30) <= now < p["los"] - timedelta(seconds=90):
                            self._meteo_done.add(key)
                            self._meteo_start({**p, "auto": True})
                            break
                if self.settings.iss_sstv and self._meteo_task is None and not self.status["live"]:
                    now = datetime.now(UTC)
                    for p in m.passes:
                        key = (p["norad"], p["aos"].timestamp())
                        if p.get("kind") == "iss" and key not in self._meteo_done and p["aos"] <= now < p["los"]:
                            self._meteo_done.add(key)
                            await self._adsb_yield(resume=True)
                            await self.live_start(weather.ISS_FREQ, "nfm", decoder="sstv", until=p["los"],
                                                  title="ISS", auto=True)
                            break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - il ciclo non deve morire per un errore
                _LOGGER.exception("Errore nella gestione dei satelliti meteo")
            await asyncio.sleep(15)

    def _log_start(self, satellite: str, kind: str, auto: bool) -> dict[str, Any]:
        """Nuova riga del registro delle ricezioni dei satelliti (Meteor e ISS)."""
        entry = {"start": time.time(), "end": None, "satellite": satellite, "kind": kind, "auto": auto,
                 "result": "in corso"}
        self.meteo_log.appendleft(entry)
        self._emit({"kind": "meteo_log", "log": list(self.meteo_log)})
        return entry

    def _log_end(self, entry: dict[str, Any], result: str) -> None:
        entry.update(end=time.time(), result=result)
        self._emit({"kind": "meteo_log", "log": list(self.meteo_log)})

    def meteo_pass_now(self, norad: int | None = None) -> dict[str, Any] | None:
        """Passaggio in corso (o che inizia entro 3 minuti), anche sotto l'elevazione minima."""
        if self.meteo is None or not self.meteo.sats:
            return None
        now = datetime.now(UTC)
        cand = []
        for n, sat in self.meteo.sats.items():
            if n not in weather.SATELLITES or (norad is not None and n != norad):
                continue
            for p in orbit_passes(sat, self.meteo.observer, now - timedelta(minutes=20), 1, 0):
                if p["aos"] - timedelta(minutes=3) <= now < p["los"]:
                    cand.append({**p, "norad": n, "satellite": sat.name, "freqs": list(weather.SATELLITES[n][1])})
        return min(cand, key=lambda p: p["aos"]) if cand else None

    def _meteo_start(self, p: dict[str, Any]) -> None:
        self._meteo_task = asyncio.get_running_loop().create_task(self._meteo_run(p), name="spettro_meteo_rx")

    async def meteo_receive(self, norad: int | None = None) -> dict[str, Any]:
        """Ricezione a mano del passaggio in corso."""
        if self._meteo_task is not None:
            raise RuntimeError("C'è già una ricezione in corso")
        if self.meteo is None or not self.meteo.sats:
            raise RuntimeError("Orbite dei satelliti non ancora scaricate da CelesTrak: riprova tra qualche minuto")
        p = self.meteo_pass_now(norad)
        if p is None:
            raise RuntimeError("Nessun satellite Meteor sopra l'orizzonte adesso (o nei prossimi 3 minuti)")
        self._meteo_done.add((p["norad"], p["aos"].timestamp()))
        self._meteo_start(p)
        return weather.pass_json(p)

    async def meteo_stop(self) -> None:
        task, self._meteo_task = self._meteo_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _meteo_run(self, p: dict[str, Any]) -> None:
        """Riceve un passaggio: chiavetta fissa sul Meteor (con il Doppler), immagine alla fine."""
        assert self.meteo is not None
        loop = asyncio.get_running_loop()
        norad, freqs = p["norad"], p["freqs"]
        info = weather.pass_json(p)
        self.meteo_rx = {"pass": info, "stats": None, "preview": None, "rows": 0, "error": None}
        await self.live_stop()
        await self._adsb_yield(resume=True)
        self._update_active()
        self._emit({"kind": "meteo_live", **self.meteo_rx})
        rate = weather.IN_RATE
        chunk = rate // 4 * 2  # 0,25 s
        discard = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard -= discard % 2
        rx: weather.MeteorReceiver | None = None
        log = self._log_start(p["satellite"], "meteor", bool(p.get("auto")))
        saved_rows = 0
        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    fi = 0
                    last_preview = loop.time()
                    while True:
                        freq = freqs[fi]
                        center = freq + weather.CENTER_OFFSET
                        await self._tune(writer, center, reader)
                        async with asyncio.timeout(IO_TIMEOUT):
                            await self._discard(reader, discard)
                        rx = weather.MeteorReceiver(freq)
                        switched = False
                        while datetime.now(UTC) < p["los"] + timedelta(seconds=10):
                            async with asyncio.timeout(IO_TIMEOUT):
                                raw = await reader.readexactly(chunk)
                            dop = self.meteo.doppler(norad, datetime.now(UTC), freq)
                            stats = await loop.run_in_executor(None, rx.process, raw, center, dop)
                            look = self.meteo.look(norad, datetime.now(UTC))
                            stats["el"] = round(look["el"], 1) if look else None
                            stats["doppler_hz"] = round(dop)
                            self.meteo_rx["stats"] = stats
                            # dopo un po' senza trame valide (e con il satellite alto) si prova l'altra frequenza
                            if (len(freqs) > 1 and not stats["vcdu_ok"] and stats["elapsed_s"] > weather.SWITCH_AFTER_S
                                    and (look is None or look["el"] > 10)):
                                fi = (fi + 1) % len(freqs)
                                switched = True
                                break
                            if loop.time() - last_preview >= 8:
                                last_preview = loop.time()
                                png, _ir, name, rows = await loop.run_in_executor(None, rx.images, p["northbound"], 4)
                                if png:
                                    self.meteo_rx.update(preview=base64.b64encode(png).decode(), rows=rows * 4,
                                                         composite=name)
                            self._emit({"kind": "meteo_live", **{k: v for k, v in self.meteo_rx.items()
                                                                 if k != "preview" or loop.time() - last_preview < 0.5}})
                        if not switched:
                            break
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
            msg = str(err) or type(err).__name__
            _LOGGER.warning("Ricezione del satellite meteo interrotta: %s", msg)
            self._emit({"kind": "meteo_live", **(self.meteo_rx or {}), "error": msg})
        finally:
            try:
                if rx is not None and rx.decoder.strips:
                    saved_rows = await asyncio.shield(self._meteo_save_image(rx, p))
            finally:
                st = rx.stats() if rx is not None else {}
                self._log_end(log, f"foto salvata: {saved_rows} righe ({st.get('vcdu_ok', 0)} trame valide)" if saved_rows
                              else "segnale agganciato ma nessuna riga d'immagine" if st.get("vcdu_ok")
                              else "nessun segnale ricevuto (antenna? frequenza?)")
                if asyncio.current_task() is self._meteo_task:
                    self._meteo_task = None
                self.meteo_rx = None
                self._update_active()
                self._emit({"kind": "meteo_live", "done": True})
                await self._adsb_maybe_resume()

    async def _meteo_save_image(self, rx: weather.MeteorReceiver, p: dict[str, Any]) -> int:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, rx.finish)
        png, ir, name, rows = await loop.run_in_executor(None, rx.images, p["northbound"], 0)
        if not png or rows < 16 or self.meteo_archive is None:
            return 0
        st = rx.stats()
        meta = {"timestamp": time.time(), "norad": p["norad"], "satellite": p["satellite"], "max_el": p["max_el"],
                "aos": p["aos"].timestamp(), "los": p["los"].timestamp(), "northbound": p["northbound"],
                "rows": rows, "composite": name, "freq_mhz": st["freq_mhz"], "vcdu_ok": st["vcdu_ok"],
                "vcdu_bad": st["vcdu_bad"]}
        entry = await loop.run_in_executor(None, self.meteo_archive.add, meta, png, ir)
        self.last_meteo_png = png
        self._emit({"kind": "meteo_image", "entry": entry})
        await self.prune_meteo()
        return rows

    async def prune_meteo(self) -> None:
        if self.meteo_archive is not None:
            n = await asyncio.get_running_loop().run_in_executor(
                None, self.meteo_archive.prune, self.settings.meteo_days * 86400)
            if n:
                self._emit({"kind": "meteo_pruned"})

    async def _image_events(self, img: _ImageListener, out: dict[str, Any]) -> None:
        """Stato, anteprima e immagini finite della decodifica SSTV/radiofax in diretta."""
        loop = asyncio.get_running_loop()
        payload: dict[str, Any] = {"kind": "live_image", "decoder": img.kind, **img.status()}
        for ev in out.get("img_events", []):
            if ev["event"] == "start":
                img.mode_name = ev.get("mode")
            if ev["event"] == "image":
                payload["saved"] = await self._save_image(img, ev)
        if loop.time() - img.last_preview >= 4 and payload["state"] != "attesa":
            img.last_preview = loop.time()
            png = await loop.run_in_executor(None, img.preview_png)
            if png:
                payload["preview"] = base64.b64encode(png).decode()
        self._emit(payload)

    async def _save_image(self, img: _ImageListener, ev: dict[str, Any]) -> dict[str, Any] | None:
        if self.meteo_archive is None:
            return None
        loop = asyncio.get_running_loop()
        pix = ev["image"]
        if pix.ndim == 2:
            pix = np.stack([pix] * 3, axis=2)
        png = await loop.run_in_executor(None, encode_png, np.ascontiguousarray(pix))
        f_mhz = round(img.freq_hz / 1e6, 4)
        if img.kind == "sstv":
            title = f"SSTV {img.title or ''}".strip() + f" · {ev.get('mode') or img.mode_name}"
        else:
            title = f"Radiofax {f_mhz * 1000 + 1.9:.1f} kHz"
        meta = {"timestamp": time.time(), "norad": img.kind, "satellite": title, "max_el": None,
                "rows": int(pix.shape[0]), "composite": ev.get("mode") or ("radiofax" if img.kind == "fax" else ""),
                "freq_mhz": f_mhz, "source": img.kind, "complete": bool(ev.get("complete")),
                "aos": None, "los": None, "northbound": False, "vcdu_ok": None, "vcdu_bad": None}
        entry = await loop.run_in_executor(None, self.meteo_archive.add, meta, png, None)
        img.saved += 1
        self.last_meteo_png = png
        self._emit({"kind": "meteo_image", "entry": entry})
        return entry

    async def _live_data(self, raw: bytes, offset_hz: float) -> None:
        """Parametri e protocolli del segnale in ascolto, su una finestra di IQ (a parte, nell'executor)."""
        home = self.meteo.location if self.meteo else None

        def work() -> dict[str, Any]:
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            return verify.live_identify((a[0::2] + 1j * a[1::2]).astype(np.complex64), LIVE_RATE, offset_hz, home)

        try:
            res = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception:  # noqa: BLE001 - una finestra andata male non deve fermare l'ascolto
            _LOGGER.exception("Riconoscimento dei dati non riuscito")
            return
        live = self.status["live"]
        if not live:
            return
        log = live.setdefault("data_log", [])
        new = []
        for v in res["found"]:
            if not v.get("log"):
                continue  # riepiloghi (DMR, POCSAG): solo nello stato attuale, non nel registro
            for line in v["lines"]:
                item = {"protocol": v["protocol"], "line": line, "time": time.time()}
                # le finestre si sovrappongono: la stessa trama si vede due volte
                if not any(x["protocol"] == item["protocol"] and x["line"] == line and item["time"] - x["time"] < 10
                           for x in log[-50:]):
                    log.append(item)
                    new.append(item)
        del log[:-200]
        live["data_params"] = res["params"]
        live["data_protocols"] = sorted({v["protocol"] for v in res["found"]} | set(live.get("data_protocols", [])))
        self._emit({"kind": "live_data", "params": res["params"],
                    "params_text": verify.params_text(res["params"]) if res["params"] else None,
                    "found": [v["kind"] for v in res["found"]], "new": new, "protocols": live["data_protocols"]})

    async def _decode_slot(self, slot: slots.SlotDecoder, start: float, seg: np.ndarray) -> None:
        """Un periodo FT8/WSPR decodificato a parte: la lettura dei campioni non si ferma."""
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, slot.decode, start, seg)
        except Exception:  # noqa: BLE001 - un periodo andato male non deve fermare l'ascolto
            _LOGGER.exception("Decodifica %s non riuscita", slot.kind)
            return
        self.digi_log.extend(res)
        self._emit({"kind": "live_digi", "mode": slot.kind, "slot": start, "decodes": res, **slot.status()})

    async def _live_rds(self, mpx: np.ndarray, fs: float) -> None:
        result = await asyncio.get_running_loop().run_in_executor(None, rds.decode, mpx, fs)
        if self.status["live"]:
            self._emit({"kind": "live_rds", "rds": result})

    async def _run(self) -> None:
        while True:
            await self._active.wait()
            self._wake.clear()
            failed = False
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except ScanPaused:
                continue
            except (OSError, TimeoutError, asyncio.IncompleteReadError, RtlTcpError) as err:
                failed = True
                msg = str(err) or type(err).__name__
                _LOGGER.warning("Scansione fallita su %s:%s: %s", self.host, self.port, msg)
                self.status["error"] = msg
            except Exception as err:  # noqa: BLE001
                failed = True
                _LOGGER.exception("Errore imprevisto durante la scansione")
                self.status["error"] = str(err)

            if self._one_shot:
                self._one_shot = False
                self._update_active()  # fatto il giro chiesto: si torna in pausa
            try:
                await self.prune_archive()
            except OSError as err:
                _LOGGER.warning("Pulizia dell'archivio non riuscita: %s", err)
            if not self._active.is_set():
                self._update_active()  # messo in pausa a fine scansione: mostra lo stato "paused"
                continue
            wait = self.settings.interval_s
            if failed:
                wait = max(wait, MIN_ERROR_RETRY)
            self._set_status(
                state="error" if failed else "idle",
                next_scan=time.time() + wait,
                progress=0.0 if failed else 1.0,
            )
            try:
                async with asyncio.timeout(wait):
                    await self._wake.wait()
            except TimeoutError:
                pass

    # --- scansione ---------------------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, cmd: int, value: int) -> None:
        writer.write(struct.pack(">BI", cmd, value & 0xFFFFFFFF))
        await writer.drain()

    async def _tune(self, writer: asyncio.StreamWriter, freq_hz: float, reader: asyncio.StreamReader) -> None:
        """Sintonizza; sotto i 24 MHz accende il direct sampling (onde corte senza sintonizzatore).

        Cambiare il direct sampling reinizializza il sintonizzatore: il guadagno torna quello di
        fabbrica e per qualche istante i campioni non valgono. Si reimposta il guadagno e si scartano.
        """
        mode = self.settings.hf_sampling if freq_hz < HF_MAX_HZ else 0
        changed = self._direct.get(id(writer), -1) != mode
        if changed:
            await self._send(writer, CMD_SET_DIRECT_SAMPLING, mode)
            self._direct[id(writer)] = mode
            await self._set_gain(writer)
        await self._send(writer, CMD_SET_FREQ, int(round(freq_hz)))
        if changed:
            await self._discard(reader, int(self._rate * 2 * DIRECT_SETTLE_S) // 2 * 2)

    async def _configure(self, writer: asyncio.StreamWriter, rate: int) -> None:
        """Sample rate, correzione ppm e guadagno presi dalle impostazioni."""
        s = self.settings
        self._direct = {id(writer): -1}  # nuova connessione: il direct sampling va reimpostato alla prima sintonia
        self._rate = rate
        await self._send(writer, CMD_SET_SAMPLE_RATE, rate)
        await self._send(writer, CMD_SET_FREQ_CORRECTION, int(s.ppm))
        await self._set_gain(writer)

    async def _set_gain(self, writer: asyncio.StreamWriter) -> None:
        s = self.settings
        await self._send(writer, CMD_SET_AGC_MODE, 0)
        if s.gain_db is None:
            await self._send(writer, CMD_SET_GAIN_MODE, 0)
        else:
            await self._send(writer, CMD_SET_GAIN_MODE, 1)
            await self._send(writer, CMD_SET_GAIN, int(round(s.gain_db * 10)))

    async def _sweep(self) -> None:
        async with self._radio:
            if not self._active.is_set():
                raise ScanPaused
            await self._sweep_locked()

    async def _sweep_locked(self) -> None:
        s = self.settings
        f_start = s.start_mhz * 1e6
        f_stop = s.stop_mhz * 1e6
        rate = int(s.sample_rate)
        n = int(s.fft_size)
        bin_hz = rate / n
        keep = int(n * USABLE_FRACTION) // 2 * 2
        step_hz = keep * bin_hz
        steps = max(1, math.ceil((f_stop - f_start) / step_hz))
        total_bins = max(1, int(round((f_stop - f_start) / bin_hz)))
        pool = max(1, math.ceil(total_bins / MAX_POINTS))
        # separati di almeno: lo 0,4% dell'intervallo (per non affollare le scansioni larghe) ma al
        # massimo 150 kHz, così le bande dense (FM, 200 kHz) non vengono fuse; e comunque la risoluzione
        min_sep_hz = max(10e3, min((f_stop - f_start) * 0.004, 150e3), 2 * pool * bin_hz)

        window = np.hanning(n).astype(np.float32)
        norm = float((127.5 * window.sum()) ** 2)
        capture_bytes = n * AVERAGES * 2
        discard_bytes = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * s.settle_ms / 1000)
        discard_bytes -= discard_bytes % 2

        spectrum = np.empty(steps * keep, dtype=np.float32)
        self._sweep_id += 1
        sweep_id = self._sweep_id

        self._set_status(state="connecting", error=None, progress=0.0, next_scan=None)
        reader, writer, tuner = await _open(self.host, self.port)
        try:
            self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
            await self._configure(writer, rate)
            self._set_status(state="scanning")

            t0 = time.monotonic()
            last_partial = t0
            analyzed: list[float] = []  # frequenze già analizzate in questa spazzata (per non ripeterle)
            auto_done = 0
            for i in range(steps):
                if not self._active.is_set():
                    raise ScanPaused
                center = f_start + (i * keep + keep / 2) * bin_hz
                await self._tune(writer, center, reader)
                async with asyncio.timeout(IO_TIMEOUT):
                    await self._discard(reader, discard_bytes)
                    raw = await reader.readexactly(capture_bytes)

                psd = compute_psd(raw, n, window, norm)
                lo = (n - keep) // 2
                spectrum[i * keep : (i + 1) * keep] = psd[lo : lo + keep]

                if s.auto_analyze and not s.auto_limit_sweep:
                    # senza limite si analizza subito, man mano che i picchi si incontrano, così
                    # l'archivio si riempie durante la spazzata e il lavoro non va perso in caso di
                    # pausa (passaggio satellite/aerei). I picchi mancati qui (blocchi larghi, bande
                    # fitte dove il rumore della zona non è ancora noto da entrambi i lati) vengono
                    # recuperati a fine spazzata dalla riconciliazione, con lo spettro completo.
                    prev = self._floor_prev
                    if prev is not None and prev[0] == (f_start, bin_hz, total_bins):
                        noise = float(np.median(prev[1][i * keep: (i + 1) * keep]))
                    else:
                        first = max(0, i - FLOOR_SPAN + 1)
                        noise = max(step_low(psd[lo: lo + keep]), zone_floor(
                            [step_low(spectrum[k * keep: (k + 1) * keep]) for k in range(first, i + 1)]))
                    f0 = f_start + i * keep * bin_hz
                    spikes = self._spike_candidates(psd[lo: lo + keep], f0, bin_hz, noise + s.threshold_db, noise)
                    if spikes:
                        if i < steps - 1:
                            last_partial = time.monotonic()
                            progress = (i + 1) / steps
                            self.status["progress"] = progress
                            self._publish(spectrum, (i + 1) * keep, total_bins, f_start, f_stop,
                                          bin_hz, pool, min_sep_hz, sweep_id, False, progress, None)
                            self._emit(dict(self.status))
                        for f, scan_snr in spikes:
                            if not self._active.is_set():
                                raise ScanPaused
                            analyzed.append(f)
                            auto_done += 1
                            await self._auto_analyze(reader, writer, rate, f, discard_bytes, scan_snr)

                now = time.monotonic()
                if now - last_partial >= PARTIAL_EVERY and i < steps - 1:
                    last_partial = now
                    progress = (i + 1) / steps
                    self.status["progress"] = progress
                    self._publish(spectrum, (i + 1) * keep, total_bins, f_start, f_stop,
                                  bin_hz, pool, min_sep_hz, sweep_id, False, progress, None)
                    self._emit(dict(self.status))

            if s.auto_analyze:
                # riconciliazione a fine spazzata: tutto ciò che è segnato sul grafico (i triangoli,
                # da find_peaks con il rumore stimato da entrambi i lati sullo spettro completo) e non
                # è ancora stato analizzato in questa spazzata viene analizzato ora, dal più forte.
                # Così i picchi che l'analisi passo-per-passo salta (blocchi larghi DAB/TV, bande fitte)
                # non restano fuori dall'archivio fino al giro dopo. Con il budget limitato è anche il
                # modo per prendere i più forti di tutta la scansione invece dei primi incontrati.
                shown, _ = find_peaks(spectrum[:total_bins], f_start, bin_hz, s.threshold_db, min_sep_hz, keep)
                recent = time.time() - s.reanalyze_min * 60
                todo: list[tuple[float, float]] = []
                for pk in shown:
                    f = pk["freq_mhz"] * 1e6
                    if any(abs(f - g) <= SPIKE_TOL_HZ for g in analyzed):
                        continue  # già analizzata passo-per-passo in questa spazzata
                    if self.archive is not None and self.archive.find(f, SPIKE_TOL_HZ, newer_than=recent):
                        continue  # analizzata di recente (vedi "Rianalizza dopo")
                    todo.append((f, pk["snr"]))
                if todo:
                    todo.sort(key=lambda c: c[1], reverse=True)  # i più forti per primi
                    budget = int(s.auto_max_per_sweep) if s.auto_limit_sweep else len(todo)
                    # il grafico completo subito, poi le analisi (che possono durare parecchi secondi)
                    self.status["progress"] = 1.0
                    self._publish(spectrum, total_bins, total_bins, f_start, f_stop,
                                  bin_hz, pool, min_sep_hz, sweep_id, False, 1.0, None)
                    self._emit(dict(self.status))
                    for f, scan_snr in todo[:budget]:
                        if not self._active.is_set():
                            raise ScanPaused
                        analyzed.append(f)
                        auto_done += 1
                        await self._auto_analyze(reader, writer, rate, f, discard_bytes, scan_snr)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        sweep_s = round(time.monotonic() - t0, 1)
        self.status.update(last_scan=time.time(), sweep_s=sweep_s, progress=1.0,
                           auto_count=auto_done if s.auto_analyze else None)
        self._publish(spectrum, total_bins, total_bins, f_start, f_stop,
                      bin_hz, pool, min_sep_hz, sweep_id, True, 1.0, sweep_s)

    def _publish(
        self,
        spectrum: np.ndarray,
        filled: int,
        total_bins: int,
        f_start: float,
        f_stop: float,
        bin_hz: float,
        pool: int,
        min_sep_hz: float,
        sweep_id: int,
        complete: bool,
        progress: float,
        sweep_s: float | None,
    ) -> None:
        data = spectrum[: min(filled, total_bins)]
        whole = len(data) // pool * pool
        pooled = data[:whole].reshape(-1, pool).max(axis=1) if whole else np.empty(0, np.float32)
        if complete and whole < len(data):
            pooled = np.append(pooled, data[whole:].max())
        block = int(self.settings.fft_size * USABLE_FRACTION)  # bin di un passo della scansione
        peaks, floor_arr = find_peaks(data, f_start, bin_hz, self.settings.threshold_db, min_sep_hz, block)
        floor = float(np.median(floor_arr)) if len(floor_arr) else 0.0
        fl_pooled = floor_arr[: len(data) // pool * pool].reshape(-1, pool).mean(axis=1) if whole else floor_arr[:0]
        if complete and whole < len(data):
            fl_pooled = np.append(fl_pooled, floor_arr[whole:].mean())
        if self.archive is not None:
            for p in peaks:
                entry = self.archive.find(p["freq_mhz"] * 1e6, SPIKE_TOL_HZ)
                if entry:
                    p["aid"] = entry["id"]
                    p["kind"] = entry["kind"]
        payload = {
            "kind": "spectrum",
            "sweep_id": sweep_id,
            "complete": complete,
            "progress": round(progress, 4),
            "range_start": f_start,
            "range_stop": f_stop,
            # frequenza del punto 0 (centro del gruppo di bin) e passo tra i punti
            "f_start": f_start + (pool - 1) / 2 * bin_hz,
            "bin_hz": pool * bin_hz,
            "values": np.round(pooled, 1).tolist(),
            "peaks": peaks,
            "floor_db": round(floor, 1),
            "floor_values": np.round(fl_pooled, 1).tolist(),  # rumore locale, punto per punto
            "timestamp": time.time(),
            "sweep_s": sweep_s,
        }
        self.last_spectrum = payload
        if complete and self.settings.lnb:
            freqs = (f_start + np.arange(len(data)) * bin_hz) / 1e6
            payload["sat"] = satellite.identify(freqs, data)
        if complete:
            self._floor_prev = ((f_start, bin_hz, total_bins), floor_arr)
            self.last_complete = payload
            self.history.append(payload)
        self._emit(payload)


class _ImageListener:
    """Immagini nell'audio della diretta: SSTV o radiofax."""

    def __init__(self, kind: str, freq_hz: float, title: str | None) -> None:
        self.kind, self.freq_hz, self.title = kind, freq_hz, title
        self.tracker = ToneTracker()
        self.dec: SstvDecoder | FaxDecoder = SstvDecoder() if kind == "sstv" else FaxDecoder()
        self.last_preview = 0.0
        self.mode_name: str | None = None
        self.saved = 0  # immagini salvate durante questo ascolto

    def feed(self, pcm: bytes) -> list[dict[str, Any]]:
        return self.dec.feed(self.tracker.feed(pcm))

    def status(self) -> dict[str, Any]:
        d = self.dec
        if isinstance(d, SstvDecoder):
            return {"state": d.state, "mode": d.mode.name if d.mode else None,
                    "rows": d.done_periods * (d.mode.rows if d.mode else 1),
                    "height": d.mode.height if d.mode else None}
        return {"state": d.state, "mode": "Radiofax 120/576", "rows": d.progress_rows(), "height": None}

    def preview_png(self) -> bytes | None:
        img = self.dec.preview()
        if img is None:
            return None
        if img.ndim == 2:  # radiofax: in scala di grigi, ridotto a metà
            img = np.stack([img[::2, ::2]] * 3, axis=2)
        return encode_png(np.ascontiguousarray(img))


def _dab_probe(raw: bytes) -> dict[str, Any] | None:
    """Un canale DAB registrato: nome del multiplex e servizi, se c'è un multiplex leggibile."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    iq = (a[0::2] + 1j * a[1::2]).astype(np.complex64)
    iq -= iq.mean()
    res = dab.decode(iq, dab.RATE, 0.0)
    if not res or not res.get("fib_ok"):
        return None
    level = float(10 * np.log10(np.mean(np.abs(iq) ** 2) / 127.5**2 + 1e-20))
    return {"ensemble": res.get("ensemble"), "eid": res.get("eid"), "services": res.get("services", []),
            "quality": round(res["fib_ok"] / max(1, res["fib_total"]), 2), "level_db": round(level, 1)}


def _live_chunk(demod: LiveDemod, raw: bytes, img: _ImageListener | None = None,
                text: textdec.MorseDecoder | textdec.RttyDecoder | None = None,
                slot: slots.SlotDecoder | None = None, now: float = 0.0) -> dict[str, Any]:
    """Un blocco di byte rtl_tcp elaborato dal demodulatore in diretta (nell'executor)."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    out = demod.process((a[0::2] + 1j * a[1::2]).astype(np.complex64))
    if img is not None:
        out["img_events"] = img.feed(out["pcm"])
    audio = out.pop("audio")
    if text is not None:
        out["text"] = text.feed(audio)
    if slot is not None:
        out["slots"] = slot.feed(audio, now)
    return out
