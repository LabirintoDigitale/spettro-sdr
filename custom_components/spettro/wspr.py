"""WSPR: i radiofari dei radioamatori (nominativo, locatore, potenza), dall'audio USB.

Ogni due minuti pari, un secondo dopo l'inizio, le stazioni trasmettono per 110,6 s 162
simboli 4-FSK da 0,683 s (toni distanti 1,46 Hz) in una banda di 200 Hz intorno a 1500 Hz
di audio. Ogni simbolo porta un bit di sincronismo (vettore fisso) e un bit dati: 50 bit
di messaggio con un codice convoluzionale K=32, r=1/2, interlacciati.

Il vettore di sincronismo, i polinomi del codice e l'impacchettamento del messaggio sono
quelli del protocollo (WSJT-X, K1JT e K9AN).
"""

from __future__ import annotations

import heapq
import math
from typing import Any

import numpy as np

from .ft8 import distance_km, grid_latlon

FS_IN = 8_000  # audio in ingresso
FS = 375  # dopo la conversione: 256 campioni per simbolo
NSPS = 256
DF = FS / NSPS  # 1,4648 Hz tra i toni
SLOT_S = 120.0
START_S = 1.0
PRE_S = 1.5  # audio preso prima dell'inizio del periodo
WINDOW_S = PRE_S + 116.0
CENTER_HZ = 1500.0  # centro della sottobanda WSPR nell'audio
HALF_BAND_HZ = 110.0
HOP = 32  # passo della cascata: 1/8 di simbolo
POLY = (0xF2D05351, 0xE4613C47)
SYNC = np.array([int(c) for c in (
    "110000001000111000100101111000000010010100000010110011010001"
    "101000011010101010010010110001101010001000001001001110110011"
    "010001110000010100110000000110101100011000"
)])
POWERS = {p for p in range(61) if p % 10 in (0, 3, 7)}
MAX_CANDIDATES = 24
MAX_NODES = 60_000  # limite della decodifica sequenziale per candidato
SNR_FIX_DB = 3.5  # la finestra e la stima del rumore sottostimano il segnale (tarato con segnali di prova)

# ordine dei bit dopo l'interlacciamento: indici a 8 bit letti al contrario, tenuti se < 162
_PERM = [j for j in (int(f"{i:08b}"[::-1], 2) for i in range(256)) if j < 162]


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def encode_symbols(n_call: int, n_grid_pwr: int) -> np.ndarray:
    """28 + 22 bit → 162 simboli (0…3). Serve per le prove."""
    bits = [(n_call >> (27 - k)) & 1 for k in range(28)] + [(n_grid_pwr >> (21 - k)) & 1 for k in range(22)] + [0] * 31
    state, ch = 0, []
    for b in bits:
        state = ((state << 1) | b) & 0xFFFFFFFF
        ch += [_parity(state & POLY[0]), _parity(state & POLY[1])]
    inter = np.zeros(162, int)
    for p, j in enumerate(_PERM):
        inter[j] = ch[p]
    return SYNC + 2 * inter


def pack_call(call: str) -> int:
    c = call.upper()
    if len(c) > 2 and not c[2].isdigit() and c[1].isdigit():
        c = " " + c
    c = c.ljust(6)
    a36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    n = a36.index(c[0])
    n = n * 36 + a36.index(c[1])
    n = n * 10 + a36.index(c[2])
    for ch in c[3:6]:
        n = n * 27 + a36.index(ch) - 10
    return n


def pack_grid_power(grid: str, power: int) -> int:
    g = [ord(c) - 65 if c.isalpha() else ord(c) - 48 for c in grid.upper()]
    m = (179 - 10 * g[0] - g[2]) * 180 + 10 * g[1] + g[3]
    return m * 128 + power + 64


