"""DAB+: dal sottocanale di una radio ai pacchetti audio HE-AAC e al testo DLS.

Catena (EN 300 401 e TS 102 563): bit morbidi del sottocanale in ogni CIF
(24 ms) → de-interleaving temporale su 16 CIF → de-puncturing EEP → Viterbi
(in batch: ogni CIF è un blocco indipendente) → dispersione energetica →
superframe di 5 CIF (120 ms) riconosciuto dal Fire code → Reed-Solomon
(120,110) → access unit AAC con il loro CRC → PAD (dentro il primo elemento
DSE) → testo DLS.

I pacchetti AAC non si decodificano qui: li suona il decoder del browser, a
cui si passa la configurazione (AudioSpecificConfig) con i blocchi da 960
campioni del DAB+.
"""

from __future__ import annotations

import base64
from collections import deque
import time
from typing import Any

import numpy as np

from .dab import PRBS, _prbs, crc16, viterbi

CU_BITS = 64  # una capacity unit
CIF_CUS = 864

# --- puncturing ---------------------------------------------------------------


def _pi(index: int) -> list[int]:
    """Vettore di puncturing PI_index (1…24): 32 bit, 8 + index a uno.

    Ognuno degli 8 gruppi da 4 bit ha 1…4 uni in testa; i bit in più si aggiungono ai gruppi
    nell'ordine 0, 4, 2, 6, 1, 5, 3, 7 (tabella dello standard).
    """
    ones = [1] * 8
    order = [0, 4, 2, 6, 1, 5, 3, 7]
    for k in range(index):
        ones[order[k % 8]] += 1
    out: list[int] = []
    for n in ones:
        out += [1] * n + [0] * (4 - n)
    return out


_PI_X = [1, 1, 0, 0] * 6

# protezione EEP: per livello, (blocchi L1, blocchi L2, PI1, PI2) in funzione di n
_EEP_A = {
    0: lambda n: (6 * n - 3, 3, 24, 23),
    1: lambda n: (5, 1, 13, 12) if n == 1 else (2 * n - 3, 4 * n + 3, 14, 13),
    2: lambda n: (6 * n - 3, 3, 8, 7),
    3: lambda n: (4 * n - 3, 2 * n + 3, 3, 2),
}
_EEP_B = {
    0: lambda n: (24 * n - 3, 3, 10, 9),
    1: lambda n: (24 * n - 3, 3, 6, 5),
    2: lambda n: (24 * n - 3, 3, 4, 3),
    3: lambda n: (24 * n - 3, 3, 2, 1),
}
_EEP_A_CUS = (12, 8, 6, 4)  # CU per 8 kbit/s
_EEP_B_CUS = (27, 21, 18, 15)  # CU per 32 kbit/s


def eep_bitrate(option: int, level: int, size_cu: int) -> int | None:
    """Velocità (kbit/s) di un sottocanale EEP dalla sua dimensione in CU."""
    if option == 0 and size_cu % _EEP_A_CUS[level] == 0:
        return size_cu // _EEP_A_CUS[level] * 8
    if option == 1 and size_cu % _EEP_B_CUS[level] == 0:
        return size_cu // _EEP_B_CUS[level] * 32
    return None


def eep_keep(option: int, level: int, bitrate: int) -> np.ndarray:
    """Posizioni, nella sequenza madre a tasso 1/4, dei bit trasmessi di un CIF del sottocanale."""
    n = bitrate // (8 if option == 0 else 32)
    l1, l2, pi1, pi2 = (_EEP_A if option == 0 else _EEP_B)[level](n)
    mask = _pi(pi1) * 4 * l1 + _pi(pi2) * 4 * l2 + _PI_X
    return np.nonzero(np.array(mask, bool))[0]


# de-interleaving temporale: il bit i di un CIF arriva con un ritardo di π(i mod 16) CIF
TIME_DELAY = np.array([0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15])

# --- Reed-Solomon (120,110) su GF(256), x^8 + x^4 + x^3 + x^2 + 1 ---------------

_EXP = np.zeros(512, np.int64)
_LOG = np.zeros(256, np.int64)
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
_EXP[255:510] = _EXP[:255]


def _gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else int(_EXP[_LOG[a] + _LOG[b]])


def _gdiv(a: int, b: int) -> int:
    return 0 if a == 0 else int(_EXP[(_LOG[a] - _LOG[b]) % 255])


def _poly_eval(p: list[int], x: int) -> int:
    y = 0
    for c in p:
        y = _gmul(y, x) ^ c
    return y


