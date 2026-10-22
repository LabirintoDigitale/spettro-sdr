"""DAB / DAB+ (radio digitale, banda III): canali e lettura del canale di servizio (FIC).

Un multiplex DAB modo I a 2,048 MS/s: trame da 196608 campioni (96 ms) che
iniziano con un simbolo nullo di 2656 campioni, poi 76 simboli OFDM da 2552
(prefisso ciclico 504 + FFT 2048) con 1536 portanti DQPSK. Il primo simbolo è
il riferimento di fase, i tre successivi portano il FIC: 4 blocchi a codice
convoluzionale (K=7, tasso 1/4, puncturing) da cui escono 12 FIB da 256 bit
con i FIG: nome del multiplex (1/0), nomi dei servizi (1/1), servizi e tipo
di audio (0/2).

Solo numpy. Le scelte che un errore di convenzione renderebbe sbagliate
(scarto intero di frequenza, segni, coniugato) si provano tutte e vince
quella che supera il CRC dei FIB.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np

from .rds import CHARSET

RATE = 2_048_000
NULL_LEN = 2656
FFT_LEN = 2048
GUARD = 504
SYM_LEN = FFT_LEN + GUARD
FRAME_LEN = 196_608
CARRIERS = 1536
FIC_SYMBOLS = 3
BLOCK_BITS = 2304  # bit codificati di un blocco FIC
INFO_BITS = 768  # bit utili di un blocco FIC (3 FIB)
WINDOW_BACKOFF = 16  # finestra FFT anticipata dentro il prefisso ciclico

# canali della banda III (MHz)
CHANNELS: dict[str, float] = {
    "5A": 174.928, "5B": 176.640, "5C": 178.352, "5D": 180.064,
    "6A": 181.936, "6B": 183.648, "6C": 185.360, "6D": 187.072,
    "7A": 188.928, "7B": 190.640, "7C": 192.352, "7D": 194.064,
    "8A": 195.936, "8B": 197.648, "8C": 199.360, "8D": 201.072,
    "9A": 202.928, "9B": 204.640, "9C": 206.352, "9D": 208.064,
    "10A": 209.936, "10B": 211.648, "10C": 213.360, "10D": 215.072,
    "11A": 216.928, "11B": 218.640, "11C": 220.352, "11D": 222.064,
    "12A": 223.936, "12B": 225.648, "12C": 227.360, "12D": 229.072,
    "13A": 230.784, "13B": 232.496, "13C": 234.208, "13D": 235.776, "13E": 237.488, "13F": 239.200,
}
HALF_BW_MHZ = 0.768


def channel_for(freq_mhz: float) -> tuple[str, float] | None:
    """Canale DAB che contiene la frequenza (entro la sua metà banda), se c'è."""
    best = min(CHANNELS.items(), key=lambda c: abs(c[1] - freq_mhz))
    return best if abs(best[1] - freq_mhz) <= HALF_BW_MHZ else None


# --- tabelle dello standard (EN 300 401) --------------------------------------


def _freq_interleave() -> np.ndarray:
    """Portante (−768…768, senza 0) su cui va ciascuno dei 1536 simboli QPSK del modo I."""
    a, out = 0, []
    for _ in range(FFT_LEN):
        if 256 <= a <= 1792 and a != 1024:
            out.append(a - 1024)
        a = (13 * a + 511) % FFT_LEN
    return np.array(out[:CARRIERS])


CARRIER_OF = _freq_interleave()

# puncturing: vettori da 32 bit; PI 16 e 15 per il FIC, PI_X per la coda
_PI16 = [1, 1, 1, 0] * 8
_PI15 = [1, 1, 1, 0] * 7 + [1, 1, 0, 0]
_PIX = [1, 1, 0, 0] * 6


def _fic_puncture() -> np.ndarray:
    """Posizioni (nella sequenza madre a tasso 1/4) dei 2304 bit trasmessi di un blocco FIC."""
    mask = _PI16 * 4 * 21 + _PI15 * 4 * 3 + _PIX
    return np.nonzero(np.array(mask, bool))[0]


FIC_KEEP = _fic_puncture()
MOTHER_BITS = (INFO_BITS + 6) * 4

