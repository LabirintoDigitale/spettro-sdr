"""Verifica del protocollo nelle analisi: si prova davvero a decodificare il segnale registrato.

Le misure (banda, livelli FSK, burst) danno un'ipotesi; qui la si conferma cercando le
strutture del protocollo: trame AX.25/APRS e AIS con il loro CRC, messaggi ADS-B con il
CRC a 24 bit, parole di sincronismo DMR/P25/dPMR e POCSAG, testo Morse e RTTY in onde
corte. Dei protocolli privati (cercapersone, radio digitali) si conferma solo il tipo,
senza mostrare il contenuto.
"""

from __future__ import annotations

import io
import math
import wave
from typing import Any

import numpy as np

from . import textdec
from .demod import _channel, demodulate
from .ft8 import distance_km

AUDIO_FS = 48_000  # FM demodulata per i protocolli a toni e a livelli


def _fm(iq: np.ndarray, rate: int, offset_hz: float, half_bw: float) -> tuple[np.ndarray, float]:
    """Frequenza istantanea (Hz) del canale intorno a offset_hz, a circa 48 kHz."""
    decim = max(1, round(rate / AUDIO_FS))
    fs = rate / decim
    z = _channel(iq, rate, offset_hz, decim, half_bw)
    f = np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi)
    return f.astype(np.float64), fs


# --- HDLC (AX.25, AIS) -----------------------------------------------------------------

def _crc_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _hdlc_frames(bits: np.ndarray, min_bytes: int) -> list[bytes]:
    """Bit già decodificati NRZI → trame tra i flag 0x7E, senza bit di riempimento, con CRC giusto."""
    frames = []
    s = "".join("1" if b else "0" for b in bits)
    for chunk in s.split("01111110"):
        if len(chunk) < min_bytes * 8 or "1111111" in chunk:
            continue
        unstuffed = chunk.replace("111110", "11111")
        n = len(unstuffed) // 8
        if n < min_bytes or len(unstuffed) % 8:
            continue
        data = bytes(int(unstuffed[8 * i: 8 * i + 8][::-1], 2) for i in range(n))  # LSB per primo
        if _crc_x25(data[:-2]) == int.from_bytes(data[-2:], "little"):
            frames.append(data[:-2])
    return frames


def _nrzi(levels: np.ndarray) -> np.ndarray:
    """NRZI: 1 = nessun cambio di livello, 0 = cambio."""
    return np.concatenate([[1], levels[1:] == levels[:-1]]).astype(np.uint8)


def _slice_bits(x: np.ndarray, sps: float, phases: int = 8) -> list[np.ndarray]:
    """Livelli (x > 0) presi a ogni bit, per alcune fasi dell'orologio."""
    out = []
    for p in range(phases):
        idx = (p * sps / phases + sps / 2 + np.arange(int((len(x) - sps) / sps)) * sps).astype(int)
        out.append(x[idx] > 0)
    return out


