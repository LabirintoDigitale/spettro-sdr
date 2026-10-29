"""Pubblicazione su MQTT (broker Mosquitto dell'utente) con l'auto-discovery di Home Assistant.

Spettro si collega direttamente al broker con le credenziali inserite nel pannello e pubblica:
- i sensori ISM (rtl_433) come entità sensore (una per misura, con unità e device_class);
- gli aerei ADS-B come un'entità "ultimo aereo" con gli attributi del volo;
- le foto dei satelliti meteo/ISS come entità immagine.

La connessione gira in un thread di paho-mqtt (loop_start), con riconnessione automatica; la
pubblicazione dal lato asyncio è sicura perché client.publish() è thread-safe.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt  # HA include già paho-mqtt
except ImportError:  # pragma: no cover
    mqtt = None


def _slug(s: Any) -> str:
    """Rende una stringa adatta a un topic/identificatore MQTT (solo [a-z0-9_])."""
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "x"


# misura rtl_433 -> (nome, unità, device_class, state_class). Con `-C si` le unità sono metriche.
_SENSOR_MAP = {
    "temperature_C": ("Temperatura", "°C", "temperature", "measurement"),
    "temperature_1_C": ("Temperatura 1", "°C", "temperature", "measurement"),
    "humidity": ("Umidità", "%", "humidity", "measurement"),
    "humidity_1": ("Umidità 1", "%", "humidity", "measurement"),
    "pressure_hPa": ("Pressione", "hPa", "pressure", "measurement"),
    "pressure_kPa": ("Pressione", "kPa", "pressure", "measurement"),
    "wind_avg_km_h": ("Vento", "km/h", "wind_speed", "measurement"),
    "wind_max_km_h": ("Raffica", "km/h", "wind_speed", "measurement"),
    "wind_avg_m_s": ("Vento", "m/s", "wind_speed", "measurement"),
    "wind_max_m_s": ("Raffica", "m/s", "wind_speed", "measurement"),
    "wind_dir_deg": ("Direzione vento", "°", None, "measurement"),
    "rain_mm": ("Pioggia totale", "mm", "precipitation", "total_increasing"),
    "rain_mm_h": ("Intensità pioggia", "mm/h", "precipitation_intensity", "measurement"),
    "uv": ("UV", None, None, "measurement"),
    "uvi": ("Indice UV", None, None, "measurement"),
    "light_lux": ("Luce", "lx", "illuminance", "measurement"),
    "lux": ("Luce", "lx", "illuminance", "measurement"),
    "battery_V": ("Batteria", "V", "voltage", "measurement"),
}
# chiavi di servizio da non trasformare in sensori
_SKIP = {"model", "id", "channel", "time", "mic", "protocol", "subtype", "freq", "freq1", "freq2",
         "rssi", "snr", "noise", "mod", "sync", "len", "seq", "num_rows", "rows"}


class MqttPublisher:
    def __init__(self) -> None:
        self._client: Any = None
        self._cfg: tuple | None = None  # (host, port, user, pass, prefix) in uso
        self._prefix = "homeassistant"
        self.connected = False
        self.error: str | None = None
        self._discovered: set[str] = set()  # unique_id già annunciati (per non ripubblicare il config)

    # --- connessione ------------------------------------------------------

    def configure(self, host: str, port: int, user: str, password: str, prefix: str = "homeassistant") -> None:
        """(Ri)collega il broker se i parametri sono cambiati. Vuoto host = disconnette."""
        host = (host or "").strip()
        cfg = (host, int(port or 1883), user or "", password or "", prefix or "homeassistant")
        if cfg == self._cfg and (self._client is not None or not host):
            return
        self.close()
        self._cfg = cfg
        self._prefix = cfg[4]
        self._discovered.clear()
        if not host:
            return
        if mqtt is None:
            self.error = "paho-mqtt non disponibile in questa installazione di Home Assistant"
            _LOGGER.warning(self.error)
            return
        try:
            try:  # paho 2.x richiede la versione dei callback; 1.x no
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            except (AttributeError, TypeError):
                client = mqtt.Client()
            if cfg[2]:
                client.username_pw_set(cfg[2], cfg[3])
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(host, cfg[1], keepalive=60)
            client.loop_start()
            self._client = client
            self.error = None
            _LOGGER.info("MQTT: connessione a %s:%s avviata", host, cfg[1])
        except Exception as err:  # noqa: BLE001
            self.error = f"MQTT: {type(err).__name__}: {err}"
            _LOGGER.warning(self.error)
            self._client = None

    def _on_connect(self, client, userdata, flags, rc, *a) -> None:
        self.connected = (rc == 0)
        if rc == 0:
            self.error = None
            _LOGGER.info("MQTT: connesso")
        else:
            self.error = f"MQTT: connessione rifiutata (codice {rc})"
            _LOGGER.warning(self.error)

    def _on_disconnect(self, client, userdata, rc, *a) -> None:
        self.connected = False

    def close(self) -> None:
        c, self._client = self._client, None
        self.connected = False
        if c is not None:
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict[str, Any]:
        host = self._cfg[0] if self._cfg else ""
        return {"enabled": bool(host), "connected": self.connected, "host": host, "error": self.error}

    # --- pubblicazione ----------------------------------------------------

    def _pub(self, topic: str, payload: Any, retain: bool = False) -> None:
        if self._client is None:
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            self._client.publish(topic, payload, qos=0, retain=retain)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("MQTT publish fallita su %s: %s", topic, err)

    def _device(self, ident: str, name: str, model: str | None = None) -> dict[str, Any]:
        dev = {"identifiers": [f"spettro_{ident}"], "name": name, "manufacturer": "Spettro"}
        if model:
            dev["model"] = model
        return dev

    def _discover_sensor(self, node: str, obj: str, name: str, device: dict, state_topic: str,
                         *, unit=None, device_class=None, state_class=None, value_template=None,
                         json_attrs_topic=None, icon=None) -> None:
        uid = f"{node}_{obj}"
        if uid in self._discovered:
            return
        cfg = {"name": name, "uniq_id": uid, "stat_t": state_topic, "dev": device}
        if unit: cfg["unit_of_meas"] = unit
        if device_class: cfg["dev_cla"] = device_class
        if state_class: cfg["stat_cla"] = state_class
        if value_template: cfg["val_tpl"] = value_template
        if json_attrs_topic: cfg["json_attr_t"] = json_attrs_topic
        if icon: cfg["ic"] = icon
        self._pub(f"{self._prefix}/sensor/{node}/{obj}/config", cfg, retain=True)
        self._discovered.add(uid)

    # ISM (rtl_433): una entità sensore per ogni misura, device per (modello, id)
    def publish_ism(self, d: dict[str, Any]) -> None:
        if self._client is None:
            return
        model = str(d.get("model", "sensore"))
        did = str(d.get("id", d.get("channel", "")))
        node = f"ism_{_slug(model)}_{_slug(did)}" if did else f"ism_{_slug(model)}"
        state_topic = f"spettro/ism/{node}/state"
        device = self._device(node, f"{model}{(' ' + did) if did else ''}", model=model)
        for key, val in d.items():
            if key in _SKIP or key.startswith("_") or not isinstance(val, (int, float)):
                continue
            name, unit, dcla, scla = _SENSOR_MAP.get(key, (key.replace("_", " ").capitalize(), None, None, "measurement"))
            self._discover_sensor(node, _slug(key), name, device, state_topic,
                                  unit=unit, device_class=dcla, state_class=scla,
                                  value_template=f"{{{{ value_json.{key} | default('') }}}}")
        self._pub(state_topic, {k: v for k, v in d.items() if not k.startswith("_")}, retain=True)

    # ADS-B: una entità "Ultimo aereo" aggiornata a ogni nuovo volo, col volo come attributi
    def publish_aircraft(self, flight: dict[str, Any]) -> None:
        if self._client is None:
            return
        node = "adsb"
        state_topic = "spettro/adsb/last/state"
        attr_topic = "spettro/adsb/last/attr"
        device = self._device(node, "Spettro Aerei", model="ADS-B")
        self._discover_sensor(node, "last", "Ultimo aereo", device, state_topic,
                              icon="mdi:airplane", json_attrs_topic=attr_topic)
        label = (flight.get("call") or flight.get("callsign") or flight.get("registration")
                 or flight.get("icao") or flight.get("hex") or "?")
        self._pub(state_topic, str(label).strip() or "?", retain=True)
        self._pub(attr_topic, {k: v for k, v in flight.items() if v not in (None, "")}, retain=True)

    # foto satellitari: entità immagine (MQTT image) aggiornata coi byte PNG
    def publish_image(self, kind: str, name: str, png: bytes, attrs: dict | None = None) -> None:
        if self._client is None or not png:
            return
        node = f"img_{_slug(kind)}"
        img_topic = f"spettro/{node}/image"
        uid = f"{node}_image"
        if uid not in self._discovered:
            cfg = {"name": name, "uniq_id": uid, "image_topic": img_topic, "content_type": "image/png",
                   "dev": self._device("satelliti", "Spettro Satelliti", model="Immagini")}
            self._pub(f"{self._prefix}/image/{node}/config", cfg, retain=True)
            self._discovered.add(uid)
        self._pub(img_topic, png, retain=True)
        if attrs:
            self._pub(f"spettro/{node}/attr", attrs, retain=True)
