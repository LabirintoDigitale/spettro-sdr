"""Analisi di un singolo segnale a partire da campioni IQ registrati a frequenza fissa.

Come scanner.py non dipende da Home Assistant: riceve i byte grezzi di rtl_tcp
e restituisce le misure (frequenza, banda, potenza, comportamento nel tempo)
più un'immagine PNG della cascata ad alta risoluzione attorno al segnale.
"""

from __future__ import annotations

import struct
from typing import Any
import zlib

import numpy as np

FFT_SIZE = 2048  # ~1 kHz di risoluzione e ~1 ms per riga a 2 MS/s
USABLE_FRACTION = 0.8  # i bordi della banda sono attenuati dal filtro della chiavetta
IMG_ROWS = 400
IMG_COLS = 512
ACTIVE_DB = 6.0  # un istante conta come "trasmissione" se supera il rumore di questi dB
JOIN_HZ = 60e3  # buchi più stretti di così restano nello stesso segnale (es. i due toni di un FSK)

# piano delle bande (MHz) usato per dare un contesto all'AI e al pannello
BANDS: list[tuple[float, float, str]] = [
    (26.965, 27.405, "CB 27 MHz (banda cittadina)"),
    (28.0, 29.7, "Radioamatori 10 m"),
    (50.0, 51.0, "Radioamatori 6 m"),
    (87.5, 108.0, "Radio FM broadcast (WFM ~200 kHz, RDS)"),
    (108.0, 118.0, "Radionavigazione aeronautica (VOR/ILS)"),
    (118.0, 137.0, "Banda aerea VHF (voce AM, canali 8,33/25 kHz; ACARS ~131,5 MHz)"),
    (137.0, 138.0, "Satelliti meteo NOAA/Meteor (APT/LRPT)"),
    (144.0, 146.0, "Radioamatori 2 m (FM, APRS 144,800 MHz, ponti)"),
    (156.0, 162.05, "VHF marino (FM; AIS 161,975/162,025 MHz)"),
    (169.4, 169.475, "Wireless M-Bus / contatori (169 MHz)"),
    (174.0, 230.0, "DAB+ radio digitale (blocchi OFDM ~1,5 MHz)"),
    (380.0, 400.0, "TETRA servizi di emergenza (cifrato)"),
    (430.0, 440.0, "Radioamatori 70 cm"),
    (433.05, 434.79, "ISM/LPD 433 MHz (telecomandi, sensori meteo, OOK/FSK)"),
    (446.0, 446.2, "PMR446 (walkie-talkie FM/digitale)"),
    (470.0, 694.0, "TV digitale terrestre DVB-T/T2 (canali OFDM 8 MHz)"),
    (791.0, 821.0, "LTE 800 downlink (cellulare)"),
    (832.0, 862.0, "LTE 800 uplink (cellulare)"),
    (863.0, 870.0, "ISM/SRD 868 MHz (LoRa/Meshtastic/MeshCore, sensori, allarmi)"),
    (880.0, 915.0, "GSM/LTE 900 uplink (cellulare)"),
    (925.0, 960.0, "GSM/LTE 900 downlink (celle, GSM 200 kHz)"),
    (1030.0, 1030.0, "Interrogazioni radar secondario (SSR)"),
    (1088.0, 1092.0, "ADS-B / Mode S aerei (1090 MHz, impulsi PPM)"),
    (1164.0, 1300.0, "GNSS L5/E6 e radar"),
    (1525.0, 1559.0, "Satelliti Inmarsat (L-band)"),
    (1559.0, 1610.0, "GNSS L1 (GPS/Galileo, sotto il rumore)"),
    (1710.0, 1785.0, "LTE/GSM 1800 uplink"),
]


def bands_for(freq_mhz: float) -> list[str]:
    """Bande che contengono la frequenza (con 0,5 MHz di tolleranza)."""
    return [name for lo, hi, name in BANDS if lo - 0.5 <= freq_mhz <= hi + 0.5]