def _ax25_addr(b: bytes) -> str:
    call = "".join(chr(c >> 1) for c in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def _aprs_position(info: str) -> tuple[float, float] | None:
    """Posizione APRS non compressa: !DDMM.mmN/DDDMM.mmE (anche =, / e @ con l'ora)."""
    if not info or info[0] not in "!=/@":
        return None
    s = info[8:] if info[0] in "/@" else info[1:]
    try:
        lat = int(s[0:2]) + float(s[2:7]) / 60
        lon = int(s[9:12]) + float(s[12:17]) / 60
    except ValueError:
        return None
    if s[7] == "S":
        lat = -lat
    if s[17] == "W":
        lon = -lon
    return lat, lon


def ax25(iq: np.ndarray, rate: int, offset_hz: float, home: tuple[float, float] | None) -> dict[str, Any] | None:
    """Packet radio AFSK 1200 baud (APRS): toni 1200/2200 Hz, NRZI, HDLC, CRC."""
    f, fs = _fm(iq, rate, offset_hz, 8e3)
    if len(f) < fs * 0.2:
        return None
    a = f - np.median(f)
    t = np.arange(len(a)) / fs
    sps = fs / 1200
    k = np.ones(int(round(sps))) / sps
    m1 = np.abs(np.convolve(a * np.exp(-2j * np.pi * 1200 * t), k, "same"))
    m2 = np.abs(np.convolve(a * np.exp(-2j * np.pi * 2200 * t), k, "same"))
    d = m1 - m2
    frames: dict[bytes, None] = {}
    for lv in _slice_bits(d, sps):
        for fr in _hdlc_frames(_nrzi(lv), 17):
            frames[fr] = None
    out = []
    for fr in frames:
        # indirizzi: 7 byte ciascuno, l'ultimo ha il bit 0 a 1
        n = next((i for i in range(6, len(fr), 7) if fr[i] & 1), None)
        if n is None or n + 3 > len(fr):
            continue
        addrs = [_ax25_addr(fr[i: i + 7]) for i in range(0, n + 1, 7)]
        info = fr[n + 3:].decode("latin-1", "replace").strip()
        path = f"{addrs[1]}>{addrs[0]}" + "".join("," + x for x in addrs[2:])
        pos = _aprs_position(info)
        item = {"from": addrs[1], "to": addrs[0], "path": path, "info": info[:120]}
        if pos and home:
            item["km"] = round(distance_km(home, pos))
        out.append(item)
    if not out:
        return None
    aprs = any(x["to"].startswith("AP") or x["info"][:1] in "!=/@;)>:`'" for x in out)
    name = "APRS (packet radio AX.25, 1200 baud)" if aprs else "packet radio AX.25 (1200 baud)"
    return {"protocol": name, "kind": f"{name}: {len(out)} trame decodificate", "log": True,
            "lines": [f"{x['path']}: {x['info']}" + (f" ({x['km']} km)" if x.get("km") is not None else "") for x in out]}


# --- AIS -------------------------------------------------------------------------------

def ais(iq: np.ndarray, rate: int, offset_hz: float, home: tuple[float, float] | None) -> dict[str, Any] | None:
    """AIS delle navi: GMSK 9600 baud, NRZI, HDLC, CRC."""
    f, fs = _fm(iq, rate, offset_hz, 10e3)
    sps = fs / 9600
    a = np.convolve(f - np.median(f), np.ones(max(1, int(sps / 2))), "same")
    frames: dict[bytes, None] = {}
    for lv in _slice_bits(a, sps):
        for fr in _hdlc_frames(_nrzi(lv), 20):
            frames[fr] = None
    out = []
    for fr in frames:
        b = "".join(f"{x:08b}" for x in fr)  # ogni byte arriva dal bit meno significativo; i campi vanno dal più significativo
        typ, mmsi = int(b[0:6], 2), int(b[8:38], 2)
        line = f"MMSI {mmsi}, messaggio di tipo {typ}"
        if typ in (1, 2, 3) and len(b) >= 116:
            lon = _signed(b[61:89]) / 600000
            lat = _signed(b[89:116]) / 600000
            sog = int(b[50:60], 2) / 10
            if abs(lat) <= 90 and abs(lon) <= 180:
                line += f": posizione {lat:.4f}, {lon:.4f}, velocità {sog:g} nodi"
                if home:
                    line += f" ({round(distance_km(home, (lat, lon)))} km)"
        if typ == 5 and len(b) >= 232:
            name = _ais_text(b[112:232])
            line += f": nave «{name}»"
        out.append(line)
    if not out:
        return None
    return {"protocol": "AIS (navi)", "kind": f"AIS: identificazione e posizione delle navi, {len(out)} messaggi",
            "lines": out, "log": True}


def _signed(bits: str) -> int:
    v = int(bits, 2)
    return v - (1 << len(bits)) if bits[0] == "1" else v


def _ais_text(bits: str) -> str:
    chars = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
    return "".join(chars[int(bits[i: i + 6], 2)] for i in range(0, len(bits) - 5, 6)).rstrip("@ ").strip()


# --- POCSAG ----------------------------------------------------------------------------

POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197


def _bch_ok(cw: int) -> bool:
    """Codice BCH(31,21) + parità pari della parola POCSAG."""
    if bin(cw).count("1") % 2:
        return False
    r = cw >> 1
    for i in range(30, 9, -1):
        if r & (1 << i):
            r ^= 0x769 << (i - 10)
    return r == 0


def pocsag(iq: np.ndarray, rate: int, offset_hz: float) -> dict[str, Any] | None:
    """Cercapersone POCSAG (512/1200/2400 baud): si conferma la parola di sincronismo, il contenuto no."""
    f, fs = _fm(iq, rate, offset_hz, 9e3)
    best: dict[str, Any] | None = None
    for baud in (512, 1200, 2400):
        sps = fs / baud
        a = np.convolve(f - np.median(f), np.ones(max(1, int(sps * 0.6))), "same")
        for lv in _slice_bits(a, sps, 4):
            for inv in (False, True):
                bits = (~lv if inv else lv).astype(np.uint8)
                s = "".join("1" if x else "0" for x in bits)
                pos = s.find(f"{POCSAG_SYNC:032b}")
                if pos < 0:
                    continue
                batches = msgs = addrs = ok = 0
                i = pos
                while i + 32 * 17 <= len(s):
                    if int(s[i: i + 32], 2) != POCSAG_SYNC:
                        break
                    batches += 1
                    for k in range(16):
                        cw = int(s[i + 32 + 32 * k: i + 64 + 32 * k], 2)
                        if cw == POCSAG_IDLE:
                            continue
                        if _bch_ok(cw):
                            ok += 1
                            if cw >> 31:
                                msgs += 1
                            else:
                                addrs += 1
                    i += 32 * 17
                if batches and (best is None or ok > best["ok"]):
                    best = {"baud": baud, "batches": batches, "ok": ok, "addrs": addrs}
    if not best:
        return None
    return {"protocol": f"POCSAG {best['baud']} baud",
            "kind": f"cercapersone POCSAG {best['baud']} baud: {best['addrs']} chiamate in {best['batches']} blocchi",
            "lines": [f"Parola di sincronismo trovata, {best['ok']} parole valide (codice BCH). Il contenuto dei "
                      "messaggi è privato e non viene mostrato."]}


# --- DMR, P25, dPMR ---------------------------------------------------------------------

# parole di sincronismo (48 bit = 24 simboli 4FSK); dibit 01 → +3, 00 → +1, 10 → −1, 11 → −3
SYNC_4FSK = {
    "DMR stazione base, voce": (0x755FD7DF75F7, 4800),
    "DMR stazione base, dati": (0xDFF57D75DF5D, 4800),
    "DMR terminale, voce": (0x7F7D5DD57DFD, 4800),
    "DMR terminale, dati": (0xD5D7F77FD757, 4800),
    "P25 fase 1": (0x5575F5FF77FF, 4800),
    "dPMR": (0x57FF5F75D577, 2400),
}
_DIBIT = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}


