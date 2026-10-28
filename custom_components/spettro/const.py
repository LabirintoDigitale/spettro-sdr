"""Costanti dell'integrazione Spettro."""

DOMAIN = "spettro"
VERSION = "1.35.0"

DEFAULT_PORT = 1234

# cartella (dentro la configurazione di Home Assistant) dell'archivio delle analisi
ARCHIVE_DIR = "spettro_archivio"
# cartella delle immagini dei satelliti meteo
METEO_DIR = "spettro_meteo"

# opzione salvata nella config entry: interruttore "Scansione"
OPT_ENABLED = "enabled"

PANEL_URL = "spettro"
PANEL_TITLE = "Spettro"
PANEL_ICON = "mdi:sine-wave"
PANEL_ELEMENT = "spettro-panel"
STATIC_URL = "/spettro_static"

DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"
DATA_WS_REGISTERED = f"{DOMAIN}_ws_registered"