def _lut() -> np.ndarray:
    """Colormap della cascata: la stessa del pannello (blu notte → ciano → giallo → rosso)."""
    stops = [(0, (8, 8, 32)), (0.3, (20, 50, 160)), (0.55, (30, 180, 200)), (0.78, (245, 210, 40)), (1, (255, 40, 0))]
    t = np.linspace(0, 1, 256)
    lut = np.empty((256, 3), np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(t, [s[0] for s in stops], [s[1][ch] for s in stops])
    return lut


def encode_png(rgb: np.ndarray) -> bytes:
    """PNG RGB minimale (niente Pillow)."""
    h, w, _ = rgb.shape
    raw = np.hstack([np.zeros((h, 1), np.uint8), rgb.reshape(h, w * 3)]).tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Intervalli [inizio, fine) in cui mask è vera."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist(), strict=True))


def analyze_iq(raw: bytes, rate: int, center_hz: float, target_hz: float) -> tuple[dict[str, Any], bytes]:
    """Misure del segnale vicino a target_hz e immagine PNG della cascata."""
    n = FFT_SIZE
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    iq = a[0::2] + 1j * a[1::2]
    iq -= iq.mean()
    frames = len(iq) // n
    if frames < 16:
        raise ValueError("Troppi pochi campioni per l'analisi")
    window = np.hanning(n).astype(np.float32)
    norm = float((127.5 * window.sum()) ** 2)
    spec = np.fft.fftshift(np.fft.fft(iq[: frames * n].reshape(frames, n) * window, axis=1), axes=1)
    lin = (spec.real**2 + spec.imag**2) / norm  # frames x n, potenza lineare
    c = n // 2
    lin[:, c - 2 : c + 3] = (lin[:, c - 4 : c - 3] + lin[:, c + 4 : c + 5]) / 2  # picco DC della chiavetta
    db = 10 * np.log10(lin + 1e-20)

    bin_hz = rate / n
    frame_s = n / rate
    freqs = center_hz + (np.arange(n) - c) * bin_hz
    keep = int(n * USABLE_FRACTION) // 2
    lo_u, hi_u = c - keep, c + keep  # bin utilizzabili

    mean_db = 10 * np.log10(lin.mean(axis=0) + 1e-20)
    # "quasi massimo" nel tempo: vede anche i segnali a impulsi, che nella media spariscono
    hold_db = np.percentile(db, 98, axis=0)
    floor = float(np.median(hold_db[lo_u:hi_u]))
    floor_mean = float(np.median(mean_db[lo_u:hi_u]))

    # picco più forte vicino alla frequenza scelta (entro 2% della banda o 25 kHz)
    t_bin = int(round((target_hz - center_hz) / bin_hz)) + c
    near = max(3, int(max(25e3, rate * 0.02) / bin_hz))
    a0, a1 = max(lo_u, t_bin - near), min(hi_u, t_bin + near + 1)
    pk = a0 + int(np.argmax(hold_db[a0:a1]))
    peak_db = float(hold_db[pk])
    snr = peak_db - floor

    # estensione del segnale: finché resta sopra il livello, scavalcando buchi fino a `gap` bin
    def edge(start: int, step: int, level: float, gap: int) -> int:
        i, miss, last = start, 0, start
        while lo_u <= i + step < hi_u:
            i += step
            if hold_db[i] > level:
                last, miss = i, 0
            else:
                miss += 1
                if miss > gap:
                    break
        return last

    detected = snr >= ACTIVE_DB
    if detected:
        join = max(2, int(JOIN_HZ / bin_hz))
        b_lo, b_hi = edge(pk, -1, floor + ACTIVE_DB, join), edge(pk, 1, floor + ACTIVE_DB, join)
        bw3_lo, bw3_hi = edge(pk, -1, peak_db - 3, 2), edge(pk, 1, peak_db - 3, 2)
    else:
        b_lo = b_hi = bw3_lo = bw3_hi = pk
    band = slice(b_lo, b_hi + 1)

    # banda occupata al 99% e centroide, sulla potenza media meno il rumore
    excess = np.clip(10 ** (mean_db[band] / 10) - 10 ** (floor_mean / 10), 0, None)
    tot = float(excess.sum())
    if tot > 0:
        cum = np.cumsum(excess) / tot
        i_lo = int(np.searchsorted(cum, 0.005))
        i_hi = int(np.searchsorted(cum, 0.995))
        obw = (i_hi - i_lo + 1) * bin_hz
        centroid = float((freqs[band] * excess).sum() / tot)
    else:
        obw, centroid = bin_hz, float(freqs[pk])

    # comportamento nel tempo: potenza nella banda del segnale, riga per riga
    nb = b_hi - b_lo + 1
    band_lin = lin[:, band].sum(axis=1)
    pw = 10 * np.log10(band_lin + 1e-20)
    noise_pw = floor_mean + 10 * np.log10(nb)
    # media su 3 righe: su pochi bin il rumore da solo supera spesso la soglia per un istante
    smooth = 10 * np.log10(np.convolve(band_lin, np.ones(3) / 3, mode="same") + 1e-20)
    active = smooth > noise_pw + ACTIVE_DB if detected else np.zeros(frames, bool)
    for s, e in _runs(active):
        if e - s < 2:
            active[s:e] = False
    runs = _runs(active)
    # unisce interruzioni brevissime (1–2 righe) dentro lo stesso burst
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] <= 2:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    durations = [(e - s) * frame_s * 1000 for s, e in merged]
    starts = [s for s, _ in merged]
    period = float(np.median(np.diff(starts)) * frame_s * 1000) if len(starts) >= 3 else None
    duty = float(active.mean())

    # andamento della frequenza durante la trasmissione: deriva, salti o chirp
    track: dict[str, Any] = {}
    act_idx = np.nonzero(active)[0]
    if detected and len(act_idx) >= 8:
        inst = freqs[b_lo + np.argmax(db[act_idx, band], axis=1)]
        steps = np.diff(inst)[np.diff(act_idx) == 1]
        moving = steps[np.abs(steps) > bin_hz * 1.5]
        track["peak_freq_std_khz"] = round(float(inst.std()) / 1e3, 2)
        if len(steps):
            track["frac_frames_freq_moving"] = round(len(moving) / len(steps), 2)
        if len(moving) >= 4:
            up = float((moving > 0).mean())
            track["frac_moves_upward"] = round(up, 2)
            track["median_step_khz_per_ms"] = round(float(np.median(np.abs(moving))) / 1e3 / (frame_s * 1000), 2)
    # toni distinti nello spettro medio (es. due toni = FSK)
    tones = 0
    if detected and nb >= 3:
        seg = mean_db[band]
        sep = max(2, nb // 20)
        idx = [i for i in range(1, nb - 1) if seg[i] >= seg[i - 1] and seg[i] > seg[i + 1] and seg[i] > floor_mean + 10]
        idx.sort(key=lambda i: -seg[i])
        chosen: list[int] = []
        for i in idx:
            if all(abs(i - j) > sep for j in chosen):
                chosen.append(i)
        tones = len(chosen)

    # immagine: finestra attorno al segnale (6 volte la sua estensione, minimo 25 kHz)
    ext = (b_hi - b_lo + 1) * bin_hz
    half_hz = min(keep * bin_hz, max(12.5e3, 3 * ext))
    mid = (b_lo + b_hi) // 2
    i0, i1 = max(lo_u, mid - int(half_hz / bin_hz)), min(hi_u, mid + int(half_hz / bin_hz) + 1)
    crop = db[:, i0:i1]
    rows = min(IMG_ROWS, frames)
    per = frames // rows
    crop = crop[: rows * per].reshape(rows, per, -1).max(axis=1)
    # larghezza fissa: i segnali stretti vengono allargati (pixel ripetuti), quelli larghi ridotti
    crop = crop[:, np.linspace(0, crop.shape[1] - 1, IMG_COLS).round().astype(int)]
    vmin, vmax = floor - 3, max(float(crop.max()), floor + 20)
    idx8 = np.clip((crop - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)
    png = encode_png(_lut()[idx8])

    duration_s = frames * frame_s
    freq_mhz = centroid / 1e6
    result: dict[str, Any] = {
        "detected": bool(detected),
        "target_mhz": round(target_hz / 1e6, 4),
        "center_mhz": round(freq_mhz, 5),
        "peak_mhz": round(float(freqs[pk]) / 1e6, 5),
        "peak_db": round(peak_db, 1),
        "floor_db": round(floor, 1),
        "snr_db": round(snr, 1),
        "bw_3db_khz": round((bw3_hi - bw3_lo + 1) * bin_hz / 1e3, 1),
        "bw_6db_above_noise_khz": round(ext / 1e3, 1),
        "obw_99_khz": round(obw / 1e3, 1),
        "tones": tones,
        "duty_cycle": round(duty, 3),
        "continuous": duty > 0.97,
        "bursts": len(merged),
        "burst_ms_min": round(min(durations), 1) if durations else None,
        "burst_ms_median": round(float(np.median(durations)), 1) if durations else None,
        "burst_ms_max": round(max(durations), 1) if durations else None,
        "burst_period_ms": round(period, 1) if period else None,
        "freq_track": track,
        "bands": bands_for(freq_mhz),
        "sample_rate": rate,
        "resolution_hz": round(bin_hz, 1),
        "time_resolution_ms": round(per * frame_s * 1000, 2),
        "duration_s": round(duration_s, 2),
        "image": {
            "f_lo_mhz": round(float(freqs[i0]) / 1e6, 5),
            "f_hi_mhz": round(float(freqs[i1 - 1]) / 1e6, 5),
            "db_min": round(vmin, 1),
            "db_max": round(vmax, 1),
            "width": int(idx8.shape[1]),
            "height": int(idx8.shape[0]),
        },
        # potenza nella banda del segnale nel tempo (per il grafico nel pannello)
        "power_db": np.round(
            pw[: rows * per].reshape(rows, per).max(axis=1), 1
        ).tolist(),
        "noise_power_db": round(noise_pw, 1),
    }
    return result, png


def describe(result: dict[str, Any]) -> str:
    """Riassunto testuale delle misure, per il prompt dell'AI."""
    r = result
    im = r["image"]
    lines = [
        f"Frequenza scelta: {r['target_mhz']} MHz; centro misurato {r['center_mhz']} MHz, picco {r['peak_mhz']} MHz.",
        f"Potenza picco {r['peak_db']} dBFS, rumore {r['floor_db']} dBFS, SNR {r['snr_db']} dB"
        + ("" if r["detected"] else " (nessun segnale chiaro sopra il rumore)") + ".",
        f"Banda: -3 dB {r['bw_3db_khz']} kHz, sopra il rumore +6 dB {r['bw_6db_above_noise_khz']} kHz, "
        f"occupata al 99% {r['obw_99_khz']} kHz. Toni distinti nello spettro medio: {r['tones']}.",
        f"Nel tempo ({r['duration_s']} s registrati): duty cycle {round(r['duty_cycle'] * 100)}%"
        + (", trasmissione continua" if r["continuous"] else f", {r['bursts']} burst")
        + (f", durata burst min/mediana/max {r['burst_ms_min']}/{r['burst_ms_median']}/{r['burst_ms_max']} ms"
           if r["bursts"] and not r["continuous"] else "")
        + (f", periodo tipico tra burst {r['burst_period_ms']} ms" if r["burst_period_ms"] else "") + ".",
    ]
    if r["freq_track"]:
        lines.append(f"Andamento della frequenza di picco durante la trasmissione: {r['freq_track']}.")
    lines.append("Bande note che contengono la frequenza: " + ("; ".join(r["bands"]) or "nessuna in tabella") + ".")
    lines.append(
        f"Ricevitore RTL-SDR, {r['sample_rate'] / 1e6:g} MS/s, risoluzione {r['resolution_hz']} Hz "
        f"e {r['time_resolution_ms']} ms per riga."
    )
    lines.append(
        f"Immagine allegata (se presente): cascata {im['width']}x{im['height']} px; asse orizzontale frequenza da "
        f"{im['f_lo_mhz']} MHz (sinistra) a {im['f_hi_mhz']} MHz (destra); asse verticale tempo dall'alto (0 s) "
        f"in basso ({r['duration_s']} s); colori da blu ({im['db_min']} dBFS) a ciano, giallo e rosso ({im['db_max']} dBFS)."
    )
    return "\n".join(lines)
