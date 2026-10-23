"""Ricevitore LRPT dei satelliti meteo Meteor-M: dal segnale radio all'immagine.

Catena (tutto in numpy, a blocchi):

1. demodulazione OQPSK 72 ksym/s: il segnale al quadrato ha due righe spettrali a
   2Δf ± 72 kHz; dalle loro fasi si ricavano blocco per blocco la portante (Δf e
   fase) e l'istante dei simboli, senza anelli campione per campione;
2. decodifica di Viterbi (codice convoluzionale K=7, 1/2, polinomi 171/133) su molti
   tratti in parallelo, con la ricerca delle ambiguità (rotazioni, scambio I/Q);
3. decodifica differenziale NRZ-M, sincronismo 1ACFFC1D, de-randomizzazione e
   Reed-Solomon (255,223) interlacciato ×4;
4. trame CCSDS (VCDU) → pacchetti → blocchi JPEG 8×8 dello strumento MSU-MR →
   righe dell'immagine (1568 pixel) per ogni canale.

Riferimenti per i parametri: meteor_demod/meteor_decode (dbdexter-dev) e SatDump.
"""

from __future__ import annotations

from typing import Any

import numpy as np

SYM_RATE = 72_000.0
IN_RATE = 1_024_000
DECIM = 4
FS = IN_RATE / DECIM  # 256 kHz: 3,56 campioni per simbolo
RRC_ALPHA = 0.5

ASM = 0x1ACFFC1D
CADU_BITS = 8192
FRAME_BYTES = 1020
VCDU_DATA = 882
IMG_APIDS = (64, 65, 66, 67, 68, 69)
LINE_PX = 1568
LOOP = 43  # pacchetti per riga: 14 segmenti × 3 canali + 1 di telemetria


# --- filtri ------------------------------------------------------------------------


def _rrc(alpha: float, sps: float, span: int = 8) -> np.ndarray:
    t = np.arange(-span * sps, span * sps + 1e-9) / sps
    t = t - (t[0] + t[-1]) / 2
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) / (
                np.pi * x * (1 - (4 * alpha * x) ** 2))
    return (h / np.sqrt((h**2).sum())).astype(np.float32)


def _lowpass(cutoff: float, fs: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.blackman(taps)
    return (h / h.sum()).astype(np.float32)


class _Fir:
    """Filtro FIR continuo tra un blocco e l'altro (con decimazione)."""

    def __init__(self, taps: np.ndarray, decim: int = 1) -> None:
        self.h, self.decim = taps, decim
        self.tail = np.zeros(len(taps) - 1, np.complex64)
        self.phase = 0  # campioni da saltare per restare allineati alla decimazione

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.tail, x])
        self.tail = buf[len(buf) - (len(self.h) - 1):]
        n = len(buf) - len(self.h) + 1
        if n <= 0:
            return np.zeros(0, np.complex64)
        # solo le uscite tenute dalla decimazione: finestre scorrevoli × coefficienti
        win = np.lib.stride_tricks.sliding_window_view(buf, len(self.h))[self.phase:: self.decim]
        y = win @ self.h[::-1]
        self.phase = (self.phase - n) % self.decim
        return y.astype(np.complex64)


# --- demodulazione OQPSK -----------------------------------------------------------


