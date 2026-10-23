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
import logging
import math
import socket
import struct
import time
from typing import Any

import numpy as np

from . import dab, dvbt, rds
from .analysis import analyze_iq
from .dabplus import DabLiveReceiver
from .archive import SpikeArchive
from .live import CHUNK, LIVE_RATE, LiveDemod

_LOGGER = logging.getLogger(__name__)

# Comandi del protocollo rtl_tcp (1 byte comando + uint32 big endian)
CMD_SET_FREQ = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03
CMD_SET_GAIN = 0x04
CMD_SET_FREQ_CORRECTION = 0x05
CMD_SET_AGC_MODE = 0x08

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


def find_peaks(
    values: np.ndarray, f_start: float, bin_hz: float, threshold_db: float, min_sep_hz: float
) -> tuple[list[dict[str, float]], float]:
    """Massimi locali sopra (rumore di fondo + soglia), separati di almeno min_sep_hz."""
    if len(values) < 3:
        return [], float(values.max()) if len(values) else 0.0
    floor = float(np.median(values))
    mid = values[1:-1]
    mask = (mid > values[:-2]) & (mid >= values[2:]) & (mid > floor + threshold_db)
    idx = np.nonzero(mask)[0] + 1
    if not len(idx):
        return [], floor
    order = idx[np.argsort(values[idx])[::-1]][:3000]
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
            "snr": round(float(values[i]) - floor, 1),
        }
        for i in chosen
    ]
    return peaks, floor


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
        self._radio = asyncio.Lock()  # una sola connessione rtl_tcp alla volta
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
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
        if self.analyzing:
            return "analysis"
        if self.status["live"]:
            return "live"
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
            self._task = asyncio.get_running_loop().create_task(self._run(), name="spettro_scanner")

    async def stop(self) -> None:
        await self.live_stop()
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
        self._wake.set()

    async def analyze(self, freq_hz: float, duration_s: float) -> dict[str, Any]:
        """Mette in pausa la scansione, registra il segnale a frequenza fissa e lo misura."""
        if self.analyzing:
            raise RuntimeError("C'è già un'analisi in corso")
        if self._live_task is not None:
            raise RuntimeError("Ferma prima l'analisi continua")
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
                    await self._send(writer, CMD_SET_FREQ, int(center))
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
                None, analyze_iq, raw, rate, center, freq_hz
            )
        finally:
            self.analyzing = False
            self._update_active()  # riparte la scansione
        return await self._store_analysis(result, png, audio, auto=False)

    async def _store_analysis(
        self, result: dict[str, Any], png: bytes, audio: dict[str, bytes], auto: bool
    ) -> dict[str, Any]:
        """Ultima analisi in memoria, archivio su disco e notifica a pannello ed entità."""
        result["timestamp"] = time.time()
        result["auto"] = auto
        entry = None
        if self.archive is not None:
            try:
                entry = await asyncio.get_running_loop().run_in_executor(None, self.archive.add, result, png, audio)
                result["id"] = entry["id"]
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

    def _spike_candidates(self, seg: np.ndarray, f0: float, bin_hz: float, level: float) -> list[float]:
        """Picchi di un passo della scansione sopra `level`, dal più forte, non ancora analizzati di recente."""
        if self.archive is None or len(seg) < 3:
            return []
        mid = seg[1:-1]
        idx = np.nonzero((mid > seg[:-2]) & (mid >= seg[2:]) & (mid > level))[0] + 1
        out: list[float] = []
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
            if any(abs(f - g) <= SPIKE_TOL_HZ for g in out):
                continue
            if self.archive.find(f, SPIKE_TOL_HZ, newer_than=recent):
                continue
            out.append(f)
            if self.settings.auto_limit_step and len(out) >= self.settings.auto_max_per_step:
                break
        return out

    async def _auto_analyze(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, rate: int, freq_hz: float, discard: int
    ) -> None:
        """Analisi di un picco a metà scansione, sulla stessa connessione e con le stesse impostazioni."""
        center = self._tune_center(freq_hz, rate)
        self._set_status(auto_mhz=round(freq_hz / 1e6, 4))
        try:
            await self._send(writer, CMD_SET_FREQ, int(center))
            duration = min(self.settings.analysis_s, ANALYSIS_MAX_S)
            async with asyncio.timeout(IO_TIMEOUT + duration):
                await self._discard(reader, discard)
                raw = await reader.readexactly(int(rate * duration) * 2)
            # durante il calcolo rtl_tcp continua a inviare: si scarta, altrimenti il passo
            # successivo della scansione leggerebbe campioni vecchi della frequenza analizzata
            job = asyncio.get_running_loop().run_in_executor(None, analyze_iq, raw, rate, center, freq_hz)
            while True:
                done, _ = await asyncio.wait([job], timeout=0)
                if done:
                    break
                async with asyncio.timeout(IO_TIMEOUT):
                    if not await reader.read(READ_LIMIT):
                        raise ConnectionError("Connessione chiusa dal server")
            result, png, audio = job.result()
            await self._store_analysis(result, png, audio, auto=True)
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

    @staticmethod
    def live_mode_for(freq_hz: float) -> str:
        """Demodulazione predefinita per la frequenza: FM larga per la radio, AM per la banda aerea."""
        mhz = freq_hz / 1e6
        tv = dvbt.channel_for(mhz)
        if tv and tv[2] == 8:
            return "tv"
        if 87.5 <= mhz <= 108:
            return "wfm"
        if 118 <= mhz <= 137:
            return "am"
        return "nfm"

    async def live_start(self, freq_hz: float, mode: str | None = None, sid: str | None = None) -> dict[str, Any]:
        """Sintonizza la chiavetta fissa su freq_hz (scansione sospesa) e demodula di continuo.

        mode "dab" con sid: una radio DAB+ del multiplex (freq_hz = centro del canale).
        """
        if self.analyzing:
            raise RuntimeError("C'è un'analisi in corso, riprova tra qualche secondo")
        if mode == "dab" and not sid:
            raise RuntimeError("Scegli una radio del multiplex")
        await self.live_stop()
        mode = mode or self.live_mode_for(freq_hz)
        self.status["live"] = {"freq_mhz": round(freq_hz / 1e6, 4), "mode": mode, "sid": sid}
        if mode == "dab":
            ch = dab.channel_for(freq_hz / 1e6)
            self.status["live"]["channel"] = ch[0] if ch else None
        if mode == "tv":
            tv = dvbt.channel_for(freq_hz / 1e6)
            if not tv:
                self.status["live"] = None
                raise RuntimeError("La frequenza non è in un canale TV (UHF 21–48 o VHF D–H2)")
            self.status["live"].update(channel=dvbt.channel_label(tv[0]), freq_mhz=tv[1])
        self._update_active()  # la scansione si ferma al passo successivo e libera la chiavetta
        run = (self._live_dab_run(freq_hz, sid) if mode == "dab"
               else self._live_tv_run(freq_hz) if mode == "tv" else self._live_run(freq_hz, mode))
        self._live_task = asyncio.get_running_loop().create_task(run, name="spettro_live")
        if self.viewers <= 0:
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
            if self.viewers <= 0 and self._live_task is not None:
                _LOGGER.info("Nessun pannello aperto: analisi continua fermata")
                asyncio.get_running_loop().create_task(self.live_stop())

        self._live_stop_handle = asyncio.get_running_loop().call_later(LIVE_NO_VIEWER_STOP_S, check)

    async def _live_run(self, freq_hz: float, mode: str) -> None:
        loop = asyncio.get_running_loop()
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
                    await self._send(writer, CMD_SET_FREQ, int(center))
                    async with asyncio.timeout(IO_TIMEOUT):
                        await self._discard(reader, discard)
                    demod = LiveDemod(LIVE_RATE, freq_hz - center, mode)
                    mpx: list[np.ndarray] = []
                    keep = int(LIVE_RDS_WINDOW_S * demod.mid)
                    last_rds = loop.time()
                    while True:
                        async with asyncio.timeout(IO_TIMEOUT):
                            raw = await reader.readexactly(CHUNK * 2)
                        out = await loop.run_in_executor(None, _live_chunk, demod, raw)
                        self._emit({
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
            if asyncio.current_task() is self._live_task:
                self._live_task = None
            self.status["live"] = None
            self._update_active()  # riparte la scansione, se è attiva

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
                    await self._send(writer, CMD_SET_FREQ, int(round(center)))
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

    async def _live_tv_run(self, freq_hz: float) -> None:
        """Misuratore di campo TV: chiavetta al centro del canale, livello e qualità del multiplex di continuo."""
        loop = asyncio.get_running_loop()
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
            return dvbt.measure(iq, rate, bw, known), dvbt.spectrum_row(iq), round(level, 1)

        try:
            async with self._radio:
                reader, writer, tuner = await _open(self.host, self.port)
                try:
                    self.status["tuner"] = TUNER_NAMES.get(tuner, f"Tipo {tuner}")
                    await self._configure(writer, rate)
                    await self._send(writer, CMD_SET_FREQ, int(round(center)))
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

    async def _configure(self, writer: asyncio.StreamWriter, rate: int) -> None:
        """Sample rate, correzione ppm e guadagno presi dalle impostazioni."""
        s = self.settings
        await self._send(writer, CMD_SET_SAMPLE_RATE, rate)
        await self._send(writer, CMD_SET_FREQ_CORRECTION, int(s.ppm))
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
        min_sep_hz = max(10e3, (f_stop - f_start) * 0.004, 2 * pool * bin_hz)

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
            auto_left = s.auto_max_per_sweep if s.auto_limit_sweep else math.inf
            auto_done = 0
            for i in range(steps):
                if not self._active.is_set():
                    raise ScanPaused
                center = f_start + (i * keep + keep / 2) * bin_hz
                await self._send(writer, CMD_SET_FREQ, int(round(center)))
                async with asyncio.timeout(IO_TIMEOUT):
                    await self._discard(reader, discard_bytes)
                    raw = await reader.readexactly(capture_bytes)

                psd = compute_psd(raw, n, window, norm)
                lo = (n - keep) // 2
                spectrum[i * keep : (i + 1) * keep] = psd[lo : lo + keep]

                # picco trovato: si interrompe la ricerca, lo si analizza subito e poi si riprende
                if s.auto_analyze and auto_left > 0:
                    level = float(np.median(spectrum[: (i + 1) * keep])) + s.threshold_db
                    f0 = f_start + i * keep * bin_hz
                    spikes = self._spike_candidates(psd[lo : lo + keep], f0, bin_hz, level)
                    if spikes and i < steps - 1:
                        # prima il grafico aggiornato: le analisi possono durare parecchi secondi
                        last_partial = time.monotonic()
                        progress = (i + 1) / steps
                        self.status["progress"] = progress
                        self._publish(spectrum, (i + 1) * keep, total_bins, f_start, f_stop,
                                      bin_hz, pool, min_sep_hz, sweep_id, False, progress, None)
                        self._emit(dict(self.status))
                    for f in spikes:
                        if auto_left <= 0:
                            break
                        if not self._active.is_set():
                            raise ScanPaused
                        auto_left -= 1
                        auto_done += 1
                        await self._auto_analyze(reader, writer, rate, f, discard_bytes)

                now = time.monotonic()
                if now - last_partial >= PARTIAL_EVERY and i < steps - 1:
                    last_partial = now
                    progress = (i + 1) / steps
                    self.status["progress"] = progress
                    self._publish(spectrum, (i + 1) * keep, total_bins, f_start, f_stop,
                                  bin_hz, pool, min_sep_hz, sweep_id, False, progress, None)
                    self._emit(dict(self.status))
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
        peaks, floor = find_peaks(data, f_start, bin_hz, self.settings.threshold_db, min_sep_hz)
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
            "timestamp": time.time(),
            "sweep_s": sweep_s,
        }
        self.last_spectrum = payload
        if complete:
            self.last_complete = payload
            self.history.append(payload)
        self._emit(payload)


def _live_chunk(demod: LiveDemod, raw: bytes) -> dict[str, Any]:
    """Un blocco di byte rtl_tcp elaborato dal demodulatore in diretta (nell'executor)."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    return demod.process((a[0::2] + 1j * a[1::2]).astype(np.complex64))
