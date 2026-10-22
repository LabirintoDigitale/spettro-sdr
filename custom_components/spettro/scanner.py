"""Client rtl_tcp e motore di scansione dello spettro.

Questo modulo non dipende da Home Assistant: si collega a un server rtl_tcp,
sintonizza la chiavetta a passi successivi, calcola la FFT di ogni passo e
ricompone uno spettro unico su tutto l'intervallo richiesto.
"""

from __future__ import annotations

import asyncio
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

from .analysis import analyze_iq
from .archive import SpikeArchive

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
ANALYSIS_MAX_S = 5  # durata massima di una registrazione per l'analisi
AUTO_ANALYSIS_S = 1.0  # registrazione dell'analisi automatica di un picco
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

        return remove

    def snapshot(self) -> list[dict[str, Any]]:
        """Stato, scansioni complete recenti (dalla più vecchia) ed eventuale parziale in corso.

        Un pannello appena aperto ricostruisce così grafico, max hold e cascata
        invece di ripartire dalla sola parte già scansionata del giro in corso.
        """
        out = [dict(self.status), *self.history]
        if self.last_spectrum is not None and not self.last_spectrum["complete"]:
            out.append(self.last_spectrum)
        return out

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
        rate = int(self.settings.sample_rate)
        capture_bytes = int(rate * min(duration_s, ANALYSIS_MAX_S)) * 2
        discard_bytes = RCV_BUF + 2 * READ_LIMIT + int(rate * 2 * max(self.settings.settle_ms, 100) / 1000)
        discard_bytes -= discard_bytes % 2
        # sintonia spostata: il segnale non deve cadere sul picco DC al centro della banda
        center = float(round(freq_hz + rate / 8))
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
        center = float(round(freq_hz + rate / 8))
        self._set_status(auto_mhz=round(freq_hz / 1e6, 4))
        try:
            await self._send(writer, CMD_SET_FREQ, int(center))
            async with asyncio.timeout(IO_TIMEOUT + AUTO_ANALYSIS_S):
                await self._discard(reader, discard)
                raw = await reader.readexactly(int(rate * AUTO_ANALYSIS_S) * 2)
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
                    for f in self._spike_candidates(psd[lo : lo + keep], f0, bin_hz, level):
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
