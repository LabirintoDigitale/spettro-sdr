"""Decodifica in testo durante l'ascolto: Morse (CW) e RTTY (FSK a due toni, codice Baudot).

Lavorano sull'audio della diretta (32 kHz): il Morse cerca da solo il tono e si adatta alla
velocità; l'RTTY cerca la coppia di toni (spostamento noto) e prova sia la polarità normale
sia quella invertita, tenendo quella che dà meno errori di trama.
"""

from __future__ import annotations

from typing import Any

import numpy as np

IN_RATE = 32_000
FS = 8_000  # dopo la decimazione per 4


def _lowpass(cutoff: float, fs: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.blackman(taps)
    return h / h.sum()


class _Audio:
    """Audio a 32 kHz (PCM int16 o float) → audio a 8 kHz (filtrato) e ultimi secondi per cercare i toni."""

    def __init__(self) -> None:
        self.h = _lowpass(3400, IN_RATE, 63)
        self.tail = np.zeros(len(self.h) - 1)
        self.phase = 0
        self.recent = np.zeros(0)

    def feed(self, pcm: bytes | np.ndarray) -> np.ndarray:
        # meglio l'audio prima del controllo automatico di volume: il livello non salta tra un blocco e l'altro
        x = np.frombuffer(pcm, "<i2").astype(np.float64) if isinstance(pcm, bytes) else np.asarray(pcm, np.float64)
        buf = np.concatenate([self.tail, x])
        self.tail = buf[len(buf) - len(self.tail):]
        y = np.convolve(buf, self.h, "valid")[self.phase::4]
        self.phase = (self.phase - len(x)) % 4
        self.recent = np.concatenate([self.recent, y])[-2 * FS:]
        return y

    def spectrum(self) -> tuple[np.ndarray, np.ndarray]:
        x = self.recent
        n = 1 << int(np.log2(max(len(x), 256)))
        segs = x[: len(x) // 1024 * 1024].reshape(-1, 1024) if len(x) >= 1024 else x[None, :n]
        p = (np.abs(np.fft.rfft(segs * np.hanning(segs.shape[1]), axis=1)) ** 2).mean(axis=0)
        return np.fft.rfftfreq(segs.shape[1], 1 / FS), p


class _Mixer:
    """Porta un tono a 0 Hz e ne filtra i dintorni (continuo tra un blocco e l'altro)."""

    def __init__(self, bw: float) -> None:
        self.h = _lowpass(bw, FS, 81).astype(np.complex128)
        self.tail = np.zeros(len(self.h) - 1, np.complex128)
        self.n = 0

    def __call__(self, x: np.ndarray, f: float) -> np.ndarray:
        z = x * np.exp(-2j * np.pi * f * (self.n + np.arange(len(x))) / FS)
        self.n += len(x)
        buf = np.concatenate([self.tail, z])
        self.tail = buf[len(buf) - len(self.tail):]
        return np.convolve(buf, self.h, "valid")


# --- Morse ----------------------------------------------------------------------------

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I",
    ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6",
    "--...": "7", "---..": "8", "----.": "9", ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/",
    "-...-": "=", ".-.-.": "+", "-....-": "-", ".--.-.": "@", "---...": ":", "-.--.": "(", "-.--.-": ")",
    "...-.-": "<SK>", "-.-.-": "<KA>", "........": "<ERR>",
}


class MorseDecoder:
    """Audio → testo Morse. Il tono e la velocità si trovano da soli."""

    STEP = 16  # campioni a 8 kHz per punto dell'inviluppo (2 ms)

    def __init__(self) -> None:
        self.audio = _Audio()
        self.mix = _Mixer(60)
        self.tone = 0.0
        self.since_tune = 0
        self.levels = np.zeros(0)
        self.state = False
        self.run = 0  # punti dell'inviluppo nello stato attuale
        self.dot = 30.0  # durata del punto, in punti dell'inviluppo (60 ms = 20 parole al minuto)
        self.symbol = ""
        self.word_done = True
        self.carry = np.zeros(0, np.complex128)

    def _find_tone(self) -> None:
        f, p = self.audio.spectrum()
        sel = (f > 250) & (f < 1500)
        if not sel.any():
            return
        i = int(np.argmax(p[sel]))
        # rispetto all'80° percentile: col filtro CW stretto il rumore copre solo una parte della banda guardata
        if p[sel][i] > 10 * np.percentile(p[sel], 80):
            new = float(f[sel][i])
            if abs(new - self.tone) > 30:
                self.tone = new

    def status(self) -> dict[str, Any]:
        wpm = 1200 / (self.dot * self.STEP / FS * 1000) if self.dot else 0
        return {"tone_hz": round(self.tone), "wpm": round(wpm), "info": f"tono {round(self.tone)} Hz · ~{round(wpm)} parole/min"
                if self.tone else "in cerca del tono…"}

    def feed(self, pcm: bytes | np.ndarray) -> str:
        x = self.audio.feed(pcm)
        self.since_tune += len(x)
        if self.since_tune >= FS // 2 or not self.tone:
            self.since_tune = 0
            had = self.tone
            self._find_tone()
            if self.tone and not had:  # tono appena trovato: si riprende dall'ultimo secondo, col primo segno intero
                x = self.audio.recent[-FS:]
        if not self.tone:
            return ""
        z = np.concatenate([self.carry, self.mix(x, self.tone)])
        k = len(z) // self.STEP
        self.carry = z[k * self.STEP:]
        env = np.abs(z[: k * self.STEP]).reshape(k, self.STEP).mean(axis=1)
        self.levels = np.concatenate([self.levels, env])[-1500:]  # ultimi 3 s
        # il massimo recente conta anche quando i segni sono pochi (es. il primo dopo una pausa lunga)
        lo = np.percentile(self.levels, 15)
        hi = max(np.percentile(self.levels, 92), 0.6 * float(self.levels.max()))
        if hi < 8 * lo + 1e-9:  # il solo rumore arriva a ~7 volte il suo 15° percentile
            # niente di abbastanza forte: si chiude il carattere in corso, se c'è
            return self._gap(1000) if self.state is False and self.symbol else ""
        on_th, off_th = lo + 0.55 * (hi - lo), lo + 0.4 * (hi - lo)
        out = []
        for v in env:
            new = v > (off_th if self.state else on_th)
            if new == self.state:
                self.run += 1
                if not self.state and self.symbol and self.run > 2.2 * self.dot:
                    out.append(self._char())
                if not self.state and not self.word_done and self.run > 6 * self.dot:
                    out.append(" ")
                    self.word_done = True
                continue
            if self.state:
                self._mark(self.run)
            self.state, self.run = new, 1
        return "".join(out)

    def _mark(self, n: int) -> None:
        if n < max(2.0, 0.3 * self.dot):  # disturbo, non un punto
            return
        if n < 2 * self.dot:
            self.symbol += "."
            self.dot = 0.85 * self.dot + 0.15 * n
        else:
            self.symbol += "-"
            self.dot = 0.85 * self.dot + 0.15 * n / 3
        self.dot = min(max(self.dot, 10.0), 120.0)  # tra 5 e 60 parole al minuto
        self.word_done = False

    def _char(self) -> str:
        c = MORSE.get(self.symbol, "_" if self.symbol else "")
        self.symbol = ""
        return c

    def _gap(self, _n: int) -> str:
        return self._char()


# --- RTTY ------------------------------------------------------------------------------

LTRS = "\x00E\nA SIU\rDRJNFCKTZLWHYPQOBG\x0eMXV\x0f"
FIGS = "\x003\n- '87\r$4\x07,!:(5\")2#6019?&\x0e./;\x0f"


class RttyDecoder:
    """Audio → testo RTTY (Baudot ITA2, 5 bit, start e stop)."""

    def __init__(self, baud: float = 45.45, shift: float = 170.0) -> None:
        self.baud, self.shift = baud, shift
        self.audio = _Audio()
        self.mix = _Mixer(shift / 2 + baud)
        self.center = 0.0
        self.since_tune = 0
        self.prev: complex | None = None
        self.bits = np.zeros(0)  # discriminatore filtrato (positivo = tono alto)
        self.spb = FS / baud  # campioni per bit
        self.uarts = [_Uart(False), _Uart(True)]  # polarità normale e invertita
        self.best = 0

    def _find_tones(self) -> None:
        f, p = self.audio.spectrum()
        df = f[1] - f[0]
        k = round(self.shift / df)
        sel = np.nonzero((f > 300) & (f + self.shift < 3200))[0]
        if not len(sel) or k < 1:
            return
        score = p[sel] + p[sel + k]
        i = int(np.argmax(score))
        if score[i] > 8 * np.percentile(score, 80):
            new = float(f[sel][i]) + self.shift / 2
            if abs(new - self.center) > 15:
                self.center = new

    def status(self) -> dict[str, Any]:
        u = self.uarts[self.best]
        return {"tone_hz": round(self.center),
                "info": (f"toni {round(self.center - self.shift / 2)}/{round(self.center + self.shift / 2)} Hz · "
                         f"{self.baud:g} baud · {'invertito' if u.invert else 'normale'} · errori {u.errors_pct()}%")
                if self.center else "in cerca dei due toni…"}

    def feed(self, pcm: bytes | np.ndarray) -> str:
        x = self.audio.feed(pcm)
        self.since_tune += len(x)
        if self.since_tune >= FS or not self.center:
            self.since_tune = 0
            self._find_tones()
        if not self.center:
            return ""
        z = self.mix(x, self.center)
        prev = z[0] if self.prev is None else self.prev
        d = np.angle(z * np.conj(np.concatenate([[prev], z[:-1]])))
        self.prev = z[-1]
        w = max(1, int(self.spb * 0.6))
        d = np.convolve(np.concatenate([self.bits[-w:] if len(self.bits) else np.zeros(w), d]), np.ones(w) / w, "valid")[1:]
        texts = [u.feed(d, self.spb) for u in self.uarts]
        # vale la polarità con meno errori di trama recenti
        self.best = int(np.argmin([u.err for u in self.uarts]))
        self.bits = d[-w:]
        return texts[self.best]


class _Uart:
    """Ricevitore asincrono a 5 bit: bit di start (spazio), 5 dati, stop (segno)."""

    def __init__(self, invert: bool) -> None:
        self.invert = invert
        self.buf = np.zeros(0)
        self.figs = False
        self.err = 0.0  # errori di trama recenti (media mobile)
        self.chars = 0

    def errors_pct(self) -> int:
        return round(self.err * 100)

    def feed(self, d: np.ndarray, spb: float) -> str:
        v = -d if self.invert else d
        self.buf = np.concatenate([self.buf, v])
        out = []
        i = 1
        need = int(7.5 * spb)
        while i + need < len(self.buf):
            if not (self.buf[i - 1] > 0 >= self.buf[i]):  # fronte segno → spazio: forse un bit di start
                i += 1
                continue
            c = [self.buf[int(i + (k + 0.5) * spb)] for k in range(7)]
            if c[0] > 0:  # il bit di start non regge: disturbo
                i += 1
                continue
            ok = c[6] > 0
            self.err = 0.95 * self.err + 0.05 * (0.0 if ok else 1.0)
            if ok:
                code = sum((1 << k) for k in range(5) if c[1 + k] > 0)
                ch = (FIGS if self.figs else LTRS)[code]
                if ch == "\x0e":  # FIGS (codice 27)
                    self.figs = True
                elif ch == "\x0f":  # LTRS (codice 31)
                    self.figs = False
                elif ch not in ("\x00", "\r", "\x07"):
                    out.append(ch)
                i += int(6.5 * spb)
            else:
                i += int(spb / 2)
        self.buf = self.buf[max(0, i - 1):]
        return "".join(out)


DECODERS = {
    "cw": lambda: MorseDecoder(),
    "rtty45": lambda: RttyDecoder(45.45, 170.0),
    "rtty50": lambda: RttyDecoder(50.0, 450.0),
}
