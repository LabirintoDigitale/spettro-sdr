"""Frequenze preferite, salvate nello storage di Home Assistant (uguali su tutti i dispositivi)."""

from __future__ import annotations

import re
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_KEY = "spettro.preferiti"
STORAGE_VERSION = 1
MATCH_HZ = 30e3  # una frequenza entro 30 kHz da un preferito è lo stesso preferito
NONAME = "NONAME"

# origine del nome: "auto" (NONAME n), "rds" (nome della stazione), "user" (scritto a mano, mai sovrascritto)


class Favorites:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[list[dict[str, Any]]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.items: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        self.items = await self._store.async_load() or []
        self.items.sort(key=lambda f: f["freq_mhz"])
        changed = False
        for fav in self.items:
            if "source" not in fav:  # preferiti della 1.7.0: il nome, se c'era, veniva dall'RDS
                fav["source"] = "rds" if fav.get("name") else "auto"
                changed = True
            if not fav.get("name"):
                fav["name"], fav["source"] = self._next_noname(), "auto"
                changed = True
        if changed:
            await self._store.async_save(self.items)

    def _next_noname(self) -> str:
        used = {int(m.group(1)) for f in self.items if (m := re.fullmatch(rf"{NONAME} (\d+)", f.get("name") or ""))}
        n = 1
        while n in used:
            n += 1
        return f"{NONAME} {n}"

    def find(self, freq_mhz: float, sid: str | None = None) -> dict[str, Any] | None:
        """Preferito della frequenza; per il DAB+ anche della radio (SId), perché un multiplex ne ha molte."""
        sid = sid.upper() if sid else None
        return next((f for f in self.items if abs(f["freq_mhz"] - freq_mhz) * 1e6 <= MATCH_HZ
                     and f.get("sid") == sid), None)

    async def async_set(self, freq_mhz: float, name: str | None, source: str = "user", sid: str | None = None,
                        mode: str | None = None, squelch: float | None = None,
                        decoder: str | None = None, bw: float | None = None) -> None:
        """Aggiunge un preferito, o ne cambia il nome; un nome RDS/DAB non sostituisce quello scritto a mano.

        mode/squelch/decoder/bw: demodulazione, soglia dell'altoparlante, decodifica/plugin scelto e
        larghezza del canale, da ripristinare quando si riascolta.
        """
        fav = self.find(freq_mhz, sid)
        if fav is None:
            fav = {"freq_mhz": round(freq_mhz, 4), "name": None, "source": "auto", "added": time.time()}
            if sid:
                from .dab import channel_for  # noqa: PLC0415

                ch = channel_for(freq_mhz)
                fav.update(sid=sid.upper(), mode="dab", channel=ch[0] if ch else None)
            self.items.append(fav)
            self.items.sort(key=lambda f: f["freq_mhz"])
        if mode and not fav.get("sid"):  # la modalità del DAB resta "dab"
            fav["mode"] = mode
        if squelch is not None:
            fav["squelch"] = round(float(squelch), 1)
        if not fav.get("sid"):  # decodifica/plugin e larghezza del canale (non per il DAB)
            # decoder="" significa "nessuna decodifica": lo salvo come rimozione del campo
            if decoder is not None:
                if decoder:
                    fav["decoder"] = decoder
                else:
                    fav.pop("decoder", None)
            if bw is not None:
                fav["bw"] = round(float(bw))
        if name is not None and not (source in ("rds", "dab") and fav.get("source") == "user"):
            name = name.strip()[:40]
            fav["name"], fav["source"] = (name, source) if name else (None, "auto")
        if not fav["name"]:
            fav["name"], fav["source"] = self._next_noname(), "auto"
        await self._store.async_save(self.items)

    async def async_remove(self, freq_mhz: float, sid: str | None = None) -> None:
        fav = self.find(freq_mhz, sid)
        if fav is not None:
            self.items.remove(fav)
            await self._store.async_save(self.items)
