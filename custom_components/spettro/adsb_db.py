"""Tabelle per arricchire i dati ADS-B senza internet: compagnia, paese, categoria.

- compagnia: le prime 3 lettere del nominativo del volo sono il codice ICAO dell'operatore;
- paese di immatricolazione: dagli intervalli di indirizzi ICAO assegnati a ogni nazione;
- categoria dell'aereo: dal codice trasmesso (leggero, pesante, elicottero…).

Sono dati pubblici. La tabella delle compagnie copre quelle più comuni sui cieli europei;
una sigla sconosciuta resta mostrata così com'è.
"""

from __future__ import annotations

# codice ICAO dell'operatore (3 lettere) → nome. Comuni in Italia/Europa più i principali del mondo.
AIRLINES: dict[str, str] = {
    "ITY": "ITA Airways", "AZA": "Alitalia", "RYR": "Ryanair", "EZY": "easyJet", "EJU": "easyJet Europe",
    "WZZ": "Wizz Air", "WMT": "Wizz Air Malta", "VLG": "Vueling", "EIN": "Aer Lingus", "DLH": "Lufthansa",
    "CLH": "Lufthansa CityLine", "EWG": "Eurowings", "AFR": "Air France", "KLM": "KLM", "BAW": "British Airways",
    "IBE": "Iberia", "IBS": "Iberia Express", "TAP": "TAP Air Portugal", "SWR": "Swiss", "AUA": "Austrian",
    "BEL": "Brussels Airlines", "SAS": "SAS", "FIN": "Finnair", "LOT": "LOT Polish", "TAR": "Tarom",
    "ROT": "Tarom", "CTN": "Croatia Airlines", "ADR": "Air Serbia", "AEE": "Aegean", "TRA": "Transavia",
    "TVF": "Transavia France", "NAX": "Norwegian", "NOZ": "Norwegian", "IBK": "Norwegian", "BTI": "airBaltic",
    "VOE": "Volotea", "NEO": "Neos", "MSC": "Air Cairo", "ETD": "Etihad", "UAE": "Emirates", "QTR": "Qatar Airways",
    "THY": "Turkish Airlines", "PGT": "Pegasus", "SXS": "SunExpress", "ELY": "El Al", "MEA": "Middle East Airlines",
    "RJA": "Royal Jordanian", "SVA": "Saudia", "MSR": "EgyptAir", "DAH": "Air Algérie", "TUX": "Tunisair",
    "AAL": "American Airlines", "UAL": "United Airlines", "DAL": "Delta", "ACA": "Air Canada", "AMX": "Aeroméxico",
    "SIA": "Singapore Airlines", "CPA": "Cathay Pacific", "ANA": "All Nippon", "JAL": "Japan Airlines",
    "CCA": "Air China", "CES": "China Eastern", "CSN": "China Southern", "KAL": "Korean Air", "AAR": "Asiana",
    "QFA": "Qantas", "ETH": "Ethiopian", "SAA": "South African", "AIC": "Air India", "UAE2": "Emirates",
    "DHL": "DHL", "DHX": "DHL", "BCS": "DHL (European Air Transport)", "FDX": "FedEx", "UPS": "UPS",
    "GTI": "Atlas Air", "ABW": "AirBridgeCargo", "CLX": "Cargolux", "QRC": "Qatar Cargo", "CKS": "Kalitta Air",
    "WJA": "WestJet", "JBU": "JetBlue", "SWA": "Southwest", "NKS": "Spirit", "FFT": "Frontier",
    "RUK": "Ryanair UK", "RYS": "Ryanair (Malta Air)", "MAY": "Malta Air", "LDM": "Lauda Europe",
    "WUK": "Wizz Air UK", "JAF": "TUI fly Belgium", "TUI": "TUI Airways", "TFL": "TUI fly Netherlands",
    "CFG": "Condor", "SUESS": "SunExpress", "GEC": "Lufthansa Cargo", "BOX": "AeroLogic", "SQC": "Singapore Cargo",
    "ICE": "Icelandair", "WIF": "Widerøe", "DAT": "Air Antwerp", "CSA": "Czech Airlines", "SMX": "Alitalia CityLiner",
    "EXS": "Jet2", "WUP": "Wizz Air", "AEA": "Air Europa", "AEU": "Air Europa Express", "MMD": "Air Dolomiti",
    "DLA": "Air Dolomiti", "BIE": "Blue Islands", "LGL": "Luxair", "MLD": "Air Moldova", "UKL": "Ukraine Air Alliance",
    "AFL": "Aeroflot", "SBI": "S7 Airlines", "SVR": "Ural Airlines", "PBD": "Pobeda",
}

