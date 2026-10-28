# Plugin di Spettro

Metti qui i tuoi plugin: un file `.py` per plugin. Si innestano nel blocco **Analisi segnale**
come un decoder scelto dal menu. Quando lo selezioni, Spettro sintonizza la frequenza, demodula e
passa al plugin — blocco per blocco — l'audio (o l'IQ grezzo del canale); il plugin restituisce un
pannello **HTML** da mostrare e/o l'**audio** da far sentire.

## Interfaccia

```python
PLUGIN = {
    "id": "mio_plugin",        # identificativo univoco
    "name": "Nome mostrato",   # etichetta nel menu dei decoder
    "mode": "nfm",             # modo consigliato: wfm/nfm/am/usb/lsb/cw, oppure None
    "input": "audio",          # "audio" (float a 32 kHz) oppure "iq" (complex64 del canale)
}

class Decoder:
    def __init__(self, freq_hz: float, rate: int):
        ...
    def feed(self, samples) -> dict | None:
        # samples: numpy float (audio) o complex64 (iq)
        # ritorna un dizionario con una o più di:
        #   "html":  str   -> pannello HTML (mostrato in un iframe sandbox)
        #   "text":  str   -> testo semplice (accodato)
        #   "audio": bytes -> PCM int16 mono 32 kHz da far sentire al posto dell'audio demodulato
        return {"html": "..."}
```

Vedi `esempio_livello.py` come modello. Dopo aver aggiunto o modificato un plugin,
**riavvia Home Assistant** e ricarica il pannello.

## Attenzione (sicurezza)

Un plugin è **codice Python eseguito dentro Home Assistant**, con i suoi permessi: installa solo
plugin di cui ti fidi. L'HTML che restituisci viene mostrato in un **iframe sandbox**, isolato dal
resto del pannello.
