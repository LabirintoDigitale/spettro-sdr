"""TV digitale terrestre (DVB-T/DVB-T2): canali e misure di un multiplex.

La chiavetta in modalità rtl_tcp cattura al massimo ~2,4 MHz dei 7–8 MHz di un
multiplex, quindi i programmi non si possono decodificare. Si può però misurare:
ogni simbolo OFDM ripete in testa (intervallo di guardia) la sua parte finale, e
questo vale anche per la sola fetta di portanti che la chiavetta riceve. La
correlazione tra il segnale e sé stesso ritardato della durata utile del simbolo
rivela così la modalità (1k…32k), l'intervallo di guardia e quindi lo standard;
quanto è forte la correlazione dice quanto il segnale è pulito (MER stimato).
"""

from __future__ import annotations

from typing import Any

import numpy as np

# canali UHF in uso per la TV in Italia (21–48: dal 2022 i 49–60 sono del 5G) e VHF banda III
# con il piano italiano (D…H2, 7 MHz); centro canale in MHz e larghezza in MHz
CHANNELS: dict[str, tuple[float, int]] = {
    **{name: (mhz, 7) for name, mhz in (("D", 177.5), ("E", 186.0), ("F", 194.5), ("G", 203.5),
                                         ("H", 212.5), ("H1", 219.5), ("H2", 226.5))},
    **{str(n): (474.0 + 8 * (n - 21), 8) for n in range(21, 49)},
}

# modalità (portanti della FFT) e intervalli di guardia: DVB-T ha solo 2k/8k e 1/4…1/32
MODES = {1024: "1k", 2048: "2k", 4096: "4k", 8192: "8k", 16384: "16k", 32768: "32k"}
T_MODES = {2048, 8192}
GUARDS: list[tuple[str, float]] = [("1/4", 1 / 4), ("19/128", 19 / 128), ("1/8", 1 / 8), ("19/256", 19 / 256),
                                   ("1/16", 1 / 16), ("1/32", 1 / 32), ("1/128", 1 / 128)]
T_GUARDS = {"1/4", "1/8", "1/16", "1/32"}
MIN_RHO = 0.004  # correlazione minima per dire che c'è un multiplex
MIN_CONTRAST = 6.0  # quanto deve superare le altre modalità
MAX_SAMPLES = 4_000_000
SEG_SYMBOLS = 24  # simboli piegati insieme prima di riallinearsi
MER_MAX = 30.0  # oltre, la stima non è affidabile (e la ricezione è comunque ottima)


def channel_for(freq_mhz: float) -> tuple[str, float, int] | None:
    """Canale TV che contiene la frequenza (entro la metà della sua larghezza), se c'è."""
    for name, (mhz, bw) in CHANNELS.items():
        if abs(freq_mhz - mhz) <= bw / 2:
            return name, mhz, bw
    return None


def channel_label(name: str) -> str:
    return f"UHF {name}" if name.isdigit() else f"VHF {name}"


def _corr(x: np.ndarray, lag: int) -> complex:
    return complex(np.vdot(x[lag:], x[:-lag]))  # somma di x[n] · conj(x[n + lag])


def _lags(rate: float) -> list[tuple[int, int, float]]:
    """(portanti, MHz di canale, ritardo in campioni) di tutte le modalità possibili."""
    out = []
    for bw in (8, 7):
        t_elem = {8: 7 / 64, 7: 1 / 8}[bw] * 1e-6  # periodo elementare dello standard
        out += [(k, bw, k * t_elem * rate) for k in MODES]
    return out


def _delay(x: np.ndarray, frac: float) -> np.ndarray:
    """Segnale anticipato di una frazione di campione (sfasamento lineare in frequenza)."""
    f = np.fft.fftfreq(len(x))
    return np.fft.ifft(np.fft.fft(x) * np.exp(2j * np.pi * f * frac)).astype(np.complex64)


def _plateau_end(prof: np.ndarray, width: int) -> int:
    """Punto in cui finisce il tratto lungo `width` con la somma più alta (profilo circolare)."""
    ext = np.r_[prof[-width:], prof]
    return int(np.argmax(np.convolve(ext, np.ones(width), "valid")[1:]))


def _folded(p: np.ndarray, period: float, bins: int, width: int) -> np.ndarray:
    """Correlazione media lungo il simbolo, con il gradino della guardia in fondo al profilo.

    Si piega a tratti di SEG_SYMBOLS simboli, ciascuno riallineato sul proprio gradino: un buco
    nel flusso (campioni persi in rete) sposta l'inizio dei simboli ma non sporca la misura.
    """
    seg = min(len(p), int(period * SEG_SYMBOLS))
    acc = np.zeros(bins)
    starts = range(0, len(p) - seg + 1, seg)
    for s0 in starts:
        n = np.arange(seg, dtype=np.float64)
        idx = ((n % period) * (bins / period)).astype(np.int64)
        prof = np.bincount(idx, p[s0 : s0 + seg], bins) / np.maximum(np.bincount(idx, None, bins), 1)
        acc += np.roll(prof, bins - 1 - _plateau_end(prof, width))
    return acc / len(starts)