class LrptDemod:
    """Da campioni IQ (1,024 MS/s) a simboli "soft" int8: I0, Q0, I1, Q1…

    Le stime si fanno ogni BLOCK campioni, multiplo esatto del periodo di simbolo
    (2048 = 576 simboli): così le fasi delle due righe restano confrontabili da un
    blocco all'altro e basta "srotolarle" per seguire portante e simboli.
    """

    BLOCK = 2048  # passo delle stime (8 ms)
    WINDOW = 8192  # finestra di ogni stima (32 ms)
    ACQ = 65536  # campioni per l'aggancio iniziale (0,26 s)
    MAX_OFFSET = 12_000.0  # Hz cercati all'aggancio (errore della chiavetta + Doppler residuo)
    LOST_AFTER = 50  # blocchi (0,4 s) con righe deboli prima di ripetere l'aggancio
    MIN_STRENGTH = 0.035  # forza media delle righe: ~0,02 sul rumore, ~0,06 con il segnale a −3 dB
    MIN_LOCK_DB = 17.0  # righe sopra il fondo per agganciare: il rumore arriva a ~13 dB

    def __init__(self) -> None:
        self.lpf = _Fir(_lowpass(95_000, IN_RATE, 129), DECIM)
        self.mf = _Fir(_rrc(RRC_ALPHA, FS / SYM_RATE))
        self.nco = 0.0  # fase del mixer d'ingresso
        self.T = FS / SYM_RATE
        self.buf = np.zeros(0, np.complex64)  # uscita del filtro adattato, già ruotata di f2
        self.base = 0  # indice assoluto del primo campione di buf
        self.f2 = 0.0  # portante residua (Hz) tolta a FS
        self.ph2 = 0.0
        self.locked = False
        self.weak = 0
        self.c0 = 0  # centro della prima stima dopo l'aggancio
        self.next_block = 0
        self.next_k = 0  # prossimo simbolo (contato da c0)
        self.track: list[tuple[float, float, float]] = []  # (centro, A, B) srotolati
        self.line_db = 0.0
        self.strength = 0.0
        self.snr_db = 0.0
        self.offset_hz = 0.0  # scarto di portante misurato oltre a quello atteso

    def _rotate(self, y: np.ndarray) -> np.ndarray:
        n = len(y)
        w = 2 * np.pi * self.f2 / FS
        out = y * np.exp(-1j * (self.ph2 + w * np.arange(n))).astype(np.complex64)
        self.ph2 = float((self.ph2 + w * n) % (2 * np.pi))
        return out

    def _acquire(self) -> bool:
        """Aggancio: cerca la coppia di righe di y² distanti 2·72 kHz."""
        y = self.buf[-self.ACQ:]
        n = len(y)
        p = np.abs(np.fft.fft((y * y) * np.hanning(n))) ** 2
        df = FS / n
        k_rs = int(round(SYM_RATE / df))
        span = int(2 * self.MAX_OFFSET / df)
        cand = np.arange(-span, span + 1)
        score = p[(cand + k_rs) % n] + p[(cand - k_rs) % n]
        i = int(np.argmax(score))
        self.line_db = float(10 * np.log10(score[i] / (float(np.median(score)) + 1e-30)))
        if self.line_db < self.MIN_LOCK_DB:
            return False
        frac = 0.0
        if 0 < i < len(score) - 1:
            a, b, c = (np.log(score[i + d] + 1e-30) for d in (-1, 0, 1))
            if a - 2 * b + c != 0:
                frac = float(0.5 * (a - c) / (a - 2 * b + c))
        delta = (int(cand[i]) + frac) * df / 2  # y² ha la portante al doppio
        # il buffer viene ruotato di delta con la fase continua dall'inizio
        w = 2 * np.pi * delta / FS
        self.buf = (self.buf * np.exp(-1j * w * np.arange(len(self.buf)))).astype(np.complex64)
        self.ph2 = float((self.ph2 + w * len(self.buf)) % (2 * np.pi))
        self.f2 += delta
        return True

    def feed(self, iq: np.ndarray, offset_hz: float) -> np.ndarray:
        """Nuovi campioni IQ a 1,024 MS/s; offset_hz = dove sta il segnale rispetto al centro."""
        n = len(iq)
        w = 2 * np.pi * offset_hz / IN_RATE
        x = (iq * np.exp(-1j * (self.nco + w * np.arange(n)))).astype(np.complex64)
        self.nco = float((self.nco + w * n) % (2 * np.pi))
        y = self._rotate(self.mf(self.lpf(x)))
        self.buf = np.concatenate([self.buf, y])
        if not self.locked:
            if len(self.buf) < self.ACQ:
                return np.zeros(0, np.int8)
            if not self._acquire():
                drop = len(self.buf) - self.ACQ // 2
                self.buf, self.base = self.buf[drop:], self.base + drop
                return np.zeros(0, np.int8)
            self.locked, self.weak, self.track = True, 0, []
            self.strength = 2 * self.MIN_STRENGTH
            self.c0 = self.next_block = self.base + self.WINDOW // 2
            self.next_k = 0
        out = self._run()
        self.offset_hz = self.f2  # errore residuo: chiavetta (ppm) e orbita
        return out

    def _run(self) -> np.ndarray:
        half = self.WINDOW // 2
        end = self.base + len(self.buf)
        nn = np.arange(-half, half)
        e = np.exp(-2j * np.pi * SYM_RATE / FS * nn)
        win = np.hanning(self.WINDOW)
        while self.next_block + half <= end:
            c = self.next_block
            z = self.buf[c - half - self.base: c + half - self.base]
            z2 = z * z * win
            lp, lm = complex(np.dot(z2, e)), complex(np.dot(z2, np.conj(e)))
            strength = (abs(lp) + abs(lm)) / (float(np.sum(np.abs(z) ** 2 * win)) + 1e-30)
            self.strength = 0.9 * self.strength + 0.1 * strength
            a, b = float(np.angle(lp)), float(np.angle(lm))
            if self.track:
                pa, pb = self.track[-1][1], self.track[-1][2]
                a = pa + (a - pa + np.pi) % (2 * np.pi) - np.pi
                b = pb + (b - pb + np.pi) % (2 * np.pi) - np.pi
            self.track.append((float(c), a, b))
            # anello di frequenza: A+B = 4θ, cresce di 8π·(errore di portante in Hz) al secondo
            if len(self.track) >= 9:
                c0, a0, b0 = self.track[-9]
                err_hz = ((a + b) - (a0 + b0)) / (c - c0) / (8 * np.pi) * FS
                self.f2 += 0.05 * err_hz
            self.weak = self.weak + 1 if self.strength < self.MIN_STRENGTH else 0
            self.next_block += self.BLOCK
        if self.weak >= self.LOST_AFTER:
            self.locked = False  # segnale perso: nuovo aggancio
            return np.zeros(0, np.int8)
        return self._symbols()

    def _symbols(self) -> np.ndarray:
        if len(self.track) < 2:
            return np.zeros(0, np.int8)
        tr = np.array(self.track)
        cs, a, b = tr[:, 0], tr[:, 1], tr[:, 2]
        theta = (a + b) / 4  # fase residua della portante
        tau = (b - a) / (4 * np.pi) * self.T  # istante dei simboli I rispetto al centro della stima
        k = np.arange(self.next_k, int((cs[-1] - self.c0) // self.T) + 1)
        approx = self.c0 + k * self.T
        pos_i = approx + np.interp(approx, cs, tau)
        pos_q = pos_i + self.T / 2
        ok = (pos_q <= cs[-1]) & (pos_i >= cs[0])
        if not ok.any():
            return np.zeros(0, np.int8)
        last = int(np.nonzero(ok)[0][-1])
        k, pos_i, pos_q = k[: last + 1], pos_i[: last + 1], pos_q[: last + 1]
        self.next_k = int(k[-1]) + 1
        vi = self._at(pos_i, cs, theta).real
        vq = self._at(pos_q, cs, theta).imag
        soft = np.empty(2 * len(k), np.float32)
        soft[0::2], soft[1::2] = vi, vq
        mag = np.abs(soft)
        scale = float(np.mean(mag)) + 1e-9
        self.snr_db = float(10 * np.log10(scale**2 / (float(np.var(mag)) + 1e-12)))
        out = np.clip(np.round(soft / scale * 40), -127, 127).astype(np.int8)
        # buffer e stime che non servono più
        keep_from = int(min(pos_i[-1], self.next_block - self.WINDOW)) - 16 - self.base
        if keep_from > 0:
            self.buf, self.base = self.buf[keep_from:], self.base + keep_from
        if len(self.track) > 32:
            self.track = self.track[-32:]
        return out

    def _at(self, pos: np.ndarray, cs: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """Campioni interpolati (cubica) e ruotati della fase residua stimata."""
        idx = pos - self.base
        i = np.clip(np.floor(idx).astype(np.int64), 1, len(self.buf) - 3)
        f = (idx - i).astype(np.float32)
        y = self.buf
        p0, p1, p2, p3 = y[i - 1], y[i], y[i + 1], y[i + 2]
        v = p1 + 0.5 * f * (p2 - p0 + f * (2 * p0 - 5 * p1 + 4 * p2 - p3 + f * (3 * (p1 - p2) + p3 - p0)))
        return v * np.exp(-1j * np.interp(pos, cs, theta))


# --- codice convoluzionale e Viterbi --------------------------------------------------

G1, G2 = 0x79, 0x5B  # polinomi 171 e 133 (ottale) sul registro di 7 bit (bit nuovo in cima)


def _parity(x: np.ndarray) -> np.ndarray:
    x = x ^ (x >> 4)
    x = x ^ (x >> 2)
    x = x ^ (x >> 1)
    return x & 1


# stati: gli ultimi 6 bit, il più recente in cima (bit 5); predecessori e uscite di ogni ramo
_S = np.arange(64)
_PRED = np.stack([((_S << 1) & 0x3F) | x for x in (0, 1)], axis=1)  # (64, 2)
_REG = (_S >> 5)[:, None] << 6 | _PRED  # registro di 7 bit del ramo
_OUT = (_parity(_REG & G1) << 1) | _parity(_REG & G2)  # 0…3: (G1, G2)


def conv_encode(bits: np.ndarray) -> np.ndarray:
    """Bit → simboli codificati (G1, G2, G1, G2…), registro che parte da zero."""
    b = np.asarray(bits, np.int64)
    pad = np.concatenate([np.zeros(6, np.int64), b])
    reg = np.zeros(len(b), np.int64)
    for k in range(7):  # bit k del registro = ingresso di 6-k passi fa
        reg |= pad[k: k + len(b)] << k
    out = np.empty(2 * len(b), np.uint8)
    out[0::2] = _parity(reg & G1)
    out[1::2] = _parity(reg & G2)
    return out


def viterbi(soft: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Decodifica di più tratti in parallelo: soft (B, 2n) con positivo = bit 0. Bit (B, n) e metrica."""
    soft = np.asarray(soft, np.float32)
    nb, n = soft.shape[0], soft.shape[1] // 2
    y1, y2 = soft[:, 0::2], soft[:, 1::2]
    bm = np.stack([y1 + y2, y1 - y2, -y1 + y2, -y1 - y2], axis=2)  # (B, n, 4) per uscite 00,01,10,11
    pm = np.zeros((nb, 64), np.float32)
    dec = np.empty((n, nb, 64), bool)
    p0, p1 = _PRED[:, 0], _PRED[:, 1]
    o0, o1 = _OUT[:, 0], _OUT[:, 1]
    for t in range(n):
        m = bm[:, t, :]
        c0 = pm[:, p0] + m[:, o0]
        c1 = pm[:, p1] + m[:, o1]
        d = c1 > c0
        dec[t] = d
        pm = np.where(d, c1, c0)
        if t & 63 == 63:
            pm -= pm.max(axis=1, keepdims=True)
    state = np.argmax(pm, axis=1)
    best = pm[np.arange(nb), state]
    bits = np.empty((nb, n), np.uint8)
    rows = np.arange(nb)
    for t in range(n - 1, -1, -1):
        bits[:, t] = state >> 5
        state = _PRED[state, dec[t, rows, state].astype(np.int64)]
    return bits, best


# trasformazioni per le ambiguità del demodulatore: spostamento di un campione, scambio
# della coppia (I/Q) e segni; la giusta è quella che, ricodificata, somiglia di più all'ingresso
TRANSFORMS = [(off, swap, sa, sb) for off in (0, 1) for swap in (0, 1) for sa in (1, -1) for sb in (1, -1)]


def _apply(soft: np.ndarray, tf: tuple[int, int, int, int], n_pairs: int) -> np.ndarray:
    off, swap, sa, sb = tf
    x = soft[off: off + 2 * n_pairs].astype(np.float32).reshape(-1, 2)
    if swap:
        x = x[:, ::-1]
    return (x * np.array([sa, sb], np.float32)).ravel()


def _ber(soft: np.ndarray, bits: np.ndarray) -> float:
    enc = conv_encode(bits)
    hard = (soft < 0).astype(np.uint8)
    lo, hi = 64, len(enc) - 64  # i bordi del tratto decodificato sono meno affidabili
    return float(np.mean(enc[lo:hi] != hard[lo:hi])) if hi > lo else 1.0


# --- Reed-Solomon (255,223) CCSDS: campo 0x187, radici β^112…β^143 con β = α^11 -----------


def _gf_tables() -> tuple[np.ndarray, np.ndarray]:
    alpha = np.zeros(255, np.int64)
    x = 1
    for i in range(255):
        alpha[i] = x
        x <<= 1
        if x & 0x100:
            x ^= 0x187
    exp = alpha[(11 * np.arange(510)) % 255]  # potenze di β
    log = np.zeros(256, np.int64)
    log[exp[:255]] = np.arange(255)
    return exp, log


_EXP, _LOG = _gf_tables()
RS_FCR, RS_NROOTS = 112, 32


def _gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else int(_EXP[_LOG[a] + _LOG[b]])


def _ginv(a: int) -> int:
    return int(_EXP[(255 - _LOG[a]) % 255])


def _rs_generator() -> list[int]:
    g = [1]
    for i in range(RS_NROOTS):
        root = int(_EXP[(RS_FCR + i) % 255])
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gmul(c, root)
        g = ng
    return g  # coefficienti dal grado più alto


_RS_GEN = _rs_generator()


def rs_encode(data: bytes) -> bytes:
    """223 byte → 255 (byte 0 = coefficiente di grado più alto)."""
    rem = [0] * RS_NROOTS
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for j in range(RS_NROOTS):
                rem[j] ^= _gmul(f, _RS_GEN[j + 1])
    return bytes(data) + bytes(rem)


# potenze per le sindromi: S_i = Σ c_j β^((fcr+i)(254-j))
_SYN_POW = ((RS_FCR + np.arange(RS_NROOTS))[:, None] * (254 - np.arange(255))[None, :]) % 255


def rs_decode(cw: bytearray) -> int:
    """Corregge sul posto un blocco di 255 byte: numero di byte corretti o -1."""
    c = np.frombuffer(bytes(cw), np.uint8).astype(np.int64)
    nz = c != 0
    terms = np.where(nz[None, :], _EXP[(_LOG[c][None, :] + _SYN_POW) % 255], 0)
    synd = np.bitwise_xor.reduce(terms, axis=1)
    if not synd.any():
        return 0
    s = [int(v) for v in synd]
    # Berlekamp-Massey
    lam, prev = [1], [1]
    deg, m, pd = 0, 1, 1
    for n in range(RS_NROOTS):
        d = s[n]
        for i in range(1, deg + 1):
            if i < len(lam):
                d ^= _gmul(lam[i], s[n - i])
        if d == 0:
            m += 1
            continue
        coef = _gmul(d, _ginv(pd))
        new = lam + [0] * max(0, len(prev) + m - len(lam))
        for i, p in enumerate(prev):
            new[i + m] ^= _gmul(coef, p)
        if 2 * deg <= n:
            prev, pd, deg, m = lam, d, n + 1 - deg, 1
        else:
            m += 1
        lam = new
    lam = lam[: deg + 1]
    if deg > RS_NROOTS // 2:
        return -1
    # Chien: posizione j (grado 254-j) è un errore se Λ(β^-(254-j)) = 0
    lam_np = np.array(lam, np.int64)
    pos_deg = np.arange(255)
    x_inv_log = (255 - pos_deg) % 255
    vals = np.zeros(255, np.int64)
    for i, l in enumerate(lam_np):
        if l:
            vals ^= _EXP[(_LOG[l] + x_inv_log * i) % 255]
    degs = [int(d) for d in pos_deg[vals == 0]]
    if len(degs) != deg:
        return -1
    # Forney: e = X^(1-fcr) Ω(X⁻¹) / Λ'(X⁻¹)
    omega = [0] * RS_NROOTS
    for i in range(RS_NROOTS):
        for j in range(min(i + 1, len(lam))):
            omega[i] ^= _gmul(lam[j], s[i - j])
    for dg in degs:
        xinv_log = (255 - dg) % 255
        num = 0
        for i in range(RS_NROOTS - 1, -1, -1):
            num = (_gmul(num, int(_EXP[xinv_log])) if num else 0) ^ omega[i]
        den = 0
        for i in range(1, len(lam), 2):
            if lam[i]:
                den ^= int(_EXP[(_LOG[lam[i]] + xinv_log * (i - 1)) % 255])
        if den == 0:
            return -1
        e = _gmul(num, _ginv(den))
        e = _gmul(e, int(_EXP[(dg * (1 - RS_FCR)) % 255])) if e else 0
        cw[254 - dg] ^= e
    return len(degs)


# --- trame, pacchetti e immagini ---------------------------------------------------------


def _pn() -> np.ndarray:
    out, st = [], 0xFF
    for _ in range(255):
        byte = 0
        for _ in range(8):
            nb = ((st >> 7) ^ (st >> 5) ^ (st >> 3) ^ st) & 1
            byte = (byte << 1) | (st & 1)
            st = (st >> 1) | (nb << 7)
        out.append(byte)
    return np.array(out, np.uint8)


PN = _pn()
_PN_FRAME = np.resize(PN, FRAME_BYTES)
_ASM_BITS = np.array([(ASM >> (31 - i)) & 1 for i in range(32)], np.int8)

# tabelle JPEG standard (luminanza) usate dall'MSU-MR
_QTAB = np.array([16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
                  14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
                  49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], np.int64)
_ZIGZAG = np.array([0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20,
                    13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52,
                    45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63])
_AC_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
_AC_VALS = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07, 0x22, 0x71,
    0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x34, 0x35, 0x36, 0x37,
    0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83,
    0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
    0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA]
_DC_CODES = ["00", "010", "011", "100", "101", "110", "1110", "11110", "111110", "1111110", "11111110", "111111110"]


def _canonical(bits: list[int], vals: list[int]) -> dict[str, int]:
    table, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            table[format(code, f"0{length}b")] = vals[k]
            code += 1
            k += 1
        code <<= 1
    return table


_AC_TABLE = _canonical(_AC_BITS, _AC_VALS)
_DC_TABLE = {c: i for i, c in enumerate(_DC_CODES)}


def _qtable(q: int) -> np.ndarray:
    f = 5000 // q if q < 50 else 200 - 2 * q
    return np.maximum(1, (_QTAB * f // 50 + 1) // 2)


_IDCT = np.array([[(np.sqrt(0.5) if u == 0 else 1.0) * np.cos((2 * x + 1) * u * np.pi / 16) for u in range(8)]
                  for x in range(8)]) / 2  # (x, u)


def _value(bits: str, size: int) -> int:
    if size == 0:
        return 0
    v = int(bits, 2)
    return v if v >= 1 << (size - 1) else v - (1 << size) + 1


def decode_mcus(data: bytes, q: int, count: int = 14) -> np.ndarray | None:
    """14 blocchi 8×8 di una striscia MSU-MR (JPEG senza intestazioni) → pixel (8, 112)."""
    bits = "".join(format(b, "08b") for b in data)
    pos, n = 0, len(bits)
    coefs = np.zeros((count, 64), np.int64)
    dc = 0
    for blk in range(count):
        for ln in range(2, 10):
            cat = _DC_TABLE.get(bits[pos: pos + ln])
            if cat is not None:
                break
        else:
            return None
        pos += ln
        dc += _value(bits[pos: pos + cat], cat)
        pos += cat
        coefs[blk, 0] = dc
        k = 1
        while k < 64:
            for ln in range(2, 17):
                rs = _AC_TABLE.get(bits[pos: pos + ln])
                if rs is not None:
                    break
            else:
                return None
            pos += ln
            if rs == 0x00:  # fine blocco
                break
            run, size = rs >> 4, rs & 0x0F
            k += run
            if k > 63:
                return None
            coefs[blk, k] = _value(bits[pos: pos + size], size)
            pos += size
            k += 1
        if pos > n:
            return None
    block = np.zeros((count, 64))
    block[:, _ZIGZAG] = coefs  # dall'ordine a zig-zag alla griglia 8×8
    block = (block * _qtable(q)).reshape(count, 8, 8)  # (riga v, colonna u)
    pix = np.einsum("yv,bvu,xu->byx", _IDCT, block, _IDCT) + 128
    return np.clip(np.round(pix), 0, 255).astype(np.uint8).transpose(1, 0, 2).reshape(8, count * 8)


class LrptDecoder:
    """Dai simboli soft alle strisce d'immagine dei canali MSU-MR."""

    SEG = 4096  # coppie di simboli per tratto di Viterbi
    OVL = 96  # coppie di contorno prima e dopo ogni tratto
    BATCH = 24  # tratti decodificati insieme (~1,4 s di segnale): il Viterbi lavora in parallelo

    def __init__(self) -> None:
        self.soft = np.zeros(0, np.int8)
        self.tf = TRANSFORMS[0]
        self.bits = np.zeros(0, np.uint8)  # bit decodificati non ancora divisi in trame
        self.last_bit = 0
        self.nrzm: bool | None = None  # codifica differenziale (Meteor M2-x) o no
        self.vcdu_ok = self.vcdu_bad = self.frames_seen = 0
        self.ber = 0.5
        self.packet = bytearray()  # pacchetto in costruzione
        self.packet_need: int | None = None
        self.strips: list[tuple[int, int, int, np.ndarray]] = []  # (apid, seq esteso, mcu, pixel)
        self._seq_last: dict[int, int] = {}
        self._seq_wrap: dict[int, int] = {}
        self.spacecraft: int | None = None
        self._vcdu_last: int | None = None
        self.synced = False  # dentro il flusso di pacchetti (sappiamo dove inizia il prossimo)

    # -- simboli → bit
    def push(self, soft: np.ndarray, final: bool = False) -> None:
        self.soft = np.concatenate([self.soft, soft])
        need = 2 * (self.SEG + 2 * self.OVL) + 2
        seg_soft = 2 * self.SEG
        count = (len(self.soft) - need) // seg_soft + 1 if len(self.soft) >= need else 0
        if final and len(self.soft) >= 2 * (2 * self.OVL + 64):
            count = max(count, 1)
        if count <= 0 or (count < self.BATCH and not final):
            return
        count = min(count, 4 * self.BATCH)
        starts = [i * seg_soft for i in range(count)]
        span = 2 * (self.SEG + 2 * self.OVL)
        chunks = []
        for s0 in starts:
            piece = self.soft[s0: s0 + span + 1]
            if len(piece) < span + 1:
                piece = np.concatenate([piece, np.zeros(span + 1 - len(piece), np.int8)])
            chunks.append(piece)
        dec = self._decode(chunks, [self.tf] * count)
        bad = [i for i, (_, ber) in enumerate(dec) if ber > 0.12]
        if bad:
            # forse è cambiata l'ambiguità: per i tratti cattivi si provano tutte insieme
            trial = self._decode([chunks[i] for i in bad for _ in TRANSFORMS], TRANSFORMS * len(bad))
            for k, i in enumerate(bad):
                opts = trial[k * len(TRANSFORMS): (k + 1) * len(TRANSFORMS)]
                j = int(np.argmin([b for _, b in opts]))
                if opts[j][1] < dec[i][1]:
                    dec[i] = opts[j]
                    self.tf = TRANSFORMS[j]
        out = []
        for bits, ber in dec:
            self.ber = 0.8 * self.ber + 0.2 * ber
            out.append(bits[self.OVL: self.OVL + self.SEG])
        self.soft = self.soft[count * seg_soft:]
        self._bits(np.concatenate(out))

    def _decode(self, chunks: list[np.ndarray], tfs: list) -> list[tuple[np.ndarray, float]]:
        n_pairs = self.SEG + 2 * self.OVL
        soft = np.stack([_apply(c, tf, n_pairs) for c, tf in zip(chunks, tfs, strict=True)])
        bits, _ = viterbi(soft)
        return [(bits[i], _ber(soft[i], bits[i])) for i in range(len(chunks))]

    # -- bit → trame
    def _bits(self, new: np.ndarray) -> None:
        self.bits = np.concatenate([self.bits, new.astype(np.uint8)])
        while len(self.bits) >= CADU_BITS + 64:
            raw = self.bits
            if self.nrzm is None:
                self.nrzm = self._guess_nrzm(raw)
            b = raw.astype(np.int8)
            if self.nrzm:
                d = b ^ np.concatenate([[self.last_bit], b[:-1]])
            else:
                d = b
            sgn = 1 - 2 * d[: CADU_BITS + 32].astype(np.int16)
            ref = 1 - 2 * _ASM_BITS.astype(np.int16)
            corr = np.correlate(sgn, ref, "valid")
            k = int(np.argmax(np.abs(corr)))
            if abs(int(corr[k])) < 26:
                # niente sincronismo in questa trama: si scarta e si riprova più avanti
                cut = CADU_BITS
                self.last_bit = int(raw[cut - 1])
                self.bits = raw[cut:]
                continue
            if len(raw) < k + CADU_BITS + 1:
                break
            frame = d[k + 32: k + CADU_BITS]
            if corr[k] < 0:
                frame = 1 - frame
            self.frames_seen += 1
            self._frame(np.packbits(frame.astype(np.uint8)))
            cut = k + CADU_BITS
            self.last_bit = int(raw[cut - 1])
            self.bits = raw[cut:]

    def _guess_nrzm(self, raw: np.ndarray) -> bool:
        """Codifica differenziale o no: quella con il sincronismo ripetuto ogni 8192 bit."""
        best = (0, False)
        for nrzm in (False, True):
            b = raw.astype(np.int8)
            d = b ^ np.concatenate([[0], b[:-1]]) if nrzm else b
            sgn = 1 - 2 * d.astype(np.int16)
            corr = np.abs(np.correlate(sgn, 1 - 2 * _ASM_BITS.astype(np.int16), "valid"))
            score = int(corr.max()) if len(corr) else 0
            if score > best[0]:
                best = (score, nrzm)
        return best[1]

    def _frame(self, data: np.ndarray) -> None:
        frame = bytearray((data[:FRAME_BYTES] ^ _PN_FRAME).tobytes())
        ok = True
        for i in range(4):
            cw = bytearray(frame[i::4])
            r = rs_decode(cw)
            if r < 0:
                ok = False
                break
            frame[i::4] = cw
        if not ok:
            self.vcdu_bad += 1
            self.synced = False
            return
        self.vcdu_ok += 1
        self._vcdu(bytes(frame))

    def _vcdu(self, v: bytes) -> None:
        version = v[0] >> 6
        vcid = v[1] & 0x3F
        if version == 0 or vcid == 0:
            return
        self.spacecraft = ((v[0] & 0x3F) << 2) | (v[1] >> 6)
        counter = (v[2] << 16) | (v[3] << 8) | v[4]
        if self._vcdu_last is not None and counter != (self._vcdu_last + 1) & 0xFFFFFF:
            self.synced = False  # trama persa: il pacchetto a metà è perso
        self._vcdu_last = counter
        hdr = ((v[8] & 0x07) << 8) | v[9]
        zone = v[10: 10 + VCDU_DATA]
        has_hdr = hdr < VCDU_DATA
        if self.synced and has_hdr:
            # il puntatore dice dove inizia il primo pacchetto: deve tornare con quello in corso
            # (con l'intestazione del pacchetto spezzata tra due trame la lunghezza non si sa ancora)
            pending = (self.packet_need - len(self.packet)) if self.packet_need is not None else (
                0 if not self.packet else None)
            if pending is not None and pending != hdr:
                self.synced = False
        if not self.synced:
            if not has_hdr:
                return
            self.packet, self.packet_need, self.synced = bytearray(), None, True
            self._packets(zone[hdr:])
        else:
            self._packets(zone)

    def _packets(self, data: bytes) -> None:
        self.packet += data
        while True:
            if self.packet_need is None:
                if len(self.packet) < 6:
                    return
                self.packet_need = 6 + ((self.packet[4] << 8) | self.packet[5]) + 1
            if len(self.packet) < self.packet_need:
                return
            pkt = bytes(self.packet[: self.packet_need])
            self.packet = self.packet[self.packet_need:]
            self.packet_need = None
            self._packet(pkt)

    def _packet(self, p: bytes) -> None:
        apid = ((p[0] & 0x07) << 8) | p[1]
        if apid == 0x7FF:  # riempitivo
            return
        seq = ((p[2] & 0x3F) << 8) | p[3]
        if apid not in IMG_APIDS or len(p) < 6 + 14 + 2:
            return
        body = p[6:]
        mcu, q = body[8], body[13]
        if q == 0 or mcu % 14 or mcu > 182:
            return
        pix = decode_mcus(body[14:], q)
        if pix is None:
            return
        last = self._seq_last.get(apid)
        wrap = self._seq_wrap.get(apid, 0)
        if last is not None and seq < last - 8192:
            wrap += 16384
        self._seq_last[apid], self._seq_wrap[apid] = seq, wrap
        self.strips.append((apid, seq + wrap, mcu, pix))

    # -- strisce → immagini
    def channels(self) -> dict[int, np.ndarray]:
        """Immagine (righe × 1568) di ogni canale ricevuto, righe allineate tra i canali."""
        if not self.strips:
            return {}
        phase: dict[int, int] = {}
        for apid, g, mcu, _ in self.strips:
            phase.setdefault(apid, (g - mcu // 14) % LOOP)
        first = min(phase)
        p0 = phase[first]
        lines: dict[int, dict[int, np.ndarray]] = {}
        for apid, g, mcu, pix in self.strips:
            rel = (phase[apid] - p0) % LOOP
            line = (g - mcu // 14 - p0 - rel) // LOOP
            lines.setdefault(apid, {}).setdefault(line, np.zeros((8, LINE_PX), np.uint8))[:, mcu * 8: mcu * 8 + 112] = pix
        lo = min(min(d) for d in lines.values())
        hi = max(max(d) for d in lines.values())
        out = {}
        for apid, d in lines.items():
            img = np.zeros(((hi - lo + 1) * 8, LINE_PX), np.uint8)
            for ln, px in d.items():
                img[(ln - lo) * 8: (ln - lo) * 8 + 8] = px
            out[apid] = img
        return out

    def stats(self) -> dict[str, Any]:
        return {"frames": self.frames_seen, "vcdu_ok": self.vcdu_ok, "vcdu_bad": self.vcdu_bad,
                "ber": round(self.ber, 3), "strips": len(self.strips), "nrzm": self.nrzm,
                "spacecraft": self.spacecraft}


def composite(ch: dict[int, np.ndarray], northbound: bool = False) -> tuple[np.ndarray | None, str]:
    """Immagine a colori (o in scala di grigi) dai canali ricevuti, con il nord in alto."""
    if not ch:
        return None, ""
    if 64 in ch and 65 in ch:
        rgb, name = np.stack([ch[65], ch[65], ch[64]], axis=2), "RGB 221 (colori quasi naturali)"
    elif 66 in ch and 65 in ch and 64 not in ch:
        rgb, name = np.stack([ch[66], ch[65], ch[65]], axis=2), "RGB 322"
    else:
        apid = 68 if 68 in ch else min(ch)
        g = ch[apid] if apid < 67 else 255 - ch[apid]  # infrarosso: nubi fredde chiare
        rgb, name = np.stack([g, g, g], axis=2), f"canale {apid - 63}" + (" (infrarosso)" if apid >= 67 else "")
    if northbound:  # salendo verso nord l'immagine arriva capovolta
        rgb = rgb[::-1, ::-1]
    return rgb, name