def _sync_wave(word: int, sps: float) -> np.ndarray:
    sym = [_DIBIT[(word >> (46 - 2 * i)) & 3] for i in range(24)]
    n = int(round(24 * sps))
    w = np.array(sym, float)[(np.arange(n) / sps).astype(int)]
    return (w - w.mean()) / np.linalg.norm(w - w.mean())


def digital_voice(iq: np.ndarray, rate: int, offset_hz: float) -> dict[str, Any] | None:
    """Radio digitali 4FSK: si cercano le parole di sincronismo nella FM demodulata."""
    f, fs = _fm(iq, rate, offset_hz, 7e3)
    f = np.convolve(f, np.ones(5) / 5, "same")
    found: dict[str, int] = {}
    for name, (word, baud) in SYNC_4FSK.items():
        w = _sync_wave(word, fs / baud)
        n = len(w)
        if len(f) < 4 * n:
            continue
        num = np.correlate(f - np.mean(f), w, "valid")
        # norma di ogni finestra (per la correlazione normalizzata)
        c2 = np.concatenate([[0.0], np.cumsum((f - np.mean(f)) ** 2)])
        s1 = np.concatenate([[0.0], np.cumsum(f - np.mean(f))])
        energy = (c2[n:] - c2[:-n]) - (s1[n:] - s1[:-n]) ** 2 / n
        corr = num / np.sqrt(np.maximum(energy, 1e-9))
        hits = np.nonzero(corr > 0.8)[0]  # col segno: voce e dati hanno sincronismi opposti
        # una sola volta per trama
        count, last = 0, -10 * n
        for h in hits:
            if h - last > n:
                count += 1
                last = h
        if count:
            found[name] = count
    if not found:
        return None
    best = max(found, key=lambda k: found[k])
    family = best.split(",")[0].split(" ")[0]
    total = sum(v for k, v in found.items() if k.startswith(family))
    return {"protocol": family, "kind": f"radio digitale {family} confermata ({best}): {total} sincronismi",
            "lines": [f"{k}: {v} parole di sincronismo" for k, v in found.items()]
            + ["Le conversazioni sono private (e spesso cifrate): si conferma solo il tipo di radio."]}