def rs_encode(data: bytes) -> bytes:
    """Parità RS(120,110) (generatore con radici α^0…α^9); usato dalle prove."""
    gen = [1]
    for i in range(10):
        gen = [a ^ _gmul(b, int(_EXP[i])) for a, b in zip(gen + [0], [0] + gen, strict=True)]
    rem = [0] * 10
    for byte in data:
        coef = byte ^ rem[0]
        rem = rem[1:] + [0]
        if coef:
            for j in range(10):
                rem[j] ^= _gmul(gen[j + 1], coef)
    return bytes(rem)


def rs_decode(cw: bytearray) -> int:
    """Corregge sul posto un codice RS(120,110); restituisce i byte corretti, -1 se non correggibile."""
    synd = [_poly_eval(list(cw), int(_EXP[j])) for j in range(10)]
    if not any(synd):
        return 0
    # Berlekamp-Massey
    c, b = [1], [1]
    length, m, bb = 0, 1, 1
    for n in range(10):
        d = synd[n]
        for i in range(1, length + 1):
            d ^= _gmul(c[i] if i < len(c) else 0, synd[n - i])
        if d == 0:
            m += 1
            continue
        coef = _gdiv(d, bb)
        t = c[:]
        shifted = [0] * m + b
        c = [(c[i] if i < len(c) else 0) ^ _gmul(coef, shifted[i] if i < len(shifted) else 0)
             for i in range(max(len(c), len(shifted)))]
        if 2 * length <= n:
            length, b, bb, m = n + 1 - length, t, d, 1
        else:
            m += 1
    if length > 5:
        return -1
    # Chien: posizioni (nel codice accorciato di 120 byte, gradi 119…0)
    errs = []
    for pos in range(120):
        xinv = int(_EXP[(255 - (119 - pos)) % 255])
        if _poly_eval(c[::-1], xinv) == 0:
            errs.append(pos)
    if len(errs) != length:
        return -1
    # Forney (prima radice α^0): e = X · Ω(X⁻¹) / Λ'(X⁻¹)
    omega = [0] * 10
    for i in range(10):
        for j in range(min(i + 1, len(c))):
            omega[i] ^= _gmul(c[j], synd[i - j])
    for pos in errs:
        x = int(_EXP[119 - pos])
        xinv = _gdiv(1, x)
        num = 0
        for i in range(9, -1, -1):
            num = _gmul(num, xinv) ^ omega[i]
        den = 0
        for i in range(1, len(c), 2):
            den ^= _gmul(c[i], int(_EXP[(_LOG[xinv] * (i - 1)) % 255]))
        if den == 0:
            return -1
        cw[pos] ^= _gmul(x, _gdiv(num, den))
    return len(errs)


# --- superframe DAB+ -------------------------------------------------------------


def firecode(data: bytes) -> int:
    """Fire code dell'intestazione del superframe: x^16+x^14+x^13+x^12+x^11+x^5+x^3+x^2+x+1."""
    crc = 0
    for byte in data:
        for k in range(7, -1, -1):
            bit = ((byte >> k) & 1) ^ (crc >> 15)
            crc = ((crc << 1) & 0xFFFF) ^ (0x782F if bit else 0)
    return crc


_SR_INDEX = {48000: 3, 32000: 5, 24000: 6, 16000: 8}


