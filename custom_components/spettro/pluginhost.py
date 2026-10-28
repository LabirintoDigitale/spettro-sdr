"""Plugin di terze parti per l'analisi in diretta.

Un plugin è un file Python nella cartella `plugins/` di questa integrazione. Si innesta nel blocco
"Analisi segnale" come un decoder: quando lo si sceglie, Spettro sintonizza la frequenza, demodula e
passa al plugin, blocco per blocco, l'audio (o l'IQ grezzo del canale). Il plugin restituisce un
pannello HTML da mostrare e/o l'audio da far sentire.

Interfaccia di un plugin (vedi plugins/esempio_livello.py):

    PLUGIN = {
        "id": "mio_plugin",            # identificativo univoco
        "name": "Nome mostrato",       # etichetta nel menu dei decoder
        "mode": "nfm",                 # modo di demodulazione consigliato (o None = quello attuale)
        "input": "audio",              # "audio", "iq" oppure "baseband" (vedi sotto)
        # solo per input "baseband":
        "rate": 48000,                 # frequenza d'uscita desiderata (si userà la più vicina ottenibile)
        "bw": 25000,                   # larghezza del canale da far passare (Hz)
    }

    class Decoder:
        def __init__(self, freq_hz: float, rate: int): ...  # rate = frequenza effettiva dei campioni
        def feed(self, samples) -> dict | None:
            # samples dipende da "input":
            #   "audio"    -> numpy float, audio demodulato a 32 kHz
            #   "iq"       -> numpy complex64, IQ grezzo dell'intero blocco (1.024 MS/s, target NON a 0)
            #   "baseband" -> numpy complex64, il canale GIÀ a banda base (target a 0), filtrato a "bw"
            #                 e decimato a ~"rate" (es. per demodulare π/4-DQPSK, FSK, PSK…)
            # Restituisce un dizionario con una o più di:
            #   {"html": "<...>", "text": "...", "audio": <bytes PCM int16 mono 32 kHz>}
            ...

ATTENZIONE SICUREZZA: un plugin è codice Python eseguito dentro Home Assistant, con i suoi permessi.
Installa solo plugin di cui ti fidi. L'HTML che un plugin restituisce viene mostrato in un riquadro
isolato (iframe sandbox) nel pannello.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

AUDIO_RATE = 32_000  # frequenza dell'audio demodulato passato ai plugin "audio"

_PLUGINS: dict[str, dict[str, Any]] | None = None


def _folder() -> Path:
    folder = Path(__file__).parent / "plugins"
    folder.mkdir(exist_ok=True)
    return folder


def load(reload: bool = False) -> dict[str, dict[str, Any]]:
    """Carica i plugin dalla cartella plugins/ (una volta sola, salvo reload). {id: descrittore}."""
    global _PLUGINS
    if _PLUGINS is not None and not reload:
        return _PLUGINS
    found: dict[str, dict[str, Any]] = {}
    for py in sorted(_folder().glob("*.py")):
        if py.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"spettro_plugin_{py.stem}", py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "PLUGIN", None)
            cls = getattr(mod, "Decoder", None)
            if not isinstance(meta, dict) or "id" not in meta or cls is None:
                _LOGGER.warning("Plugin %s ignorato: mancano PLUGIN{id,…} o la classe Decoder", py.name)
                continue
            pid = str(meta["id"])
            inp = meta.get("input")
            inp = inp if inp in ("iq", "baseband") else "audio"
            desc = {
                "id": pid,
                "name": str(meta.get("name", pid)),
                "mode": meta.get("mode"),
                "input": inp,
                "cls": cls,
            }
            if inp == "baseband":
                # canale pronto: a banda base, filtrato e decimato alla frequenza chiesta
                desc["rate"] = int(meta.get("rate", 48_000))      # frequenza d'uscita desiderata (Hz)
                desc["bw"] = float(meta.get("bw", 25_000))        # larghezza del canale da far passare (Hz)
            found[pid] = desc
            _LOGGER.info("Plugin Spettro caricato: %s (%s)", pid, py.name)
        except Exception:  # noqa: BLE001 - un plugin rotto non deve impedire l'avvio
            _LOGGER.exception("Errore caricando il plugin %s", py.name)
    _PLUGINS = found
    return _PLUGINS


def listing() -> list[dict[str, Any]]:
    """Descrittori senza la classe, per il frontend (id, name, mode, input)."""
    return [{k: v for k, v in p.items() if k != "cls"} for p in load().values()]


def get(pid: str | None) -> dict[str, Any] | None:
    return load().get(pid) if pid else None