# --- ADS-B / Mode S ---------------------------------------------------------------------

def _crc24(bits: list[int]) -> int:
    poly = 0x1FFF409
    n = int("".join(map(str, bits)), 2)
    nb = len(bits)
    for i in range(nb - 1, 23, -1):
        if n >> i & 1:
            n ^= poly << (i - 24)
    return n & 0xFFFFFF


_ADSB_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def _nl(lat: float) -> int:
    if abs(lat) >= 87:
        return 1
    a = 1 - math.cos(math.pi / 30)
    return int(math.floor(2 * math.pi / math.acos(1 - a / math.cos(math.pi / 180 * lat) ** 2)))


def _cpr_global(even: tuple[int, int], odd: tuple[int, int], odd_last: bool) -> tuple[float, float] | None:
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


def adsb(iq: np.ndarray, rate: int, offset_hz: float, home: tuple[float, float] | None) -> dict[str, Any] | None:
    """Messaggi ADS-B (1090 MHz) degli aerei: preambolo a impulsi, 112 bit, CRC a 24 bit."""
    if rate < 1_900_000:
        return None
    n = np.arange(len(iq))
    m = np.abs(iq * np.exp(-2j * np.pi * offset_hz * n / rate)).astype(np.float32)
    us = rate / 1e6  # campioni per microsecondo
    p = [int(round(x * us)) for x in (0, 1, 3.5, 4.5)]
    q = [int(round(x * us)) for x in (0.5, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 5.5)]
    end = len(m) - int(121 * us)
    if end <= 0:
        return None
    hi = np.min(np.stack([m[k: k + end] for k in p]), axis=0)
    lo = np.mean(np.stack([m[k: k + end] for k in q]), axis=0)
    cand = np.nonzero(hi > 1.8 * lo)[0]
    noise = float(np.median(m)) + 1e-9
    cand = cand[hi[cand] > 3 * noise]
    planes: dict[str, dict[str, Any]] = {}
    msgs = 0
    last = -1000
    # ogni campione somma la parte di impulsi che cade nel suo intervallo [k, k+1]: a 2 MS/s gli impulsi da
    # 0,5 µs finiscono a cavallo di due campioni, quindi si ricostruiscono i 224 mezzi bit con i minimi quadrati
    h = 0.5 * us
    base = float(np.median(m))
    nchip = 224
    for c in cand:
        if c - last < int(100 * us):
            continue
        amp = float(np.mean([m[c + k] for k in p])) - base
        for shift in (0.0, -0.25, 0.25):
            t0 = c + shift + 8 * us
            n0, n1 = int(t0), int(t0 + nchip * h) + 1
            if n1 >= len(m) or amp <= 0:
                break
            edges = t0 + np.arange(nchip + 1) * h
            lo_e, hi_e = edges[:-1], edges[1:]
            rows = np.arange(n0, n1)
            w = np.clip(np.minimum(rows[:, None] + 1, hi_e[None, :]) - np.maximum(rows[:, None], lo_e[None, :]), 0, None)
            y = (m[n0:n1] - base) / amp
            chips = np.linalg.solve(w.T @ w + 0.05 * np.eye(nchip), w.T @ y)
            soft = chips[0::2] - chips[1::2]
            bits = _adsb_fix((soft > 0).astype(int).tolist(), np.abs(soft))
            if bits is None:
                continue
            last = c
            msgs += 1
            _adsb_parse(bits, planes)
            break
    if not msgs:
        return None
    lines = []
    for icao, pl in planes.items():
        pos = None
        if "even" in pl and "odd" in pl:
            pos = _cpr_global(pl["even"], pl["odd"], pl["odd_last"])
        parts = [f"aereo {icao}"]
        if pl.get("call"):
            parts.append(f"volo {pl['call']}")
        if pl.get("alt_ft") is not None:
            parts.append(f"quota {round(pl['alt_ft'] * 0.3048):,} m".replace(",", "."))
        if pl.get("speed_kt") is not None:
            parts.append(f"{round(pl['speed_kt'] * 1.852)} km/h verso {pl['heading']}°")
        if pos:
            parts.append(f"posizione {pos[0]:.3f}, {pos[1]:.3f}")
            if home:
                parts.append(f"a {round(distance_km(home, pos))} km")
        lines.append(", ".join(parts))
    n_pl = "1 aereo" if len(planes) == 1 else f"{len(planes)} aerei"
    return {"protocol": "ADS-B", "kind": f"ADS-B (aerei, 1090 MHz): {n_pl}, {msgs} messaggi decodificati",
            "lines": lines}