def audio_config(dac_rate: int, sbr: bool, stereo: bool, ps: bool) -> dict[str, Any]:
    """AudioSpecificConfig per il decoder del browser: AAC con blocchi da 960 campioni, SBR e PS espliciti."""
    core = dac_rate // 2 if sbr else dac_rate
    bits = f"{2:05b}{_SR_INDEX[core]:04b}{(2 if stereo else 1):04b}" + "100"  # frameLengthFlag = 960
    if sbr:
        bits += f"{0x2B7:011b}{5:05b}1{_SR_INDEX[dac_rate]:04b}"
        if ps:
            bits += f"{0x548:011b}1"
    bits += "0" * (-len(bits) % 8)
    asc = int(bits, 2).to_bytes(len(bits) // 8, "big")
    # piano B per i decoder senza SBR a 960 campioni: solo il nucleo AAC (meno acuti, senza stereo PS)
    core_bits = f"{2:05b}{_SR_INDEX[core]:04b}{(2 if stereo else 1):04b}" + "100"
    core_bits += "0" * (-len(core_bits) % 8)
    fallback = {
        "asc": int(core_bits, 2).to_bytes(len(core_bits) // 8, "big").hex(),
        "codec": "mp4a.40.2", "sample_rate": core, "channels": 2 if stereo else 1,
        "sbr": False, "ps": False, "samples_per_au": 960,
    } if sbr else None
    return {
        "fallback": fallback,
        "asc": asc.hex(),
        "codec": "mp4a.40.29" if ps else "mp4a.40.5" if sbr else "mp4a.40.2",
        "sample_rate": dac_rate,
        "channels": 2 if (stereo or ps) else 1,
        "sbr": sbr,
        "ps": ps,
        "samples_per_au": 1920 if sbr else 960,
    }


def _read_pad(au: bytes) -> bytes | None:
    """PAD del DAB+: contenuto del DSE, se è il primo elemento dell'access unit."""
    if not au or au[0] >> 5 != 4:  # id_syn_ele 4 = DSE
        return None
    bitpos = 3 + 4  # id + element_instance_tag
    align = (au[0] >> (7 - (bitpos % 8))) & 1 if bitpos < 8 else 0
    bitpos += 1

    def bits(start: int, n: int) -> int:
        v = 0
        for k in range(start, start + n):
            v = (v << 1) | ((au[k // 8] >> (7 - k % 8)) & 1)
        return v

    if len(au) < 4:
        return None
    count = bits(bitpos, 8)
    bitpos += 8
    if count == 255:
        count += bits(bitpos, 8)
        bitpos += 8
    if align:
        bitpos += -bitpos % 8
    start = bitpos // 8
    if bitpos % 8:
        data = bytes(bits(bitpos + 8 * k, 8) for k in range(count) if bitpos + 8 * k + 8 <= len(au) * 8)
    else:
        data = au[start : start + count]
    return data if len(data) == count else None


_XPAD_LEN = (4, 6, 8, 12, 16, 24, 32, 48)


class DlsReader:
    """Dati PAD del DAB+: testo DLS (applicazioni X-PAD 2 e 3) e slideshow MOT (1, 12 e 13).

    Il DLS è una serie di segmenti con prefisso e CRC; le immagini della slideshow viaggiano in
    gruppi di dati MSC (applicazione 12 l'inizio, 13 il seguito) annunciati da un indicatore di
    lunghezza (applicazione 1): l'intestazione MOT (tipo 3) dice il formato, il corpo (tipo 4) è
    l'immagine divisa in segmenti numerati.
    """

    def __init__(self) -> None:
        self.text: str | None = None
        self._group = bytearray()
        self._expect = 0
        self._segments: dict[int, str] = {}
        self._toggle: int | None = None
        self._last_app: int | None = None
        # slideshow
        self.image: bytes | None = None
        self.image_mime: str | None = None
        self.image_count = 0
        self._dgli: int | None = None
        self._mot: bytearray | None = None
        self._mot_len = 0
        self._mot_types: dict[int, tuple[int, int]] = {}  # TId → (tipo, sottotipo) dall'intestazione
        self._bodies: dict[int, dict[int, bytes]] = {}
        self._body_last: dict[int, int] = {}

    def feed(self, pad: bytes) -> None:
        if len(pad) < 2:
            return
        fpad = pad[-2:]
        xpad = pad[:-2][::-1]  # l'X-PAD viaggia al contrario
        ind = (fpad[0] >> 4) & 0x3
        ci_flag = (fpad[1] >> 1) & 1
        if ind == 0 or not xpad:
            return
        subfields: list[tuple[int, int]] = []
        pos = 0
        if ci_flag:
            if ind == 1:  # X-PAD corto: un CI e 3 byte
                subfields.append((xpad[0] & 0x1F, 3))
                pos = 1
            else:
                while pos < len(xpad) and len(subfields) < 4:
                    ci = xpad[pos]
                    pos += 1
                    app = ci & 0x1F
                    if app == 0:
                        break
                    subfields.append((app, _XPAD_LEN[ci >> 5]))
        elif self._last_app in (2, 3):
            subfields.append((3, len(xpad)))
        elif self._last_app in (12, 13):
            subfields.append((13, len(xpad)))
        for app, length in subfields:
            data = xpad[pos : pos + length]
            pos += length
            self._last_app = app
            if app == 1 and len(data) >= 4:  # lunghezza del prossimo gruppo di dati
                if crc16(data[:2]) ^ 0xFFFF == int.from_bytes(data[2:4], "big"):
                    self._dgli = ((data[0] & 0x3F) << 8) | data[1]
                continue
            if app == 12:
                if self._dgli:
                    self._mot, self._mot_len, self._dgli = bytearray(data), self._dgli, None
                else:
                    self._mot = None
            elif app == 13 and self._mot is not None:
                self._mot += data
            if app in (12, 13):
                if self._mot is not None and len(self._mot) >= self._mot_len:
                    self._data_group(bytes(self._mot[: self._mot_len]))
                    self._mot = None
                continue
            if app == 2:
                self._group = bytearray(data)
                self._expect = 0
            elif app == 3 and self._group:
                self._group += data
            else:
                continue
            self._try_group()

    def _data_group(self, g: bytes) -> None:
        """Un gruppo di dati MSC della slideshow: segmento di intestazione o di corpo MOT."""
        if len(g) < 4:
            return
        ext, has_crc, has_seg, has_ua, dtype = g[0] >> 7, (g[0] >> 6) & 1, (g[0] >> 5) & 1, (g[0] >> 4) & 1, g[0] & 0x0F
        end = len(g)
        if has_crc:
            if crc16(g[:-2]) ^ 0xFFFF != int.from_bytes(g[-2:], "big"):
                return
            end -= 2
        p = 2 + (2 if ext else 0)
        last, segno, tid = 0, 0, None
        if has_seg:
            last, segno = g[p] >> 7, ((g[p] & 0x7F) << 8) | g[p + 1]
            p += 2
        if has_ua:
            length, has_tid = g[p] & 0x0F, (g[p] >> 4) & 1
            if has_tid:
                tid = (g[p + 1] << 8) | g[p + 2]
            p += 1 + length
        data = g[p:end]
        if tid is None or len(data) < 2 or dtype not in (3, 4):
            return
        size = ((data[0] & 0x1F) << 8) | data[1]
        seg = data[2 : 2 + size]
        if dtype == 3 and segno == 0 and len(seg) >= 7:  # intestazione MOT (di solito in un segmento)
            self._mot_types[tid] = ((seg[5] >> 1) & 0x3F, ((seg[5] & 1) << 8) | seg[6])
            return
        if dtype == 4:
            parts = self._bodies.setdefault(tid, {})
            parts[segno] = seg
            if last:
                self._body_last[tid] = segno
            n = self._body_last.get(tid)
            if n is not None and all(k in parts for k in range(n + 1)):
                body = b"".join(parts[k] for k in range(n + 1))
                ctype = self._mot_types.get(tid)
                if body[:2] == b"\xff\xd8" or ctype == (2, 1):
                    mime = "image/jpeg"
                elif body[:4] == b"\x89PNG" or ctype == (2, 3):
                    mime = "image/png"
                else:
                    mime = None
                if mime:
                    self.image, self.image_mime = body, mime
                    self.image_count += 1
                self._bodies.pop(tid, None)
                self._body_last.pop(tid, None)
            # le immagini lasciate a metà non si accumulano
            while len(self._bodies) > 4:
                self._bodies.pop(next(iter(self._bodies)))

    def _try_group(self) -> None:
        g = self._group
        if len(g) < 2:
            return
        cflag = (g[0] >> 4) & 1
        nchars = 0 if cflag else (g[0] & 0x0F) + 1
        total = 2 + nchars + 2
        if len(g) < total:
            return
        seg = bytes(g[:total])
        self._group = bytearray()
        if crc16(seg[:-2]) ^ 0xFFFF != int.from_bytes(seg[-2:], "big") or cflag:
            return
        toggle, first, last = seg[0] >> 7, (seg[0] >> 6) & 1, (seg[0] >> 5) & 1
        segnum = 0 if first else (seg[1] >> 4) & 0x7
        if toggle != self._toggle:
            self._segments, self._toggle = {}, toggle
        from .rds import CHARSET  # noqa: PLC0415

        self._segments[segnum] = "".join(chr(b) if 0x20 <= b <= 0x7E else CHARSET.get(b, "?") for b in seg[2:-2])
        if last:
            self._last_seg = segnum
        last_seg = getattr(self, "_last_seg", None)
        if last_seg is not None and all(k in self._segments for k in range(last_seg + 1)):
            self.text = "".join(self._segments[k] for k in range(last_seg + 1)).strip()


class DabPlusAudio:
    """Riceve i CIF di un sottocanale DAB+ e restituisce access unit AAC e testo DLS."""

    def __init__(self, option: int, level: int, bitrate: int, deinterleave: str = "pi") -> None:
        self.bitrate = bitrate
        self.info_bits = bitrate * 24
        self.keep = eep_keep(option, level, bitrate)
        self.coded_bits = len(self.keep)
        self.mother_bits = 4 * (self.info_bits + 6)
        self.prbs = _prbs(self.info_bits) if self.info_bits != len(PRBS) else PRBS
        self._delay = TIME_DELAY if deinterleave == "pi" else 15 - TIME_DELAY
        self._history: deque[np.ndarray] = deque(maxlen=16)
        self._pending: list[np.ndarray] = []
        self._frames: deque[bytes] = deque(maxlen=5)
        self._sync = False
        self.config: dict[str, Any] | None = None
        self.dls = DlsReader()
        self.stats = {"superframes": 0, "rs_fixed": 0, "rs_failed": 0, "au_ok": 0, "au_bad": 0}

    def push_cifs(self, cifs: list[np.ndarray]) -> list[bytes]:
        """Bit morbidi del sottocanale di uno o più CIF consecutivi; restituisce le AU complete trovate."""
        for cif in cifs:
            self._history.append(cif)
            if len(self._history) == 16:
                # bit i del CIF logico più vecchio ancora incompleto: arriva nel CIF di indice π(i mod 16)
                idx = np.arange(self.coded_bits)
                rows = np.stack(list(self._history))
                self._pending.append(rows[self._delay[idx % 16], idx])
        aus: list[bytes] = []
        if len(self._pending) >= 20:  # Viterbi in batch: ~0,5 s di audio insieme
            aus = self._decode(self._pending)
            self._pending = []
        return aus

    def flush(self) -> list[bytes]:
        aus = self._decode(self._pending) if self._pending else []
        self._pending = []
        return aus

    def _decode(self, frames: list[np.ndarray]) -> list[bytes]:
        mother = np.zeros((len(frames), self.mother_bits), np.float32)
        mother[:, self.keep] = np.stack(frames)
        bits = viterbi(mother)[:, : self.info_bits] ^ self.prbs
        aus: list[bytes] = []
        for row in bits:
            self._frames.append(np.packbits(row.astype(np.uint8)).tobytes())
            if len(self._frames) == 5:
                aus += self._superframe()
        return aus

    def _superframe(self) -> list[bytes]:
        first = self._frames[0]
        if firecode(first[2:11]) != int.from_bytes(first[:2], "big"):
            return []  # non è l'inizio di un superframe: si aspetta il CIF successivo
        sf = bytearray(b"".join(self._frames))
        self._frames.clear()
        s = self.bitrate // 8
        for r in range(s):  # codici RS intercalati: il codice r prende un byte ogni s
            cw = bytearray(sf[r::s])
            fixed = rs_decode(cw)
            if fixed < 0:
                self.stats["rs_failed"] += 1
            elif fixed:
                self.stats["rs_fixed"] += fixed
                sf[r::s] = cw
        self.stats["superframes"] += 1
        flags = sf[2]
        dac_rate = 48000 if flags & 0x40 else 32000
        sbr, stereo, ps = bool(flags & 0x20), bool(flags & 0x10), bool(flags & 0x08)
        num_aus = {(0, 1): 2, (1, 1): 3, (0, 0): 4, (1, 0): 6}[(dac_rate == 48000, sbr)]
        starts = [{2: 5, 3: 6, 4: 8, 6: 11}[num_aus]]
        for k in range(num_aus - 1):
            bitpos = 24 + 12 * k
            v = int.from_bytes(sf[bitpos // 8 : bitpos // 8 + 2], "big")
            starts.append((v >> (4 - bitpos % 8)) & 0xFFF if bitpos % 8 == 0 else v & 0xFFF)
        starts.append(110 * s)
        if self.config is None or (self.config["sample_rate"], self.config["sbr"], self.config["ps"]) != (dac_rate, sbr, ps):
            self.config = audio_config(dac_rate, sbr, stereo, ps)
        aus = []
        for a, b in zip(starts, starts[1:], strict=False):
            if not (0 < a < b <= len(sf)):
                self.stats["au_bad"] += 1
                continue
            au = bytes(sf[a:b])
            if crc16(au[:-2]) ^ 0xFFFF == int.from_bytes(au[-2:], "big"):
                self.stats["au_ok"] += 1
                aus.append(au[:-2])
                pad = _read_pad(au[:-2])
                if pad:
                    self.dls.feed(pad)
            else:
                self.stats["au_bad"] += 1
        return aus


def _subchannel_layout(start_cu: int, size_cu: int) -> list[tuple[list[int], int, int]]:
    """Per ognuno dei 4 CIF di una trama: simboli (indici 4…75) che contengono il sottocanale
    e posizione dei suoi bit nella loro concatenazione."""
    sym_bits = 3072
    lo, hi = start_cu * CU_BITS, (start_cu + size_cu) * CU_BITS
    first, last = lo // sym_bits, (hi - 1) // sym_bits
    out = []
    for c in range(4):
        syms = [4 + c * 18 + k for k in range(first, last + 1)]
        out.append((syms, lo - first * sym_bits, hi - first * sym_bits))
    return out


class DabLiveReceiver:
    """Analisi continua di una radio DAB+: byte rtl_tcp in ingresso, AU AAC, DLS e qualità in uscita."""

    def __init__(self, sid: str) -> None:
        from .dab import DabStream  # noqa: PLC0415

        self.sid = sid.upper()
        self.stream = DabStream()
        self.rx: DabPlusAudio | None = None
        self.service: dict[str, Any] | None = None
        self.error: str | None = None
        self._frames_seen = 0
        self._sent_info: tuple | None = None
        self._layout: list[tuple[list[int], int, int]] = []
        self._load: float | None = None
        self._t_feed = 0.0
        self._slides = 0

    def process(self, raw: bytes) -> dict[str, Any]:
        t_feed = time.perf_counter()
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
        frames = self.stream.feed((a[0::2] + 1j * a[1::2]).astype(np.complex64))
        self._t_feed = time.perf_counter() - t_feed
        out: dict[str, Any] = {"aus": [], "row": None, "level_db": None}
        self._frames_seen += len(frames)
        fic = self.stream.fic
        if fic is not None:
            svc = fic.service(int(self.sid, 16))
            # nomi e sottocanali arrivano a turno: si aspetta finché il sottocanale della radio è noto
            if self.rx is None and self.error is None:
                if svc["type"] not in ("DAB+", "audio"):
                    self.error = f"la radio è {svc['type']}: si possono ascoltare solo le radio DAB+"
                elif "start" in svc:
                    if not svc.get("bitrate"):
                        self.error = "protezione del sottocanale non supportata"
                    else:
                        self.service = svc
                        self.rx = DabPlusAudio(svc["option"], svc["level"], svc["bitrate"])
                        # da qui si demodulano solo i simboli della radio, e il FIC più di rado
                        self._layout = _subchannel_layout(svc["start"], svc["size"])
                        self.stream.need = sorted({k for c in self._layout for k in c[0]})
                        self.stream.fic_every = 40
                elif self._frames_seen > 160:  # ~15 s
                    self.error = "radio non trovata nel multiplex"
            info = (fic.ensemble, svc["name"], svc.get("bitrate"), svc.get("protection"))
            if info != self._sent_info:
                self._sent_info = info
                out["info"] = {"ensemble": info[0], "service": info[1], "bitrate": info[2], "protection": info[3]}
        t0 = time.perf_counter()
        for fr in frames:
            if self.rx is not None and self.service is not None and all(k in fr["rows"] for k in self.stream.need):
                cifs = []
                for syms, lo, hi in self._layout:
                    bits = np.concatenate([fr["rows"][k] for k in syms])
                    cifs.append(bits[lo:hi])
                out["aus"] += self.rx.push_cifs(cifs)
            p = fr["power"].reshape(256, -1).mean(axis=1)
            out["row"] = np.round(10 * np.log10(p / (127.5 * 2048) ** 2 + 1e-20), 1).tolist()
            out["level_db"] = round(float(10 * np.log10(np.mean(fr["power"]) / (127.5 * 2048) ** 2 + 1e-20)), 1)
        if frames:
            # carico: tempo di calcolo rispetto alla durata del segnale (media mobile)
            load = (time.perf_counter() - t0 + self._t_feed) / (len(frames) * 0.096)
            self._load = load if self._load is None else 0.8 * self._load + 0.2 * load
        if self.rx is not None:
            pad = self.rx.dls
            if pad.image_count != self._slides and pad.image:
                self._slides = pad.image_count
                out["slide"] = {"mime": pad.image_mime, "data": base64.b64encode(pad.image).decode()}
            out["config"] = self.rx.config
            out["dls"] = self.rx.dls.text
            out["stats"] = dict(self.rx.stats, load=round(self._load or 0, 2))
        out["error"] = self.error
        out["synced"] = self.stream.next_start is not None
        return out