# codice convoluzionale: x0 = a(i)+a(i-2)+a(i-3)+a(i-5)+a(i-6), x1 = a(i)+a(i-1)+a(i-2)+a(i-3)+a(i-6),
# x2 = a(i)+a(i-1)+a(i-4)+a(i-6), x3 = x0  (polinomi ottali 133, 171, 145, 133; bit 6 = a(i))
POLYS = (0o133, 0o171, 0o145, 0o133)


def _parity(v: np.ndarray) -> np.ndarray:
    v = v.copy()
    for shift in (4, 2, 1):
        v ^= v >> shift
    return v & 1


def _trellis() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per ogni stato d'arrivo: i due stati di partenza e i segni attesi dei 4 bit d'uscita."""
    ns = np.arange(64)
    u = ns >> 5
    prev = np.stack([((ns << 1) & 63) | b for b in (0, 1)], axis=1)  # 64 x 2
    reg = (u[:, None] << 6) | prev  # registro a(i)…a(i-6)
    out = np.stack([_parity(reg & p) for p in POLYS], axis=-1)  # 64 x 2 x 4
    return prev, 1 - 2 * out.astype(np.float32), u


_PREV, _SIGNS, _INPUT = _trellis()


def viterbi(soft: np.ndarray) -> np.ndarray:
    """Decodifica a decisione morbida di più blocchi insieme (righe di soft: positivo = bit 0,
    0 = bit mancante); il registro parte e finisce a zero."""
    soft = np.atleast_2d(soft)
    nb, steps = soft.shape[0], soft.shape[1] // 4
    s = soft[:, : steps * 4].reshape(nb, steps, 4).astype(np.float32)
    metric = np.full((nb, 64), -1e9, np.float32)
    metric[:, 0] = 0
    choice = np.empty((steps, nb, 64), np.int8)
    rows = np.arange(nb)[:, None]
    cols = np.arange(64)[None, :]
    for i in range(steps):
        cand = metric[:, _PREV] + np.einsum("sbk,nk->nsb", _SIGNS, s[:, i])  # nb x 64 x 2
        c = np.argmax(cand, axis=2)
        choice[i] = c
        metric = cand[rows, cols, c]
        metric -= metric.max(axis=1, keepdims=True)
    bits = np.empty((nb, steps), np.int8)
    state = np.zeros(nb, np.int64)
    for i in range(steps - 1, -1, -1):
        bits[:, i] = _INPUT[state]
        state = _PREV[state, choice[i, np.arange(nb), state]]
    return bits


def _prbs(n: int) -> np.ndarray:
    """Dispersione energetica: PRBS x^9 + x^5 + 1 con registro iniziale tutto a uno."""
    reg = [1] * 9
    out = np.empty(n, np.int8)
    for i in range(n):
        b = reg[8] ^ reg[4]
        out[i] = b
        reg = [b] + reg[:8]
    return out


PRBS = _prbs(INFO_BITS)


def crc16(data: bytes) -> int:
    """CRC-CCITT (x^16 + x^12 + x^5 + 1) con registro iniziale 0xFFFF; il FIB porta il complemento."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


# --- demodulazione ------------------------------------------------------------


def _frame_starts(x: np.ndarray) -> list[int]:
    """Inizio delle trame: il simbolo nullo è il tratto di 2656 campioni con meno energia."""
    p = np.abs(x) ** 2
    c = np.concatenate(([0.0], np.cumsum(p)))
    if len(x) < FRAME_LEN + NULL_LEN:
        return []
    win = c[NULL_LEN:] - c[:-NULL_LEN]
    first = int(np.argmin(win[:FRAME_LEN]))
    starts = []
    pos = first
    while pos + FRAME_LEN <= len(x) - NULL_LEN:
        lo, hi = max(0, pos - 200), min(len(win), pos + 200)
        pos = lo + int(np.argmin(win[lo:hi]))
        # serve energia vera dopo il nullo: un tratto tutto muto non è una trama DAB
        if win[pos] * 4 < (c[pos + NULL_LEN + SYM_LEN] - c[pos + NULL_LEN]) * NULL_LEN / SYM_LEN:
            starts.append(pos)
        pos += FRAME_LEN
    return starts