def _adsb_fix(bits: list[int], conf: np.ndarray) -> list[int] | None:
    """Messaggio DF17/18 con CRC giusto, correggendo se serve fino a 2 dei bit meno sicuri."""
    weak = [int(i) for i in np.argsort(conf[5:])[:10] + 5]  # il tipo (DF) deve essere già giusto
    if int("".join(map(str, bits[:5])), 2) not in (17, 18):
        return None
    tries: list[tuple[int, ...]] = [(), *((i,) for i in weak), *((i, j) for k, i in enumerate(weak) for j in weak[k + 1:])]
    for flip in tries:
        b = bits.copy()
        for i in flip:
            b[i] ^= 1
        if _crc24(b) == 0:
            return b
    return None


def _adsb_parse(bits: list[int], planes: dict[str, dict[str, Any]]) -> None:
    v = lambda a, b: int("".join(map(str, bits[a:b])), 2)  # noqa: E731
    icao = f"{v(8, 32):06X}"
    pl = planes.setdefault(icao, {})
    me = bits[32:88]
    tc = int("".join(map(str, me[:5])), 2)
    mv = lambda a, b: int("".join(map(str, me[a:b])), 2)  # noqa: E731
    if 1 <= tc <= 4:
        pl["call"] = "".join(_ADSB_CHARS[mv(8 + 6 * i, 14 + 6 * i)] for i in range(8)).replace("#", "").strip()
    elif 9 <= tc <= 18:
        alt = mv(8, 20)
        if alt & 0x10:
            pl["alt_ft"] = ((alt >> 5) << 4 | (alt & 0xF)) * 25 - 1000
        odd = me[21]
        pl["odd" if odd else "even"] = (mv(22, 39), mv(39, 56))
        pl["odd_last"] = bool(odd)
    elif tc == 19 and mv(5, 8) in (1, 2):
        ew = (mv(14, 24) - 1) * (-1 if me[13] else 1)
        ns = (mv(25, 35) - 1) * (-1 if me[24] else 1)
        pl["speed_kt"] = math.hypot(ew, ns)
        pl["heading"] = round(math.degrees(math.atan2(ew, ns)) % 360)


