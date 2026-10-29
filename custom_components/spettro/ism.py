"""Ricevitore nativo dei sensori ISM (433/868/315 MHz…) basato su rtl_433.

A differenza del plugin, questo legge i byte grezzi di rtl_tcp — che sono GIÀ nel formato nativo
RTL-SDR (cu8: I/Q interleaved a 8 bit) — e li passa così come sono a `rtl_433`, senza conversioni.
Lo scanner lo pilota: tiene una lista di frequenze e a ciclo sintonizza, riceve per qualche secondo,
poi passa alla successiva (i dispositivi ISM stanno su bande diverse: 433.92, 868.3, 315…).

Richiede il programma `rtl_433` (incluso nell'integrazione in plugins/bin/, oppure nel PATH o in
/config/spettro_plugins/bin/rtl_433).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

RATE = 1_024_000  # campionamento: va bene sia per l'OOK a 433 sia per l'FSK a 868 (Fine Offset/Ecowitt)

# chiavi di servizio di rtl_433 da non mostrare come "misura"
_SKIP = {"model", "id", "channel", "time", "mic", "protocol", "subtype", "freq", "freq1", "freq2",
         "rssi", "snr", "noise", "mod", "sync", "len", "seq", "num_rows", "rows"}

_LABELS = {
    "temperature_C": "temp °C", "temperature_F": "temp °F", "temperature_1_C": "temp1 °C",
    "humidity": "umidità %", "humidity_1": "umidità1 %",
    "wind_avg_m_s": "vento m/s", "wind_avg_km_h": "vento km/h", "wind_max_m_s": "raffica m/s",
    "wind_max_km_h": "raffica km/h", "wind_dir_deg": "direzione °",
    "rain_mm": "pioggia mm", "rain_mm_h": "pioggia mm/h", "rain_in": "pioggia in",
    "pressure_hPa": "pressione hPa", "pressure_kPa": "pressione kPa",
    "uv": "UV", "uvi": "indice UV", "light_lux": "luce lux", "lux": "luce lux",
    "battery_ok": "batteria ok", "battery_V": "batteria V",
}


def find_rtl433() -> str | None:
    """Percorso dell'eseguibile rtl_433 (prima quello incluso nella release), reso eseguibile se serve."""
    bundled = Path(__file__).parent / "plugins" / "bin" / "rtl_433"
    for cand in (str(bundled), shutil.which("rtl_433"), "/config/spettro_plugins/bin/rtl_433",
                 "/usr/bin/rtl_433", "/usr/local/bin/rtl_433"):
        if cand and os.path.exists(cand):
            try:
                st = os.stat(cand)
                if not st.st_mode & 0o111:
                    os.chmod(cand, st.st_mode | 0o111)
            except OSError:
                pass
            return cand
    return None


def describe(d: dict[str, Any]) -> dict[str, Any]:
    """Da un messaggio JSON di rtl_433 a una riga pronta per il pannello."""
    vals = []
    for k, v in d.items():
        if k in _SKIP or k.startswith("_"):
            continue
        vals.append(f"{_LABELS.get(k) or k.replace('_', ' ')}={v}")
    return {
        "t": time.strftime("%H:%M:%S"),
        "model": str(d.get("model", "?")),
        "id": str(d.get("id", d.get("channel", ""))),
        "vals": " · ".join(vals) or (d.get("data") or ""),
    }


class Rtl433:
    """Un processo rtl_433 a cui si danno byte cu8 (da rtl_tcp) e da cui si leggono i sensori decodificati.

    La scrittura verso rtl_433 avviene in un thread con coda che SCARTA se rtl_433 resta indietro, così
    non blocca mai il loop della ricezione (come per il plugin). `poll()` restituisce i nuovi messaggi.
    """

    def __init__(self, rate: int = RATE) -> None:
        self.rate = int(rate)
        self.err: str | None = None
        self.proc: subprocess.Popen | None = None
        self._buf = b""
        exe = find_rtl433()
        if exe is None:
            self.err = ("rtl_433 non trovato. È incluso nell'integrazione; se manca, mettilo in "
                        "/config/spettro_plugins/bin/rtl_433 o nel PATH.")
            return
        try:
            self.proc = subprocess.Popen(
                [exe, "-r", "cu8:-", "-s", str(self.rate), "-C", "si", "-F", "json"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            os.set_blocking(self.proc.stdout.fileno(), False)
        except OSError as e:
            self.err = f"rtl_433 non avviabile: {e}"
            self.proc = None
            return
        self._stop = False
        self._q: queue.Queue = queue.Queue(maxsize=16)
        self._writer = threading.Thread(target=self._write_loop, name="ism_rtl433_writer", daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        while not self._stop:
            try:
                data = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                self.err = "rtl_433 si è chiuso"
                break

    def feed(self, raw: bytes) -> None:
        """Dai a rtl_433 un blocco di byte cu8 (I/Q a 8 bit) così come arrivano da rtl_tcp."""
        if self.proc is None or not raw:
            return
        try:
            self._q.put_nowait(raw)
        except queue.Full:
            pass  # rtl_433 è indietro: scarta (meglio perdere campioni che bloccare)

    def poll(self) -> list[dict[str, Any]]:
        """I messaggi JSON decodificati finora (già pronti per il pannello). Non blocca."""
        if self.proc is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            data = self.proc.stdout.read()
        except (BlockingIOError, OSError):
            data = None
        if data:
            self._buf += data
            *lines, self._buf = self._buf.split(b"\n")
            for line in lines:
                line = line.strip()
                if line.startswith(b"{"):
                    try:
                        out.append(describe(json.loads(line.decode("utf-8", "ignore"))))
                    except ValueError:
                        pass
        return out

    def close(self) -> None:
        self._stop = True
        q = getattr(self, "_q", None)
        if q is not None:
            try:
                q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None