def _fine_offset(x: np.ndarray, starts: list[int]) -> float:
    """Scarto fine di frequenza (Hz, ±500) dal prefisso ciclico dei simboli."""
    acc = 0j
    for s in starts[:4]:
        for sym in range(76):
            t = s + NULL_LEN + sym * SYM_LEN
            if t + SYM_LEN > len(x):
                break
            acc += np.vdot(x[t + FFT_LEN : t + SYM_LEN], x[t : t + GUARD])
    return float(-np.angle(acc) * RATE / (2 * np.pi * FFT_LEN))


def _symbols(x: np.ndarray, start: int, count: int) -> np.ndarray:
    idx = start + NULL_LEN + np.arange(count)[:, None] * SYM_LEN + GUARD - WINDOW_BACKOFF + np.arange(FFT_LEN)
    return np.fft.fft(x[idx], axis=1)


def _coarse_offset(spectra: np.ndarray) -> int:
    """Scarto intero in portanti (1 kHz): dove la finestra delle 1536 portanti raccoglie più energia."""
    power = np.mean(np.abs(spectra) ** 2, axis=0)
    shifted = np.fft.fftshift(power)
    c = np.concatenate(([0.0], np.cumsum(np.concatenate((shifted, shifted)))))
    best, best_e = 0, -1.0
    for e in range(-100, 101):
        lo = FFT_LEN // 2 + e - 768
        energy = c[lo + 1537 + FFT_LEN] - c[lo + FFT_LEN]
        if energy > best_e:
            best, best_e = e, energy
    return best


def _fic_soft(spectra: np.ndarray, offset: int, variant: tuple[bool, bool, bool]) -> np.ndarray:
    """9216 bit morbidi del FIC (3 simboli) dati i 4 spettri (riferimento + FIC)."""
    conj, flip_re, flip_im = variant
    # I e Q scambiati: spettro specchiato (portante k nel bin −k) e coniugato
    bins = ((-CARRIER_OF if conj else CARRIER_OF) + offset) % FFT_LEN
    y = spectra[:, bins]
    if conj:
        y = np.conj(y)
    d = y[1:] * np.conj(y[:-1])  # 3 x 1536, differenziale
    # lo scarto intero (portanti da 1 kHz) non è corretto nel tempo: tra due simboli (2552 campioni,
    # non 2048) la fase ruota e va tolta. Con I/Q scambiati lo scarto ha il segno opposto.
    eff = -offset if conj else offset
    d *= np.exp(-2j * np.pi * eff * (RATE / FFT_LEN) * SYM_LEN / RATE)
    d = d / (np.mean(np.abs(d)) + 1e-12)
    re, im = d.real, d.imag
    if flip_re:
        re = -re
    if flip_im:
        im = -im
    return np.concatenate([np.concatenate((re[k], im[k])) for k in range(FIC_SYMBOLS)]).astype(np.float32)


def _fic_blocks(soft: np.ndarray) -> list[list[bytes | None]]:
    """Blocchi FIC (righe da 2304 bit morbidi): per ognuno i 3 FIB da 32 byte, None se il CRC è errato."""
    soft = np.atleast_2d(soft)
    mother = np.zeros((soft.shape[0], MOTHER_BITS), np.float32)
    mother[:, FIC_KEEP] = soft
    bits = viterbi(mother)[:, :INFO_BITS] ^ PRBS
    out = []
    for row in bits:
        fibs: list[bytes | None] = []
        for k in range(3):
            b = np.packbits(row[k * 256 : (k + 1) * 256].astype(np.uint8)).tobytes()
            fibs.append(b if crc16(b[:30]) ^ 0xFFFF == int.from_bytes(b[30:], "big") else None)
        out.append(fibs)
    return out


def _label(raw: bytes) -> str:
    return "".join(chr(c) if 0x20 <= c <= 0x7E else CHARSET.get(c, "?") for c in raw).strip()