def measure(iq: np.ndarray, rate: float, bw_hint: int | None = None,
            known: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Modalità, intervallo di guardia, standard e qualità di un multiplex DVB-T/T2, o None se non c'è.

    `known` è una misura precedente dello stesso multiplex: in diretta si cercano solo le sue
    modalità e guardia, molto più in fretta.
    """
    x = np.asarray(iq[:MAX_SAMPLES], np.complex64)
    x = x - x.mean()
    power = float(np.vdot(x, x).real) / len(x)
    if power <= 0 or len(x) < rate * 0.1:
        return None
    cands = [c for c in _lags(rate) if bw_hint is None or c[1] == bw_hint]
    if known:
        cands = [c for c in cands if c[0] == known["fft"] and c[1] == known["bandwidth_mhz"]] + [
            c for c in cands if c[0] != known["fft"]][:3]  # qualche modalità diversa come confronto
    scores = []
    for _k, _bw, lag in cands:
        lo = int(lag)
        rho = max(abs(_corr(x, lo)), abs(_corr(x, lo + 1))) / (power * (len(x) - lo))
        scores.append(rho)
    best = int(np.argmax(scores))
    others = np.median([s for i, s in enumerate(scores) if i != best])
    if scores[best] < MIN_RHO or scores[best] < MIN_CONTRAST * others:
        return None
    k, bw, lag = cands[best]

    # ritardo frazionario esatto: x[n] · conj(x[n + lag])
    lo = int(lag)
    xs = _delay(x, lag - lo)  # xs[n] = x(n + frazione)
    c = x[: len(x) - lo - 1] * np.conj(xs[lo : len(x) - 1])
    ref = complex(c.sum())
    p = (c * np.conj(ref) / abs(ref)).real  # parte della correlazione in fase con il totale

    # intervallo di guardia: la correlazione "piegata" sul periodo del simbolo giusto mostra il gradino
    chosen: tuple[str, float, np.ndarray, int] | None = None
    best_contrast = -np.inf
    for name, g in GUARDS if not known else [x for x in GUARDS if x[0] == known["guard"]]:
        bins = max(128, int(24 * (1 + g) / g))  # almeno ~24 punti sul gradino, anche con guardia 1/128
        width = max(1, int(round(g / (1 + g) * bins)))
        prof = _folded(p, lag * (1 + g), bins, width)
        contrast = float(prof[-width:].mean() - np.median(prof))
        if contrast > best_contrast:
            chosen, best_contrast = (name, g, prof, width), contrast
    assert chosen is not None
    name, g, prof, width = chosen
    # qualità: correlazione nel 60% centrale del gradino (l'eco dei multipercorsi ne sporca i bordi)
    trim = int(width * 0.2)
    core = prof[len(prof) - width + trim : len(prof) - trim]
    rho_cp = float(np.clip(core.mean() / power, 0, 0.9995))
    mer = 10 * np.log10(rho_cp / (1 - rho_cp)) if rho_cp > 0 else -10.0
    standard = "DVB-T" if k in T_MODES and name in T_GUARDS else "DVB-T2"
    useful_us = lag / rate * 1e6
    guard_samples = lag * g
    return {
        "standard": standard,
        "mode": MODES[k],
        "fft": k,
        "guard": name,
        "bandwidth_mhz": bw,
        "symbol_us": round(useful_us * (1 + g), 1),
        "useful_us": round(useful_us, 1),
        "carrier_spacing_hz": round(1e6 / useful_us, 1),
        "mer_db": round(float(np.clip(mer, -5, MER_MAX)), 1),
        "correlation": round(scores[best], 4),
        # con una guardia corta (pochi campioni) i bordi del gradino pesano: la stima resta per difetto
        "mer_at_least": bool(guard_samples < 100 and mer >= 14),
        "level_db": round(float(10 * np.log10(power / 127.5**2 + 1e-20)), 1),
    }


def quality_label(m: dict[str, Any]) -> str:
    """Giudizio sulla ricezione dal MER stimato (soglie tipiche di 64-QAM in DVB-T e 256-QAM in DVB-T2).

    La stima dalla sola guardia tende a stare un po' sotto il vero: le soglie ne tengono conto.
    """
    mer_db, need = m["mer_db"], min_mer(m["standard"])
    if m.get("mer_at_least") and mer_db < need + 3:
        return "buona o migliore (stima per difetto: intervallo di guardia corto)"
    if mer_db >= need + 8:
        return "ottima"
    if mer_db >= need + 3:
        return "buona"
    if mer_db >= need:
        return "discreta"
    if mer_db >= need - 4:
        return "al limite (possibili blocchi dell'immagine)"
    return "insufficiente"


def min_mer(standard: str) -> float:
    """MER minimo indicativo per vedere senza blocchi."""
    return 18.0 if standard == "DVB-T" else 17.0


def spectrum_row(iq: np.ndarray, bins: int = 256) -> list[float]:
    """Una riga di cascata (dBFS) dalla media di più FFT, per la diretta."""
    n = len(iq) // bins
    blocks = iq[: n * bins].reshape(n, bins) * np.hanning(bins)
    p = (np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2).mean(axis=0)
    p[bins // 2] = (p[bins // 2 - 1] + p[bins // 2 + 1]) / 2  # picco DC della chiavetta
    return np.round(10 * np.log10(p / (127.5 * np.hanning(bins).sum()) ** 2 + 1e-20), 1).tolist()
