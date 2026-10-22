"""Frequenze preferite, salvate nello storage di Home Assistant (uguali su tutti i dispositivi)."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_KEY = "spettro.preferiti"
STORAGE_VERSION = 1
MATCH_HZ = 30e3  # una frequenza entro 30 kHz da un preferito è lo stesso preferito


class Favorites:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[list[dict[str, Any]]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.items: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        self.items = await self._store.async_load() or []
        self.items.sort(key=lambda f: f["freq_mhz"])

    def find(self, freq_mhz: float) -> dict[str, Any] | None:
        return next((f for f in self.items if abs(f["freq_mhz"] - freq_mhz) * 1e6 <= MATCH_HZ), None)

    async def async_set(self, freq_mhz: float, name: str | None) -> None:
        """Aggiunge un preferito, o ne cambia il nome se c'è già."""
        fav = self.find(freq_mhz)
        if fav is None:
            fav = {"freq_mhz": round(freq_mhz, 4), "name": None, "added": time.time()}
            self.items.append(fav)
            self.items.sort(key=lambda f: f["freq_mhz"])
        if name is not None:
            fav["name"] = name.strip()[:40] or None
        await self._store.async_save(self.items)

    async def async_remove(self, freq_mhz: float) -> None:
        fav = self.find(freq_mhz)
        if fav is not None:
            self.items.remove(fav)
            await self._store.async_save(self.items)
