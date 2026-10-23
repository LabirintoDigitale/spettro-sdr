"""Radiofax meteo (HF-FAX, es. DWD Pinneberg): carte del tempo dalle onde corte.

Come per l'SSTV la luminosità è la frequenza del tono (1500 Hz nero, 2300 Hz
bianco, in USB con la radio 1,9 kHz sotto la frequenza assegnata). Una carta
inizia con il tono di avvio (bianco/nero alternati a 300 Hz per 5 s), poi 30 s di
righe di fasatura (bianche con un impulso nero: dicono dove inizia la riga), poi
le righe dell'immagine a 120 al minuto; finisce con il tono di stop (450 Hz).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .sstv import FS

START_HZ, STOP_HZ = 300.0, 450.0
TONE_MIN = 0.25  # ampiezza della componente alternata (0…0,64) per riconoscere avvio e stop
MAX_LINES = 2400  # 20 minuti: oltre si chiude comunque l'immagine


class FaxDecoder:
    """Frequenza del tono (16 kHz) → carte meteo."""

    def __init__(self, lpm: int = 120, ioc: int = 576) -> None:
        self.line = int(round(60.0 / lpm * FS))  # campioni per riga
        self.width = int(round(np.pi * ioc))  # 1810 pixel con IOC 576
        self.lum = np.zeros(0, np.float32)
        self.state = "attesa"
        self.tone_count = 0  # finestre consecutive con il tono di avvio
        self.stop_count = 0
        self.offset = 0  # inizio della prima riga dentro self.lum
        self.rows: list[np.ndarray] = []
        self.phasing_left = 0
        self.checked = 0  # campioni già esaminati per i toni
        self.phase_from = 0
        self.in_phasing = False
        self.pulse_level = 0.0

    def progress_rows(self) -> int:
        return len(self.rows)

    @staticmethod
    def _tone(x: np.ndarray, hz: float) -> float:
        n = np.arange(len(x))
        return float(np.abs(np.dot(x - x.mean(), np.exp(-2j * np.pi * hz * n / FS))) * 2 / len(x))

    def feed(self, f: np.ndarray) -> list[dict[str, Any]]:
        self.lum = np.concatenate([self.lum, np.clip((f - 1500.0) / 800.0, 0, 1).astype(np.float32)])
        events: list[dict[str, Any]] = []
        win, hop = FS, FS // 4
        while self.checked + win <= len(self.lum):
            seg = self.lum[self.checked: self.checked + win]
            start, stop = self._tone(seg, START_HZ), self._tone(seg, STOP_HZ)
            self.checked += hop
            if self.state == "attesa":
                self.tone_count = self.tone_count + 1 if start > TONE_MIN else 0
                if self.tone_count >= 6:  # 1,5 s di tono di avvio
                    self.state = "avvio"
                    events.append({"event": "start"})
            elif self.state == "avvio":
                if start < TONE_MIN / 2:  # finito il tono: iniziano le righe di fasatura
                    self.state = "fasatura"
                    self.phase_from = self.checked + win - hop
            elif self.state == "immagine":
                self.stop_count = self.stop_count + 1 if stop > TONE_MIN else 0
                if self.stop_count >= 6 or start > TONE_MIN:
                    events.append(self._finish(drop_s=3.0 if self.stop_count >= 6 else 0.0))
                    if start > TONE_MIN:
                        self.tone_count = 1
                    continue
        if self.state == "fasatura" and len(self.lum) - self.phase_from >= 12 * self.line:
            self._phase()
        if self.state == "immagine":
            self._lines()
            if len(self.rows) >= MAX_LINES:
                events.append(self._finish(0.0))
        if self.state == "attesa" and len(self.lum) > 4 * FS:
            cut = len(self.lum) - 2 * FS
            self.lum, self.checked = self.lum[cut:], max(0, self.checked - cut)
        return events

    def _phase(self) -> None:
        """Dove inizia la riga: l'impulso delle righe di fasatura, mediate una sull'altra."""
        seg = self.lum[self.phase_from: self.phase_from + 12 * self.line]
        prof = seg[: 12 * self.line].reshape(12, self.line).mean(axis=0)
        med = float(np.median(prof))
        dark = med > 0.5  # di solito righe bianche con impulso nero; per sicurezza anche il contrario
        far = prof < med - 0.3 if dark else prof > med + 0.3
        # l'impulso è il tratto (circolare) più lungo lontano dal fondo: la riga inizia dove inizia lui
        if not far.any():
            pulse = 0
        else:
            ext = np.concatenate([far, far])
            best, run, start, best_start = 0, 0, 0, 0
            for i, v in enumerate(ext):
                if v:
                    if run == 0:
                        start = i
                    run += 1
                    if run > best:
                        best, best_start = run, start
                else:
                    run = 0
            pulse = best_start % self.line
        self.pulse_level = 0.0 if dark else 1.0
        self.offset = self.phase_from + pulse
        self.state = "immagine"
        self.in_phasing = True

    def _lines(self) -> None:
        edges = np.round(np.arange(self.width + 1) * self.line / self.width).astype(np.int64)
        while self.offset + self.line <= len(self.lum):
            seg = self.lum[self.offset: self.offset + self.line]
            self.offset += self.line
            if self.in_phasing:
                # righe di fasatura ancora in corso: tutto del colore di fondo tranne l'impulso iniziale
                body = np.convolve(seg[int(self.line * 0.07): int(self.line * 0.97)], np.ones(32) / 32, "valid")
                bg = 1 - self.pulse_level
                if float(np.mean(np.abs(body - bg) < 0.3)) > 0.97:
                    continue
                self.in_phasing = False
            cs = np.concatenate([[0.0], np.cumsum(seg, dtype=np.float64)])
            row = (cs[edges[1:]] - cs[edges[:-1]]) / np.diff(edges)
            self.rows.append(np.clip(row * 255, 0, 255).astype(np.uint8))
        # si tiene solo ciò che serve ancora (righe e ricerca dei toni)
        cut = min(self.offset, self.checked)
        if cut > 0:
            self.lum = self.lum[cut:]
            self.offset -= cut
            self.checked -= cut
            self.phase_from -= cut

    def _finish(self, drop_s: float) -> dict[str, Any]:
        rows = self.rows
        drop = int(drop_s * FS / self.line)
        if drop and len(rows) > drop:
            rows = rows[:-drop]
        self.rows, self.state, self.tone_count, self.stop_count = [], "attesa", 0, 0
        if len(rows) < 20:
            return {"event": "lost"}
        return {"event": "image", "image": np.stack(rows), "complete": drop > 0}

    def preview(self) -> np.ndarray | None:
        return np.stack(self.rows) if self.rows else None
