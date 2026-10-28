"""Plugin di terze parti per l'analisi in diretta.

Un plugin è un file Python. Quelli inclusi nell'integrazione (esempi e rtl_433) stanno in
`custom_components/spettro/plugins/` e si caricano direttamente da lì (aggiornati con la release). I
plugin dell'utente vanno nella cartella persistente `spettro_plugins/` della configurazione di Home
Assistant (di solito /config/spettro_plugins/; non si cancella con gli aggiornamenti). Si innesta nel blocco
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
_ERRORS: list[dict[str, str]] = []  # plugin che non si sono caricati, col motivo
_USER_DIR: Path | None = None  # cartella persistente nella config di HA (impostata all'avvio)


def bundled_dir() -> Path:
    """Gli esempi inclusi nell'integrazione (sovrascritti a ogni aggiornamento: solo riferimento)."""
    return Path(__file__).parent / "plugins"


def init_user_dir(path: str | Path) -> Path:
    """Imposta la cartella dei plugin **dell'utente** (persistente, sotto la config di HA).

    I plugin inclusi nell'integrazione (esempi e rtl_433) NON stanno più qui: si caricano direttamente
    dall'integrazione, così gli aggiornamenti della release li aggiornano da soli. Qui vanno solo i
    plugin aggiunti dall'utente, che restano tra un aggiornamento e l'altro.

    Migrazione: le versioni precedenti copiavano qui i plugin inclusi (tenendone un registro in
    `.spettro_bundled.json`). Quelle vecchie copie vengono rimosse, per non avere doppioni datati che
    coprirebbero la versione aggiornata inclusa nella release. I file aggiunti dall'utente non si
    toccano.
    """
    global _USER_DIR
    import json  # noqa: PLC0415

    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)

    manifest = p / ".spettro_bundled.json"
    try:
        delivered = list(json.loads(manifest.read_text("utf-8")))
    except (OSError, ValueError):
        delivered = []
    for rel in delivered:  # rimuovi le copie che avevamo consegnato noi (solo quelle)
        f = p / rel
        try:
            if f.is_file():
                f.unlink()
                _LOGGER.info("Rimossa vecchia copia inclusa: %s", rel)
        except OSError:
            _LOGGER.warning("Non riesco a rimuovere la vecchia copia %s", f)
    try:
        if manifest.exists():
            manifest.unlink()
    except OSError:
        pass
    # togli eventuali sottocartelle rimaste vuote (es. bin/)
    for sub in sorted({Path(rel).parts[0] for rel in delivered if len(Path(rel).parts) > 1}):
        d = p / sub
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    _USER_DIR = p
    _LOGGER.info("Cartella plugin dell'utente: %s", p)
    return p


def _folder() -> Path:
    if _USER_DIR is not None:
        return _USER_DIR
    folder = bundled_dir()
    folder.mkdir(exist_ok=True)
    return folder


def _load_one(py: Path, found: dict[str, dict[str, Any]], errors: list[dict[str, str]]) -> None:
    """Carica un singolo file plugin e lo aggiunge a `found` (o a `errors` se è rotto)."""
    try:
        spec = importlib.util.spec_from_file_location(f"spettro_plugin_{py.stem}", py)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        meta = getattr(mod, "PLUGIN", None)
        cls = getattr(mod, "Decoder", None)
        if not isinstance(meta, dict) or "id" not in meta or cls is None:
            msg = "manca il dizionario PLUGIN{id,…} o la classe Decoder"
            _LOGGER.warning("Plugin %s ignorato: %s", py.name, msg)
            errors.append({"file": py.name, "error": msg})
            return
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
        found[pid] = desc  # a parità di id, l'ultimo caricato vince (l'utente ha la precedenza)
        _LOGGER.info("Plugin Spettro caricato: %s (%s)", pid, py.name)
    except Exception as err:  # noqa: BLE001 - un plugin rotto non deve impedire l'avvio
        _LOGGER.exception("Errore caricando il plugin %s", py.name)
        errors.append({"file": py.name, "error": f"{type(err).__name__}: {err}"})


def load(reload: bool = False) -> dict[str, dict[str, Any]]:
    """Carica i plugin (una volta sola, salvo reload). {id: descrittore}.

    Si caricano prima i plugin **inclusi nell'integrazione** (sempre aggiornati con la release) e poi
    quelli **dell'utente** nella cartella persistente: a parità di id, quello dell'utente ha la
    precedenza (così si può sovrascrivere un plugin incluso mettendone uno con lo stesso id).
    """
    global _PLUGINS, _ERRORS
    if _PLUGINS is not None and not reload:
        return _PLUGINS
    found: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    dirs = [bundled_dir()]
    if _USER_DIR is not None and _USER_DIR.resolve() != bundled_dir().resolve():
        dirs.append(_USER_DIR)
    for folder in dirs:
        for py in sorted(folder.glob("*.py")):
            if not py.name.startswith("_"):
                _load_one(py, found, errors)
    _PLUGINS = found
    _ERRORS = errors
    return _PLUGINS


def listing() -> list[dict[str, Any]]:
    """Descrittori senza la classe, per il frontend (id, name, mode, input)."""
    return [{k: v for k, v in p.items() if k != "cls"} for p in load().values()]


def errors() -> list[dict[str, str]]:
    """Plugin che non si sono caricati, col motivo: [{"file":…, "error":…}]."""
    load()
    return list(_ERRORS)


def folder_path() -> str:
    """Il percorso della cartella da cui si caricano i plugin (per mostrarlo all'utente)."""
    return str(_folder())


def get(pid: str | None) -> dict[str, Any] | None:
    return load().get(pid) if pid else None
