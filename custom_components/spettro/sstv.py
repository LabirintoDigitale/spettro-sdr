"""Immagini trasmesse come suoni: SSTV (ISS, radioamatori) e misura della frequenza del tono.

L'audio demodulato (FM per la ISS, USB/LSB in onde corte) contiene un tono la cui
frequenza è la luminosità: 1500 Hz nero, 2300 Hz bianco, 1200 Hz sincronismo.
ToneTracker ne ricava la frequenza istante per istante (16 kHz); SstvDecoder
riconosce il codice VIS che apre ogni immagine, sceglie il modo (PD, Martin,
Scottie, Robot), segue gli impulsi di sincronismo (l'immagine non viene storta
se l'orologio di chi trasmette è un po' diverso) e costruisce le righe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

FS = 16_000  # frequenza del tono misurata 16 000 volte al secondo
IN_RATE = 32_000  # audio dalla demodulazione in diretta


def _lowpass(cutoff: float, fs: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.blackman(taps)
    return h / h.sum()


class ToneTracker:
    """Audio PCM (int16, 32 kHz) → frequenza istantanea del tono (Hz) a 16 kHz."""

    def __init__(self, lo: float = 700.0, hi: float = 3000.0) -> None:
        self.aa = _lowpass(3800, IN_RATE, 63)
        center, half = (lo + hi) / 2, (hi - lo) / 2
        n = np.arange(97) - 48
        self.bp = (_lowpass(half, FS, 97) * np.exp(2j * np.pi * center * n / FS)).astype(np.complex64)
        self.tail_a = np.zeros(len(self.aa) - 1)
        self.tail_b = np.zeros(len(self.bp) - 1, np.complex64)
        self.phase = 0  # campione dispari/pari per la decimazione per 2
        self.prev: complex | None = None
        self.level = 0.0

    def feed(self, pcm: bytes | np.ndarray) -> np.ndarray:
        x = np.frombuffer(pcm, "<i2").astype(np.float64) if isinstance(pcm, bytes | bytearray) else pcm
        buf = np.concatenate([self.tail_a, x])
        self.tail_a = buf[len(buf) - len(self.tail_a):]
        y = np.convolve(buf, self.aa, "valid")
        y = y[self.phase::2]
        self.phase = (self.phase + len(x)) % 2
        zb = np.concatenate([self.tail_b, y.astype(np.complex64)])
        self.tail_b = zb[len(zb) - len(self.tail_b):]
        z = np.convolve(zb, self.bp, "valid")
        if not len(z):
            return np.zeros(0, np.float32)
        self.level = 0.9 * self.level + 0.1 * float(np.mean(np.abs(z)))
        prev = z[0] if self.prev is None else self.prev
        d = z * np.conj(np.concatenate([[prev], z[:-1]]))
        self.prev = z[-1]
        return (np.angle(d) * FS / (2 * np.pi)).astype(np.float32)


def to_level(f: np.ndarray) -> np.ndarray:
    """Frequenza → luminosità 0…255 (1500 Hz nero, 2300 Hz bianco)."""
    return np.clip((f - 1500.0) / 800.0 * 255.0, 0, 255)


# --- modi SSTV ------------------------------------------------------------------------


@dataclass(frozen=True)
class Mode:
    name: str
    width: int
    height: int
    period_ms: float  # durata di un periodo (una riga, o due righe per i PD)
    sync_ms: float  # inizio del sincronismo nel periodo
    sync_len: float
    parts: tuple[tuple[str, float, float], ...]  # (componente, inizio ms, durata ms)
    color: str  # "rgb", "pd", "robot36", "robot72"
    first_ms: float = 0.0  # dalla fine del VIS all'inizio del primo periodo
    rows: int = 1  # righe per periodo


def _pd(name: str, w: int, h: int, comp: float) -> Mode:
    s = 20.0 + 2.08
    return Mode(name, w, h, s + 4 * comp, 0.0, 20.0,
                (("y0", s, comp), ("cr", s + comp, comp), ("cb", s + 2 * comp, comp), ("y1", s + 3 * comp, comp)),
                "pd", rows=2)


def _martin(name: str, comp: float) -> Mode:
    a = 4.862 + 0.572
    return Mode(name, 320, 256, a + 3 * (comp + 0.572), 0.0, 4.862,
                (("g", a, comp), ("b", a + comp + 0.572, comp), ("r", a + 2 * (comp + 0.572), comp)), "rgb")


def _scottie(name: str, comp: float) -> Mode:
    g, b = 1.5, 1.5 + comp + 1.5
    sync = b + comp
    r = sync + 9.0 + 1.5
    return Mode(name, 320, 256, r + comp, sync, 9.0, (("g", g, comp), ("b", b, comp), ("r", r, comp)), "rgb",
                first_ms=9.0)


MODES: dict[int, Mode] = {
    93: _pd("PD50", 320, 256, 91.52),
    99: _pd("PD90", 320, 256, 170.24),
    95: _pd("PD120", 640, 496, 121.6),
    98: _pd("PD160", 512, 400, 195.584),
    96: _pd("PD180", 640, 496, 183.04),
    97: _pd("PD240", 640, 496, 244.48),
    94: _pd("PD290", 800, 616, 228.8),
    44: _martin("Martin 1", 146.432),
    40: _martin("Martin 2", 73.216),
    60: _scottie("Scottie 1", 138.24),
    56: _scottie("Scottie 2", 88.064),
    76: _scottie("Scottie DX", 345.6),
    8: Mode("Robot 36", 320, 240, 150.0, 0.0, 9.0, (("y", 12.0, 88.0), ("sep", 100.0, 4.5), ("c", 106.0, 44.0)),
            "robot36"),
    12: Mode("Robot 72", 320, 240, 300.0, 0.0, 9.0,
             (("y", 12.0, 138.0), ("cr", 156.0, 69.0), ("cb", 231.0, 69.0)), "robot72"),
}


def _ms(x: float) -> int:
    return int(round(x * FS / 1000))


def _ycc(y: np.ndarray, cr: np.ndarray, cb: np.ndarray) -> np.ndarray:
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255)


class SstvDecoder:
    """Frequenza del tono → immagini SSTV, una dopo l'altra."""

    MAX_BUFFER_S = 2.0  # in attesa del VIS si tengono solo gli ultimi secondi

    def __init__(self) -> None:
        self.f = np.zeros(0, np.float32)
        self.base = 0  # indice assoluto di f[0]
        self.mode: Mode | None = None
        self.start = 0  # inizio (assoluto) del primo periodo dell'immagine
        self.period = 0.0  # periodo stimato dai sincronismi (campioni)
        self.searched = 0  # fin dove si è cercato il VIS
        self.syncs: dict[int, float] = {}
        self.image: np.ndarray | None = None
        self.done_periods = 0
        self.missed = 0

    @property
    def state(self) -> str:
        return "ricezione" if self.mode else "attesa"

    def progress(self) -> float:
        if not self.mode:
            return 0.0
        return min(1.0, self.done_periods * self.mode.rows / self.mode.height)

    def feed(self, f: np.ndarray) -> list[dict[str, Any]]:
        """Nuovi campioni di frequenza: eventi "start" e "image" (immagine completa o interrotta)."""
        self.f = np.concatenate([self.f, f])
        events: list[dict[str, Any]] = []
        for _ in range(4):
            if self.mode is None:
                ev = self._find_vis()
                if ev is None:
                    break
                events.append(ev)
            else:
                done = self._decode()
                if done is None:
                    break
                events.append(done)
        if self.mode is None:
            keep = _ms(self.MAX_BUFFER_S * 1000)
            if len(self.f) > keep:
                cut = len(self.f) - keep
                self.f, self.base = self.f[cut:], self.base + cut
        return events

    # -- VIS: 1900 Hz (300 ms), 1200 (10 ms), 1900 (300 ms), bit di start 1200 (30 ms), 8 bit, stop
    def _find_vis(self) -> dict[str, Any] | None:
        f = self.f
        n = len(f)
        bit = _ms(30)
        need = _ms(300) + 10 * bit
        c = np.concatenate([[0.0], np.cumsum(f, dtype=np.float64)])

        def mean(a: np.ndarray, length: int) -> np.ndarray:
            return (c[a + length] - c[a]) / length

        lo = max(_ms(300), self.searched - self.base)
        hi = n - need + _ms(300)
        if hi <= lo:
            return None
        s = np.arange(lo, hi, _ms(1))
        leader = mean(s - _ms(250), _ms(240))
        start = mean(s + _ms(3), bit - _ms(6))
        ok = (np.abs(leader - 1900) < 70) & (np.abs(start - 1200) < 70)
        self.searched = self.base + int(s[-1]) + 1
        for i in np.nonzero(ok)[0]:
            s0 = int(s[i])
            # fronte esatto 1900 → 1200
            k = _ms(2)
            seg = f[s0 - _ms(10): s0 + _ms(10)]
            sm = np.convolve(seg, np.ones(k) / k, "valid")  # senza bordi: niente falsi fronti
            edge = np.nonzero(sm < 1550)[0]
            if len(edge):
                s0 = s0 - _ms(10) + int(edge[0]) + k // 2
            bits = []
            for k in range(8):
                a = s0 + (k + 1) * bit + _ms(4)
                if a + bit - _ms(8) > n:
                    self.searched = self.base + s0 - _ms(5)
                    return None
                bits.append(1 if float(np.mean(f[a: a + bit - _ms(8)])) < 1200 else 0)
            code = sum(b << k for k, b in enumerate(bits[:7]))
            if code in MODES and sum(bits) % 2 == 0:
                mode = MODES[code]
                self.mode = mode
                self.start = self.base + s0 + 10 * bit + _ms(mode.first_ms)  # start + 8 bit + stop
                self.period = mode.period_ms * FS / 1000
                self.syncs, self.done_periods, self.missed = {}, 0, 0
                self.image = np.zeros((mode.height, mode.width, 3), np.uint8)
                cut = s0 + 9 * bit - _ms(50)
                self.f, self.base = self.f[cut:], self.base + cut
                return {"event": "start", "mode": mode.name, "width": mode.width, "height": mode.height}
        return None

    # -- righe
    def _fit(self) -> tuple[float, float]:
        """Inizio e periodo dai sincronismi trovati (retta ai minimi quadrati, scarti esclusi)."""
        m = self.mode
        assert m is not None
        nominal = (float(self.start), m.period_ms * FS / 1000)
        if len(self.syncs) < 6:
            return nominal
        i = np.array(list(self.syncs), float)
        t = np.array(list(self.syncs.values()))
        keep = np.ones(len(i), bool)
        a, b = nominal
        for _ in range(3):
            if keep.sum() < 6:
                return nominal
            b, a = np.polyfit(i[keep], t[keep], 1)
            keep = np.abs(t - (a + b * i)) < _ms(1.5)
        if abs(b / nominal[1] - 1) > 0.01:  # più dell'1% di differenza: sincronismi sbagliati
            return nominal
        return float(a), float(b)

    def _sync_at(self, k_line: int, a: float, b: float) -> None:
        m = self.mode
        assert m is not None
        exp = a + b * k_line + _ms(m.sync_ms) - self.base
        lo, hi = int(exp - _ms(m.sync_len)), int(exp + _ms(m.sync_len))
        if lo < 0 or hi + _ms(m.sync_len) > len(self.f):
            return
        seg = self.f[lo: hi + _ms(m.sync_len)]
        # fronte di discesa: primo punto (media su 0,5 ms) sotto 1350 Hz seguito da un sincronismo intero
        k = _ms(0.5)
        cs = np.concatenate([[0.0], np.cumsum(seg, dtype=np.float64)])
        short = (cs[k:] - cs[:-k]) / k
        w = max(1, _ms(m.sync_len * 0.6))
        long_ = (cs[w:] - cs[:-w]) / w
        n = min(hi - lo, len(long_))
        cand = np.nonzero((short[:n] < 1350) & (long_[:n] < 1350))[0]
        if len(cand):
            self.syncs[k_line] = self.base + lo + int(cand[0]) + k / 2 - _ms(m.sync_ms)
            self.missed = 0
        else:
            self.missed += 1

    def _decode(self) -> dict[str, Any] | None:
        m = self.mode
        assert m is not None and self.image is not None
        total = m.height // m.rows
        a, b = self._fit()
        cs = None
        while self.done_periods < total:
            k = self.done_periods
            end = a + b * (k + 1) - self.base
            if end + _ms(m.sync_len) + 2 > len(self.f):
                return None
            self._sync_at(k, a, b)
            if self.missed > 12:  # niente sincronismi da un po': trasmissione finita
                return self._finish(interrupted=True)
            if cs is None:
                cs = np.concatenate([[0.0], np.cumsum(self.f, dtype=np.float64)])
            t0 = a + b * k - self.base
            scale = b / (m.period_ms * FS / 1000)
            comp = {}
            for name, start, dur in m.parts:
                edges = t0 + (start + np.arange(m.width + 1) * dur / m.width) * FS / 1000 * scale
                e = np.clip(np.round(edges).astype(np.int64), 0, len(self.f) - 1)
                width = np.maximum(e[1:] - e[:-1], 1)
                comp[name] = to_level((cs[e[:-1] + width] - cs[e[:-1]]) / width)
            self._paint(k, comp)
            self.done_periods += 1
            if k % 8 == 7:
                a, b = self._fit()
                self.start, self.period = int(round(a)), b
        return self._finish(interrupted=False)

    def _paint(self, k: int, c: dict[str, np.ndarray]) -> None:
        m, img = self.mode, self.image
        assert m is not None and img is not None
        if m.color == "pd":
            img[2 * k] = _ycc(c["y0"], c["cr"], c["cb"])
            if 2 * k + 1 < m.height:
                img[2 * k + 1] = _ycc(c["y1"], c["cr"], c["cb"])
        elif m.color == "rgb":
            img[k] = np.stack([c["r"], c["g"], c["b"]], axis=-1)
        elif m.color == "robot72":
            img[k] = _ycc(c["y"], c["cr"], c["cb"])
        else:  # Robot 36: crominanza a righe alterne (R-Y con separatore a 1500 Hz, B-Y a 2300 Hz)
            self._r36 = getattr(self, "_r36", {})
            self._r36[k] = (c["y"], c["c"], float(np.mean(c["sep"])))
            if k % 2 == 1 and k - 1 in self._r36:
                (y0, c0, s0), (y1, c1, _s1) = self._r36.pop(k - 1), self._r36.pop(k)
                cr, cb = (c0, c1) if s0 < 128 else (c1, c0)
                img[k - 1] = _ycc(y0, cr, cb)
                img[k] = _ycc(y1, cr, cb)

    def _finish(self, interrupted: bool) -> dict[str, Any] | None:
        m, img = self.mode, self.image
        assert m is not None and img is not None
        rows = self.done_periods * m.rows
        end = int(self.start + self.period * self.done_periods) - self.base
        self.f, self.base = self.f[max(0, end):], self.base + max(0, end)
        self.searched = self.base
        self.mode, self.image = None, None
        if rows < m.height * 0.25:
            return {"event": "lost", "mode": m.name}
        return {"event": "image", "mode": m.name, "image": img[:rows].copy() if interrupted else img,
                "complete": not interrupted}

    def preview(self) -> np.ndarray | None:
        if self.image is None or self.mode is None:
            return None
        return self.image
