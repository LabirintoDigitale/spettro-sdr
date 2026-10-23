"""Orbite dei satelliti meteo: SGP4 dai TLE, passaggi sopra la stazione e Doppler.

Implementazione del modello SGP4 (solo orbite basse, "near earth", come quelle dei
Meteor-M: periodo < 225 minuti) seguendo Vallado et al., "Revisiting Spacetrack
Report #3" (2006), con le costanti WGS-72 con cui sono calcolati i TLE. Non serve
nessuna libreria esterna: bastano gli elementi orbitali (TLE) scaricati da CelesTrak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from typing import Any

# costanti WGS-72 del modello SGP4
MU = 398600.8
RE = 6378.135
XKE = 60.0 / math.sqrt(RE**3 / MU)
J2 = 0.001082616
J3 = -0.00000253881
J4 = -0.00000165597
J3OJ2 = J3 / J2
VKMPERSEC = RE * XKE / 60.0
TWOPI = 2 * math.pi
X2O3 = 2.0 / 3.0

# ellissoide WGS-84 per la posizione della stazione
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
OMEGA_EARTH = 7.292115e-5  # rad/s
C_KM_S = 299792.458


class TleError(ValueError):
    """TLE non valido o orbita non gestita."""


def _checksum_ok(line: str) -> bool:
    s = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return line[68:69].isdigit() and s % 10 == int(line[68])


def _expo(field: str) -> float:
    """Campo TLE in notazione compatta (es. ' 12345-4' = 0.12345e-4)."""
    f = field.strip()
    if not f:
        return 0.0
    sign = -1.0 if f[0] == "-" else 1.0
    f = f.lstrip("+-")
    mant, exp = f[:-2], f[-2:]
    return sign * float("0." + mant.strip()) * 10 ** int(exp)


def _jday(dt: datetime) -> float:
    """Data giuliana (UTC) di un istante."""
    return dt.timestamp() / 86400.0 + 2440587.5


def gmst(jd: float) -> float:
    """Tempo siderale medio di Greenwich (radianti), modello IAU-82."""
    t = (jd - 2451545.0) / 36525.0
    s = -6.2e-6 * t**3 + 0.093104 * t**2 + (876600.0 * 3600 + 8640184.812866) * t + 67310.54841
    return math.fmod(math.radians(s / 240.0), TWOPI) % TWOPI


@dataclass
class Satellite:
    """Satellite propagato con SGP4."""

    name: str
    norad: int
    epoch: datetime

    def __init__(self, name: str, line1: str, line2: str) -> None:
        line1, line2 = line1.rstrip(), line2.rstrip()
        if len(line1) < 69 or len(line2) < 69 or line1[0] != "1" or line2[0] != "2":
            raise TleError("formato TLE non valido")
        if not (_checksum_ok(line1) and _checksum_ok(line2)):
            raise TleError("checksum TLE errato")
        self.name = name.strip()
        self.norad = int(line1[2:7])
        yy = int(line1[18:20])
        year = 2000 + yy if yy < 57 else 1900 + yy
        doy = float(line1[20:32])
        self.epoch = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=doy - 1)
        self.jd_epoch = _jday(self.epoch)
        self.bstar = _expo(line1[53:61])
        self.inclo = math.radians(float(line2[8:16]))
        self.nodeo = math.radians(float(line2[17:25]))
        self.ecco = float("0." + line2[26:33].strip())
        self.argpo = math.radians(float(line2[34:42]))
        self.mo = math.radians(float(line2[43:51]))
        self.no_kozai = float(line2[52:63]) * TWOPI / 1440.0  # rad/min
        if TWOPI / self.no_kozai >= 225.0:
            raise TleError("orbita alta (deep space): non gestita")
        self._init()

    def _init(self) -> None:  # noqa: PLR0915 - trascrizione diretta di sgp4init
        ecco, inclo, no_kozai, bstar = self.ecco, self.inclo, self.no_kozai, self.bstar
        eccsq = ecco * ecco
        omeosq = 1.0 - eccsq
        rteosq = math.sqrt(omeosq)
        cosio = math.cos(inclo)
        cosio2 = cosio * cosio
        ak = (XKE / no_kozai) ** X2O3
        d1 = 0.75 * J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
        del_ = d1 / (ak * ak)
        adel = ak * (1.0 - del_ * del_ - del_ * (1.0 / 3.0 + 134.0 * del_ * del_ / 81.0))
        del_ = d1 / (adel * adel)
        self.no = no = no_kozai / (1.0 + del_)
        ao = (XKE / no) ** X2O3
        sinio = math.sin(inclo)
        po = ao * omeosq
        con42 = 1.0 - 5.0 * cosio2
        self.con41 = con41 = -con42 - cosio2 - cosio2
        posq = po * po
        rp = ao * (1.0 - ecco)

        ss = 78.0 / RE + 1.0
        qzms2t = ((120.0 - 78.0) / RE) ** 4
        self.isimp = rp < (220.0 / RE + 1.0)
        sfour, qzms24 = ss, qzms2t
        perige = (rp - 1.0) * RE
        if perige < 156.0:
            sfour = perige - 78.0
            if perige < 98.0:
                sfour = 20.0
            qzms24 = ((120.0 - sfour) / RE) ** 4
            sfour = sfour / RE + 1.0
        pinvsq = 1.0 / posq
        tsi = 1.0 / (ao - sfour)
        self.eta = eta = ao * ecco * tsi
        etasq = eta * eta
        eeta = ecco * eta
        psisq = abs(1.0 - etasq)
        coef = qzms24 * tsi**4
        coef1 = coef / psisq**3.5
        cc2 = coef1 * no * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
                            + 0.375 * J2 * tsi / psisq * con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
        self.cc1 = cc1 = bstar * cc2
        cc3 = -2.0 * coef * tsi * J3OJ2 * no * sinio / ecco if ecco > 1.0e-4 else 0.0
        self.x1mth2 = x1mth2 = 1.0 - cosio2
        self.cc4 = 2.0 * no * coef1 * ao * omeosq * (
            eta * (2.0 + 0.5 * etasq) + ecco * (0.5 + 2.0 * etasq)
            - J2 * tsi / (ao * psisq) * (-3.0 * con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                                         + 0.75 * x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq)) * math.cos(2.0 * self.argpo)))
        self.cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
        cosio4 = cosio2 * cosio2
        temp1 = 1.5 * J2 * pinvsq * no
        temp2 = 0.5 * temp1 * J2 * pinvsq
        temp3 = -0.46875 * J4 * pinvsq * pinvsq * no
        self.mdot = no + 0.5 * temp1 * rteosq * con41 + 0.0625 * temp2 * rteosq * (13.0 - 78.0 * cosio2 + 137.0 * cosio4)
        self.argpdot = (-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                        + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
        xhdot1 = -temp1 * cosio
        self.nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio
        self.omgcof = bstar * cc3 * math.cos(self.argpo)
        self.xmcof = -X2O3 * coef * bstar / eeta if ecco > 1.0e-4 else 0.0
        self.nodecf = 3.5 * omeosq * xhdot1 * cc1
        self.t2cof = 1.5 * cc1
        den = 1.0 + cosio if abs(cosio + 1.0) > 1.5e-12 else 1.5e-12
        self.xlcof = -0.25 * J3OJ2 * sinio * (3.0 + 5.0 * cosio) / den
        self.aycof = -0.5 * J3OJ2 * sinio
        self.delmo = (1.0 + eta * math.cos(self.mo)) ** 3
        self.sinmao = math.sin(self.mo)
        self.x7thm1 = 7.0 * cosio2 - 1.0
        if not self.isimp:
            cc1sq = cc1 * cc1
            self.d2 = d2 = 4.0 * ao * tsi * cc1sq
            temp = d2 * tsi * cc1 / 3.0
            self.d3 = d3 = (17.0 * ao + sfour) * temp
            self.d4 = d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * cc1
            self.t3cof = d2 + 2.0 * cc1sq
            self.t4cof = 0.25 * (3.0 * d3 + cc1 * (12.0 * d2 + 10.0 * cc1sq))
            self.t5cof = 0.2 * (3.0 * d4 + 12.0 * cc1 * d3 + 6.0 * d2 * d2 + 15.0 * cc1sq * (2.0 * d2 + cc1sq))

    def propagate(self, tsince: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Posizione (km) e velocità (km/s) nel sistema TEME, `tsince` minuti dopo l'epoca del TLE."""
        t = tsince
        xmdf = self.mo + self.mdot * t
        argpdf = self.argpo + self.argpdot * t
        nodedf = self.nodeo + self.nodedot * t
        argpm, mm = argpdf, xmdf
        t2 = t * t
        nodem = nodedf + self.nodecf * t2
        tempa = 1.0 - self.cc1 * t
        tempe = self.bstar * self.cc4 * t
        templ = self.t2cof * t2
        if not self.isimp:
            delomg = self.omgcof * t
            delm = self.xmcof * ((1.0 + self.eta * math.cos(xmdf)) ** 3 - self.delmo)
            temp = delomg + delm
            mm = xmdf + temp
            argpm = argpdf - temp
            t3 = t2 * t
            t4 = t3 * t
            tempa = tempa - self.d2 * t2 - self.d3 * t3 - self.d4 * t4
            tempe = tempe + self.bstar * self.cc5 * (math.sin(mm) - self.sinmao)
            templ = templ + self.t3cof * t3 + t4 * (self.t4cof + t * self.t5cof)
        am = (XKE / self.no) ** X2O3 * tempa * tempa
        nm = XKE / am**1.5
        em = max(self.ecco - tempe, 1.0e-6)
        mm = mm + self.no * templ
        xlm = mm + argpm + nodem
        nodem = math.fmod(nodem, TWOPI)
        argpm = math.fmod(argpm, TWOPI)
        xlm = math.fmod(xlm, TWOPI)
        mm = math.fmod(xlm - argpm - nodem, TWOPI)
        inclm = self.inclo
        sinip, cosip = math.sin(inclm), math.cos(inclm)

        axnl = em * math.cos(argpm)
        temp = 1.0 / (am * (1.0 - em * em))
        aynl = em * math.sin(argpm) + temp * self.aycof
        xl = mm + argpm + nodem + temp * self.xlcof * axnl
        u = math.fmod(xl - nodem, TWOPI)
        eo1, tem5, ktr = u, 9999.9, 1
        sineo1 = coseo1 = 0.0
        while abs(tem5) >= 1.0e-12 and ktr <= 10:
            sineo1, coseo1 = math.sin(eo1), math.cos(eo1)
            tem5 = 1.0 - coseo1 * axnl - sineo1 * aynl
            tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / tem5
            tem5 = max(-0.95, min(0.95, tem5))
            eo1 += tem5
            ktr += 1
        ecose = axnl * coseo1 + aynl * sineo1
        esine = axnl * sineo1 - aynl * coseo1
        el2 = axnl * axnl + aynl * aynl
        pl = am * (1.0 - el2)
        if pl < 0.0:
            raise TleError("orbita decaduta")
        rl = am * (1.0 - ecose)
        rdotl = math.sqrt(am) * esine / rl
        rvdotl = math.sqrt(pl) / rl
        betal = math.sqrt(1.0 - el2)
        temp = esine / (1.0 + betal)
        sinu = am / rl * (sineo1 - aynl - axnl * temp)
        cosu = am / rl * (coseo1 - axnl + aynl * temp)
        su = math.atan2(sinu, cosu)
        sin2u = (cosu + cosu) * sinu
        cos2u = 1.0 - 2.0 * sinu * sinu
        temp = 1.0 / pl
        temp1 = 0.5 * J2 * temp
        temp2 = temp1 * temp
        mrt = rl * (1.0 - 1.5 * temp2 * betal * self.con41) + 0.5 * temp1 * self.x1mth2 * cos2u
        su = su - 0.25 * temp2 * self.x7thm1 * sin2u
        xnode = nodem + 1.5 * temp2 * cosip * sin2u
        xinc = inclm + 1.5 * temp2 * cosip * sinip * cos2u
        mvt = rdotl - nm * temp1 * self.x1mth2 * sin2u / XKE
        rvdot = rvdotl + nm * temp1 * (self.x1mth2 * cos2u + 1.5 * self.con41) / XKE
        sinsu, cossu = math.sin(su), math.cos(su)
        snod, cnod = math.sin(xnode), math.cos(xnode)
        sini, cosi = math.sin(xinc), math.cos(xinc)
        xmx, xmy = -snod * cosi, cnod * cosi
        ux, uy, uz = xmx * sinsu + cnod * cossu, xmy * sinsu + snod * cossu, sini * sinsu
        vx, vy, vz = xmx * cossu - cnod * sinsu, xmy * cossu - snod * sinsu, sini * cossu
        r = (mrt * ux * RE, mrt * uy * RE, mrt * uz * RE)
        v = ((mvt * ux + rvdot * vx) * VKMPERSEC, (mvt * uy + rvdot * vy) * VKMPERSEC,
             (mvt * uz + rvdot * vz) * VKMPERSEC)
        return r, v

    def at(self, when: datetime) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Posizione e velocità nel sistema terrestre (ECEF, km e km/s) a un istante UTC."""
        r, v = self.propagate((_jday(when) - self.jd_epoch) * 1440.0)
        g = gmst(_jday(when))
        c, s = math.cos(g), math.sin(g)
        x, y = c * r[0] + s * r[1], -s * r[0] + c * r[1]
        vx, vy = c * v[0] + s * v[1], -s * v[0] + c * v[1]
        # velocità rispetto alla Terra che ruota: v - ω × r
        return (x, y, r[2]), (vx + OMEGA_EARTH * y, vy - OMEGA_EARTH * x, v[2])


class Observer:
    """Stazione a terra (latitudine e longitudine in gradi, quota in metri)."""

    def __init__(self, lat: float, lon: float, alt_m: float = 0.0) -> None:
        self.lat, self.lon = math.radians(lat), math.radians(lon)
        e2 = WGS84_F * (2 - WGS84_F)
        n = WGS84_A / math.sqrt(1 - e2 * math.sin(self.lat) ** 2)
        h = alt_m / 1000.0
        self.ecef = ((n + h) * math.cos(self.lat) * math.cos(self.lon),
                     (n + h) * math.cos(self.lat) * math.sin(self.lon),
                     (n * (1 - e2) + h) * math.sin(self.lat))

    def look(self, sat: Satellite, when: datetime) -> dict[str, float]:
        """Azimut, elevazione (gradi), distanza (km) e velocità radiale (km/s) del satellite."""
        r, v = sat.at(when)
        dx, dy, dz = r[0] - self.ecef[0], r[1] - self.ecef[1], r[2] - self.ecef[2]
        sl, cl = math.sin(self.lat), math.cos(self.lat)
        so, co = math.sin(self.lon), math.cos(self.lon)
        east = -so * dx + co * dy
        north = -sl * co * dx - sl * so * dy + cl * dz
        up = cl * co * dx + cl * so * dy + sl * dz
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        return {
            "az": math.degrees(math.atan2(east, north)) % 360.0,
            "el": math.degrees(math.asin(up / rng)),
            "range_km": rng,
            "range_rate": (dx * v[0] + dy * v[1] + dz * v[2]) / rng,
            "lat": math.degrees(math.atan2(r[2], math.hypot(r[0], r[1]))),
        }

    def doppler(self, sat: Satellite, when: datetime, freq_hz: float) -> float:
        """Spostamento Doppler (Hz) della frequenza trasmessa dal satellite."""
        return -self.look(sat, when)["range_rate"] / C_KM_S * freq_hz


def passes(sat: Satellite, obs: Observer, start: datetime, hours: float = 24.0,
           min_el: float = 15.0) -> list[dict[str, Any]]:
    """Passaggi sopra l'orizzonte nelle prossime `hours` ore con elevazione massima ≥ min_el."""
    out: list[dict[str, Any]] = []
    step = timedelta(seconds=30)
    t, end = start, start + timedelta(hours=hours)
    prev = obs.look(sat, t)["el"]
    aos: datetime | None = t if prev > 0 else None

    def cross(a: datetime, b: datetime) -> datetime:
        ea = obs.look(sat, a)["el"]
        for _ in range(12):  # bisezione fino a ~0,01 s
            m = a + (b - a) / 2
            em = obs.look(sat, m)["el"]
            if (em > 0) == (ea > 0):
                a, ea = m, em
            else:
                b = m
        return a + (b - a) / 2

    while t < end:
        n = t + step
        el = obs.look(sat, n)["el"]
        if prev <= 0 < el:
            aos = cross(t, n)
        elif prev > 0 >= el and aos is not None:
            los = cross(t, n)
            # massimo: campioni ogni 10 s, poi rifinitura
            best, tbest = -90.0, aos
            k = aos
            while k <= los:
                e = obs.look(sat, k)["el"]
                if e > best:
                    best, tbest = e, k
                k += timedelta(seconds=10)
            if best >= min_el:
                a0, a1 = obs.look(sat, aos), obs.look(sat, los)
                out.append({
                    "satellite": sat.name, "norad": sat.norad,
                    "aos": aos, "los": los, "tca": tbest, "max_el": round(best, 1),
                    "az_aos": round(a0["az"]), "az_los": round(a1["az"]),
                    "northbound": a1["lat"] > a0["lat"],
                })
            aos = None
        prev, t = el, n
    return out
