"""Interruttore "Scansione": mette in pausa o riattiva la scansione dello spettro."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpettroConfigEntry
from .const import DOMAIN
from .ws_api import set_scan_enabled


async def async_setup_entry(
    hass: HomeAssistant, entry: SpettroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SpettroScanSwitch(entry)])


class SpettroScanSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "scan"
    _attr_icon = "mdi:radar"

    def __init__(self, entry: SpettroConfigEntry) -> None:
        self._entry = entry
        self._scanner = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_scan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Spettro SDR",
            manufacturer="rtl_tcp",
            model=f"{self._scanner.host}:{self._scanner.port}",
        )

    @property
    def is_on(self) -> bool:
        return self._scanner.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._scanner.status
        return {
            "stato": status["state"],
            "solo_con_pannello_aperto": self._scanner.settings.only_when_viewed,
            "pannelli_aperti": self._scanner.viewers,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._scanner.add_listener(self._handle))

    @callback
    def _handle(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "status":
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        set_scan_enabled(self.hass, self._entry, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        set_scan_enabled(self.hass, self._entry, False)
