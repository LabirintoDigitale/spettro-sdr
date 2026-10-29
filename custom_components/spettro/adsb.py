"""ADS-B (aerei, 1090 MHz): decodifica dei messaggi e tracciamento degli aerei nel tempo.

Gli aerei trasmettono in chiaro, 1090 MHz, impulsi PPM a 1 Mbit/s: un preambolo e 112 bit
protetti da un CRC a 24 bit (Mode S esteso, DF17/18). Ogni messaggio porta il codice ICAO
dell'aereo e, a seconda del tipo, il nominativo del volo, la quota, la velocità o la
posizione (codificata CPR). Con la posizione della stazione la posizione dell'aereo si
ricava da un solo messaggio (CPR locale), senza aspettare la coppia pari/dispari.

Solo numpy. Le formule CPR e il CRC sono quelle dello standard (DO-260 / ICAO Annex 10).
"""

from __future__ import annotations

import contextlib
import math
import time
from typing import Any

import numpy as np

from . import adsb_db

# La ricostruzione ai minimi quadrati usa BLAS, che per default si spalma su TUTTI i core
# (su una macchina multi-core = CPU al 200%+ e load altissimo durante lo scan aerei). La
# limitiamo a un solo thread: più prevedibile e, su matrici piccole come queste, anche più
# veloce. threadpoolctl è leggero (puro Python) ed è tra i requirements; se manca, nessun
# limite (i tetti MAX_REFINE/MAX_CAND bastano comunque a non far esplodere il lavoro).
try:
    from threadpoolctl import threadpool_limits

    def _un_solo_core():
        return threadpool_limits(limits=1)
except Exception:  # pragma: no cover - threadpoolctl non installato
    def _un_solo_core():
        return contextlib.nullcontext()

RATE = 2_400_000  # campionamento del ricevitore ADS-B in diretta (≥ 2 MS/s)
MAX_REFINE = 64  # max ricostruzioni ai minimi quadrati per blocco (0,25 s): tetto anti-saturazione CPU
MAX_CAND = 400  # max candidati preambolo elaborati per blocco (si tengono i più forti): ~1600/s
PATH_MAX = 3000  # punti massimi della traccia di un volo (una posizione ~2/s → copre ~25 min interi)
FREQ = 1_090_000_000.0
_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def _crc24(bits: list[int]) -> int:
    poly = 0x1FFF409
    n = int("".join(map(str, bits)), 2)
    for i in range(len(bits) - 1, 23, -1):
        if n >> i & 1:
            n ^= poly << (i - 24)
    return n & 0xFFFFFF


def _nl(lat: float) -> int:
    """Numero di zone di longitudine alla latitudine data (NL della codifica CPR)."""
    if abs(lat) >= 87:
        return 1
    a = 1 - math.cos(math.pi / 30)
    return int(math.floor(2 * math.pi / math.acos(1 - a / math.cos(math.pi / 180 * lat) ** 2)))


def cpr_local(lat_cpr: int, lon_cpr: int, odd: bool, ref: tuple[float, float]) -> tuple[float, float] | None:
    """Posizione da un solo messaggio, usando una posizione di riferimento entro ~300 km (CPR locale)."""
    lat_ref, lon_ref = ref
    dlat = 360.0 / (59 if odd else 60)
    yz = lat_cpr / 131072
    j = math.floor(lat_ref / dlat) + math.floor(0.5 + (lat_ref % dlat) / dlat - yz)
    lat = dlat * (j + yz)
    nl = _nl(lat)
    dlon = 360.0 / max(nl - (1 if odd else 0), 1)
    xz = lon_cpr / 131072
    m = math.floor(lon_ref / dlon) + math.floor(0.5 + (lon_ref % dlon) / dlon - xz)
    lon = dlon * (m + xz)
    if abs(lat) > 90 or abs(lat - lat_ref) > 5 or abs(((lon - lon_ref + 180) % 360) - 180) > 7:
        return None  # soluzione troppo lontana dal riferimento: scartata
    return lat, ((lon + 180) % 360) - 180