# categoria dell'aereo (codice tipo TC + CA del messaggio di identità)
_CATEGORY = {
    (4, 1): "aereo leggero (<7 t)", (4, 2): "aereo piccolo (7–34 t)", (4, 3): "aereo grande (34–136 t)",
    (4, 4): "grande ad alta scia (es. B757)", (4, 5): "aereo pesante (>136 t)", (4, 6): "alte prestazioni",
    (4, 7): "elicottero", (3, 1): "aliante", (3, 2): "dirigibile o pallone", (3, 3): "paracadutista",
    (3, 4): "ultraleggero/deltaplano", (3, 6): "drone", (3, 7): "veicolo spaziale",
    (2, 1): "veicolo di servizio", (2, 3): "ostacolo fisso",
}

# intervalli di indirizzi ICAO (24 bit) → paese. Dall'allocazione ICAO (Annesso 10, vol. III).
_COUNTRY_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x3C0000, 0x3FFFFF, "Germania"), (0x3A0000, 0x3AFFFF, "Francia"), (0x380000, 0x39FFFF, "Francia"),
    (0x400000, 0x43FFFF, "Regno Unito"), (0x300000, 0x33FFFF, "Italia"), (0x340000, 0x37FFFF, "Spagna"),
    (0x480000, 0x4BFFFF, "Paesi Bassi"), (0x448000, 0x44FFFF, "Belgio"), (0x450000, 0x457FFF, "Danimarca"),
    (0x458000, 0x45FFFF, "Portogallo"), (0x460000, 0x467FFF, "Finlandia"), (0x468000, 0x46FFFF, "Grecia"),
    (0x470000, 0x477FFF, "Ungheria"), (0x478000, 0x47FFFF, "Norvegia"), (0x440000, 0x447FFF, "Austria"),
    (0x4A0000, 0x4A7FFF, "Svezia"), (0x4B0000, 0x4B7FFF, "Svizzera"), (0x4C0000, 0x4C7FFF, "Portogallo"),
    (0x4C8000, 0x4CFFFF, "Irlanda"), (0x4CA000, 0x4CAFFF, "Irlanda"), (0x490000, 0x497FFF, "Polonia"),
    (0x498000, 0x49FFFF, "Rep. Ceca"), (0x4A8000, 0x4AFFFF, "Turchia"), (0x4B8000, 0x4BFFFF, "Croazia"),
    (0x500000, 0x5FFFFF, "Europa (altri)"), (0x140000, 0x15FFFF, "Russia"), (0x100000, 0x1FFFFF, "Russia"),
    (0xA00000, 0xAFFFFF, "Stati Uniti"), (0xC00000, 0xC3FFFF, "Canada"), (0x7C0000, 0x7FFFFF, "Australia"),
    (0x800000, 0x83FFFF, "India"), (0x840000, 0x87FFFF, "Giappone"), (0x780000, 0x7BFFFF, "Cina"),
    (0x710000, 0x717FFF, "Arabia Saudita"), (0x896000, 0x896FFF, "Emirati Arabi Uniti"),
    (0x8A0000, 0x8A7FFF, "Indonesia"), (0x718000, 0x71FFFF, "Corea del Sud"), (0x738000, 0x73FFFF, "Israele"),
    (0x0A0000, 0x0A7FFF, "Algeria"), (0x020000, 0x027FFF, "Egitto"), (0x0D0000, 0x0D7FFF, "Tunisia"),
    (0x008000, 0x00FFFF, "Sudafrica"), (0x010000, 0x017FFF, "Egitto"), (0xE00000, 0xE3FFFF, "Argentina"),
    (0xE40000, 0xE7FFFF, "Brasile"), (0x0B0000, 0x0B7FFF, "Marocco"), (0x730000, 0x737FFF, "Iran"),
)


def airline(callsign: str | None) -> tuple[str | None, str | None]:
    """(nome compagnia, numero di volo) dal nominativo, se il prefisso è una compagnia nota."""
    if not callsign or len(callsign) < 3 or not callsign[:3].isalpha():
        return None, None
    name = AIRLINES.get(callsign[:3].upper())
    flight = callsign[3:].strip() or None
    return name, (flight if name else None)


def country(icao_hex: str) -> str | None:
    try:
        n = int(icao_hex, 16)
    except ValueError:
        return None
    for lo, hi, name in _COUNTRY_RANGES:
        if lo <= n <= hi:
            return name
    return None


def category(tc: int, ca: int) -> str | None:
    return _CATEGORY.get((tc, ca))
