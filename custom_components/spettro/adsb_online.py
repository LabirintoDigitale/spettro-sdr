"""Dettagli degli aerei da internet (opzionale): modello, immatricolazione, operatore e rotta.

Usa l'API pubblica e gratuita di adsbdb.com (senza chiave): dal codice ICAO dell'aereo si
ricavano immatricolazione, tipo e proprietario; dalla sigla del volo la rotta
partenza → arrivo. Richiede che Home Assistant possa uscire su internet. È tutto spento
finché l'utente non attiva l'opzione nel pannello.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
from typing import Any

AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{icao}"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"

HttpGet = Callable[[str], Awaitable[str]]


async def aircraft_info(http_get: HttpGet, icao: str) -> dict[str, Any] | None:
    """Immatricolazione, tipo e proprietario di un aereo dal suo codice ICAO."""
    try:
        data = json.loads(await http_get(AIRCRAFT_URL.format(icao=icao)))
    except Exception:  # noqa: BLE001 - rete o risposta inattesa: si riprova più tardi
        return None
    ac = (data.get("response") or {}).get("aircraft") if isinstance(data.get("response"), dict) else None
    if not ac:
        return {}  # cercato ma non trovato: non si riprova
    return {
        "registration": ac.get("registration"),
        "type": ac.get("type") or ac.get("icao_type"),
        "icao_type": ac.get("icao_type"),
        "manufacturer": ac.get("manufacturer"),
        "owner": ac.get("registered_owner"),
        "owner_country": ac.get("registered_owner_country_name"),
        "photo": ac.get("url_photo_thumbnail") or ac.get("url_photo"),
    }


def _airport(a: dict[str, Any] | None) -> dict[str, Any] | None:
    if not a:
        return None
    lat, lon = a.get("latitude"), a.get("longitude")
    return {"iata": a.get("iata_code"), "icao": a.get("icao_code"), "name": a.get("name"),
            "city": a.get("municipality"), "country": a.get("country_name"),
            "lat": float(lat) if lat is not None else None, "lon": float(lon) if lon is not None else None}


async def route_info(http_get: HttpGet, callsign: str) -> dict[str, Any] | None:
    """Rotta (aeroporto di partenza e di arrivo) dalla sigla del volo."""
    try:
        data = json.loads(await http_get(ROUTE_URL.format(callsign=callsign.strip())))
    except Exception:  # noqa: BLE001
        return None
    fr = (data.get("response") or {}).get("flightroute") if isinstance(data.get("response"), dict) else None
    if not fr:
        return {}
    return {
        "operator": (fr.get("airline") or {}).get("name"),
        "origin": _airport(fr.get("origin")),
        "destination": _airport(fr.get("destination")),
    }