# --- onde corte: Morse e RTTY -----------------------------------------------------------

def _pcm(wav: bytes) -> bytes:
    with wave.open(io.BytesIO(wav)) as w:
        return w.readframes(w.getnframes())


def _plausible(text: str, min_len: int) -> bool:
    t = text.strip()
    if len(t) < min_len:
        return False
    good = sum(c.isalnum() or c in " .,?/-=+" for c in t)
    return good / len(t) > 0.8 and t.count("_") / len(t) < 0.2


def hf_text(iq: np.ndarray, rate: int, offset_hz: float, bw_khz: float) -> dict[str, Any] | None:
    """Telescrivente RTTY (45, 50, 75 baud) o Morse, provati sull'audio della registrazione."""
    best = None
    if bw_khz > 4:  # troppo largo per una telescrivente o il Morse (l'analisi misura la banda a passi di ~1 kHz)
        return None
    usb = _pcm(demodulate(iq, rate, offset_hz - 1500, "usb"))  # segnale portato intorno a 1500 Hz di audio
    for baud, shift, label in ((45.45, 170, "RTTY 45 baud, 170 Hz (radioamatori)"), (50, 450, "RTTY 50 baud, 450 Hz (meteo)"),
                               (50, 85, "RTTY 50 baud, 85 Hz"), (75, 170, "RTTY 75 baud, 170 Hz"), (50, 850, "RTTY 50 baud, 850 Hz")):
        d = textdec.RttyDecoder(baud, shift)
        text = d.feed(usb)
        u = d.uarts[d.best]
        # con dati qualsiasi il bit di stop è giusto una volta su due: qui quasi sempre
        if not d.center or u.good < 6 or u.bad > 0.08 * (u.good + u.bad) or not _plausible(text, 6):
            continue
        # i due toni devono essere proprio a quella distanza: picchi dello spettro a centro ± spostamento/2
        f, pw = d.audio.spectrum()
        tone = sum(float(pw[np.argmin(np.abs(f - (d.center + sgn * shift / 2)))]) for sgn in (-1, 1)) / (2 * float(pw.max()))
        score = (tone, u.good)
        if best is None or score > best[0]:
            best = (score, label, text)
    if best and best[0][0] > 0.3:
        return {"protocol": "RTTY", "kind": f"telescrivente {best[1]}: testo decodificato",
                "lines": [best[2].strip()[:300]]}
    cw = _pcm(demodulate(iq, rate, offset_hz, "cw"))
    # due passate: la prima trova tono e velocità, la seconda rilegge dall'inizio senza perdere i primi segni
    first = textdec.MorseDecoder()
    first.feed(cw)
    if not first.tone:
        return None
    d = textdec.MorseDecoder()
    d.tone, d.dot = first.tone, first.dot
    text = d.feed(cw) + d.flush()
    if _plausible(text, 3):
        st = d.status()
        return {"protocol": "Morse", "kind": f"telegrafia Morse (CW), ~{st['wpm']} parole/min: testo decodificato",
                "lines": [text.strip()[:300]]}
    return None


# --- parametri di un segnale dati qualsiasi -----------------------------------------------

# velocità dei simboli (baud) note e cosa usa quella velocità
KNOWN_RATES = (
    (18000, None, "TETRA (π/4-DQPSK, servizi di emergenza, cifrato)"),
    (9600, 2, "packet radio 9600 o AIS (GMSK)"),
    (4800, 4, "DMR, P25 o NXDN (4FSK)"),
    (4800, 2, "dati 2FSK a 4800 baud"),
    (3600, 4, "NXDN 4800 (4FSK)"),
    (2400, 4, "dPMR o NXDN48 (4FSK)"),
    (2400, 2, "POCSAG 2400 o telemetria"),
    (1200, 2, "POCSAG 1200, telemetria o packet AFSK"),
    (512, 2, "POCSAG 512 (cercapersone)"),
)