def _parse_fib(fib: bytes, out: dict[str, Any]) -> None:
    i = 0
    while i < 30:
        head = fib[i]
        if head == 0xFF:
            break
        ftype, length = head >> 5, head & 0x1F
        data = fib[i + 1 : i + 1 + length]
        i += 1 + length
        if len(data) < length or not data:
            break
        if ftype == 1:
            ext = data[0] & 0x07
            if ext == 0 and len(data) >= 19:
                out["eid"][data[1] << 8 | data[2]] += 1
                out["ensemble"][_label(data[3:19])] += 1
            elif ext == 1 and len(data) >= 19:
                sid = data[1] << 8 | data[2]
                out["labels"].setdefault(sid, Counter())[_label(data[3:19])] += 1
            elif ext == 5 and len(data) >= 21:
                sid = int.from_bytes(data[1:5], "big")
                out["data_labels"].setdefault(sid, Counter())[_label(data[5:21])] += 1
        elif ftype == 0:
            ext = data[0] & 0x1F
            pd = (data[0] >> 5) & 1
            if ext == 2 and pd == 0:
                j = 1
                while j + 3 <= len(data):
                    sid = data[j] << 8 | data[j + 1]
                    ncomp = data[j + 2] & 0x0F
                    comps = data[j + 3 : j + 3 + 2 * ncomp]
                    j += 3 + 2 * ncomp
                    for k in range(0, len(comps) - 1, 2):
                        tmid, ascty = comps[k] >> 6, comps[k] & 0x3F
                        primary = (comps[k + 1] >> 1) & 1
                        if tmid == 0 and primary:
                            out["audio_type"][sid] = "DAB+" if ascty == 63 else "DAB" if ascty == 0 else f"audio {ascty}"


def decode(iq: np.ndarray, rate: int, offset_hz: float = 0.0) -> dict[str, Any] | None:
    """Nome del multiplex e servizi da una registrazione centrata sul canale (offset_hz = centro DAB − centro)."""
    if rate != RATE:
        return {"error": "serve la frequenza di campionamento 2,048 MS/s (Avanzate → Sample rate)"}
    x = iq.astype(np.complex64)
    if offset_hz:
        x = x * np.exp(-2j * np.pi * offset_hz * np.arange(len(x)) / RATE).astype(np.complex64)
    starts = _frame_starts(x)
    if not starts:
        return None
    fine = _fine_offset(x, starts)
    x = x * np.exp(-2j * np.pi * fine * np.arange(len(x)) / RATE).astype(np.complex64)
    first = _symbols(x, starts[0], 1 + FIC_SYMBOLS)
    coarse = _coarse_offset(_symbols(x, starts[0], 12))

    # varianti: scarto intero vicino alla stima, coniugato, segni; vince chi supera il CRC
    # (tutte decodificate insieme sul primo blocco FIC della prima trama)
    trials = [(e, (c, fr, fi)) for e in range(coarse - 3, coarse + 4)
              for c in (False, True) for fr in (False, True) for fi in (False, True)]
    soft0 = np.stack([_fic_soft(first, e, v)[:BLOCK_BITS] for e, v in trials])
    good = [t for t, fibs in zip(trials, _fic_blocks(soft0), strict=True) if any(fibs)]
    best = min(good, key=lambda t: abs(t[0] - coarse)) if good else None
    if best is None:
        return {"frames": len(starts), "fib_ok": 0, "fib_total": 0, "freq_offset_hz": round(coarse * 1000 + fine)}

    e, variant = best
    out: dict[str, Any] = {"eid": Counter(), "ensemble": Counter(), "labels": {}, "data_labels": {}, "audio_type": {}}
    ok = total = 0
    soft = np.concatenate([_fic_soft(_symbols(x, s, 1 + FIC_SYMBOLS), e, variant).reshape(4, BLOCK_BITS)
                           for s in starts])
    for fibs in _fic_blocks(soft):
        for fib in fibs:
            total += 1
            if fib is not None:
                ok += 1
                _parse_fib(fib, out)
    services = []
    for sid, names in out["labels"].items():
        services.append({"sid": f"{sid:04X}", "name": names.most_common(1)[0][0],
                         "type": out["audio_type"].get(sid, "audio")})
    for sid, names in out["data_labels"].items():
        services.append({"sid": f"{sid:08X}", "name": names.most_common(1)[0][0], "type": "dati"})
    services.sort(key=lambda s: s["name"].lower())
    eid = out["eid"].most_common(1)[0][0] if out["eid"] else None
    return {
        "ensemble": out["ensemble"].most_common(1)[0][0] if out["ensemble"] else None,
        "eid": f"{eid:04X}" if eid is not None else None,
        "services": services,
        "frames": len(starts),
        "fib_ok": ok,
        "fib_total": total,
        "freq_offset_hz": round(e * 1000 + fine),
    }
