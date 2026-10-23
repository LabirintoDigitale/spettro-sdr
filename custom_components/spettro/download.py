"""Scaricare la registrazione grezza (IQ) di un'analisi, per aprirla con altri programmi SDR.

Due formati: WAV IQ a 16 bit (I a sinistra, Q a destra: SDR#, SDR++, SDRangel, SatDump,
Universal Radio Hacker) e cu8 grezzo (come rtl_sdr). Il nome del file segue la convenzione di
SDR# (…_<frequenza>Hz_IQ.wav), da cui i programmi leggono la frequenza centrale.
"""

from __future__ import annotations

from http import HTTPStatus
import io
import json
import time
import wave

from aiohttp import web
import numpy as np

from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .const import DOMAIN

FORMATS = ("wav", "cu8")


def iq_url(entry_id: str, fmt: str) -> str:
    return f"/api/spettro/iq/{entry_id}/{fmt}"


def _wav(raw: bytes, rate: int) -> bytes:
    a = (np.frombuffer(raw, np.uint8).astype(np.int16) - 128) * 256  # 8 bit senza segno → 16 bit con segno
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.astype("<i2").tobytes())
    return buf.getvalue()


class IqDownloadView(HomeAssistantView):
    """GET /api/spettro/iq/<id>/<wav|cu8>: la registrazione dell'analisi come file da scaricare."""

    url = "/api/spettro/iq/{entry_id}/{fmt}"
    name = "api:spettro:iq"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, fmt: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        entry = hass.data.get(DOMAIN)
        archive = entry.runtime_data.archive if entry else None
        if archive is None or fmt not in FORMATS:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        path = await hass.async_add_executor_job(archive.iq_path, entry_id)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Registrazione non salvata per questa analisi")

        def load() -> tuple[bytes, dict]:
            meta = json.loads((path.with_suffix(".json")).read_text())
            return path.read_bytes(), meta

        raw, meta = await hass.async_add_executor_job(load)
        rate = int(meta.get("sample_rate") or 2_048_000)
        tune = int(round(meta.get("tune_hz") or meta.get("target_mhz", 0) * 1e6))
        stamp = time.strftime("%Y%m%d_%H%M%SZ", time.gmtime(meta.get("timestamp", time.time())))
        name = f"spettro_{stamp}_{tune}Hz_IQ"  # come SDR#: il campionamento è nell'intestazione del WAV
        if fmt == "cu8":
            body, ctype = raw, "application/octet-stream"
        else:
            body, ctype = await hass.async_add_executor_job(_wav, raw, rate), "audio/wav"
        return web.Response(body=body, content_type=ctype,
                            headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})
