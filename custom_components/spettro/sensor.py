"""Sensori: picco principale, rumore di fondo e numero di segnali trovati."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpettroConfigEntry
from .const import DOMAIN
from .scanner import SpectrumScanner


@dataclass(frozen=True, kw_only=True)
class SpettroSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    source_kind: str = "spectrum"  # quale tipo di messaggio aggiorna il sensore
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS = (
    SpettroSensorDescription(
        key="main_peak",
        translation_key="main_peak",
        icon="mdi:sine-wave",
        native_unit_of_measurement="MHz",
        suggested_display_precision=3,
        value_fn=lambda p: p["peaks"][0]["freq_mhz"] if p["peaks"] else None,
    ),
    SpettroSensorDescription(
        key="noise_floor",
        translation_key="noise_floor",
        icon="mdi:waveform",
        native_unit_of_measurement="dBFS",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda p: p["floor_db"],
    ),
    SpettroSensorDescription(
        key="signals",
        translation_key="signals",
        icon="mdi:access-point",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: len(p["peaks"]),
    ),
    SpettroSensorDescription(
        key="aircraft_count",
        translation_key="aircraft_count",
        icon="mdi:airplane",
        state_class=SensorStateClass.MEASUREMENT,
        source_kind="adsb",
        value_fn=lambda p: p.get("count", 0),
    ),
    SpettroSensorDescription(
        key="nearest_aircraft",
        translation_key="nearest_aircraft",
        icon="mdi:airplane-marker",
        native_unit_of_measurement="km",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        source_kind="adsb",
        value_fn=lambda p: next((a["km"] for a in p.get("aircraft", []) if a.get("km") is not None), None),
        attrs_fn=lambda p: _nearest_attrs(p),
    ),
)


def _nearest_attrs(p: dict[str, Any]) -> dict[str, Any]:
    a = next((a for a in p.get("aircraft", []) if a.get("km") is not None), None)
    if not a:
        return {}
    return {"volo": a.get("call"), "icao": a["icao"], "quota_m": a.get("alt_m"),
            "velocita_kmh": a.get("speed_kmh"), "direzione_da_casa": a.get("bearing"),
            "latitudine": a.get("lat"), "longitudine": a.get("lon")}


async def async_setup_entry(
    hass: HomeAssistant, entry: SpettroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(SpettroSensor(entry, entry.runtime_data, d) for d in SENSORS)


class SpettroSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: SpettroSensorDescription

    def __init__(self, entry: SpettroConfigEntry, scanner: SpectrumScanner, description: SpettroSensorDescription) -> None:
        self.entity_description = description
        self._scanner = scanner
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Spettro SDR",
            manufacturer="rtl_tcp",
            model=f"{scanner.host}:{scanner.port}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._scanner.add_listener(self._handle))
        if self._scanner.last_complete:
            self._update(self._scanner.last_complete)

    @callback
    def _handle(self, payload: dict[str, Any]) -> None:
        kind = self.entity_description.source_kind
        if kind == "spectrum" and payload.get("kind") == "spectrum" and payload.get("complete"):
            self._update(payload)
            self.async_write_ha_state()
        elif kind == "adsb" and payload.get("kind") == "adsb":
            self._update(payload)
            self.async_write_ha_state()

    def _update(self, payload: dict[str, Any]) -> None:
        self._attr_native_value = self.entity_description.value_fn(payload)
        if self.entity_description.attrs_fn is not None:
            self._attr_extra_state_attributes = self.entity_description.attrs_fn(payload)
        if self.entity_description.key == "main_peak":
            top = payload["peaks"][:10]
            self._attr_extra_state_attributes = {
                "potenza_db": top[0]["db"] if top else None,
                "picchi": top,
                "da_mhz": round(payload["range_start"] / 1e6, 3),
                "a_mhz": round(payload["range_stop"] / 1e6, 3),
                "ultima_scansione": datetime.fromtimestamp(payload["timestamp"], UTC).isoformat(),
                "durata_s": payload["sweep_s"],
            }