def nhash(s: str) -> int:
    """Hash a 15 bit del nominativo (lookup3 di Bob Jenkins, come WSJT-X)."""
    def rot(x: int, k: int) -> int:
        return ((x << k) | (x >> (32 - k))) & 0xFFFFFFFF

    data = s.encode()
    n = len(data)
    a = b = c = (0xDEADBEEF + n + 146) & 0xFFFFFFFF
    m = 0xFFFFFFFF
    while n > 12:
        a = (a + int.from_bytes(data[0:4], "little")) & m
        b = (b + int.from_bytes(data[4:8], "little")) & m
        c = (c + int.from_bytes(data[8:12], "little")) & m
        a = (a - c) & m; a ^= rot(c, 4); c = (c + b) & m  # noqa: E702
        b = (b - a) & m; b ^= rot(a, 6); a = (a + c) & m  # noqa: E702
        c = (c - b) & m; c ^= rot(b, 8); b = (b + a) & m  # noqa: E702
        a = (a - c) & m; a ^= rot(c, 16); c = (c + b) & m  # noqa: E702
        b = (b - a) & m; b ^= rot(a, 19); a = (a + c) & m  # noqa: E702
        c = (c - b) & m; c ^= rot(b, 4); b = (b + a) & m  # noqa: E702
        data, n = data[12:], n - 12
    if n == 0:
        return c & 32767
    tail = data.ljust(12, b"\0")
    a = (a + int.from_bytes(tail[0:4], "little")) & m
    b = (b + int.from_bytes(tail[4:8], "little")) & m
    c = (c + int.from_bytes(tail[8:12], "little")) & m
    c ^= b; c = (c - rot(b, 14)) & m  # noqa: E702
    a ^= c; a = (a - rot(c, 11)) & m  # noqa: E702
    b ^= a; b = (b - rot(a, 25)) & m  # noqa: E702
    c ^= b; c = (c - rot(b, 16)) & m  # noqa: E702
    a ^= c; a = (a - rot(c, 4)) & m  # noqa: E702
    b ^= a; b = (b - rot(a, 14)) & m  # noqa: E702
    c ^= b; c = (c - rot(b, 24)) & m  # noqa: E702
    return c & 32767


def _unpack_call(n: int) -> str | None:
    if n >= 262177560:
        return None
    a = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    c = [""] * 6
    for k in range(3):
        c[5 - k] = a[n % 27 + 10]
        n //= 27
    c[2] = a[n % 10]
    n //= 10
    c[1] = a[n % 36]
    n //= 36
    if n > 36:
        return None
    c[0] = a[n]
    return "".join(c).strip()