def cpr_global(even: tuple[int, int], odd: tuple[int, int], odd_last: bool) -> tuple[float, float] | None:
    """Posizione da una coppia pari/dispari (senza riferimento)."""
    lat0, lon0 = even[0] / 131072, even[1] / 131072
    lat1, lon1 = odd[0] / 131072, odd[1] / 131072
    j = math.floor(59 * lat0 - 60 * lat1 + 0.5)
    la0 = 360 / 60 * ((j % 60) + lat0)
    la1 = 360 / 59 * ((j % 59) + lat1)
    la0 -= 360 if la0 >= 270 else 0
    la1 -= 360 if la1 >= 270 else 0
    if _nl(la0) != _nl(la1):
        return None
    lat = la1 if odd_last else la0
    nl = _nl(lat)
    ni = max(nl - (1 if odd_last else 0), 1)
    m = math.floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
    lon = 360 / ni * ((m % ni) + (lon1 if odd_last else lon0))
    lon -= 360 if lon >= 180 else 0
    return lat, lon


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> int:
    """Direzione da a verso b, in gradi (0 = nord)."""
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return round(math.degrees(math.atan2(x, y)) % 360)


def _fix(bits: list[int], conf: np.ndarray) -> list[int] | None:
    """Messaggio DF17/18 con CRC giusto, correggendo se serve fino a 2 dei bit meno sicuri."""
    if int("".join(map(str, bits[:5])), 2) not in (17, 18):
        return None
    weak = [int(i) for i in np.argsort(conf[5:])[:10] + 5]
    tries: list[tuple[int, ...]] = [(), *((i,) for i in weak), *((i, j) for k, i in enumerate(weak) for j in weak[k + 1:])]
    for flip in tries:
        b = bits.copy()
        for i in flip:
            b[i] ^= 1
        if _crc24(b) == 0:
            return b
    return None


def decode_frames(iq: np.ndarray, rate: int, offset_hz: float = 0.0, carry_us: float = 0.0) -> list[list[int]]:
    """Trova i messaggi DF17/18 con CRC valido in una finestra di IQ.

    I preamboli ADS-B sono 4 impulsi da 0,5 µs seguiti da 112 bit PPM. A rate non multiplo
    esatto gli impulsi cadono a cavallo dei campioni: i 224 mezzi-bit si ricostruiscono con i
    minimi quadrati sulle aree. carry_us = quanto della fine va ignorato (gestito da chi chiama).
    """
    n = np.arange(len(iq))
    m = np.abs(iq * np.exp(-2j * np.pi * offset_hz * n / rate)).astype(np.float32)
    us = rate / 1e6
    p = [int(round(x * us)) for x in (0, 1, 3.5, 4.5)]
    q = [int(round(x * us)) for x in (0.5, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 5.5)]
    end = len(m) - int(121 * us)
    if end <= 0:
        return []
    # correlazione del preambolo senza impilare array (meno memoria/CPU: conta sui computer deboli)
    hi = m[p[0]: p[0] + end].copy()
    for k in p[1:]:
        np.minimum(hi, m[k: k + end], out=hi)
    lo = m[q[0]: q[0] + end].astype(np.float64)
    for k in q[1:]:
        lo += m[k: k + end]
    lo /= len(q)
    noise = float(np.median(m)) + 1e-9
    cand = np.nonzero((hi > 1.8 * lo) & (hi > 3 * noise))[0]
    # un vero preambolo accende più campioni adiacenti: tieni solo il picco di ogni gruppo. Senza questo,
    # con segnale/rumore forte si elaborano migliaia di candidati quasi identici (e la ricostruzione ai
    # minimi quadrati, pesante, satura la CPU su tutti i core).
    if len(cand) > 1:
        brk = np.nonzero(np.diff(cand) > int(us))[0] + 1
        cand = np.array([g[int(np.argmax(hi[g]))] for g in np.split(cand, brk)], dtype=cand.dtype)
    if len(cand) > MAX_CAND:  # ambiente affollato/disturbato: elabora solo i candidati più forti
        cand = np.sort(cand[np.argsort(hi[cand])[-MAX_CAND:]])
    h = 0.5 * us
    nchip = 224
    base = float(np.median(m))
    bit_c = np.arange(112) * us  # inizio di ogni bit rispetto a t0
    fast = rate >= 2_300_000  # con pochi campioni per bit la decisione dura non è affidabile: solo ricostruzione
    out: list[list[int]] = []
    last = -1000

    def refine(t0: float) -> list[int] | None:
        """Ricostruzione ai minimi quadrati dei 224 mezzi bit, con correzione di 1-2 bit."""
        n0, n1 = int(t0), int(t0 + nchip * h) + 1
        if n1 >= len(m):
            return None
        amp = float(np.mean([m[c + k] for k in p])) - base
        if amp <= 0:
            return None
        edges = t0 + np.arange(nchip + 1) * h
        rows = np.arange(n0, n1)
        w = np.clip(np.minimum(rows[:, None] + 1, edges[1:][None, :]) - np.maximum(rows[:, None], edges[:-1][None, :]), 0, None)
        chips = np.linalg.solve(w.T @ w + 0.05 * np.eye(nchip), w.T @ ((m[n0:n1] - base) / amp))
        soft = chips[0::2] - chips[1::2]
        return _fix((soft > 0).astype(int).tolist(), np.abs(soft))

    refines = 0  # tetto alle ricostruzioni pesanti per blocco: le trame pulite passano comunque dal CRC
    with _un_solo_core():  # niente fan-out BLAS su tutti i core durante la ricostruzione
        for c in cand:
            if c - last < int(100 * us):
                continue
            bits = None
            for shift in (0.0, -0.2, 0.2):
                t0 = c + shift + 8 * us
                if int(t0 + 112 * us) + 1 >= len(m):
                    break
                if fast:
                    # filtro veloce: decisione dura ai centri dei mezzi bit; la parte pesante
                    # (ricostruzione) parte solo se il tipo (DF) è giusto ma il CRC no
                    a = m[(t0 + bit_c + 0.25 * us).astype(np.int64)]
                    b = m[(t0 + bit_c + 0.75 * us).astype(np.int64)]
                    hard = (a > b).astype(np.int64)
                    if (hard[0] << 4 | hard[1] << 3 | hard[2] << 2 | hard[3] << 1 | hard[4]) not in (17, 18):
                        continue
                    hl = hard.tolist()
                    if _crc24(hl) == 0:
                        bits = hl
                        break
                if refines >= MAX_REFINE:  # limite di sicurezza: non far saturare la CPU in ambienti affollati
                    continue
                refines += 1
                bits = refine(t0)
                if bits is not None:
                    break
            if bits is not None:
                out.append(bits)
                last = c
    return out


