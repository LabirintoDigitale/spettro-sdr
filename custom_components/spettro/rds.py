"""Decodifica RDS (Radio Data System) dal multiplex di una stazione FM broadcast.

Il multiplex (l'uscita del discriminatore FM) contiene l'audio mono fino a
15 kHz, il pilota stereo a 19 kHz, la differenza L-R a 23-53 kHz e la
sottoportante RDS a 57 kHz: dati BPSK a 1187,5 bit/s, codificati in modo
differenziale e bifase, raggruppati in blocchi da 26 bit (16 di dati e 10 di
controllo) e gruppi da 4 blocchi.

Solo numpy: si lavora su tutta la registrazione insieme, non in tempo reale.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

import numpy as np

SUBCARRIER_HZ = 57_000.0
BIT_RATE = 1187.5  # 57 kHz / 48
RDS_BW_HZ = 2_400.0  # metà banda occupata dalla sottoportante
BASEBAND_RATE = 19_000.0  # 16 campioni per bit
PILOT_HZ = 19_000.0

POLY = 0x5B9  # g(x) = x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}
BLOCK_INDEX = {"A": 0, "B": 1, "C": 2, "C'": 2, "D": 3}

PTY_NAMES = [
    "nessuno", "notizie", "attualità", "informazione", "sport", "educazione", "teatro", "cultura",
    "scienza", "varietà", "musica pop", "musica rock", "musica leggera", "classica leggera",
    "musica classica", "altra musica", "meteo", "finanza", "programmi per ragazzi", "temi sociali",
    "religione", "filo diretto", "viaggi", "tempo libero", "jazz", "country", "musica nazionale",
    "musica del passato", "folk", "documentari", "test di allarme", "allarme",
]

# set di caratteri RDS: ASCII più le lettere accentate più comuni (tabella E.1)
CHARSET = {0x80: "á", 0x81: "à", 0x82: "é", 0x83: "è", 0x84: "í", 0x85: "ì", 0x86: "ó", 0x87: "ò",
           0x88: "ú", 0x89: "ù", 0x8A: "Ñ", 0x8B: "Ç", 0x8D: "ß", 0x91: "â", 0x92: "ä", 0x93: "ê",
           0x94: "ë", 0x95: "î", 0x96: "ï", 0x97: "ô", 0x98: "ö", 0x99: "û", 0x9A: "ü", 0x9B: "ñ",
           0x9C: "ç", 0xC0: "Á", 0xC1: "À", 0xC2: "É", 0xC3: "È", 0xC6: "Ó", 0xC7: "Ò", 0xC8: "Ú",
           0xC9: "Ù"}


def crc10(data: int) -> int:
    """Parola di controllo di 10 bit dei 16 bit di dati (senza l'offset del blocco)."""
    reg = data << 10
    for bit in range(25, 9, -1):
        if reg & (1 << bit):
            reg ^= POLY << (bit - 10)
    return reg & 0x3FF


def _crc_table() -> np.ndarray:
    """crc10 di tutti i 65536 valori di dati, calcolato in blocco."""
    reg = np.arange(1 << 16, dtype=np.int64) << 10
    for bit in range(25, 9, -1):
        reg ^= np.where(reg & (1 << bit), POLY << (bit - 10), 0)
    return (reg & 0x3FF).astype(np.int64)


_CRC_TABLE = _crc_table()


def _char(code: int) -> str:
    if 0x20 <= code <= 0x7E:
        return chr(code)
    return CHARSET.get(code, "?")


def _baseband(mpx: np.ndarray, fs: float) -> tuple[np.ndarray, float]:
    """Sottoportante a 57 kHz portata in banda base e filtrata a ±2,4 kHz (circa 16 campioni per bit)."""
    n = len(mpx)
    spec = np.fft.rfft(mpx)
    k0 = int(round(SUBCARRIER_HZ * n / fs))
    m = int(RDS_BW_HZ * n / fs)
    nb = int(round(n * BASEBAND_RATE / fs))
    z = np.zeros(nb, np.complex128)
    z[: m + 1] = spec[k0 : k0 + m + 1]
    z[nb - m :] = spec[k0 - m : k0]
    return np.fft.ifft(z) * (nb / n), nb * fs / n


def _pilot(mpx: np.ndarray, fs: float) -> float:
    """Di quanti dB il pilota a 19 kHz spicca sui dintorni (stereo se > ~15 dB)."""
    spec = np.abs(np.fft.rfft(mpx * np.hanning(len(mpx)))) ** 2
    f = np.fft.rfftfreq(len(mpx), 1 / fs)
    peak = spec[(f > PILOT_HZ - 50) & (f < PILOT_HZ + 50)].max()
    around = np.median(spec[((f > 16_500) & (f < 18_500)) | ((f > 19_500) & (f < 21_500))])
    return float(10 * np.log10(peak / (around + 1e-30)))


def _carrier_lock(z: np.ndarray, fs: float) -> tuple[np.ndarray, float]:
    """Toglie scarto di frequenza e deriva di fase della portante (BPSK: si stima su z²)."""
    n = len(z)
    sq = z**2
    spec = np.abs(np.fft.fft(sq))
    f = np.fft.fftfreq(n, 1 / fs)
    near = np.abs(f) < 100  # 2 × scarto massimo atteso
    df = float(f[near][np.argmax(spec[near])]) / 2
    t = np.arange(n) / fs
    z = z * np.exp(-2j * np.pi * df * t)
    # fase seguita a finestre di 20 ms: angolo di z² diviso 2 (resta un'ambiguità di 180°,
    # che la codifica differenziale rende innocua)
    w = max(1, int(fs * 0.02))
    k = n // w
    if k < 2:
        return z * np.exp(-1j * np.angle(np.sum(z**2)) / 2), df
    theta = np.unwrap(np.angle((z[: k * w] ** 2).reshape(k, w).sum(axis=1))) / 2
    centers = (np.arange(k) + 0.5) * w
    phase = np.interp(np.arange(n), centers, theta)
    return z * np.exp(-1j * phase), df


def _bits(r: np.ndarray, fs: float, bit_rate: float) -> np.ndarray:
    """Bit dalla codifica bifase: prima metà del bit meno seconda metà, con la migliore fase di clock."""
    period = fs / bit_rate
    csum = np.concatenate(([0.0], np.cumsum(r)))
    idx = np.arange(len(csum))
    nbits = int((len(r) - period) / period) - 1
    if nbits < 26:
        return np.empty(0, np.int8)
    n = np.arange(nbits)

    def values(t0: float) -> np.ndarray:
        start = t0 + n * period
        c0 = np.interp(start, idx, csum)
        c1 = np.interp(start + period / 2, idx, csum)
        c2 = np.interp(start + period, idx, csum)
        return (c1 - c0) - (c2 - c1)

    best = max(np.linspace(0, period, 32, endpoint=False), key=lambda t0: float(np.sum(values(t0) ** 2)))
    raw = (values(best) > 0).astype(np.int8)
    return raw[1:] ^ raw[:-1]  # decodifica differenziale


def _blocks(bits: np.ndarray) -> list[tuple[int, str, int]]:
    """Blocchi validi (posizione in bit, tipo A/B/C/C'/D, 16 bit di dati)."""
    n = len(bits) - 25
    if n <= 0:
        return []
    weights = 1 << np.arange(25, -1, -1, dtype=np.int64)
    windows = np.lib.stride_tricks.sliding_window_view(bits.astype(np.int64), 26)
    words = windows @ weights
    data = (words >> 10).astype(np.int64)
    check = (words & 0x3FF).astype(np.int64)
    syndrome = check ^ _CRC_TABLE[data]
    found = []
    for name, off in OFFSETS.items():
        for pos in np.nonzero(syndrome == off)[0]:
            found.append((int(pos), name, int(data[pos])))
    found.sort()
    return found


def _groups(blocks: list[tuple[int, str, int]]) -> list[dict[str, int]]:
    """Blocchi raccolti per gruppo (4 blocchi consecutivi da 26 bit).

    Un blocco valido per caso capita (1 posizione su 1024 per tipo), due allineati quasi mai:
    si tengono i gruppi con il blocco B e almeno un altro blocco.
    """
    by_start: dict[int, dict[str, int]] = {}
    for pos, name, data in blocks:
        start = pos - 26 * BLOCK_INDEX[name]
        by_start.setdefault(start, {})[name] = data
    return [g for _, g in sorted(by_start.items()) if "B" in g and len(g) >= 2]


MISSING = "·"  # carattere non ricevuto


def _vote(votes: list[Counter], length: int) -> str:
    """Carattere più ricevuto in ogni posizione (le ripetizioni correggono gli errori isolati)."""
    return "".join(votes[i].most_common(1)[0][0] if votes[i] else MISSING for i in range(length))


def decode(mpx: np.ndarray, fs: float) -> dict[str, Any] | None:
    """Informazioni RDS trovate nel multiplex, None se non c'è RDS decodificabile."""
    if len(mpx) < fs * 0.3:
        return None
    pilot_db = _pilot(mpx, fs)
    z, fs_b = _baseband(mpx, fs)
    z, df = _carrier_lock(z, fs_b)
    # stesso errore relativo per portante e clock dei bit (entrambi derivati dal pilota della stazione)
    bits = _bits(z.real, fs_b, BIT_RATE * (1 + df / SUBCARRIER_HZ))
    groups = _groups(_blocks(bits))
    if not groups:
        return None

    pi = Counter()
    pty = Counter()
    tp = Counter()
    ta = Counter()
    ps = [Counter() for _ in range(8)]
    rt = {0: [Counter() for _ in range(64)], 1: [Counter() for _ in range(64)]}
    rt_seen = Counter()
    rt_end: dict[int, Counter] = {0: Counter(), 1: Counter()}
    af: set[float] = set()
    ct = None
    for g in groups:
        b = g["B"]
        gtype, version = b >> 12, (b >> 11) & 1
        tp[bool(b & 0x400)] += 1
        pty[(b >> 5) & 0x1F] += 1
        if "A" in g:
            pi[g["A"]] += 1
        if "C'" in g:
            pi[g["C'"]] += 1
        if gtype == 0:
            ta[bool(b & 0x10)] += 1
            seg = b & 0x3
            if "D" in g:
                d = g["D"]
                ps[2 * seg][_char(d >> 8)] += 1
                ps[2 * seg + 1][_char(d & 0xFF)] += 1
            if version == 0 and "C" in g:
                for code in (g["C"] >> 8, g["C"] & 0xFF):
                    if 1 <= code <= 204:
                        af.add(round(87.5 + code * 0.1, 1))
        elif gtype == 2:
            flag, seg = (b >> 4) & 1, b & 0xF
            rt_seen[flag] += 1
            chars = []
            if version == 0:
                base = 4 * seg
                if "C" in g:
                    chars += [(base, g["C"] >> 8), (base + 1, g["C"] & 0xFF)]
                if "D" in g:
                    chars += [(base + 2, g["D"] >> 8), (base + 3, g["D"] & 0xFF)]
            elif "D" in g:
                base = 2 * seg
                chars += [(base, g["D"] >> 8), (base + 1, g["D"] & 0xFF)]
            for pos, code in chars:
                if code == 0x0D:
                    rt_end[flag][pos] += 1
                elif pos < 64:
                    rt[flag][pos][_char(code)] += 1
        elif gtype == 4 and version == 0 and "C" in g and "D" in g:
            c, d = g["C"], g["D"]
            mjd = ((b & 0x3) << 15) | (c >> 1)
            hour, minute = ((c & 1) << 4) | (d >> 12), (d >> 6) & 0x3F
            offset = (d & 0x1F) * 0.5 * (-1 if d & 0x20 else 1)
            if 15_000 < mjd < 100_000 and hour < 24 and minute < 60:
                day = date(1858, 11, 17) + timedelta(days=mjd)
                total = hour * 60 + minute + int(offset * 60)
                local_day = day + timedelta(days=total // 1440)
                total %= 1440
                ct = f"{local_day.isoformat()} {total // 60:02d}:{total % 60:02d} (UTC{offset:+g})"

    ps_text = _vote(ps, 8)
    rt_text = ""
    if rt_seen:
        flag = rt_seen.most_common(1)[0][0]
        end = min(rt_end[flag], default=64) if rt_end[flag] else 64
        rt_text = _vote(rt[flag], end).rstrip(" " + MISSING)
    pi_code = pi.most_common(1)[0][0] if pi else None
    pty_code = pty.most_common(1)[0][0] if pty else 0
    return {
        "pi": f"{pi_code:04X}" if pi_code is not None else None,
        "ps": ps_text.strip() if ps_text.strip(MISSING + " ") else None,
        "ps_complete": all(ps),
        "rt": rt_text or None,
        "pty": PTY_NAMES[pty_code],
        "tp": tp.most_common(1)[0][0] if tp else False,
        "ta": ta.most_common(1)[0][0] if ta else False,
        "af": sorted(af),
        "ct": ct,
        "groups": len(groups),
        "stereo": pilot_db > 15,
    }