def fsk_params(iq: np.ndarray, rate: int, offset_hz: float, half_bw: float = 12.5e3) -> dict[str, Any] | None:
    """Livelli di frequenza, deviazione e velocità dei simboli di un segnale dati, con i protocolli compatibili."""
    f, fs = _fm(iq, rate, offset_hz, half_bw)
    decim = max(1, round(rate / AUDIO_FS))
    z = _channel(iq, rate, offset_hz, decim, half_bw)
    pw = np.abs(z[1:]) ** 2
    on = pw > 0.25 * np.median(pw[pw > np.percentile(pw, 50)])
    if on.sum() < fs * 0.05:
        return None
    # livelli: dove la frequenza si ferma più spesso (istogramma della FM demodulata, solo quando trasmette)
    fs5 = np.convolve(f, np.ones(5) / 5, "same")[on]
    lo, hi = np.percentile(fs5, [1, 99])
    levels: list[float] = []
    if hi - lo > 400:
        pad = 0.15 * (hi - lo)  # margine: i livelli estremi non devono cadere sul bordo dell'istogramma
        h, e = np.histogram(fs5, bins=60, range=(lo - pad, hi + pad))
        h = np.convolve(h, [1, 2, 3, 2, 1], "same").astype(float)
        peaks = [i for i in range(1, len(h) - 1) if h[i] >= h[i - 1] and h[i] > h[i + 1] and h[i] > 0.3 * h.max()]
        keep: list[int] = []
        for i in peaks:
            if keep and min(h[keep[-1]: i + 1]) > 0.75 * min(h[keep[-1]], h[i]):
                if h[i] > h[keep[-1]]:
                    keep[-1] = i
                continue
            keep.append(i)
        levels = [float((e[i] + e[i + 1]) / 2) for i in keep]
    # velocità dei simboli: riga spettrale dei cambi di frequenza (e dell'inviluppo, per le modulazioni di fase)
    baud = None
    best = 0.0
    seg = 16384  # spettro mediato su più tratti (Welch): il rumore non fa righe false
    for x in (np.abs(np.diff(f)) * on[1:], np.abs(z[1:]) * on):
        k = len(x) // seg
        if k < 2:
            continue
        xs = x[: k * seg].reshape(k, seg)
        xs = xs - xs.mean(axis=1, keepdims=True)
        spec = (np.abs(np.fft.rfft(xs * np.hanning(seg), axis=1)) ** 2).mean(axis=0)
        fr = np.fft.rfftfreq(seg, 1 / fs)
        sel = (fr > 300) & (fr < min(25e3, fs / 2 * 0.9))
        i = int(np.argmax(spec[sel]))
        ratio = float(spec[sel][i] / (np.median(spec[sel]) + 1e-12))
        if ratio > 12 and ratio > best:
            best, baud = ratio, float(fr[sel][i])
    n_lv = len(levels) if len(levels) in (2, 4) else None
    guess = []
    if baud:
        for b, n, name in KNOWN_RATES:
            if abs(baud - b) / b < 0.03 and (n is None or n_lv is None or n == n_lv):
                guess.append(name)
    out = {"baud": round(baud) if baud else None, "levels_hz": [round(v) for v in levels],
           "deviation_khz": round((max(levels) - min(levels)) / 2000, 2) if len(levels) >= 2 else None,
           "guess": guess}
    if not n_lv and not guess:
        return None  # né livelli chiari né una velocità nota: niente di affidabile da dire
    return out


def params_text(p: dict[str, Any]) -> str:
    parts = []
    if p.get("levels_hz") and len(p["levels_hz"]) in (2, 4):
        parts.append(f"{len(p['levels_hz'])}FSK, deviazione ±{p['deviation_khz']:g} kHz")
    if p.get("baud"):
        parts.append(f"{p['baud']} simboli/s")
    text = ", ".join(parts)
    if p.get("guess"):
        text += " → compatibile con " + " o ".join(p["guess"])
    return text


