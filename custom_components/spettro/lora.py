"""LoRa (chirp spread spectrum) dall'IQ dell'analisi continua: pacchetti grezzi con SNR e parametri.

Livello radio come gr-lora_sdr (EPFL, Tapparel et al.): dechirp + FFT, mappa di Gray, deinterleaving
diagonale, Hamming (CR 4/5…4/8), dewhitening, intestazione esplicita e CRC-16. Nessun parametro da
impostare: ogni raffica viene misurata (centro e larghezza del canale dallo spettro, SF dal preambolo)
e l'errore di frequenza della chiavetta si ricava da preambolo e chirp discendenti.

Il canale arriva già centrato sulla frequenza in ascolto e decimato a WORK_RATE; si tengono in
memoria gli ultimi secondi e si decodifica ogni raffica appena finisce.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .demod import MARGIN_OUT, _channel

WORK_RATE = 256_000  # 1,024 MS/s / 4: basta per i canali LoRa fino a 250 kHz
BANDWIDTHS = (62_500.0, 125_000.0, 250_000.0)
SFS = range(7, 13)
BINS = 256  # punti dello spettro di controllo (1 kHz l'uno a WORK_RATE)
FRAME = 16 * BINS  # 16 ms: una riga dello spettro di controllo
FLOOR_FRAMES = 400  # righe (6,4 s) per il rumore di fondo di ogni punto
FLOOR_P20 = 1.27  # media / 20° percentile di una riga di solo rumore
ACTIVE_EXCESS = 0.25  # eccesso medio su 62,5 kHz sopra il fondo (≈ +1 dB) per dire "c'è qualcosa"
GAP_FRAMES = 3  # buchi tollerati dentro una raffica
PAD_FRAMES = 3  # margine attorno alla raffica (il preambolo può essere sotto la soglia)
MIN_FRAMES = 3
KEEP_S = 12.0  # raffica più lunga che si aspetta prima di decodificare quel che c'è
PEAK_RATIO = 10.0  # picco del dechirp sopra la media degli altri punti (nel rumore puro ≈ 6)
LDRO_S = 0.016  # sopra 16 ms per simbolo il trasmettitore usa il "low data rate optimize"
OVERSAMPLE = 8  # per le finestre a posizione frazionaria
MAX_OVERSAMPLED = 6_000_000  # oltre (pacchetti lunghissimi) si sovracampiona solo ×2
TIMING_GAIN = 0.3  # inseguimento della temporizzazione, per simbolo
TIMING_GAIN2 = 0.03  # e della deriva del clock (anello del secondo ordine)
EARLY_LATE = 0.25  # spostamento (in chip) delle finestre di confronto
MAX_PPM = 120  # deriva del clock massima considerata (chiavetta + trasmettitore)

WHITENING = bytes.fromhex(
    "fffefcf8f0e1c2850b172f5ebc78f1e3c68d1a3468d0a0408001020408112347"
    "8e1c3871e2c48912254b972e5cb870e0c08103060c193264c992244993264d9b"
    "376edcb972e4c890204182050a152b56ad5bb66ddab56bd6ac59b265cb962c58"
    "b061c3870f1f3e7dfbf6eddbb76fdebd7af5ebd7ae5dba74e8d1a24488102143"
    "860d1b366cd8b163c78f1e3c79f3e7ce9c3973e6cc983162c58b162d5ab469d2"
    "a4489122458a142952a54a952a54a953a74e9d3b77eeddbb76ecd9b367cf9e3d"
    "7bf7efdfbf7efdfaf4e9d3a64c993366cd9a356ad4a851a3468c183060c18307"
    "0e1d3a75ead5aa55ab57af5fbe7cf9f2e5ca942850a142840913274f9f3f7f"
)  # sequenza di whitening (LFSR) del payload
# codici di Hamming validi per i 16 nibble: [d0 d1 d2 d3 | p0 p1 p2 p3], e la variante CR 4/5
HAMMING = (0, 23, 45, 58, 78, 89, 99, 116, 139, 156, 166, 177, 197, 210, 232, 255)
HAMMING_CR5 = (0, 24, 40, 48, 72, 80, 96, 120, 136, 144, 160, 184, 192, 216, 232, 240)

_CHIRPS: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def chirps(sf: int) -> tuple[np.ndarray, np.ndarray]:
    """Chirp di riferimento (salita, discesa) a un campione per chip."""
    if sf not in _CHIRPS:
        n = 1 << sf
        k = np.arange(n)
        up = np.exp(2j * np.pi * (k * k / (2 * n) - k / 2)).astype(np.complex64)
        _CHIRPS[sf] = (up, up.conj())
    return _CHIRPS[sf]


def _peak(x: np.ndarray) -> tuple[float, float, float, float]:
    """Picco di uno spettro complesso: posizione frazionaria, rapporto sul resto, potenza picco e media."""
    p = np.abs(x) ** 2
    n = len(x)
    i = int(np.argmax(p))
    a, b, c = x[i - 1], x[i], x[(i + 1) % n]
    den = 2 * b - a - c
    d = float(np.real((a - c) / den)) if abs(den) > 0 else 0.0  # stimatore di Jacobsen
    rest = (p.sum() - p[i]) / (n - 1) + 1e-20
    return (i + max(-0.5, min(0.5, d))) % n, float(p[i] / rest), float(p[i]), float(rest)


def _wrap(v: float, n: int) -> float:
    return (v + n / 2) % n - n / 2


def _gray(s: int) -> int:
    return s ^ (s >> 1)


def _hamming(c: int, cw_len: int) -> int:
    """Nibble dal codice più vicino (distanza di Hamming): corregge un errore con CR 4/7 e 4/8."""
    lut = HAMMING_CR5 if cw_len == 5 else HAMMING
    best = min(range(16), key=lambda n: bin((lut[n] >> (8 - cw_len)) ^ c).count("1"))
    v = lut[best] >> 4  # d0 nel bit più alto
    return ((v & 1) << 3) | ((v & 2) << 1) | ((v & 4) >> 1) | ((v & 8) >> 3)


def _deinterleave(symbols: list[int], sf_app: int, cw_len: int) -> list[int]:
    """Un blocco di cw_len simboli (sf_app bit ciascuno) → sf_app codici di cw_len bit."""
    rows = [[(s >> (sf_app - 1 - j)) & 1 for j in range(sf_app)] for s in symbols]
    cw = [[0] * cw_len for _ in range(sf_app)]
    for i in range(cw_len):
        for j in range(sf_app):
            cw[(i - j - 1) % sf_app][i] = rows[i][j]
    return [int("".join(map(str, c)), 2) for c in cw]


def _header_ok(h: list[int]) -> bool:
    b = [[(x >> k) & 1 for k in range(4)] for x in h[:3]]  # b[nibble][bit], bit 0 = meno significativo
    c4 = b[0][3] ^ b[0][2] ^ b[0][1] ^ b[0][0]
    c3 = b[0][3] ^ b[1][3] ^ b[1][2] ^ b[1][1] ^ b[2][0]
    c2 = b[0][2] ^ b[1][3] ^ b[1][0] ^ b[2][3] ^ b[2][1]
    c1 = b[0][1] ^ b[1][2] ^ b[1][0] ^ b[2][2] ^ b[2][1] ^ b[2][0]
    c0 = b[0][0] ^ b[1][1] ^ b[2][3] ^ b[2][2] ^ b[2][1] ^ b[2][0]
    return ((h[3] & 1) << 4 | h[4]) == (c4 << 4 | c3 << 3 | c2 << 2 | c1 << 1 | c0)


def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if ((crc >> 8) ^ byte) & 0x80 else crc << 1) & 0xFFFF
            byte = (byte << 1) & 0xFF
    return crc


def _crc_ok(payload: bytes, crc: int) -> bool:
    if len(payload) < 2:
        return False
    return (crc16(payload[:-2]) ^ payload[-1] ^ (payload[-2] << 8)) == crc


def _resample(seg: np.ndarray, fc: float, bw: float) -> np.ndarray:
    """Porta fc a 0 e ricampiona a bw campioni/s (un campione per chip) con filtro a mattone."""
    n = np.arange(len(seg))
    x = np.fft.fft(seg * np.exp(-2j * np.pi * fc * n / WORK_RATE).astype(np.complex64))
    m = int(round(len(seg) * bw / WORK_RATE))
    h = m // 2
    y = np.zeros(m, np.complex128)
    y[:h] = x[:h]
    y[m - h :] = x[len(x) - h :]
    return (np.fft.ifft(y) * (m / len(seg))).astype(np.complex64)


def decode_burst(y: np.ndarray, bw: float) -> dict[str, Any] | None:
    """Cerca e decodifica un pacchetto LoRa in y (un campione per chip, canale già centrato)."""
    best = None
    for sf in SFS:
        n = 1 << sf
        k = len(y) // n
        if k < 6:
            continue
        _, down = chirps(sf)
        x = np.fft.fft(y[: k * n].reshape(k, n) * down, axis=1)
        p = np.abs(x) ** 2
        idx = p.argmax(1)
        pk = p.max(1)
        ratio = pk / ((p.sum(1) - pk) / (n - 1) + 1e-20)
        run, start = 0, 0
        for w in range(k):
            same = run and ratio[w] > PEAK_RATIO and abs(_wrap(idx[w] - idx[w - 1], n)) <= 1
            if same:
                run += 1
            elif ratio[w] > PEAK_RATIO:
                run, start = 1, w
            else:
                run = 0
            if run >= 4 and (best is None or run > best[2] or (run == best[2] and ratio[w] > best[3])):
                best = (sf, start, run, float(ratio[start : start + run].mean()))
    if best is None:
        return None
    sf, w0, run, _ = best
    n = 1 << sf
    up, down = chirps(sf)
    # preambolo: picco (senza le finestre ai bordi, che possono essere a cavallo); la pendenza dei
    # picchi è la deriva del clock (simboli più lunghi o più corti di n campioni)
    mid = list(range(w0 + 1, w0 + run - 1) if run > 3 else range(w0, w0 + run))
    ups = [_peak(np.fft.fft(y[w * n : (w + 1) * n] * down)) for w in mid]
    ref = ups[len(ups) // 2][0]
    pos = np.array([_wrap(u[0] - ref, n) for u in ups])
    slope = float(np.polyfit(mid, pos, 1)[0]) if len(mid) >= 4 else 0.0
    slope = max(-MAX_PPM * 1e-6 * n, min(MAX_PPM * 1e-6 * n, slope))
    wc = float(np.mean(mid))
    p_up = ref + float(pos.mean())
    rough = _snr([u[1] for u in ups], n)
    # chirp discendenti (SFD) dopo la parola di sincronismo
    dn = None
    for w in range(w0 + run, w0 + run + 7):
        if (w + 1) * n > len(y):
            break
        seg = y[w * n : (w + 1) * n]
        d = _peak(np.fft.fft(seg * up))
        if d[1] > PEAK_RATIO and d[2] > _peak(np.fft.fft(seg * down))[2] * 2 and (dn is None or d[2] > dn[2]):
            dn = (*d, w)
    if dn is None:
        return {"ok": False, "sf": sf, "bw": bw, "snr": rough, "error": "preambolo senza chirp discendenti"}
    # salita: τ + cfo, discesa: cfo − τ (in punti FFT), entrambe alla finestra dei chirp discendenti;
    # ambiguità di mezzo simbolo: provo entrambe
    wd = dn[4]
    p_up += slope * (wd - wc)
    tau0, cfo0 = (p_up - dn[0]) / 2, (p_up + dn[0]) / 2
    per = n - slope  # campioni per simbolo
    found = []
    for tau, cfo in ((tau0, cfo0), (tau0 + n / 2, cfo0 + n / 2)):
        res = _decode_aligned(y, sf, bw, wd * n - tau % n - (wd - w0) * per, _wrap(cfo, n), per)
        if res is not None:
            found.append(res)
            if res.get("crc_ok"):
                break
    found.sort(key=lambda r: (bool(r.get("crc_ok")), bool(r.get("ok"))), reverse=True)
    if found:
        return found[0]
    return {"ok": False, "sf": sf, "bw": bw, "snr": rough, "error": "intestazione illeggibile"}


def _snr(ratios: list[float], n: int) -> float:
    """SNR nella banda del canale dal rapporto picco/resto del dechirp (come lo riportano le radio)."""
    return round(float(10 * np.log10(max(float(np.mean(ratios)) - 1, 1e-3) / n)), 1)


def _decode_aligned(y: np.ndarray, sf: int, bw: float, b0: float, cfo: float, per: float,
                    refine: bool = True) -> dict[str, Any] | None:
    """Decodifica con i simboli allineati: b0 = inizio (frazionario) di un chirp del preambolo,
    per = durata di un simbolo in campioni (n, più la deriva del clock)."""
    n = 1 << sf
    up, down = chirps(sf)
    m = len(y)
    spec = np.fft.fft(y * np.exp(-2j * np.pi * cfo * np.arange(m) / n))
    # sovracampionato (interpolazione esatta per un segnale a banda limitata): le finestre partono
    # da posizioni frazionarie, così il chirp non ha il salto di fase dove torna a -BW/2
    os_ = OVERSAMPLE if m * OVERSAMPLE <= MAX_OVERSAMPLED else 2
    big = np.zeros(m * os_ + 1, np.complex64)
    h = (m + 1) // 2
    big[:h] = spec[:h]
    big[m * os_ - (m - h) : m * os_] = spec[h:]
    yo = np.fft.ifft(big[:-1]) * os_
    yo = np.append(yo, 0).astype(np.complex64)

    def window(pos: float) -> np.ndarray | None:
        if pos < 0 or pos + n + 1 > m:
            return None
        t = (pos + np.arange(n)) * os_
        i = t.astype(np.int64)
        f = (t - i).astype(np.float32)
        return yo[i] * (1 - f) + yo[i + 1] * f

    k0 = int(np.ceil(-b0 / per)) if b0 < 0 else 0
    sfd = None
    for k in range(k0, k0 + 40):
        win = window(b0 + k * per)
        if win is None:
            return None
        e_up = _peak(np.fft.fft(win * down))
        e_dn = _peak(np.fft.fft(win * up))
        if e_dn[1] > PEAK_RATIO and e_dn[2] > 2 * e_up[2]:
            sfd = k
            break
    if sfd is None or sfd - k0 < 3:
        return None
    pre = [_peak(np.fft.fft(window(b0 + k * per) * down)) for k in range(max(k0, sfd - 6), sfd - 2)]
    if refine:
        # residui con i simboli allineati: salita = dτ + dcfo, discesa = dcfo − dτ
        e_up = float(np.mean([_wrap(p[0], n) for p in pre]))
        e_dn = _wrap(_peak(np.fft.fft(window(b0 + sfd * per) * up))[0], n)
        if abs(e_up) < 2 and abs(e_dn) < 2:
            return _decode_aligned(y, sf, bw, b0 - (e_up - e_dn) / 2, cfo + (e_up + e_dn) / 2, per, False)
    snr = _snr([p[1] for p in pre], n)
    sync = [round(_peak(np.fft.fft(window(b0 + j * per) * down))[0]) % n for j in (sfd - 2, sfd - 1)]
    sync_word = ((sync[0] >> 3) & 0xF) << 4 | ((sync[1] >> 3) & 0xF) if all(s % 8 == 0 for s in sync) else None
    ldro = n / bw > LDRO_S
    adj = rate = 0.0
    start = b0 + (sfd + 2.25) * per
    ramp = np.arange(n) / n

    def coherence(pos: float, f: float) -> float | None:
        w = window(pos)
        return None if w is None else float(abs(np.dot(w * down, np.exp(-2j * np.pi * f * ramp))) ** 2)

    def demod(i: int) -> int | None:
        nonlocal adj, rate
        pos = start + i * per + adj
        w = window(pos)
        if w is None:
            return None
        f = _peak(np.fft.fft(w * down))[0]
        v = round(f) % n
        # early-late: con la finestra in ritardo di δ il chirp ha un salto di fase 2πδ dove torna a
        # -BW/2 (dopo n-v campioni) e il simbolo perde coerenza; il confronto a ±1/4 di chip dà δ
        # senza dipendere dall'errore di frequenza (che sposta solo il picco)
        a, b = n - v, v
        if min(a, b) > n // 8:
            e = coherence(pos - EARLY_LATE, f - EARLY_LATE)
            l_ = coherence(pos + EARLY_LATE, f + EARLY_LATE)
            c = coherence(pos, f)
            if e is not None and l_ is not None and c:
                amp2 = c / n**2
                d = (e - l_) / (4 * a * b * np.sin(2 * np.pi * EARLY_LATE) * amp2) / (2 * np.pi)
                d = max(-0.3, min(0.3, d)) * 4 * a * b / n**2  # pesato: affidabile a metà simbolo
                adj -= TIMING_GAIN * d
                rate -= TIMING_GAIN2 * d
        adj += rate  # deriva del clock (in campioni per simbolo)
        return (v - 1) % n

    syms = [demod(i) for i in range(8)]
    if None in syms:
        return None
    base = {"sf": sf, "bw": bw, "snr": snr, "sync_word": sync_word, "cfo_bins": cfo}
    nib = [_hamming(c, 8) for c in _deinterleave([_gray(s >> 2) for s in syms], sf - 2, 8)]
    if not _header_ok(nib):
        return {**base, "ok": False, "error": "intestazione con checksum errato"}
    length = nib[0] << 4 | nib[1]
    has_crc = bool(nib[2] & 1)
    cr = nib[2] >> 1
    if length == 0 or not 1 <= cr <= 4:
        return {**base, "ok": False, "error": "intestazione non valida"}
    nibbles = nib[5:]
    need = 2 * length + (4 if has_crc else 0)
    sf_app = sf - 2 if ldro else sf
    cw_len = 4 + cr
    i = 8
    while len(nibbles) < need:
        block = [demod(i + j) for j in range(cw_len)]
        i += cw_len
        if None in block:
            break
        g = [_gray(s >> 2 if ldro else s) for s in block]
        nibbles += [_hamming(c, cw_len) for c in _deinterleave(g, sf_app, cw_len)]
    complete = len(nibbles) >= need
    nibbles += [0] * max(0, need - len(nibbles))
    data = bytes((nibbles[2 * j + 1] << 4 | nibbles[2 * j]) ^ WHITENING[j] for j in range(length))
    crc_ok = None
    if has_crc:
        c = nibbles[2 * length : 2 * length + 4]
        crc_ok = complete and _crc_ok(data, c[0] | c[1] << 4 | c[2] << 8 | c[3] << 12)
    return {**base, "ok": True, "cr": cr, "ldro": ldro, "payload": data, "crc_ok": crc_ok, "complete": complete}


class LoraReceiver:
    """Raffiche LoRa dal flusso IQ in diretta: canale, spettro di controllo e decodifica."""

    def __init__(self, rate: int, offset_hz: float) -> None:
        self.rate, self.offset = rate, offset_hz
        self.decim = rate // WORK_RATE
        self.half_bw = WORK_RATE / 2 * 0.99  # i canali da 250 kHz arrivano quasi al bordo
        self.margin = MARGIN_OUT * self.decim
        self.tail = np.zeros(2 * self.margin, np.complex64)
        self.first = -2 * self.margin
        self.chunks: list[np.ndarray] = []
        self.base = 0  # indice (a WORK_RATE) del primo campione in memoria
        self.total = 0  # campioni ricevuti in tutto
        self.next_frame = 0
        self.spectra: list[np.ndarray] = []  # righe dello spettro di controllo ancora in memoria
        self.frame0 = 0  # indice della prima riga in self.spectra
        self.history: list[np.ndarray] = []  # righe recenti per il fondo di rumore
        self.floor: np.ndarray | None = None
        self.active: list[bool] = []
        self.burst: int | None = None  # riga di inizio della raffica in corso
        self.quiet = 0
        self.window = np.hanning(BINS).astype(np.float32)

    def feed(self, iq: np.ndarray) -> list[dict[str, Any]]:
        buf = np.concatenate((self.tail, iq))
        z = _channel(buf, self.rate, self.offset, self.decim, self.half_bw, self.first)
        z = z[MARGIN_OUT : len(z) - MARGIN_OUT]
        self.first += len(iq)
        self.tail = buf[-2 * self.margin :]
        self.chunks.append(z)
        self.total += len(z)
        out = []
        while self.next_frame + FRAME <= self.total:
            row = self._frame(self.next_frame)
            self.next_frame += FRAME
            res = self._step(row)
            if res is not None:
                out.append(res)
        self._trim()
        return out

    def _get(self, a: int, b: int) -> np.ndarray:
        a, b = max(a, self.base), min(b, self.total)
        parts, pos = [], self.base
        for c in self.chunks:
            lo, hi = max(a, pos), min(b, pos + len(c))
            if lo < hi:
                parts.append(c[lo - pos : hi - pos])
            pos += len(c)
        return np.concatenate(parts) if parts else np.zeros(0, np.complex64)

    def _frame(self, a: int) -> np.ndarray:
        x = self._get(a, a + FRAME).reshape(-1, BINS) * self.window
        return np.fft.fftshift(np.mean(np.abs(np.fft.fft(x, axis=1)) ** 2, axis=0))

    def _step(self, row: np.ndarray) -> dict[str, Any] | None:
        self.spectra.append(row)
        self.history.append(row)
        del self.history[:-FLOOR_FRAMES]
        if self.floor is None or len(self.history) % 25 == 0 or len(self.history) < 50:
            # 20° percentile (regge anche un canale occupato a lungo), riportato alla media del rumore
            self.floor = np.percentile(np.array(self.history), 20, axis=0) * FLOOR_P20 + 1e-12
        if len(self.history) < 20:
            self.active.append(False)
            return None
        ex = row / self.floor - 1
        k = int(BANDWIDTHS[0] / (WORK_RATE / BINS))
        box = np.convolve(ex, np.ones(k) / k, "valid")
        on = float(box.max()) > ACTIVE_EXCESS
        self.active.append(on)
        frame = self.frame0 + len(self.spectra) - 1
        if on:
            if self.burst is None:
                self.burst = frame
            self.quiet = 0
            if (frame - self.burst) * FRAME < KEEP_S * WORK_RATE:
                return None
        elif self.burst is None:
            return None
        else:
            self.quiet += 1
            if self.quiet <= GAP_FRAMES:
                return None
        first, last = self.burst, frame - self.quiet
        self.burst, self.quiet = None, 0
        if last - first + 1 < MIN_FRAMES:
            return None
        return self._decode(first, last)

    def _decode(self, first: int, last: int) -> dict[str, Any] | None:
        rows = np.array(self.spectra[first - self.frame0 : last - self.frame0 + 1])
        ex = np.convolve(np.mean(rows, axis=0) / self.floor - 1, np.ones(5) / 5, "same")
        top = int(np.argmax(ex))
        lo = hi = top
        while lo > 0 and ex[lo - 1] > 0.25 * ex[top]:
            lo -= 1
        while hi < BINS - 1 and ex[hi + 1] > 0.25 * ex[top]:
            hi += 1
        step = WORK_RATE / BINS
        width = (hi - lo + 1) * step
        fc = ((lo + hi) / 2 - BINS / 2) * step
        a = (first - PAD_FRAMES) * FRAME
        b = (last + 1 + PAD_FRAMES) * FRAME
        seg = self._get(a, b)
        level = float(10 * np.log10(np.mean(np.abs(self._get(first * FRAME, (last + 1) * FRAME)) ** 2) / 127.5**2 + 1e-20))
        res = None
        for bw in sorted(BANDWIDTHS, key=lambda v: abs(np.log(width / v))):
            if not 0.55 < width / bw < 1.45:
                continue
            res = decode_burst(_resample(seg, fc, bw), bw)
            if res is not None and res.get("ok"):
                break
        if res is None:
            return None  # non è LoRa (larghezza diversa o niente preambolo)
        cfo_hz = res.pop("cfo_bins", 0.0) * res["bw"] / (1 << res["sf"])
        return {**res, "offset_hz": round(fc + cfo_hz), "level_db": round(level, 1),
                "end_s": (self.total - (last + 1) * FRAME) / WORK_RATE,
                "duration_s": round((last - first + 1) * FRAME / WORK_RATE, 3)}

    def _trim(self) -> None:
        """Libera i campioni e le righe che non servono più (resta la raffica in corso)."""
        keep_frame = (self.burst if self.burst is not None else self.frame0 + len(self.spectra)) - PAD_FRAMES - 1
        keep = max(keep_frame * FRAME, self.total - int(KEEP_S * WORK_RATE))
        while self.chunks and self.base + len(self.chunks[0]) < keep:
            self.base += len(self.chunks[0])
            self.chunks.pop(0)
        drop = max(0, min(keep_frame - self.frame0, len(self.spectra)))
        if drop:
            del self.spectra[:drop]
            del self.active[:drop]
            self.frame0 += drop
