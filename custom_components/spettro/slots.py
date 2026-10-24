"""Modi digitali a periodi fissi (FT8, WSPR) nell'ascolto in diretta.

L'audio arriva a blocchi: si tiene in memoria con l'ora in cui è stato ricevuto e, finito
ogni periodo UTC, se ne ritaglia la parte giusta per il decoder. L'ora dei campioni si
stima dal ritardo minimo tra arrivo dei blocchi e campioni contati (i blocchi in ritardo
non spostano la stima).
"""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Any

import numpy as np

from . import ft8, wspr
from .textdec import FS, _Audio

MODES = {"ft8": ft8, "wspr": wspr}
OFFSET_WINDOW_S = 60.0  # su quanti secondi si cerca il ritardo minimo


class SlotDecoder:
    def __init__(self, kind: str, dial_hz: float, home: tuple[float, float] | None) -> None:
        self.kind = kind
        self.mod = MODES[kind]
        self.dial_hz = dial_hz
        self.home = home
        self.audio = _Audio()
        self.keep = int((self.mod.WINDOW_S + 20) * FS)
        self.buf = np.zeros(0, np.float32)
        self.first = 0  # indice (nel flusso) del primo campione di self.buf
        self.total = 0
        self.offsets: deque[tuple[float, float]] = deque()  # (ora di arrivo, ora stimata del campione 0)
        self.next_slot: float | None = None
        self.ft8_hashes = ft8.Hashes()
        self.wspr_hashes: dict[int, str] = {}
        self.slots_done = 0
        self.decoded = 0

    def t0(self) -> float:
        return min(o for _, o in self.offsets)

    def feed(self, pcm: np.ndarray, now: float | None = None) -> list[tuple[float, np.ndarray]]:
        """Audio a 32 kHz appena ricevuto → periodi completi da decodificare (inizio UTC, audio)."""
        now = time.time() if now is None else now
        x = self.audio.feed(pcm).astype(np.float32)
        self.total += len(x)
        self.buf = np.concatenate([self.buf, x])
        if len(self.buf) > self.keep:
            cut = len(self.buf) - self.keep
            self.buf = self.buf[cut:]
            self.first += cut
        self.offsets.append((now, now - self.total / FS))
        while self.offsets and now - self.offsets[0][0] > OFFSET_WINDOW_S:
            self.offsets.popleft()
        t0 = self.t0()
        slot = self.mod.SLOT_S
        if self.next_slot is None:
            # primo periodo utile: quello in cui l'audio c'è quasi tutto
            self.next_slot = math.floor((t0 + self.first / FS) / slot) * slot
            if t0 + self.first / FS > self.next_slot + self.mod.START_S + 1.0:
                self.next_slot += slot
        ready = []
        while True:
            start = self.next_slot - self.mod.PRE_S
            end = start + self.mod.WINDOW_S
            if t0 + self.total / FS < end:
                break
            a = int(round((start - t0) * FS)) - self.first
            b = a + int(self.mod.WINDOW_S * FS)
            if b > 0:
                seg = self.buf[max(a, 0): b]
                if a < 0:  # l'ascolto è iniziato da poco: manca l'inizio, si mette silenzio
                    seg = np.concatenate([np.zeros(-a, np.float32), seg])
                if len(seg) < b - a:
                    seg = np.concatenate([seg, np.zeros(b - a - len(seg), np.float32)])
                ready.append((self.next_slot, seg))
            self.next_slot += slot
        return ready

    def decode(self, slot_start: float, x: np.ndarray) -> list[dict[str, Any]]:
        """Messaggi di un periodo (nell'executor), con la frequenza radio e la distanza."""
        if self.kind == "ft8":
            res = ft8.decode(x, self.ft8_hashes)
            for m in res:
                parts = m["message"].split()
                grid = parts[-1] if parts and len(parts[-1]) == 4 and parts[-1] != "RR73" else None
                ll = ft8.grid_latlon(grid) if grid else None
                m["grid"] = grid if ll else None
                m["km"] = round(ft8.distance_km(self.home, ll)) if ll and self.home else None
                m["cq"] = parts[0] == "CQ"
        else:
            res = wspr.decode(x, self.wspr_hashes, self.home)
        for m in res:
            m["rf_hz"] = round(self.dial_hz + m["freq_hz"])
            m["time"] = slot_start
            m["mode"] = self.kind
        self.slots_done += 1
        self.decoded += len(res)
        return res

    def status(self) -> dict[str, Any]:
        nxt = None
        if self.next_slot is not None:
            nxt = self.next_slot - self.mod.PRE_S + self.mod.WINDOW_S
        return {"slots": self.slots_done, "decoded": self.decoded, "next_decode": nxt, "slot_s": self.mod.SLOT_S}
