"""Immagine "Ultima analisi": la cascata del segnale analizzato, allegabile all'AI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpettroConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: SpettroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SpettroAnalysisImage(hass, entry), SpettroMeteoImage(hass, entry)])


class SpettroAnalysisImage(ImageEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "analysis"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, entry: SpettroConfigEntry) -> None:
        super().__init__(hass)
        self._scanner = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_analysis"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Spettro SDR",
            manufacturer="rtl_tcp",
            model=f"{self._scanner.host}:{self._scanner.port}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._scanner.add_listener(self._handle))

    @callback
    def _handle(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "analysis":
            self._attr_image_last_updated = datetime.now(UTC)
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        r = self._scanner.last_analysis
        if r is None:
            return None
        return {
            "frequenza_mhz": r["center_mhz"],
            "tipo": r["kind"],
            "snr_db": r["snr_db"],
            "banda_khz": r["obw_99_khz"],
            "duty_cycle": r["duty_cycle"],
            "burst": r["bursts"],
            "rds_stazione": (r.get("rds") or {}).get("ps"),
            "rds_testo": (r.get("rds") or {}).get("rt"),
        }

    async def async_image(self) -> bytes | None:
        return self._scanner.last_analysis_png


class SpettroMeteoImage(ImageEntity):
    """Ultima immagine ricevuta da un satellite meteo Meteor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "meteo"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, entry: SpettroConfigEntry) -> None:
        super().__init__(hass)
        self._scanner = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_meteo"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        archive = self._scanner.meteo_archive
        if archive is not None and archive.entries:
            self._attr_image_last_updated = datetime.fromtimestamp(archive.entries[0]["timestamp"], UTC)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._scanner.add_listener(self._handle))

    @callback
    def _handle(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "meteo_image":
            self._attr_image_last_updated = datetime.now(UTC)
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        archive = self._scanner.meteo_archive
        if archive is None or not archive.entries:
            return None
        e = archive.entries[0]
        return {
            "satellite": e["satellite"],
            "elevazione_max": e["max_el"],
            "righe": e["rows"],
            "composizione": e["composite"],
            "frequenza_mhz": e["freq_mhz"],
        }

    async def async_image(self) -> bytes | None:
        return self._scanner.last_meteo_png
