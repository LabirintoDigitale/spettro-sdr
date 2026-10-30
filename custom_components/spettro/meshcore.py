"""Pacchetti MeshCore ricevuti via LoRa: tipo, nodi, ripetitori attraversati e nodi sentiti.

Formato (docs/packet_format.md e payloads.md di MeshCore): [header][transport codes][path_len][path][payload].
Si legge solo quello che viaggia in chiaro: gli annunci dei nodi (nome, tipo, posizione, chiave pubblica)
e gli hash di mittente, destinatario, canale e ripetitori. Messaggi e richieste sono cifrati e restano tali.
"""

from __future__ import annotations

import hashlib
import struct
import time
from base64 import b64decode
from typing import Any

ROUTES = {0: "flood + regione", 1: "flood", 2: "diretto", 3: "diretto + regione"}
TYPES = {
    0: ("REQ", "richiesta"),
    1: ("RESPONSE", "risposta"),
    2: ("TXT_MSG", "messaggio privato"),
    3: ("ACK", "conferma"),
    4: ("ADVERT", "annuncio nodo"),
    5: ("GRP_TXT", "messaggio di canale"),
    6: ("GRP_DATA", "dati di canale"),
    7: ("ANON_REQ", "richiesta anonima"),
    8: ("PATH", "percorso di ritorno"),
    9: ("TRACE", "traccia del percorso"),
    10: ("MULTIPART", "pacchetto multiplo"),
    11: ("CONTROL", "controllo / scoperta"),
    15: ("RAW_CUSTOM", "dati personalizzati"),
}
NODE_TYPES = {1: "compagno (chat)", 2: "ripetitore", 3: "room server", 4: "sensore"}
# canale "Public" preconfigurato in tutte le app (chiave nota): il suo hash identifica il canale
PUBLIC_CHANNEL = hashlib.sha256(b64decode("izOH6cXN6mrJ5e26oRXNcg==")).digest()[0]
MAX_NODES = 300


def parse(raw: bytes) -> dict[str, Any] | None:
    """Intestazione e campi in chiaro di un pacchetto MeshCore (None se non ha il formato giusto)."""
    if len(raw) < 2:
        return None
    hdr = raw[0]
    route, ptype, ver = hdr & 3, (hdr >> 2) & 15, hdr >> 6
    i = 1
    if route in (0, 3):  # codici di trasporto (regione)
        if len(raw) < 6:
            return None
        i = 5
    path_len = raw[i]
    i += 1
    size = (path_len >> 6) + 1
    hops = path_len & 63
    if size == 4 or hops * size > 64 or i + hops * size >= len(raw):
        return None
    path = raw[i : i + hops * size]
    i += hops * size
    payload = raw[i:]
    name, label = TYPES.get(ptype, (f"TIPO_{ptype}", f"tipo {ptype}"))
    out: dict[str, Any] = {"route": ROUTES[route], "type": name, "label": label, "version": ver + 1,
                           "path": [path[k : k + size].hex() for k in range(0, len(path), size)], "size": len(raw)}
    if ptype == 9 and route in (2, 3):
        # TRACE diretto: nel percorso ogni ripetitore aggiunge l'SNR con cui l'ha ricevuto (×4)
        out["hop_snr"] = [round(struct.unpack("b", bytes([b]))[0] / 4, 2) for b in path]
        out["path"] = []
        if len(payload) >= 9:
            hsz = 1 << (payload[8] & 3)
            rest = payload[9:]
            out["trace_route"] = [rest[k : k + hsz].hex() for k in range(0, len(rest) - hsz + 1, hsz)]
    elif ptype in (0, 1, 2, 8) and len(payload) >= 4:
        out["dst"], out["src"] = f"{payload[0]:02x}", f"{payload[1]:02x}"
    elif ptype == 7 and len(payload) >= 35:
        out["dst"], out["src_key"] = f"{payload[0]:02x}", payload[1:33].hex()
        out["src"] = out["src_key"][:2]
    elif ptype in (5, 6) and len(payload) >= 3:
        out["channel"] = f"{payload[0]:02x}"
        if payload[0] == PUBLIC_CHANNEL:
            out["channel_name"] = "Public"
    elif ptype == 4:
        adv = _advert(payload)
        if adv is None:
            return None
        out.update(adv)
    return out


