"""Satelliti meteo: orbite (TLE da CelesTrak), passaggi, ricezione LRPT e archivio delle immagini.

Non dipende da Home Assistant: chi lo usa passa la funzione per scaricare i TLE e
la posizione della stazione. I metodi che toccano il disco vanno nell'executor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any

import numpy as np

from . import orbit
from .analysis import encode_png
from .meteor import IN_RATE, LrptDecoder, LrptDemod, composite

_LOGGER = logging.getLogger(__name__)

# satelliti Meteor-M attivi: numero NORAD, nome e frequenze LRPT (la prima è la solita; a volte le scambiano)
SATELLITES: dict[int, tuple[str, tuple[float, ...]]] = {
    57166: ("Meteor-M2 3", (137.9e6, 137.1e6)),
    59051: ("Meteor-M2 4", (137.1e6, 137.9e6)),
}
ISS_NORAD = 25544  # Stazione spaziale: SSTV su 145,800 MHz durante gli eventi ARISS
ISS_FREQ = 145.8e6
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"
TLE_MAX_AGE_S = 12 * 3600
PASS_HOURS = 36
CENTER_OFFSET = 150e3  # la chiavetta si sintonizza un po' a lato: il segnale non cade sul picco DC
SWITCH_AFTER_S = 25.0  # senza aggancio si prova l'altra frequenza


class MeteoArchive:
    """Immagini ricevute: <id>.png (composita), <id>.ir.png (infrarosso, se c'è) e <id>.json."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []  # dalla più recente

    def load(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        out = []
        for f in self.path.glob("*.json"):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        out.sort(key=lambda e: e["timestamp"], reverse=True)
        self.entries = out

    def add(self, meta: dict[str, Any], png: bytes, ir_png: bytes | None) -> dict[str, Any]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["timestamp"]))
        entry_id = f"{stamp}-{meta['norad']}"
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / f"{entry_id}.png").write_bytes(png)
        if ir_png:
            (self.path / f"{entry_id}.ir.png").write_bytes(ir_png)
        meta = {**meta, "id": entry_id, "has_ir": bool(ir_png),
                "bytes": len(png) + (len(ir_png) if ir_png else 0)}
        (self.path / f"{entry_id}.json").write_text(json.dumps(meta))
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        self.entries.insert(0, meta)
        return meta

    def get(self, entry_id: str, ir: bool = False) -> bytes | None:
        if not any(e["id"] == entry_id for e in self.entries):
            return None
        try:
            return (self.path / f"{entry_id}{'.ir' if ir else ''}.png").read_bytes()
        except OSError:
            return None

    def delete(self, entry_id: str | None = None) -> int:
        drop = [e for e in self.entries if entry_id is None or e["id"] == entry_id]
        for e in drop:
            for f in self.path.glob(f"{e['id']}.*"):
                f.unlink(missing_ok=True)
        self.entries = [e for e in self.entries if e not in drop]
        return len(drop)

    def prune(self, max_age_s: float) -> int:
        limit = time.time() - max_age_s
        old = [e["id"] for e in self.entries if e["timestamp"] < limit]
        for entry_id in old:
            self.delete(entry_id)
        return len(old)


