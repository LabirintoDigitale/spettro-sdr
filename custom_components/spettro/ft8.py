"""FT8: i messaggi digitali dei radioamatori (WSJT-X), decodificati dall'audio USB.

Ogni 15 secondi, allineati all'ora UTC, le stazioni trasmettono 79 simboli 8-FSK da 0,16 s
(toni distanti 6,25 Hz): tre blocchi di sincronismo Costas 7×7 e 174 bit protetti da un
codice LDPC(174,91) con CRC a 14 bit. Il messaggio (77 bit) contiene di solito due
nominativi e un locatore o un rapporto di segnale.

Le tabelle del codice LDPC (matrice generatrice e controlli di parità) sono quelle del
protocollo, come pubblicate in WSJT-X e in ft8_lib (Karlis Goba, licenza MIT).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

FS = 8_000  # audio usato dal decoder
NSPS = 1280  # campioni per simbolo (0,16 s)
TOSR, FOSR = 4, 4
HOP = NSPS // TOSR
NFFT = FOSR * NSPS
BIN_HZ = FS / NFFT
SLOT_S = 15.0
START_S = 0.5  # la trasmissione inizia mezzo secondo dopo l'inizio del periodo
PRE_S = 1.5  # audio preso prima dell'inizio del periodo (orologi un po' indietro)
WINDOW_S = PRE_S + 16.0  # audio analizzato per ogni periodo
COSTAS = (3, 1, 4, 0, 6, 5, 2)
SYNC_POS = (0, 36, 72)
GRAY = (0, 1, 3, 2, 5, 6, 4, 7)  # 3 bit → tono
DATA_SYMBOLS = [s for s in range(79) if not any(p <= s < p + 7 for p in SYNC_POS)]
_SYNC_SET = set(range(79)) - set(DATA_SYMBOLS)
F_MIN, F_MAX = 100.0, 3000.0  # banda audio esplorata
MAX_CANDIDATES = 500
LDPC_ITERS = 30
SOFT_K = 5

_LDPC_GEN = (
    "8329ce11bf31eaf509f27fc0 761c264e25c2593354931320 dc265902fb277c6410a1bdc0 1b3f417858cd2dd33ec7f620 "
    "09fda4fee04195fd034783a0 077cccc11b8873ed5c3d48a0 29b62afe3ca036f4fe1a9da0 6054faf5f35d96d3b0c8c3e0 "
    "e20798e4310eed27884ae900 775c9c08e80e26ddae563180 b0b811028c2bf997213487c0 18a0c9231fc60adf5c5ea320 "
    "76471e8302a0721e01b12b80 ffbccb80ca8341fafb47b2e0 66a72a158f9325a2bf671700 c4243689fe85b1c51363a180 "
    "0dff739414d1a1b34b1c2700 15b48830636c8b99894972e0 29a89c0d3de81d665489b0e0 4f126f37fa51cbe61bd6b940 "
    "99c47239d0d97d3c84e09400 1919b75119765621bb4f1e80 09db12d731faee0b86df6b80 488fc33df43fbdeea4eafb40 "
    "827423ee40b675f756eb5fe0 abe197c484cb74757144a9a0 2b500e4bc0ec5a6d2bdbdd00 c474aa53d702187616693600 "
    "8eba1a13db3390bd6718cec0 753844673a27782cc42012e0 06ff83a145c37035a5c12680 3b37417858cc2dd33ec3f620 "
    "9a4a5a28ee17ca9c324842c0 bc29f465309c977e89610a40 2663ae6ddf8b5ce2bb294880 46f231efe457034c18144180 "
    "3fb2ce85abe9b0c72e06fbe0 de87481f282c153971a0a2e0 fcd7ccf23c69fa99bba14120 f0261447e9490ca8e474cec0 "
    "4410115818196f95cdd70120 088fc31df4bfbde2a4eafb40 b8fef1b6307729fb0a078c00 5afea7acccb77bbc9d99a900 "
    "49a7016ac653f65ecdc90760 1944d085be4e7da8d6cc7d00 251f62adc4032f0ee7140020 56471f8702a0721e00b12b80 "
    "2b8e4923f2dd51e2d537fa00 6b550a40a66f4755de95c260 a18ad28d4e27fe92a4f6c840 10c2e586388cb82a3d807580 "
    "ef34a41817ee02133db2eb00 7e9c0c54325a9c15836e0000 3693e572d1fde4cdf079e860 bfb2cec5abe1b0c72e07fbe0 "
    "7ee18230c583cccc57d4b080 a066cb2fedafc9f526641260 bb23725abc47cc5f4cc4cd20 ded9dba3bee40c59b5609b40 "
    "d9a7016ac653e6decdc90360 9ad46aed5f707f280ab5fc40 e5921c77822587316d7d3c20 4f14da8242a8b86dca733520 "
    "8b8b507ad467d4441df770e0 22831c9cf1169467ad04b680 213b838fe2ae54c38ee71800 5d926b6dd71f085181a4e120 "
    "66ab79d4b29ee6e69509e560 958148682d748a38dd68baa0 b8ce020cf069c32a723ab140 f4331d6d461607e957527460 "
    "6da23ba424b9596133cf9c80 a636bcbc7b30c5fbeae67fe0 5cb0d86a07df654a9089a200 f11f106848780fc9ecdd80a0 "
    "1fbb5364fb8d2c9d730d5ba0 fcb86bc70a50c9d02a5d0340 a534433029eac15f322e34c0 c989d9c7c3d3b8c55d751300 "
    "7bb38b2f0186d46643ae9620 2644ebadeb44b9467d1f42c0 608cc857594bfbb55d696000"
)
_LDPC_NM = (
    "4 31 59 91 92 96 153; 5 32 60 93 115 146; 6 24 61 94 122 151; 7 33 62 95 96 143; 8 25 63 83 93 96 148; 6 "
    "32 64 97 126 138; 5 34 65 78 98 107 154; 9 35 66 99 139 146; 10 36 67 100 107 126; 11 37 67 87 101 139 "
    "158; 12 38 68 102 105 155; 13 39 69 103 149 162; 8 40 70 82 104 114 145; 14 41 71 88 102 123 156; 15 42 "
    "59 106 123 159; 1 33 72 106 107 157; 16 43 73 108 141 160; 17 37 74 81 109 131 154; 11 44 75 110 121 "
    "166; 45 55 64 111 130 161 173; 8 46 71 112 119 166; 18 36 76 89 113 114 143; 19 38 77 104 116 163; 20 47 "
    "70 92 138 165; 2 48 74 113 128 160; 21 45 78 83 117 121 151; 22 47 58 118 127 164; 16 39 62 112 134 158; "
    "23 43 79 120 131 145; 19 35 59 73 110 125 161; 20 36 63 94 136 161; 14 31 79 98 132 164; 3 44 80 124 127 "
    "169; 19 46 81 117 135 167; 7 49 58 90 100 105 168; 12 50 61 118 119 144; 13 51 64 114 118 157; 24 52 76 "
    "129 148 149; 25 53 69 90 101 130 156; 20 46 65 80 120 140 170; 21 54 77 100 140 171; 35 82 133 142 171 "
    "174; 14 30 83 113 125 170; 4 29 68 120 134 173; 1 4 52 57 86 136 152; 26 51 56 91 122 137 168; 52 84 110 "
    "115 145 168; 7 50 81 99 132 173; 23 55 67 95 172 174; 26 41 77 109 141 148; 2 27 41 61 62 115 133; 27 40 "
    "56 124 125 126; 18 49 55 124 141 167; 6 33 85 108 116 156; 28 48 70 85 105 129 158; 9 54 63 131 147 155; "
    "22 53 68 109 121 174; 3 13 48 78 95 123; 31 69 133 150 155 169; 12 43 66 89 97 135 159; 5 39 75 102 136 "
    "167; 2 54 86 101 135 164; 15 56 87 108 119 171; 10 44 82 91 111 144 149; 23 34 71 94 127 153; 11 49 88 "
    "92 142 157; 29 34 87 97 147 162; 30 50 60 86 137 142 162; 10 53 66 84 112 128 165; 22 57 85 93 140 159; "
    "28 32 72 103 132 166; 28 29 84 88 117 143 150; 1 26 45 80 128 147; 17 27 89 103 116 153; 51 57 98 163 "
    "165 172; 21 37 73 138 152 169; 16 47 76 130 137 154; 3 24 30 72 104 139; 9 40 90 106 134 151; 15 58 60 "
    "74 111 150 163; 18 42 79 144 146 152; 25 38 65 99 122 160; 17 42 75 129 170 172"
)

_GEN = np.array([[int(h, 16) >> (95 - j) & 1 for j in range(91)] for h in _LDPC_GEN.split()], np.uint8)  # (83, 91)
_NM_ROWS = [[int(v) - 1 for v in r.split()] for r in _LDPC_NM.split(";")]
_NM = np.array([r + [r[0]] * (7 - len(r)) for r in _NM_ROWS])  # (83, 7): righe da 6 completate ripetendo
_NM_OK = np.array([[k < len(r) for k in range(7)] for r in _NM_ROWS])
_EDGE_TO_BIT = np.zeros((83 * 7, 174))  # somma dei messaggi controllo → bit
for _j, _r in enumerate(_NM_ROWS):
    for _k, _i in enumerate(_r):
        _EDGE_TO_BIT[_j * 7 + _k, _i] = 1.0
_INV_GRAY = np.argsort(GRAY)  # tono → 3 bit
# per ognuno dei 3 bit: quali toni lo hanno a 1
_TONE_BIT = np.array([[(_INV_GRAY[t] >> (2 - b)) & 1 for t in range(8)] for b in range(3)], bool)

NTOKENS = 2063592
MAX22 = 4194304
MAXGRID4 = 32400
_A_FULL = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?"
_A38 = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/"
_A37 = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_A36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_A27 = " ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def crc14(bits: np.ndarray) -> int:
    """CRC a 14 bit (polinomio 0x2757) dei bit dati, il primo è il più significativo."""
    rem = 0
    for b in bits:
        top = ((rem >> 13) & 1) ^ int(b)
        rem = (rem << 1) & 0x3FFF
        if top:
            rem ^= 0x2757
    return rem


def _num(bits: np.ndarray) -> int:
    n = 0
    for b in bits:
        n = (n << 1) | int(b)
    return n


def encode_bits(payload77: np.ndarray) -> np.ndarray:
    """77 bit → 174 bit del codice (messaggio, CRC, parità). Serve per le prove."""
    crc = crc14(np.concatenate([payload77, np.zeros(5, np.uint8)]))
    a91 = np.concatenate([payload77, [(crc >> (13 - k)) & 1 for k in range(14)]]).astype(np.uint8)
    return np.concatenate([a91, (_GEN @ a91) % 2]).astype(np.uint8)


def tones_from_codeword(cw: np.ndarray) -> list[int]:
    """174 bit del codice → 79 toni (sincronismo Costas + dati con codice Gray)."""
    data = iter(GRAY[4 * cw[3 * i] + 2 * cw[3 * i + 1] + cw[3 * i + 2]] for i in range(58))
    return [COSTAS[s % 36] if s in _SYNC_SET else next(data) for s in range(79)]


def tones_for(payload77: np.ndarray) -> list[int]:
    return tones_from_codeword(encode_bits(payload77))


def ldpc_decode(llr: np.ndarray, iters: int = LDPC_ITERS) -> tuple[np.ndarray, np.ndarray]:
    """Somma-prodotto su più candidati insieme. llr (B, 174), positivo = bit 0.

    Ritorna i bit (B, 174) e per ognuno se tutti i controlli di parità tornano.
    """
    b = llr.shape[0]
    e = np.zeros((b, 83, 7))
    bits = (llr < 0).astype(np.uint8)
    done = np.zeros(b, bool)
    out = bits.copy()
    for _ in range(iters):
        tot = llr + e.reshape(b, -1) @ _EDGE_TO_BIT
        bits = (tot < 0).astype(np.uint8)
        ok = ((bits[:, _NM] * _NM_OK).sum(axis=2) % 2 == 0).all(axis=1)
        new = ok & ~done
        out[new] = bits[new]
        done |= ok
        if done.all():
            break
        m = tot[:, _NM] - e
        t = np.tanh(np.clip(m, -40, 40) / 2)
        t = np.where(np.abs(t) < 1e-12, 1e-12, t)
        t[:, ~_NM_OK] = 1.0
        x = np.clip(np.prod(t, axis=2, keepdims=True) / t, -0.999999, 0.999999)
        e = 2 * np.arctanh(x)
        e[:, ~_NM_OK] = 0.0
    out[~done] = bits[~done]
    return out, done


def callsign_hash(call: str) -> int | None:
    """Hash a 22 bit del nominativo (per i nominativi abbreviati in <...>)."""
    n = 0
    s = call.strip().upper()[:11].ljust(11)
    for c in s:
        j = _A38.find(c)
        if j < 0:
            return None
        n = n * 38 + j
    return ((47055833459 * n) & 0xFFFFFFFFFFFFFFFF) >> 42


class Hashes:
    """Nominativi già visti, per sciogliere quelli trasmessi solo come hash."""

    def __init__(self) -> None:
        self.n22: dict[int, str] = {}

    def save(self, call: str) -> None:
        h = callsign_hash(call)
        if h is not None and len(call) >= 3 and "<" not in call and "." not in call:
            self.n22[h] = call
            if len(self.n22) > 5000:
                self.n22.pop(next(iter(self.n22)))

    def lookup(self, h: int, bits: int) -> str:
        for k, v in self.n22.items():
            if k >> (22 - bits) == h:
                return f"<{v}>"
        return "<...>"


def _unpack28(n28: int, ip: int, i3: int, hashes: Hashes) -> str | None:
    if n28 < NTOKENS:
        if n28 <= 2:
            return ("DE", "QRZ", "CQ")[n28]
        if n28 <= 1002:
            return f"CQ {n28 - 3:03d}"
        if n28 <= 532443:
            n, s = n28 - 1003, ""
            for _ in range(4):
                s = _A27[n % 27] + s
                n //= 27
            return "CQ " + s.strip()
        return None
    n28 -= NTOKENS
    if n28 < MAX22:
        return hashes.lookup(n28, 22)
    n = n28 - MAX22
    c = [""] * 6
    for k, (alpha, base) in enumerate(((_A27, 27), (_A27, 27), (_A27, 27), ("0123456789", 10), (_A36, 36), (_A37, 37))):
        c[5 - k] = alpha[n % base]
        n //= base
    if n:
        return None
    call = "".join(c).strip()
    if call.startswith("3D0") and len(call) > 3:
        call = "3DA0" + call[3:]
    elif len(call) > 1 and call[0] == "Q" and call[1].isalpha():
        call = "3X" + call[1:]
    if len(call) < 3 or " " in call:
        return None
    if ip:
        call += "/R" if i3 == 1 else "/P"
    hashes.save(call)
    return call


def _grid(igrid4: int, ir: int) -> str:
    if igrid4 <= MAXGRID4:
        n = igrid4
        g = chr(65 + n // 1800) + chr(65 + n // 100 % 18) + str(n // 10 % 10) + str(n % 10)
        return ("R " if ir else "") + g
    irpt = igrid4 - MAXGRID4
    if irpt == 1:
        return ""
    if irpt in (2, 3, 4):
        return ("RRR", "RR73", "73")[irpt - 2]
    return ("R" if ir else "") + f"{irpt - 35:+03d}"


def unpack(bits77: np.ndarray, hashes: Hashes) -> str | None:
    """77 bit → testo del messaggio, come lo scrive WSJT-X."""
    i3 = _num(bits77[74:77])
    if i3 in (1, 2):
        a, b = _num(bits77[0:29]), _num(bits77[29:58])
        c1 = _unpack28(a >> 1, a & 1, i3, hashes)
        c2 = _unpack28(b >> 1, b & 1, i3, hashes)
        if c1 is None or c2 is None:
            return None
        g = _grid(_num(bits77[59:74]), int(bits77[58]))
        if g.startswith("R ") and c1.startswith("CQ"):
            return None
        return " ".join(p for p in (c1, c2, g) if p)
    if i3 == 4:
        n12, n58 = _num(bits77[0:12]), _num(bits77[12:70])
        iflip, nrpt, icq = int(bits77[70]), _num(bits77[71:73]), int(bits77[73])
        s = ""
        for _ in range(11):
            s = _A38[n58 % 38] + s
            n58 //= 38
        call = s.strip()
        hashes.save(call)
        other = hashes.lookup(n12, 12)
        c1, c2 = (call, other) if iflip else (other, call)
        if icq:
            return f"CQ {c2}"
        return " ".join(p for p in (c1, c2, ("", "RRR", "RR73", "73")[nrpt]) if p)
    if i3 == 0:
        n3 = _num(bits77[71:74])
        n = _num(bits77[0:71])
        if n3 == 0:
            s = ""
            for _ in range(13):
                s = _A_FULL[n % 42] + s
                n //= 42
            return s.strip() or None
        if n3 == 5:
            return f"telemetria {n:018X}"
        return f"(messaggio speciale, tipo 0.{n3})"
    return f"(messaggio speciale, tipo {i3})"


def grid_latlon(grid: str) -> tuple[float, float] | None:
    """Centro del quadrato del locatore (4 o 6 caratteri)."""
    g = grid.strip().upper()
    if len(g) < 4 or not ("A" <= g[0] <= "R" and "A" <= g[1] <= "R" and g[2:4].isdigit()):
        return None
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6 and "A" <= g[4] <= "X" and "A" <= g[5] <= "X":
        return lat + (ord(g[5]) - 65 + 0.5) / 24, lon + (ord(g[4]) - 65 + 0.5) / 12
    return lat + 0.5, lon + 1.0


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


_WINDOW = np.kaiser(NSPS, 3.0)  # provato sulle registrazioni di riferimento: meglio del rettangolare e di Hann


def spectrogram(x: np.ndarray) -> np.ndarray:
    """Potenza (righe = frazioni di simbolo, colonne = frazioni del passo tra i toni)."""
    n = (len(x) - NSPS) // HOP + 1
    idx = np.arange(NSPS)[None, :] + HOP * np.arange(n)[:, None]
    return np.abs(np.fft.rfft(x[idx] * np.kaiser(NSPS, 4.0), NFFT, axis=1)) ** 2


def decode(x: np.ndarray, hashes: Hashes | None = None, passes: int = 3) -> list[dict[str, Any]]:
    """Messaggi FT8 in un periodo. x: audio a 8 kHz che inizia PRE_S prima del periodo.

    dt è lo scarto dall'istante giusto (0,5 s dopo l'inizio del periodo), in secondi. Dopo ogni
    passata i segnali decodificati si tolgono dalla cascata: i più deboli sotto riemergono.
    """
    hashes = hashes or Hashes()
    p = spectrogram(np.asarray(x, np.float64))
    nf = p.shape[1]
    f_lo, f_hi = int(F_MIN / BIN_HZ), min(int(F_MAX / BIN_HZ), nf - 8 * FOSR)
    if f_hi <= f_lo or p.shape[0] <= TOSR * 79:
        return []
    noise = float(np.median(p[:, f_lo:f_hi])) / math.log(2) + 1e-30
    lp = np.log(p + noise * 1e-3)
    out: dict[str, dict[str, Any]] = {}
    for _ in range(passes):
        found = _pass(p, lp, noise, f_lo, f_hi, hashes, out)
        if not found:
            break
        for t0, f0, tones in found:
            for s, k in enumerate(tones):
                t = t0 + TOSR * s
                f = f0 + FOSR * k
                lp[max(0, t - 1): t + TOSR, max(0, f - FOSR + 1): f + FOSR] = math.log(noise)
    return sorted(out.values(), key=lambda m: m["freq_hz"])


def _pass(p: np.ndarray, lp: np.ndarray, noise: float, f_lo: int, f_hi: int, hashes: Hashes,
          out: dict[str, dict[str, Any]]) -> list[tuple[int, int, list[int]]]:
    """Una passata: candidati dal sincronismo, LDPC, CRC. Ritorna inizio, frequenza e toni dei nuovi messaggi."""
    t_max = lp.shape[0] - TOSR * 78
    # sincronismo: i toni Costas contro la media degli 8 toni possibili, per ogni inizio e frequenza
    tone_mean = sum(lp[:, f_lo + FOSR * k: f_hi + FOSR * k] for k in range(8)) / 8
    score = np.zeros((t_max, f_hi - f_lo))
    for p0 in SYNC_POS:
        for j, c in enumerate(COSTAS):
            t = TOSR * (p0 + j)
            score += lp[t: t + t_max, f_lo + FOSR * c: f_hi + FOSR * c] - tone_mean[t: t + t_max]
    score /= 21
    # massimi locali
    pad = np.pad(score, 1, constant_values=-np.inf)
    neigh = np.max(np.stack([pad[1 + dt: 1 + dt + t_max, 1 + df: 1 + df + f_hi - f_lo]
                             for dt in (-1, 0, 1) for df in (-1, 0, 1) if dt or df]), axis=0)
    peaks = np.argwhere((score >= neigh) & (score > 0.2))
    cands = peaks[np.argsort(-score[peaks[:, 0], peaks[:, 1]])[:MAX_CANDIDATES]]
    if not len(cands):
        return []
    t0 = cands[:, 0]
    f0 = cands[:, 1] + f_lo
    syms = np.array(DATA_SYMBOLS)
    # potenze dei toni per ogni simbolo dati: (B, 58, 8)
    tt = t0[:, None, None] + TOSR * syms[None, :, None]
    ff = f0[:, None, None] + FOSR * np.arange(8)[None, None, :]
    lt = lp[tt, ff]
    llr = np.empty((len(cands), 58, 3))
    for b in range(3):
        one = np.where(_TONE_BIT[b], lt, -np.inf).max(axis=2)
        zero = np.where(~_TONE_BIT[b], lt, -np.inf).max(axis=2)
        llr[:, :, b] = zero - one
    llr = llr.reshape(len(cands), 174)
    llr *= 5.0 / (np.std(llr, axis=1, keepdims=True) + 1e-9)
    bits, ok = ldpc_decode(llr)
    found = []
    for i in np.nonzero(ok)[0]:
        a91 = bits[i, :91]
        if not a91.any() or crc14(np.concatenate([a91[:77], np.zeros(5, np.uint8)])) != _num(a91[77:91]):
            continue
        text = unpack(a91[:77], hashes)
        if not text or text in out:
            continue
        tones = tones_from_codeword(bits[i])
        # SNR in 2500 Hz, come WSJT-X: potenza dei toni ricevuti contro il rumore
        rows = t0[i] + TOSR * np.arange(79)
        sig = float(np.mean(p[rows, f0[i] + FOSR * np.array(tones)])) - noise
        snr = 10 * math.log10(max(sig, noise * 1e-4) / noise) + 10 * math.log10(FS / NSPS / 2500)
        out[text] = {"message": text, "snr_db": round(max(-30.0, snr)), "dt_s": round(float(t0[i] * HOP / FS - PRE_S - START_S), 1),
                     "freq_hz": int(round(float(f0[i] * BIN_HZ)))}
        found.append((int(t0[i]), int(f0[i]), tones))
    return found