def _unpack_grid(ngrid: int) -> str | None:
    ngrid >>= 7
    if ngrid >= 32400:
        return None
    dlat = ngrid % 180 - 90
    dlong = (ngrid // 180) * 2 - 180 + 2
    nlong = int(60.0 * (180.0 - dlong) / 5.0)
    nlat = int(60.0 * (dlat + 90) / 2.5)
    return chr(65 + nlong // 240) + chr(65 + nlat // 240) + str(nlong % 240 // 24) + str(nlat % 240 // 24)


def _valid_call(call: str) -> bool:
    # nominativo plausibile: lettere e cifre, con una cifra nella 2ª o 3ª posizione
    return 3 <= len(call) <= 6 and call.isalnum() and (call[1].isdigit() or (len(call) > 2 and call[2].isdigit()))


def unpack(n1: int, n2: int, hashes: dict[int, str]) -> str | None:
    """Messaggio di tipo 1 (nominativo, locatore, potenza), 2 (nominativo con prefisso) o 3 (locatore a 6)."""
    call = _unpack_call(n1)
    if call is None:
        return None
    ntype = (n2 & 127) - 64
    if 0 <= ntype <= 62 and ntype in POWERS:
        grid = _unpack_grid(n2)
        if grid is None or not _valid_call(call):
            return None
        hashes[nhash(call)] = call
        return f"{call} {grid} {ntype}"
    if 0 <= ntype <= 62:
        nu = ntype % 10
        nadd = nu - 3 if nu > 3 else nu
        nadd = nu - 7 if nu > 7 else nadd
        n3 = n2 // 128 + 32768 * (nadd - 1)
        pwr = ntype - nadd
        if pwr not in POWERS or not _valid_call(call):
            return None
        if n3 < 60000:
            pfx, n = "", n3
            for _ in range(3):
                nc = n % 37
                pfx = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "[nc]) + pfx
                n //= 37
            full = f"{pfx.strip()}/{call}"
        else:
            nc = n3 - 60000
            if nc <= 35:
                full = f"{call}/{'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[nc]}"
            elif nc <= 125:
                full = f"{call}/{(nc - 26) // 10}{(nc - 26) % 10}"
            else:
                return None
        hashes[nhash(full)] = full
        return f"{full} {pwr}"
    if ntype < 0:
        pwr = -(ntype + 1)
        grid6 = call[-1:] + call[:-1] if len(call) == 6 else ""
        if pwr not in POWERS or len(grid6) != 6 or not (grid6[:2].isalpha() and grid6[2:4].isdigit()):
            return None
        ihash = (n2 - ntype - 64) // 128
        return f"<{hashes.get(ihash, '...')}> {grid6} {pwr}"
    return None


def _baseband(x: np.ndarray) -> np.ndarray:
    """Audio a 8 kHz → segnale complesso a 375 Hz centrato su CENTER_HZ."""
    n = len(x)
    spec = np.fft.fft(np.asarray(x, np.float64))
    f = np.fft.fftfreq(n, 1 / FS_IN)
    n_out = int(n * FS / FS_IN)
    keep = np.abs(f - CENTER_HZ) < FS / 2
    out = np.zeros(n_out, np.complex128)
    k = np.round((f[keep] - CENTER_HZ) * n_out / FS).astype(int) % n_out
    out[k] = spec[keep]
    return np.fft.ifft(out) * (n_out / n)


def _fano_metric(llr: np.ndarray) -> np.ndarray:
    """Per ogni bit del canale il guadagno (in bit) se vale 0 o 1: log2(2·P) − 1/2."""
    p0 = 1 / (1 + np.exp(-np.clip(llr, -30, 30)))
    m = np.stack([np.log2(np.maximum(2 * p0, 1e-9)), np.log2(np.maximum(2 * (1 - p0), 1e-9))], axis=1) - 0.5
    return m  # (162, 2)


def sequential_decode(llr: np.ndarray, max_nodes: int = MAX_NODES) -> tuple[list[int], float] | None:
    """Decodifica a pila (stack algorithm) del codice K=32: 50 bit + 31 zeri di coda."""
    met = _fano_metric(llr)
    heap: list[tuple[float, int, int, tuple[int, ...]]] = [(0.0, 0, 0, ())]
    nodes = 0
    while heap:
        neg, depth, state, bits = heapq.heappop(heap)
        if depth == 81:
            return list(bits[:50]), -neg
        nodes += 1
        if nodes > max_nodes:
            return None
        for b in ((0, 1) if depth < 50 else (0,)):
            s = ((state << 1) | b) & 0xFFFFFFFF
            c0, c1 = _parity(s & POLY[0]), _parity(s & POLY[1])
            gain = met[2 * depth, c0] + met[2 * depth + 1, c1]
            heapq.heappush(heap, (neg - gain, depth + 1, s, (*bits, b)))
    return None


def decode(x: np.ndarray, hashes: dict[int, str] | None = None,
           home: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Messaggi WSPR in un periodo di 2 minuti. x: audio a 8 kHz che inizia PRE_S prima del periodo."""
    hashes = {} if hashes is None else hashes
    z = _baseband(x)
    # cascata: finestre di un simbolo ogni 1/8 di simbolo, righe da mezzo tono
    n = (len(z) - NSPS) // HOP + 1
    if n < 162 * 8:
        return []
    idx = np.arange(NSPS)[None, :] + HOP * np.arange(n)[:, None]
    sp = np.fft.fftshift(np.abs(np.fft.fft(z[idx], 2 * NSPS, axis=1)) ** 2, axes=1)  # (n, 512), 0,73 Hz
    fbin = (np.arange(2 * NSPS) - NSPS) * DF / 2
    band = np.abs(fbin) <= HALF_BAND_HZ
    avg = sp.mean(axis=0)
    noise = float(np.percentile(avg[band], 30)) + 1e-30
    # candidati: i 4 toni (8 righe) sommati, massimi locali sopra il rumore
    sm = np.convolve(avg, np.ones(7), "same")
    snr_c = 10 * np.log10(sm / (7 * noise) + 1e-12)
    cand = [i for i in np.nonzero(band)[0][2:-2]
            if sm[i] >= sm[i - 2: i + 3].max() and snr_c[i] > 0.6]
    cand = sorted(cand, key=lambda i: -sm[i])[:MAX_CANDIDATES]
    amp = np.sqrt(sp)
    out: dict[str, dict[str, Any]] = {}
    s_idx = np.arange(162)
    t_lo = max(0, int((PRE_S + START_S - 2.5) * FS / HOP))
    t_hi = min(n - 8 * 161 - 1, int((PRE_S + START_S + 4.0) * FS / HOP))
    for c in cand:
        best = (-1e9, 0, 0, 0.0)
        for dfb in (-2, -1, 0, 1, 2):
            for drift in (-4, -2, 0, 2, 4):  # righe di mezzo tono su tutta la trasmissione
                f_s = np.round(c + dfb + drift * (s_idx - 81) / 162).astype(int)  # centro del segnale per simbolo
                tone_bins = f_s[:, None] + np.array([-3, -1, 1, 3])[None, :]  # i 4 toni, in righe di mezzo tono
                rows = np.arange(t_lo, t_hi)[:, None] + 8 * s_idx[None, :]  # (T, 162)
                a = amp[rows[:, :, None], tone_bins[None, :, :]]  # (T, 162, 4)
                metric = ((a[..., 1] + a[..., 3]) - (a[..., 0] + a[..., 2])) * (2 * SYNC - 1)
                ss = metric.sum(axis=1) / (a.sum(axis=(1, 2)) + 1e-20)
                k = int(np.argmax(ss))
                if ss[k] > best[0]:
                    best = (float(ss[k]), t_lo + k, dfb, drift)
        sync, t0, dfb, drift = best
        if sync < 0.08:
            continue
        f_s = np.round(c + dfb + drift * (s_idx - 81) / 162).astype(int)
        tone_bins = f_s[:, None] + np.array([-3, -1, 1, 3])[None, :]
        a = amp[t0 + 8 * s_idx[:, None], tone_bins]  # (162, 4)
        # bit dati: toni 2/3 contro 0/1, a parità di bit di sincronismo
        soft = np.where(SYNC == 1, a[:, 1] - a[:, 3], a[:, 0] - a[:, 2])  # positivo = bit 0
        soft = soft / (np.sqrt(np.mean(soft**2)) + 1e-20)
        deint = soft[_PERM]
        res = None
        for scale in (3.0, 5.0):
            res = sequential_decode(deint * scale)
            if res is not None:
                break
        if res is None:
            continue
        bits, _metric = res
        n1 = int("".join(map(str, bits[:28])), 2)
        n2 = int("".join(map(str, bits[28:50])), 2)
        text = unpack(n1, n2, hashes)
        if not text or text in out:
            continue
        # controllo: i simboli ricodificati devono somigliare a quelli ricevuti
        sym = encode_symbols(n1, n2)
        agree = float(np.mean(np.argmax(a, axis=1) == sym))
        if agree < 0.45:
            continue
        sig = float(np.mean(a[s_idx, sym] ** 2)) - noise
        snr = 10 * math.log10(max(sig, noise * 1e-4) / noise) + 10 * math.log10(DF / 2 / 2500) + SNR_FIX_DB
        parts = text.split()
        km = None
        if home and len(parts) == 3:
            ll = grid_latlon(parts[1])
            km = round(distance_km(home, ll)) if ll else None
        out[text] = {
            "message": text, "snr_db": round(max(-35.0, snr)), "dt_s": round(float(t0 * HOP / FS - PRE_S - START_S), 1),
            "freq_hz": round(float(CENTER_HZ + fbin[c] + dfb * DF / 2), 1), "drift_hz": round(drift * DF / 2, 1),
            "call": parts[0], "grid": parts[1] if len(parts) == 3 else None,
            "dbm": int(parts[-1]), "km": km,
        }
    return sorted(out.values(), key=lambda m: m["freq_hz"])
