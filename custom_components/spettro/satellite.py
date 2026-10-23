"""TV satellitare attraverso l'LNB: riconoscimento del satellite (Hotbird, Astra…) dai transponder.

La chiavetta non riceve i 10,7–12,75 GHz della parabola ma la frequenza intermedia
che l'LNB manda sul cavo (950–2150 MHz; la chiavetta arriva a ~1766 MHz). Un
transponder DVB-S/S2 è largo 27–36 MHz: nello spettro della scansione è una
"gobba" piatta. I transponder non si decodificano (la chiavetta ne riceve 2 MHz),
ma ogni satellite ha il suo piano di frequenze: confrontando le gobbe con i piani
noti si capisce dove è puntata la parabola, con quale banda e polarizzazione.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# LNB universale: banda bassa 10,70–11,70 GHz (oscillatore 9750 MHz), banda alta 11,70–12,75 GHz
# (10600 MHz, scelta dal tono a 22 kHz); polarizzazione verticale a 13 V, orizzontale a 18 V
BANDS = (("bassa", 9750.0, 10700.0, 11700.0), ("alta", 10600.0, 11700.0, 12750.0))
IF_MIN, IF_MAX = 950.0, 2150.0
POLS = {"H": "orizzontale (18 V)", "V": "verticale (13 V)"}
ROLLOFF = 1.2  # banda occupata ≈ symbol rate × (1 + roll-off)
MAX_LO_ERROR = 8.0  # MHz: errore tipico dell'oscillatore di un LNB economico
MIN_EXCESS_DB = 3.0
MIN_SCORE = 0.35  # correlazione minima con il piano del satellite
MIN_MARGIN = 0.08  # vantaggio minimo sul secondo satellite per dirlo con sicurezza

# transponder larghi (≥ 15 Msym/s) dei satelliti ricevibili dall'Italia: frequenza Ku (MHz),
# polarizzazione e symbol rate (ksym/s). Fonte: satellites.xml di Enigma2 (oe-alliance), ottobre 2026.
SATELLITES: dict[str, list[tuple[int, str, int]]] = {
    "Eutelsat 5°W": [
        (10972, "V", 29950), (11054, "V", 29950), (11096, "V", 29950), (11471, "V", 29950), (11512, "V", 29950),
        (11554, "V", 29950), (11679, "V", 29950), (12522, "V", 29950), (12536, "H", 35300), (12564, "V", 35300),
        (12585, "H", 35500), (12606, "V", 35300), (12627, "H", 35295), (12648, "V", 29500), (12690, "V", 35296),
        (12732, "V", 29500),
    ],
    "Astra 4,8°E": [
        (11727, "H", 27500), (11747, "V", 27500), (11766, "H", 27500), (11766, "H", 30000), (11785, "V", 27500),
        (11804, "H", 27500), (11862, "V", 27500), (11900, "V", 27500), (11919, "H", 27500), (11938, "V", 27500),
        (11958, "H", 27500), (11977, "V", 27500), (11996, "H", 27500), (12015, "V", 29950), (12034, "H", 27500),
        (12054, "V", 27500), (12054, "V", 30000), (12073, "H", 27500), (12073, "H", 30000), (12130, "V", 27500),
        (12207, "V", 30000), (12303, "H", 27500), (12380, "H", 27500), (12418, "H", 27500), (12437, "V", 30000),
        (12565, "V", 30000),
    ],
    "Eutelsat 7°E": [
        (10721, "V", 27500), (10971, "H", 30000), (10981, "V", 45000), (11044, "V", 45000), (11106, "V", 45000),
        (11169, "V", 45000), (11285, "V", 30000), (11410, "V", 30000), (11481, "V", 35000), (11554, "H", 30000),
        (11595, "H", 30000), (11637, "H", 30000), (11669, "V", 45000),
    ],
    "Eutelsat 9°E": [
        (11727, "V", 30000), (11785, "H", 27500), (11823, "H", 27500), (11843, "V", 27500), (11861, "H", 27500),
        (11881, "V", 30000), (11900, "H", 27500), (11919, "V", 30000), (11938, "H", 27500), (11958, "V", 27500),
        (11977, "H", 30000), (11996, "V", 27500), (12015, "H", 27500), (12034, "V", 27500), (12054, "H", 27500),
        (12092, "H", 27500), (12111, "V", 31400), (12149, "V", 31400), (12169, "H", 27500), (12207, "H", 27500),
        (12226, "V", 30000), (12226, "V", 31400), (12245, "H", 27500), (12284, "H", 27500), (12303, "V", 30000),
        (12303, "V", 31400), (12341, "V", 31400), (12380, "V", 31400), (12399, "H", 27500), (12466, "V", 41950),
        (12475, "H", 27500),
    ],
    "Hotbird 13°E": [
        (10719, "V", 27500), (10727, "H", 30000), (10758, "V", 27500), (10775, "H", 29900), (10796, "V", 27500),
        (10873, "V", 27500), (10892, "H", 27500), (10911, "V", 27500), (10930, "H", 30000), (10949, "V", 27500),
        (10971, "H", 29700), (10992, "V", 27500), (11013, "H", 29900), (11034, "V", 27500), (11117, "V", 27500),
        (11137, "H", 27500), (11158, "V", 27500), (11179, "H", 27500), (11258, "H", 27500), (11278, "V", 27500),
        (11296, "H", 27500), (11334, "H", 27500), (11373, "H", 27500), (11393, "V", 27500), (11411, "H", 27500),
        (11432, "V", 29900), (11449, "H", 27500), (11488, "H", 27500), (11508, "V", 27500), (11566, "H", 29900),
        (11585, "V", 27500), (11662, "V", 27500), (11727, "V", 29900), (11747, "H", 27500), (11766, "V", 29900),
        (11785, "H", 27500), (11804, "V", 30000), (11823, "H", 27500), (11843, "V", 29900), (11862, "H", 29900),
        (11881, "V", 27500), (11900, "H", 27500), (11919, "V", 29900), (11938, "H", 30000), (11958, "V", 27500),
        (11996, "V", 29900), (12015, "H", 27500), (12034, "V", 29900), (12073, "V", 29900), (12111, "V", 27500),
        (12130, "H", 30000), (12149, "V", 27500), (12169, "H", 30000), (12188, "V", 27500), (12265, "V", 27500),
        (12284, "H", 27500), (12303, "V", 27500), (12322, "H", 27500), (12341, "V", 29900), (12380, "V", 30000),
        (12399, "H", 29700), (12418, "V", 29900), (12466, "V", 29900), (12476, "H", 29900), (12520, "V", 27500),
        (12558, "V", 27500), (12577, "H", 27500), (12616, "H", 30000), (12635, "V", 30000), (12654, "H", 30000),
        (12673, "V", 30000), (12713, "V", 30000), (12731, "H", 30000),
    ],
    "Eutelsat 16°E": [
        (10721, "H", 30000), (10762, "H", 30000), (10804, "H", 29950), (10804, "H", 30000), (10845, "H", 30000),
        (10887, "H", 30000), (10928, "H", 30000), (11055, "V", 27500), (11094, "V", 27500), (11139, "V", 27500),
        (11178, "V", 27500), (11216, "H", 22500), (11231, "V", 42000), (11262, "H", 30000), (11283, "V", 30000),
        (11324, "V", 30000), (11366, "V", 30000), (11387, "H", 32000), (11402, "V", 17500), (11429, "V", 28330),
        (11471, "H", 30000), (11471, "V", 30000), (11512, "H", 30000), (11512, "V", 29950), (11554, "H", 30000),
        (11554, "V", 30000), (11595, "H", 30000), (11595, "V", 30000), (11637, "H", 30000), (11637, "V", 30000),
        (11678, "H", 30000), (11678, "V", 30000), (12562, "H", 30000), (12568, "V", 16667), (12604, "H", 30000),
        (12687, "H", 30000),
    ],
    "Astra 19,2°E": [
        (10729, "V", 23500), (10758, "V", 22000), (10773, "H", 22000), (10788, "V", 22000), (10803, "H", 22000),
        (10818, "V", 22000), (10832, "H", 22000), (10847, "V", 22000), (10876, "V", 22000), (10891, "H", 22000),
        (10906, "V", 22000), (10921, "H", 22000), (10936, "V", 22000), (10964, "H", 22000), (10979, "V", 22000),
        (10994, "H", 22000), (11009, "V", 23500), (11038, "V", 22000), (11053, "H", 22000), (11068, "V", 22000),
        (11082, "H", 22000), (11097, "V", 22000), (11112, "H", 22000), (11127, "V", 22000), (11156, "V", 22000),
        (11171, "H", 22000), (11186, "V", 22000), (11229, "V", 22000), (11244, "H", 22000), (11259, "V", 22000),
        (11273, "H", 22000), (11288, "V", 22000), (11303, "H", 22000), (11318, "V", 22000), (11347, "V", 22000),
        (11362, "H", 22000), (11377, "V", 22000), (11391, "H", 22000), (11464, "H", 22000), (11494, "H", 22000),
        (11523, "H", 22000), (11538, "V", 22000), (11553, "H", 22000), (11582, "H", 22000), (11671, "H", 22000),
        (11739, "V", 27500), (11758, "H", 27500), (11778, "V", 29500), (11798, "H", 27500), (11836, "H", 27500),
        (11856, "V", 29700), (11895, "V", 29700), (11914, "H", 27500), (11934, "V", 29700), (11954, "H", 27500),
        (11973, "V", 27500), (11992, "H", 27500), (12012, "V", 29700), (12032, "H", 27500), (12051, "V", 27500),
        (12090, "V", 29700), (12110, "H", 27500), (12148, "H", 27500), (12168, "V", 29700), (12188, "H", 27500),
        (12207, "V", 29700), (12226, "H", 27500), (12285, "V", 29700), (12304, "H", 27500), (12324, "V", 29700),
        (12344, "H", 27500), (12363, "V", 27500), (12382, "H", 27500), (12402, "V", 29700), (12422, "H", 27500),
        (12460, "H", 27500), (12480, "V", 27500), (12515, "H", 22000), (12545, "H", 22000), (12552, "V", 22000),
        (12574, "H", 22000), (12604, "H", 22000), (12610, "V", 22000), (12633, "H", 22000), (12640, "V", 23500),
        (12663, "H", 22000), (12670, "V", 23500),
    ],
    "Astra 23,5°E": [
        (11739, "V", 29900), (11856, "V", 29900), (11914, "H", 30000), (11934, "V", 29900), (11973, "V", 29900),
        (12012, "V", 29900), (12032, "H", 27500), (12070, "H", 27500), (12090, "V", 29900), (12110, "H", 29900),
        (12129, "V", 29900), (12148, "H", 30000), (12168, "V", 27500), (12188, "H", 29900), (12246, "V", 30000),
        (12266, "H", 29900), (12285, "V", 30000), (12304, "H", 30000), (12344, "H", 29900), (12363, "V", 29500),
        (12402, "V", 30000), (12422, "H", 30000), (12441, "V", 30000), (12460, "H", 30000), (12480, "V", 30000),
    ],
    "Astra 28,2°E": [
        (10773, "H", 23000), (10788, "V", 23000), (10818, "V", 23000), (10847, "V", 23000), (10936, "V", 23000),
        (11023, "H", 23000), (11082, "H", 23000), (11095, "V", 30000), (11112, "H", 23000), (11135, "V", 30000),
        (11141, "H", 23000), (11224, "H", 27500), (11224, "V", 23000), (11264, "V", 27500), (11305, "H", 27500),
        (11306, "V", 27500), (11344, "H", 27500), (11344, "V", 27500), (11386, "H", 27500), (11386, "V", 27500),
        (11426, "H", 27500), (11426, "V", 29500), (11494, "H", 22000), (11509, "V", 23000), (11523, "H", 23000),
        (11553, "H", 23000), (11582, "H", 23000), (11595, "V", 30000), (11597, "V", 23000), (11627, "V", 23000),
        (11635, "H", 30000), (11635, "V", 30000), (11641, "H", 23000), (11656, "V", 22000), (11671, "H", 23000),
        (11675, "H", 30000), (11675, "V", 30000), (11686, "V", 23000), (11758, "H", 27500), (11778, "V", 27500),
        (12012, "V", 27500), (12032, "H", 29500), (12090, "V", 29500), (12110, "H", 27500), (12129, "V", 27500),
        (12148, "H", 27500), (12168, "V", 27500), (12188, "H", 27500), (12207, "V", 27500), (12226, "H", 27500),
        (12246, "V", 27500), (12266, "H", 27500), (12285, "V", 27500), (12324, "V", 27500), (12363, "V", 27500),
        (12382, "H", 29500), (12525, "V", 30000), (12605, "V", 30000), (12725, "V", 30000),
    ],
}


def _grid(freqs_mhz: np.ndarray, values_db: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Spettro della scansione su una griglia da 1 MHz (massimo di ogni MHz) dentro la banda dell'LNB."""
    lo, hi = max(IF_MIN, float(freqs_mhz[0])), min(IF_MAX, float(freqs_mhz[-1]))
    if hi - lo < 150:
        return None
    grid = np.arange(np.ceil(lo), np.floor(hi) + 1)
    idx = np.clip(np.round(freqs_mhz - grid[0]).astype(int), 0, len(grid) - 1)
    keep = (freqs_mhz >= grid[0] - 0.5) & (freqs_mhz <= grid[-1] + 0.5)
    out = np.full(len(grid), -np.inf)
    np.maximum.at(out, idx[keep], values_db[keep])
    ok = np.isfinite(out)
    if ok.sum() < len(grid) * 0.8:
        return None
    out[~ok] = np.interp(grid[~ok], grid[ok], out[ok])
    return grid, out


