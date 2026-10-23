"""Demodulazione continua per l'analisi in diretta: audio, cascata e multiplex FM a blocchi.

I blocchi di campioni IQ arrivano uno dopo l'altro; il filtro di canale lavora
su ogni blocco più una coda del precedente e scarta i bordi, così l'uscita è
continua (niente clic tra un blocco e l'altro). Il filtro audio è un FIR con la
sua coda; per la FM broadcast include la de-enfasi europea di 50 µs.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .demod import AUDIO_RATE, DEEMPHASIS_S, MARGIN_OUT, MODES, SSB_MODES, _channel, ssb_shift

LIVE_RATE = 1_024_000  # 4 × 256 kHz e 32 × 32 kHz: tutte le decimazioni sono intere
CHUNK_S = 0.25
CHUNK = int(LIVE_RATE * CHUNK_S)
ROW_BINS = 256  # punti di ogni riga della cascata in diretta
AGC_TARGET = 0.25  # livello RMS dell'audio in uscita (fondo scala = 1)
AGC_MAX_GAIN = 40.0


def _lowpass(cutoff: float, fs: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(taps)
    return h / h.sum()


class LiveDemod:
    """Stato della demodulazione di un segnale in diretta."""

    def __init__(self, rate: int, offset_hz: float, mode: str) -> None:
        target, self.half_bw, audio_bw = MODES[mode]
        self.rate, self.offset, self.mode = rate, offset_hz, mode
        self.shift = ssb_shift(mode) if mode in SSB_MODES else 0.0
        self.offset += self.shift  # SSB: si filtra la banda audio, non la portante
        self.out_index = 0
        self.decim = max(1, int(round(rate / target)))
        self.mid = rate / self.decim
        self.margin = MARGIN_OUT * self.decim
        self.tail = np.zeros(2 * self.margin, np.complex64)
        self.first = -2 * self.margin  # posizione nel flusso del primo campione di tail
        self.prev: complex | None = None
        self.adecim = max(1, int(round(self.mid / AUDIO_RATE)))
        taps = int(3.3 * self.mid / max(audio_bw * 0.3, 500)) | 1
        h = _lowpass(audio_bw, self.mid, taps)
        if mode == "wfm":
            t = np.arange(int(8 * DEEMPHASIS_S * self.mid) + 1) / self.mid
            de = np.exp(-t / DEEMPHASIS_S)
            h = np.convolve(h, de / de.sum())
        self.h = h
        self.atail = np.zeros(len(h) - 1)
        self.dc = 0.0
        self.rms: float | None = None

    def process(self, iq: np.ndarray) -> dict[str, Any]:
        """Un blocco IQ: audio PCM a 32 kHz, multiplex (FM larga), riga di cascata e livello."""
        buf = np.concatenate((self.tail, iq))
        z = _channel(buf, self.rate, self.offset, self.decim, self.half_bw, self.first)
        z = z[MARGIN_OUT : len(z) - MARGIN_OUT]
        self.first += len(iq)
        self.tail = buf[-2 * self.margin :]

        if self.shift:
            n = self.out_index + np.arange(len(z))
            x = (z * np.exp(2j * np.pi * self.shift * n / self.mid)).real.astype(np.float64)
            self.out_index += len(z)
        elif self.mode == "am":
            x = np.abs(z).astype(np.float64)
            self.dc = x.mean() if self.prev is None else 0.9 * self.dc + 0.1 * x.mean()
            x -= self.dc
        else:
            prev = z[0] if self.prev is None else self.prev
            x = np.angle(z * np.conj(np.concatenate(([prev], z[:-1])))).astype(np.float64)
        self.prev = z[-1]

        xa = np.concatenate((self.atail, x))
        self.atail = xa[len(xa) - (len(self.h) - 1) :]
        y = np.convolve(xa, self.h, "valid")[:: self.adecim]
        rms = float(np.sqrt(np.mean(y**2))) + 1e-9
        self.rms = rms if self.rms is None else 0.7 * self.rms + 0.3 * rms
        gain = min(AGC_TARGET / self.rms, AGC_MAX_GAIN)
        pcm = (np.tanh(y * gain) * 32000).astype("<i2")

        frames = len(z) // ROW_BINS
        spec = np.fft.fftshift(np.fft.fft(z[: frames * ROW_BINS].reshape(frames, ROW_BINS)
                                          * np.hanning(ROW_BINS), axis=1), axes=1)
        row = 10 * np.log10(np.mean(np.abs(spec) ** 2, axis=0) / (127.5 * ROW_BINS / 2) ** 2 + 1e-20)
        level = float(10 * np.log10(np.mean(np.abs(z) ** 2) / 127.5**2 + 1e-20))
        return {
            "pcm": pcm.tobytes(),
            "mpx": x if self.mode == "wfm" else None,
            "row": np.round(row, 1).tolist(),
            "span_hz": self.mid,
            "level_db": round(level, 1),
        }
