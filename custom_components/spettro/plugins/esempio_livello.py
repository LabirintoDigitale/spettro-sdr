"""Plugin di esempio: misuratore del livello audio.

Mostra un pannello HTML con il livello dell'audio (in dB) e una barra, e lascia passare l'audio.
Serve come modello: copialo e cambia `feed()` per fare quello che vuoi (decodificare toni, contare
impulsi, ricavare testo, ecc.). Scegli `"input": "iq"` se preferisci l'IQ grezzo del canale.
"""

from __future__ import annotations

import numpy as np

PLUGIN = {
    "id": "esempio_livello",
    "name": "Esempio · misuratore di livello",
    "mode": "nfm",       # modo di demodulazione consigliato
    "input": "audio",    # riceve l'audio demodulato (float a 32 kHz)
}


class Decoder:
    def __init__(self, freq_hz: float, rate: int) -> None:
        self.freq_hz = freq_hz
        self.rate = rate
        self.peak_db = -99.0

    def feed(self, audio: np.ndarray) -> dict | None:
        if audio is None or len(audio) == 0:
            return None
        rms = float(np.sqrt(np.mean(audio.astype("float64") ** 2)))
        db = 20.0 * np.log10(rms + 1e-9)
        self.peak_db = max(self.peak_db - 0.5, db)  # picco che scende piano
        pct = max(0.0, min(100.0, (db + 60.0) / 60.0 * 100.0))
        html = (
            '<div style="font:13px sans-serif;color:#ddd">'
            f"Frequenza: <b>{self.freq_hz / 1e6:.4f} MHz</b><br>"
            f"Livello audio: <b>{db:.1f} dB</b> · picco {self.peak_db:.1f} dB"
            '<div style="height:12px;background:#333;border-radius:6px;margin-top:6px;overflow:hidden">'
            f'<div style="height:100%;width:{pct:.0f}%;'
            'background:linear-gradient(90deg,#43a047,#ffb300,#e53935)"></div></div>'
            "</div>"
        )
        return {"html": html}  # per far sentire l'audio modificato: {"html": html, "audio": pcm_bytes}