def _advert(p: bytes) -> dict[str, Any] | None:
    if len(p) < 101:
        return None
    key = p[:32]
    ts = struct.unpack("<I", p[32:36])[0]
    app = p[100:]
    out: dict[str, Any] = {"src_key": key.hex(), "src": key[:1].hex(), "advert_time": ts}
    if not app:
        return out
    flags = app[0]
    i = 1
    out["node_type"] = NODE_TYPES.get(flags & 15, f"tipo {flags & 15}")
    if flags & 0x10 and len(app) >= i + 8:
        lat, lon = struct.unpack("<ii", app[i : i + 8])
        i += 8
        if lat or lon:
            out["lat"], out["lon"] = lat / 1e6, lon / 1e6
    if flags & 0x20:
        i += 2
    if flags & 0x40:
        i += 2
    if flags & 0x80 and len(app) > i:
        out["name"] = app[i:].split(b"\0")[0].decode("utf-8", "replace")[:32]
    return out


class MeshTracker:
    """Nodi annunciati (per dare un nome agli hash) e statistiche di ricezione."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}  # chiave pubblica (hex) → nodo
        self.heard: dict[str, dict[str, Any]] = {}  # hash → ultimo pacchetto ricevuto direttamente da lui
        self.stats = {"ok": 0, "crc_err": 0, "unreadable": 0}

    def names(self, h: str) -> str | None:
        """Nome dei nodi annunciati la cui chiave inizia con l'hash h (più nodi possono condividerlo)."""
        found = [n["name"] or n["id"] for k, n in self.nodes.items() if k.startswith(h)]
        return " / ".join(found[:3]) if found else None

    def packet(self, res: dict[str, Any], freq_hz: float, when: float) -> dict[str, Any]:
        """Un risultato del ricevitore LoRa → riga per il pannello (e aggiorna nodi e statistiche)."""
        row: dict[str, Any] = {
            "time": round(when, 1), "freq_mhz": round((freq_hz + res.get("offset_hz", 0)) / 1e6, 4),
            "offset_khz": round(res.get("offset_hz", 0) / 1e3, 1), "snr": res.get("snr"),
            "level_db": res.get("level_db"), "sf": res.get("sf"), "bw_khz": res["bw"] / 1e3 if res.get("bw") else None,
            "cr": f"4/{res['cr'] + 4}" if res.get("cr") else None, "sync": res.get("sync_word"),
        }
        if not res.get("ok"):
            self.stats["unreadable"] += 1
            return {**row, "status": "illeggibile", "error": res.get("error")}
        payload: bytes = res["payload"]
        row["len"] = len(payload)
        row["hex"] = payload[:48].hex()
        if res.get("crc_ok") is False:
            self.stats["crc_err"] += 1
            return {**row, "status": "crc"}
        self.stats["ok"] += 1
        mc = parse(payload)
        if mc is None:
            return {**row, "status": "ok", "type": "?", "label": "non MeshCore"}
        row.update(mc, status="ok")
        if mc["type"] == "ADVERT" and mc.get("src_key"):
            node = self.nodes.setdefault(mc["src_key"], {"id": mc["src_key"][:8], "name": None, "count": 0})
            node.update({k: mc[k] for k in ("name", "node_type", "lat", "lon") if k in mc})
            node["last"] = row["time"]
            node["count"] += 1
            if len(self.nodes) > MAX_NODES:
                del self.nodes[min(self.nodes, key=lambda k: self.nodes[k].get("last", 0))]
        row["path_names"] = [self.names(h) for h in mc["path"]]
        if mc.get("src"):
            row["src_name"] = mc.get("name") or self.names(mc["src"])
        if mc.get("dst"):
            row["dst_name"] = self.names(mc["dst"])
        # chi abbiamo sentito davvero: l'ultimo ripetitore del percorso, o il mittente se non ci sono salti
        if mc["route"].startswith("flood"):
            last = mc["path"][-1] if mc["path"] else mc.get("src")
            if last:
                row["heard_from"] = last
                self.heard[last] = {"hash": last, "name": self.names(last), "snr": row["snr"],
                                    "level_db": row["level_db"], "last": row["time"],
                                    "count": self.heard.get(last, {}).get("count", 0) + 1,
                                    "repeater": bool(mc["path"])}
        return row

    def summary(self) -> dict[str, Any]:
        nodes = sorted(self.nodes.values(), key=lambda n: -n.get("last", 0))[:100]
        heard = sorted(self.heard.values(), key=lambda n: -n["last"])[:50]
        for h in heard:
            h["name"] = self.names(h["hash"]) or h.get("name")
        return {"nodes": nodes, "heard": heard, "stats": dict(self.stats), "updated": time.time()}