def _excess(v: np.ndarray, half: int = 60) -> np.ndarray:
    """dB sopra il rumore locale (8° percentile su ±60 MHz): la sensibilità cala salendo in frequenza.

    Con i transponder fitti (Hotbird) tra una gobba e l'altra resta poco rumore: serve un percentile basso.
    """
    pad = np.pad(v, half, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    return np.clip(v - np.percentile(win, 8, axis=1), 0, 12)


def identify(freqs_mhz: np.ndarray, values_db: np.ndarray) -> dict[str, Any] | None:
    """Satellite più compatibile con le gobbe dello spettro (frequenze in MHz sul cavo), o None."""
    g = _grid(np.asarray(freqs_mhz, float), np.asarray(values_db, float))
    if g is None:
        return None
    grid, v = g
    ex = _excess(v)
    if (ex >= MIN_EXCESS_DB).mean() < 0.08:
        return {"found": False, "confident": False, "reason": "nessun transponder sopra il rumore"}
    hump = np.convolve(ex >= MIN_EXCESS_DB, np.ones(3) / 3, "same")  # 1 dove c'è una gobba
    total = float(hump.sum())
    deltas = np.arange(-MAX_LO_ERROR, MAX_LO_ERROR + 0.25, 0.5)
    results = []
    for name, tps in SATELLITES.items():
        for band, lo, k_lo, k_hi in BANDS:
            for pol in "HV":
                sel = [(f - lo, s / 1000 * ROLLOFF, f, s) for f, p, s in tps if p == pol and k_lo <= f < k_hi]
                sel = [t for t in sel if grid[0] + t[1] / 2 <= t[0] <= grid[-1] - t[1] / 2]
                if len(sel) < 3:
                    continue
                best = (-1.0, 0.0)
                for d in deltas:
                    t = np.zeros(len(grid))
                    on = 0
                    for c, w, _f, _s in sel:
                        m = np.abs(grid - (c + d)) <= w / 2
                        t[m] = 1
                        core = np.abs(grid - (c + d)) <= w / 4
                        on += bool(core.any() and hump[core].mean() > 0.6)
                    cov = float(t.mean())
                    if cov >= 0.95:
                        continue
                    # quanta parte delle gobbe cade dove il satellite ha i transponder (oltre il caso)
                    # e quanti dei suoi transponder si vedono; i transponder spenti pesano meno
                    precision = float((hump * t).sum()) / total
                    lift = (precision - cov) / (1 - cov)
                    score = lift * (0.4 + 0.6 * on / len(sel))
                    if score > best[0]:
                        best = (score, float(d))
                results.append((best[0], best[1], name, band, lo, pol, sel))
    if not results:
        return {"found": False, "confident": False, "reason": "intervallo di scansione troppo stretto per riconoscere il satellite"}
    results.sort(key=lambda r: -r[0])
    score, delta, name, band, lo, pol, sel = results[0]
    second = next((r[0] for r in results if r[2] != name), 0.0)
    transponders = []
    for c, w, f, s in sel:
        i = np.abs(grid - (c + delta)) <= w / 4
        transponders.append({"ku_mhz": f, "if_mhz": round(c + delta, 1), "sr": s,
                             "above_db": round(float(np.median(ex[i])) if i.any() else 0.0, 1)})
    present = sum(t["above_db"] >= MIN_EXCESS_DB for t in transponders)
    found = score >= MIN_SCORE and present >= 3
    return {
        "found": found,
        "confident": found and score - second >= MIN_MARGIN,
        "satellite": name,
        "band": band,
        "lo_mhz": lo,
        "polarization": POLS[pol],
        "lo_offset_mhz": round(delta, 1),
        "score": round(score, 2),
        "present": present,
        "transponders": transponders,
        "ranking": [{"satellite": r[2], "band": r[3], "polarization": POLS[r[5]], "score": round(r[0], 2)}
                    for r in results[:4]],
        "reason": None if found else "le gobbe non corrispondono a nessun satellite noto",
    }
