"""Plugin di esempio con input "baseband".

Riceve il canale GIÀ a banda base (target a 0), filtrato a `bw` e decimato a ~`rate`: è la materia
prima su cui si applica un demodulatore (π/4-DQPSK, FSK, PSK, ecc.). Questo esempio non demodula
niente: mostra solo la frequenza di campionamento effettiva e la potenza del canale, per far vedere
come si riceve il baseband. Copialo e metti il tuo demodulatore dentro `feed()`.
"""

from __future__ import annotations

import numpy as np

PLUGIN = {
    "id": "esempio_baseband",
    "name": "Esempio · canale a banda base",
    "mode": "nfm",          # modo irrilevante per il baseband, ma serve una demodulazione attiva
    "input": "baseband",
    "rate": 48_000,          # frequenza d'uscita desiderata (si usa la più vicina ottenibile)
    "bw": 25_000,            # larghezza del canale da far passare
}


class Decoder:
    def __init__(self, freq_hz: float, rate: int) -> None:
        self.freq_hz = freq_hz
        self.rate = rate        # frequenza effettiva dei campioni complessi che arrivano a feed()
        self.n = 0

    def feed(self, z: np.ndarray) -> dict | None:
        if z is None or len(z) == 0:
            return None
        self.n += len(z)
        power = float(np.mean(np.abs(z) ** 2))
        db = 10.0 * np.log10(power + 1e-20)
        html = (
            '<div style="font:13px sans-serif;color:#ddd">'
            f"Canale a <b>{self.freq_hz / 1e6:.4f} MHz</b><br>"
            f"Campionamento: <b>{self.rate / 1000:.2f} kHz</b> complessi (I/Q)<br>"
            f"Potenza canale: <b>{db:.1f} dB</b> · {len(z)} campioni/blocco · {self.n} totali"
            "</div>"
        )
        return {"html": html}
