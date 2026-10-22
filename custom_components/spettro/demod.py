"""Demodulazione AM / FM della registrazione IQ, per ascoltarla nel pannello.

Solo numpy: i filtri sono "a mattone" nel dominio della frequenza, applicati a
blocchi sovrapposti per non tenere in memoria FFT enormi.
"""

from __future__ import annotations

import io
import wave

import numpy as np

AUDIO_RATE = 32_000
BLOCK_OUT = 4096  # campioni in uscita per blocco della prima decimazione
MARGIN_OUT = 256  # margine scartato ai lati di ogni blocco (assestamento del filtro)

# modo: (frequenza intermedia desiderata, mezza banda del canale, banda audio)
MODES: dict[str, tuple[float, float, float]] = {
    "wfm": (256e3, 125e3, 15e3),  # radio FM broadcast (canale largo: serve anche la sottoportante RDS)
    "nfm": (32e3, 7.5e3, 4e3),  # FM a banda stretta: radioamatori, PMR, marina
    "am": (32e3, 5e3, 4.5e3),  # banda aerea, onde medie/corte
}
DEEMPHASIS_S = 50e-6  # de-enfasi della FM broadcast in Europa


def _channel(
    iq: np.ndarray, rate: int, offset_hz: float, decim: int, half_bw: float, first_index: int = 0
) -> np.ndarray:
    """Porta il segnale in banda base, lo filtra e lo decima di `decim`.

    first_index è la posizione del primo campione nel flusso: in diretta, blocchi successivi
    restano in fase tra loro.
    """
    blk, m = BLOCK_OUT * decim, MARGIN_OUT * decim
    length = blk + 2 * m
    n_in = len(iq)
    freqs = np.fft.fftfreq(length, 1 / rate)
    keep = np.abs(freqs) < half_bw
    lo = length // decim
    out_idx = np.nonzero(keep)[0]
    out_pos = np.where(out_idx < length // 2, out_idx, lo - (length - out_idx))
    parts = []
    for start in range(0, n_in, blk):
        a, b = start - m, start + blk + m
        seg = np.zeros(length, np.complex64)
        s0, s1 = max(a, 0), min(b, n_in)
        idx = np.arange(s0, s1) + first_index
        seg[s0 - a : s1 - a] = iq[s0:s1] * np.exp(-2j * np.pi * offset_hz * idx / rate).astype(np.complex64)
        spec = np.fft.fft(seg)
        z = np.zeros(lo, np.complex64)
        z[out_pos] = spec[out_idx]
        y = np.fft.ifft(z) * (lo / length)
        take = min(BLOCK_OUT, (n_in - start + decim - 1) // decim)
        parts.append(y[MARGIN_OUT : MARGIN_OUT + take].astype(np.complex64))
    return np.concatenate(parts)


def _to_audio(x: np.ndarray, rate: float, cutoff: float, deemph: bool) -> np.ndarray:
    """Filtra la banda audio e ricampiona a AUDIO_RATE."""
    x = x - x.mean()
    n_out = int(round(len(x) * AUDIO_RATE / rate))
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / rate)
    spec[f > cutoff] = 0
    if deemph:
        spec /= 1 + 2j * np.pi * f * DEEMPHASIS_S
    keep = n_out // 2 + 1
    spec = spec[:keep] if len(spec) >= keep else np.pad(spec, (0, keep - len(spec)))
    return np.fft.irfft(spec, n_out) * (n_out / len(x))


def _wav(audio: np.ndarray) -> bytes:
    peak = float(np.max(np.abs(audio))) or 1.0
    pcm = np.clip(audio / peak * 0.9 * 32767, -32767, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def demodulate(iq: np.ndarray, rate: int, offset_hz: float, mode: str) -> bytes:
    """WAV mono a 32 kHz del segnale che sta a `offset_hz` dal centro della registrazione."""
    if mode == "wfm":
        return wfm_audio(*fm_multiplex(iq, rate, offset_hz))
    target, half_bw, audio_bw = MODES[mode]
    decim = max(1, int(round(rate / target)))
    z = _channel(iq, rate, offset_hz, decim, half_bw)
    mid_rate = rate / decim
    if mode == "am":
        x = np.abs(z).astype(np.float64)
    else:
        x = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float64)  # discriminatore FM
    return _wav(_to_audio(x, mid_rate, audio_bw, deemph=False))


def fm_multiplex(iq: np.ndarray, rate: int, offset_hz: float) -> tuple[np.ndarray, float]:
    """Uscita del discriminatore FM larga: il multiplex completo (audio, pilota, stereo, RDS)."""
    target, half_bw, _ = MODES["wfm"]
    decim = max(1, int(round(rate / target)))
    z = _channel(iq, rate, offset_hz, decim, half_bw)
    return np.angle(z[1:] * np.conj(z[:-1])).astype(np.float64), rate / decim


def wfm_audio(mpx: np.ndarray, fs: float) -> bytes:
    """Audio mono (L+R) del multiplex, con la de-enfasi europea."""
    return _wav(_to_audio(mpx, fs, MODES["wfm"][2], deemph=True))