def _alt_ft(me_bits: list[int]) -> int | None:
    """Quota barometrica da un messaggio di posizione in volo (bit Q)."""
    alt = int("".join(map(str, me_bits[8:20])), 2)
    if not alt & 0x10:
        return None
    return (((alt >> 5) << 4) | (alt & 0xF)) * 25 - 1000


class AircraftTracker:
    """Stato degli aerei visti, aggiornato messaggio per messaggio e invecchiato nel tempo."""

    def __init__(self, home: tuple[float, float] | None = None) -> None:
        self.home = home
        self.planes: dict[str, dict[str, Any]] = {}
        self.messages = 0  # messaggi totali con CRC valido
        self.positions = 0
        self.record = False  # se True tiene il percorso (lista di posizioni) di ogni aereo, per lo storico

    def feed_frames(self, frames: list[list[int]], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for bits in frames:
            self.messages += 1
            self._update(bits, now)

    def _update(self, bits: list[int], now: float) -> None:
        icao = f"{int(''.join(map(str, bits[8:32])), 2):06X}"
        pl = self.planes.get(icao)
        if pl is None:
            pl = self.planes[icao] = {"icao": icao, "first": now, "msgs": 0}
        pl["last"] = now
        pl["msgs"] += 1
        me = bits[32:88]
        mv = lambda a, b: int("".join(map(str, me[a:b])), 2)  # noqa: E731
        tc = mv(0, 5)
        if 1 <= tc <= 4:
            call = "".join(_CHARS[mv(8 + 6 * i, 14 + 6 * i)] for i in range(8)).replace("#", "").strip()
            if call:
                pl["call"] = call
            pl["cat"] = (tc, mv(5, 8))  # categoria (leggero, pesante, elicottero…)
        elif 5 <= tc <= 18:  # posizione (superficie 5-8, in volo 9-18)
            if 9 <= tc <= 18:
                alt = _alt_ft(me)
                if alt is not None:
                    pl["alt_ft"] = alt
            odd = bool(me[21])
            lat_cpr, lon_cpr = mv(22, 39), mv(39, 56)
            pl_odd = ("odd" if odd else "even")
            pl[pl_odd] = (lat_cpr, lon_cpr, now)
            pos = None
            if self.home is not None:
                pos = cpr_local(lat_cpr, lon_cpr, odd, self.home)
            if pos is None and "odd" in pl and "even" in pl and abs(pl["odd"][2] - pl["even"][2]) < 10:
                pos = cpr_global(pl["even"][:2], pl["odd"][:2], odd)
            if pos is not None:
                pl["lat"], pl["lon"], pl["pos_time"] = pos[0], pos[1], now
                self.positions += 1
                if self.record:
                    path = pl.setdefault("path", [])
                    pt = [round(pos[0], 4), round(pos[1], 4), pl.get("alt_ft"), round(now, 1)]
                    # un punto per ogni posizione ricevuta (si salta solo se identica all'ultima)
                    if not path or path[-1][0] != pt[0] or path[-1][1] != pt[1]:
                        path.append(pt)
                        if len(path) > PATH_MAX:
                            del path[0]
        elif tc == 19 and mv(5, 8) in (1, 2):  # velocità al suolo
            ew = (mv(14, 24) - 1) * (-1 if me[13] else 1)
            ns = (mv(25, 35) - 1) * (-1 if me[24] else 1)
            pl["speed_kt"] = round(math.hypot(ew, ns))
            pl["track"] = round(math.degrees(math.atan2(ew, ns)) % 360)
            vr = mv(37, 46)
            if vr:
                pl["vrate_fpm"] = (vr - 1) * 64 * (-1 if me[36] else 1)
        elif tc == 28 and mv(5, 8) == 1:  # stato dell'aereo: emergenza dichiarata
            em = mv(8, 11)
            pl["emergency"] = em if em else None

    def prune(self, max_age_s: float, now: float | None = None) -> list[dict[str, Any]]:
        """Toglie gli aerei non più sentiti e li restituisce (per eventuale salvataggio nello storico)."""
        now = time.time() if now is None else now
        old = [k for k, p in self.planes.items() if now - p["last"] > max_age_s]
        return [self.planes.pop(k) for k in old]

    def aircraft(self) -> list[dict[str, Any]]:
        """Aerei attuali per il pannello, i più vicini (o i più recenti) per primi."""
        out = []
        for p in self.planes.values():
            a: dict[str, Any] = {"icao": p["icao"], "call": p.get("call"), "msgs": p["msgs"],
                                 "age_s": round(time.time() - p["last"], 1)}
            if "alt_ft" in p:
                a["alt_ft"] = p["alt_ft"]
                a["alt_m"] = round(p["alt_ft"] * 0.3048)
            if "speed_kt" in p:
                a["speed_kt"] = p["speed_kt"]
                a["speed_kmh"] = round(p["speed_kt"] * 1.852)
                a["track"] = p.get("track")
            if "vrate_fpm" in p:
                a["vrate_fpm"] = p["vrate_fpm"]
            a["country"] = adsb_db.country(p["icao"])
            name, flight = adsb_db.airline(p.get("call"))
            a["airline"], a["flight"] = name, flight
            if "cat" in p:
                a["category"] = adsb_db.category(*p["cat"])
            if p.get("emergency"):
                a["emergency"] = ("generica", "medica", "carburante", "interferenza illecita",
                                  "nessuna comunicazione", "aereo in difficoltà")[min(p["emergency"] - 1, 5)]
            if "lat" in p:
                a["lat"], a["lon"] = round(p["lat"], 5), round(p["lon"], 5)
                if self.home is not None:
                    a["km"] = round(distance_km(self.home, (p["lat"], p["lon"])), 1)
                    a["bearing"] = bearing_deg(self.home, (p["lat"], p["lon"]))
            out.append(a)
        out.sort(key=lambda a: (a.get("km", 1e9), -a["msgs"]))
        return out
