"""Plugin: sensori ISM 433/868/315 MHz con rtl_433.

Decodifica stazioni meteo, sensori di temperatura/umidità, TPMS (pressione gomme), contatti, telecomandi
e centinaia di altri dispositivi ISM — tutti in chiaro. Riceve il canale a banda base da Spettro, lo
converte nel formato nativo RTL-SDR (cu8) e lo passa a `rtl_433`, poi mostra in tabella ciò che decodifica.

Richiede il programma `rtl_433` installato sul sistema dove gira Home Assistant (nel PATH, oppure in
/config/spettro_plugins/bin/rtl_433). Sintonizza su 433.920 MHz (o 868.3 / 315) e scegli questo plugin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import numpy as np

PLUGIN = {
    "id": "rtl433",
    "name": "Sensori ISM (rtl_433)",
    "mode": "nfm",
    "input": "baseband",
    # 1 Msps come quando si lancia rtl_433 normalmente: va bene sia per l'OOK a 433 sia, soprattutto,
    # per l'FSK a 868 (stazioni meteo Fine Offset/Ecowitt), che a 250 kHz spesso non aggancia.
    "rate": 1_000_000,
    "bw": 300_000,     # canale largo: i dispositivi stanno a decine di kHz dalla portante
}


# chiavi di servizio di rtl_433 da non mostrare come "misura"
_SKIP = {"model", "id", "channel", "time", "mic", "protocol", "subtype", "freq", "freq1", "freq2",
         "rssi", "snr", "noise", "mod", "sync", "len", "seq", "num_rows", "rows"}

# etichette più leggibili per i campi più comuni (per gli altri si usa il nome con gli _ sostituiti)
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


def _find_rtl433() -> str | None:
    import pathlib  # noqa: PLC0415
    bundled = pathlib.Path(__file__).parent / "bin" / "rtl_433"  # binario incluso nella release
    for cand in (str(bundled), shutil.which("rtl_433"), "/config/spettro_plugins/bin/rtl_433",
                 "/usr/bin/rtl_433", "/usr/local/bin/rtl_433"):
        if cand and os.path.exists(cand):
            try:  # lo zip di release può perdere il bit di esecuzione: lo rimetto se serve
                st = os.stat(cand)
                if not st.st_mode & 0o111:
                    os.chmod(cand, st.st_mode | 0o111)
            except OSError:
                pass
            return cand
    return None


class Decoder:
    def __init__(self, freq_hz: float, rate: int) -> None:
        self.freq_hz = freq_hz
        self.rate = int(rate)
        self.rows: list[dict] = []   # ultimi messaggi decodificati
        self.buf = b""               # resto di una riga JSON non ancora completa
        self.err: str | None = None
        self.proc: subprocess.Popen | None = None
        exe = _find_rtl433()
        if exe is None:
            self.err = ("rtl_433 non trovato. Installalo sul sistema di Home Assistant "
                        "(nel PATH) oppure mettilo in /config/spettro_plugins/bin/rtl_433.")
            return
        try:
            self.proc = subprocess.Popen(
                [exe, "-r", "cu8:-", "-s", str(self.rate), "-F", "json"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            os.set_blocking(self.proc.stdout.fileno(), False)
        except OSError as e:
            self.err = f"rtl_433 non avviabile: {e}"
            self.proc = None

    def feed(self, z: np.ndarray) -> dict | None:
        if self.err:
            return {"html": f'<div style="color:#e53935;font:13px sans-serif">⚠ {self.err}</div>'}
        if self.proc is None or z is None or len(z) == 0:
            return None
        # il baseband (complex64, ampiezza ~±127) si converte nel formato nativo RTL-SDR cu8
        iq = np.empty(len(z) * 2, dtype=np.uint8)
        iq[0::2] = np.clip(z.real + 127.5, 0, 255).astype(np.uint8)
        iq[1::2] = np.clip(z.imag + 127.5, 0, 255).astype(np.uint8)
        try:
            self.proc.stdin.write(iq.tobytes())
        except (BrokenPipeError, OSError):
            self.err = "rtl_433 si è chiuso (controlla che sia installato correttamente)."
            return {"html": f'<div style="color:#e53935;font:13px sans-serif">⚠ {self.err}</div>'}
        try:
            data = self.proc.stdout.read()
            if data:
                self.buf += data
                *lines, self.buf = self.buf.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith(b"{"):
                        try:
                            self._add(json.loads(line.decode("utf-8", "ignore")))
                        except ValueError:
                            pass
        except (BlockingIOError, OSError):
            pass
        return {"html": self._html()}

    def _add(self, d: dict) -> None:
        # mostra TUTTE le misure decodificate (temp, umidità, vento, direzione, pioggia, UV, luce,
        # pressione, batteria…), non più una lista fissa: così si vede tutto ciò che il sensore manda.
        # Si saltano solo le chiavi di servizio (modello, id, ora, qualità del segnale, ecc.).
        vals = []
        for k, v in d.items():
            if k in _SKIP or k.startswith("_"):
                continue
            lab = _LABELS.get(k) or k.replace("_", " ")
            vals.append(f"{lab}={v}")
        self.rows.insert(0, {
            "t": time.strftime("%H:%M:%S"),
            "model": str(d.get("model", "?")),
            "id": str(d.get("id", d.get("channel", ""))),
            "vals": " · ".join(vals) or (d.get("data") or ""),
        })
        del self.rows[20:]

    def _html(self) -> str:
        head = ('<div style="font:13px sans-serif;color:#ddd">'
                f'<b>Sensori ISM</b> su {self.freq_hz / 1e6:.3f} MHz · {len(self.rows)} messaggi')
        if not self.rows:
            return head + '<div style="color:#888;margin-top:6px">In ascolto… (sensori 433/868/315 MHz)</div></div>'
        rows = "".join(
            f'<tr><td style="color:#888;padding-right:8px">{r["t"]}</td>'
            f'<td style="padding-right:8px"><b>{_esc(r["model"])}</b>{" #" + _esc(r["id"]) if r["id"] else ""}</td>'
            f'<td>{_esc(r["vals"])}</td></tr>'
            for r in self.rows)
        return head + f'<table style="margin-top:6px;border-collapse:collapse;width:100%">{rows}</table></div>'

    def __del__(self) -> None:
        if getattr(self, "proc", None):
            try:
                self.proc.terminate()
            except OSError:
                pass


def _esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