class Meteo:
    """Orbite e passaggi dei Meteor sopra la stazione."""

    def __init__(self, lat: float, lon: float, alt_m: float,
                 fetch: Callable[[str], Awaitable[str]] | None = None) -> None:
        self.observer = orbit.Observer(lat, lon, alt_m)
        self.location = (lat, lon)
        self.fetch = fetch
        self.sats: dict[int, orbit.Satellite] = {}
        self.tle_text: dict[int, tuple[str, str, str]] = {}
        self.tle_time = 0.0
        self.tle_try = 0.0  # ultimo tentativo di scaricare i TLE
        self.tle_error: str | None = None
        self.passes: list[dict[str, Any]] = []
        self.passes_time = 0.0

    def set_tles(self, tles: dict[int, tuple[str, str, str]], when: float) -> None:
        for norad, (name, l1, l2) in tles.items():
            try:
                label = "ISS" if norad == ISS_NORAD else SATELLITES.get(norad, (name,))[0]
                self.sats[norad] = orbit.Satellite(label, l1, l2)
                self.tle_text[norad] = (name, l1, l2)
            except orbit.TleError as err:
                _LOGGER.warning("TLE di %s non valido: %s", name, err)
        self.tle_time = when
        self.passes_time = 0.0

    async def update_tles(self) -> bool:
        """Scarica i TLE da CelesTrak; True se sono cambiati."""
        if self.fetch is None:
            return False
        self.tle_try = time.time()
        got: dict[int, tuple[str, str, str]] = {}
        for norad in (*SATELLITES, ISS_NORAD):
            try:
                text = await self.fetch(TLE_URL.format(norad=norad))
            except Exception as err:  # noqa: BLE001 - rete o servizio: si riprova più tardi
                self.tle_error = f"CelesTrak non raggiungibile: {err}"
                continue
            lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
            for i in range(len(lines) - 2):
                if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 ") and int(lines[i + 1][2:7]) == norad:
                    got[norad] = (lines[i].strip(), lines[i + 1], lines[i + 2])
        if got:
            self.tle_error = None
            self.set_tles(got, time.time())
        return bool(got)

    def missing(self) -> bool:
        """Manca l'orbita di qualche satellite (es. la ISS, aggiunta dopo che i TLE erano già salvati)."""
        return any(n not in self.sats for n in (*SATELLITES, ISS_NORAD))

    def compute_passes(self, min_el: float, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for norad, sat in self.sats.items():
            for p in orbit.passes(sat, self.observer, now - timedelta(minutes=20), PASS_HOURS, min_el):
                if p["los"] > now:
                    iss = norad == ISS_NORAD
                    out.append({**p, "norad": norad, "satellite": sat.name, "kind": "iss" if iss else "meteor",
                                "freqs": [ISS_FREQ] if iss else list(SATELLITES.get(norad, ("", (137.9e6,)))[1])})
        out.sort(key=lambda p: p["aos"])
        self.passes = out
        self.passes_time = time.time()
        return out

    def doppler(self, norad: int, when: datetime, freq: float) -> float:
        sat = self.sats.get(norad)
        return self.observer.doppler(sat, when, freq) if sat else 0.0

    def look(self, norad: int, when: datetime) -> dict[str, float] | None:
        sat = self.sats.get(norad)
        return self.observer.look(sat, when) if sat else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "meteo_passes",
            "passes": [pass_json(p) for p in self.passes[:12]],
            "tle_age_h": round((time.time() - self.tle_time) / 3600, 1) if self.tle_time else None,
            "tle_error": self.tle_error,
            "satellites": [{"norad": n, "name": SATELLITES[n][0], "loaded": n in self.sats} for n in SATELLITES]
            + [{"norad": ISS_NORAD, "name": "ISS", "loaded": ISS_NORAD in self.sats}],
            "location": [round(self.location[0], 2), round(self.location[1], 2)],
        }


def pass_json(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "norad": p["norad"], "satellite": p["satellite"], "max_el": p["max_el"],
        "aos": p["aos"].timestamp(), "los": p["los"].timestamp(), "tca": p["tca"].timestamp(),
        "az_aos": p["az_aos"], "az_los": p["az_los"], "northbound": p["northbound"], "kind": p.get("kind", "meteor"),
    }


class MeteorReceiver:
    """Demodulatore + decoder di un passaggio, con le statistiche e l'anteprima dell'immagine."""

    def __init__(self, freq: float) -> None:
        self.freq = freq
        self.demod = LrptDemod()
        self.decoder = LrptDecoder()
        self.samples = 0
        self.last_lock = 0.0

    def process(self, raw: bytes, center: float, doppler: float) -> dict[str, Any]:
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
        iq = (a[0::2] + 1j * a[1::2]).astype(np.complex64)
        self.samples += len(iq)
        soft = self.demod.feed(iq, self.freq + doppler - center)
        if len(soft):
            self.decoder.push(soft)
        if self.demod.locked:
            self.last_lock = self.samples / IN_RATE
        level = float(10 * np.log10(np.mean(a * a) * 2 / 127.5**2 + 1e-20))
        return self.stats(level)

    def stats(self, level: float | None = None) -> dict[str, Any]:
        d, s = self.demod, self.decoder.stats()
        return {**s, "locked": d.locked, "line_db": round(d.line_db, 1), "snr_db": round(d.snr_db, 1),
                "offset_hz": round(d.offset_hz), "level_db": None if level is None else round(level, 1),
                "elapsed_s": round(self.samples / IN_RATE, 1), "freq_mhz": round(self.freq / 1e6, 3)}

    def finish(self) -> None:
        self.decoder.push(np.zeros(0, np.int8), final=True)

    def images(self, northbound: bool, preview: int = 0) -> tuple[bytes | None, bytes | None, str, int]:
        """PNG della composita e dell'infrarosso; preview > 0 = riduzione (anteprima in diretta)."""
        ch = self.decoder.channels()
        if not ch:
            return None, None, "", 0
        rgb, name = composite(ch, northbound)
        ir = None
        if 68 in ch and len(ch) > 1:
            g = 255 - ch[68]
            if northbound:
                g = g[::-1, ::-1]
            ir = np.stack([g, g, g], axis=2)
        if preview:
            rgb = rgb[::preview, ::preview]
            ir = None
        return encode_png(np.ascontiguousarray(rgb)), encode_png(np.ascontiguousarray(ir)) if ir is not None else None, \
            name, int(rgb.shape[0])