# modi digitali che non si decodificano in pochi secondi: si riconoscono dalla frequenza
FT8_DIALS = (1.840, 3.573, 7.074, 10.136, 14.074, 18.100, 21.074, 24.915, 28.074, 50.313, 144.174)
FT4_DIALS = (3.575, 7.0475, 10.140, 14.080, 18.104, 21.140, 24.919, 28.180, 50.318)
WSPR_DIALS = (3.5686, 7.0386, 10.1387, 14.0956, 18.1046, 21.0946, 24.9246, 28.1246, 50.293, 144.489)


def by_frequency(f_mhz: float) -> dict[str, Any] | None:
    for dials, name, lo, hi, how in ((WSPR_DIALS, "WSPR", 1.4e-3, 1.6e-3, "Radiofari WSPR"),
                                     (FT8_DIALS, "FT8", 0.1e-3, 3.0e-3, "Messaggi FT8"),
                                     (FT4_DIALS, "FT4", 0.1e-3, 3.0e-3, None)):
        for d in dials:
            if d + lo <= f_mhz <= d + hi:
                return {"protocol": name, "kind": f"probabile {name} (modo digitale dei radioamatori)",
                        "lines": [f"La frequenza è nella sottobanda {name} ({d:.4f} MHz in USB). "
                                  + (f"Per leggere i messaggi: ▶ Analisi continua sulla frequenza {d:.4f} in USB con "
                                     f"la decodifica «{how}»." if how else "")]}
    return None


def run(iq: np.ndarray, rate: int, center_hz: float, result: dict[str, Any],
        home: tuple[float, float] | None = None) -> dict[str, Any] | None:
    """Prova i decoder adatti al segnale misurato; il primo che trova strutture valide vince."""
    target = result["target_mhz"] * 1e6
    if abs(target - 1090e6) < 1.5e6:  # aerei: impulsi brevissimi, nello spettro medio quasi non si vedono
        try:
            return adsb(iq, rate, 1090e6 - center_hz, home)
        except (ValueError, IndexError):
            return None
    if not result.get("detected"):
        return None
    f_hz = result["center_mhz"] * 1e6
    off = f_hz - center_hz
    bw = result["obw_99_khz"]
    f_mhz = f_hz / 1e6
    tries = []
    if f_mhz < 30:
        tries.append(lambda: by_frequency(target / 1e6))
        tries.append(lambda: hf_text(iq, rate, off, bw))
    elif bw <= 30:
        if 161.9 <= f_mhz <= 162.1:
            tries.append(lambda: ais(iq, rate, off, home))
        tries.append(lambda: ax25(iq, rate, off, home))
        tries.append(lambda: digital_voice(iq, rate, off))
        tries.append(lambda: pocsag(iq, rate, off))
        if 144.0 <= f_mhz <= 146.0:
            tries.append(lambda: by_frequency(f_mhz))
    for t in tries:
        try:
            v = t()
        except (ValueError, IndexError):
            v = None
        if v:
            return v
    return None


def live_identify(iq: np.ndarray, rate: int, offset_hz: float, home: tuple[float, float] | None) -> dict[str, Any]:
    """Ascolto in diretta: parametri del segnale e tutti i decoder di dati sulla finestra di IQ."""
    out: dict[str, Any] = {"params": None, "found": []}
    try:
        out["params"] = fsk_params(iq, rate, offset_hz)
    except (ValueError, IndexError):
        pass
    for fn in (lambda: ax25(iq, rate, offset_hz, home), lambda: pocsag(iq, rate, offset_hz),
               lambda: digital_voice(iq, rate, offset_hz), lambda: ais(iq, rate, offset_hz, home)):
        try:
            v = fn()
        except (ValueError, IndexError):
            v = None
        if v:
            out["found"].append(v)
    return out
