"""Archivio delle analisi ("spike"): misure, cascata e audio salvati su disco.

Ogni analisi diventa tre gruppi di file nella cartella dell'archivio:
<id>.json (misure), <id>.png (cascata) e <id>.<modo>.wav (audio demodulato).
L'indice resta in memoria; i metodi che toccano il disco sono bloccanti e
vanno chiamati nell'executor. Le analisi più vecchie di N ore si cancellano
con prune().
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

AUDIO_MODES = ("wfm", "nfm", "am")  # audio demodulato salvato con ogni analisi

# campi dell'analisi copiati nell'indice (quello che serve a elenchi e tabella dei picchi)
META_FIELDS = ("target_mhz", "center_mhz", "kind", "snr_db", "peak_db", "obw_99_khz", "duty_cycle", "audio_mode")


class SpikeArchive:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []  # dalla più recente

    def load(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        entries = []
        for f in self.path.glob("*.json"):
            try:
                result = json.loads(f.read_text())
                entries.append(self._meta(f.stem, result, self._size(f.stem)))
            except (OSError, ValueError, KeyError):
                continue
        entries.sort(key=lambda e: e["timestamp"], reverse=True)
        self.entries = entries

    def _files(self, entry_id: str) -> list[Path]:
        # nomi esatti: una ricerca nella cartella per ogni analisi, con migliaia di file, dura minuti
        names = [f"{entry_id}.json", f"{entry_id}.png", *(f"{entry_id}.{m}.wav" for m in AUDIO_MODES)]
        return [self.path / n for n in names]

    def _size(self, entry_id: str) -> int:
        total = 0
        for f in self._files(entry_id):
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total

    @property
    def total_bytes(self) -> int:
        return sum(e["bytes"] for e in self.entries)

    @staticmethod
    def _meta(entry_id: str, result: dict[str, Any], size: int) -> dict[str, Any]:
        meta = {"id": entry_id, "timestamp": result["timestamp"], "auto": bool(result.get("auto")), "bytes": size}
        meta.update({k: result.get(k) for k in META_FIELDS})
        return meta

    def add(self, result: dict[str, Any], png: bytes, audio: dict[str, bytes]) -> dict[str, Any]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(result["timestamp"]))
        entry_id = f"{stamp}-{round(result['target_mhz'] * 1000)}"
        n = 1
        while any(e["id"] == entry_id for e in self.entries):
            n += 1
            entry_id = f"{stamp}-{round(result['target_mhz'] * 1000)}-{n}"
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / f"{entry_id}.png").write_bytes(png)
        for mode, wav in audio.items():
            (self.path / f"{entry_id}.{mode}.wav").write_bytes(wav)
        (self.path / f"{entry_id}.json").write_text(json.dumps(result))
        meta = self._meta(entry_id, result, self._size(entry_id))
        self.entries.insert(0, meta)
        return meta

    def get(self, entry_id: str) -> tuple[dict[str, Any], bytes, dict[str, bytes]] | None:
        if not any(e["id"] == entry_id for e in self.entries):
            return None
        try:
            result = json.loads((self.path / f"{entry_id}.json").read_text())
            png = (self.path / f"{entry_id}.png").read_bytes()
        except OSError:
            return None
        audio = {}
        for mode in AUDIO_MODES:
            f = self.path / f"{entry_id}.{mode}.wav"
            if f.exists():
                audio[mode] = f.read_bytes()
        return result, png, audio

    def _remove_files(self, entry_id: str) -> None:
        for f in self._files(entry_id):
            f.unlink(missing_ok=True)

    def delete(self, entry_id: str) -> bool:
        if not any(e["id"] == entry_id for e in self.entries):
            return False
        self._remove_files(entry_id)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        return True

    def clear(self) -> int:
        count = len(self.entries)
        # tutta la cartella in una passata (anche file rimasti orfani)
        for f in self.path.iterdir():
            if f.is_file() and f.suffix in (".json", ".png", ".wav"):
                f.unlink(missing_ok=True)
        self.entries = []
        return count

    def prune(self, max_age_s: float) -> int:
        """Cancella le analisi più vecchie di max_age_s."""
        limit = time.time() - max_age_s
        keep, drop = [], []
        for e in self.entries:
            (keep if e["timestamp"] >= limit else drop).append(e)
        for e in drop:
            self._remove_files(e["id"])
        self.entries = keep
        return len(drop)

    def find(self, freq_hz: float, tol_hz: float, newer_than: float = 0) -> dict[str, Any] | None:
        """Analisi più recente della stessa frequenza (entro tol_hz), opzionalmente non più vecchia di newer_than."""
        for e in self.entries:
            if e["timestamp"] < newer_than:
                break
            if abs(e["target_mhz"] * 1e6 - freq_hz) <= tol_hz or abs(e["center_mhz"] * 1e6 - freq_hz) <= tol_hz:
                return e
        return None
