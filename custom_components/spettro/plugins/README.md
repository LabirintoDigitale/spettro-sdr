# Plugin di Spettro

I tuoi plugin vanno nella cartella **`spettro_plugins/` dentro la configurazione di Home Assistant**
(di solito `/config/spettro_plugins/`) — **non** in `custom_components/spettro/plugins/`, che viene
sovrascritta a ogni aggiornamento. La cartella `spettro_plugins/` è **persistente**: i tuoi plugin
restano anche quando aggiorni Spettro. Al primo avvio viene creata e riempita con questi esempi.

I plugin **inclusi nella release** (gli esempi e quelli pronti all'uso, come `rtl433.py`) vengono
consegnati nella tua cartella automaticamente: ognuno viene copiato una sola volta, la prima volta
che compare in un aggiornamento. Spettro **non** sovrascrive mai un plugin che hai modificato (copia
solo se il file non c'è) e **non** ripristina quelli che hai cancellato di proposito (tiene un
registro in `.spettro_bundled.json`). Dopo un aggiornamento, **riavvia Home Assistant** e il nuovo
plugin comparirà nel menu (eventualmente premi 🔄 Ricarica plugin).

Un file `.py` per plugin. Si innestano nel blocco **Analisi segnale**
come un decoder scelto dal menu. Quando lo selezioni, Spettro sintonizza la frequenza, demodula e
passa al plugin — blocco per blocco — l'audio (o l'IQ grezzo del canale); il plugin restituisce un
pannello **HTML** da mostrare e/o l'**audio** da far sentire.

## Interfaccia

```python
PLUGIN = {
    "id": "mio_plugin",        # identificativo univoco
    "name": "Nome mostrato",   # etichetta nel menu dei decoder
    "mode": "nfm",             # modo consigliato: wfm/nfm/am/usb/lsb/cw, oppure None
    "input": "audio",          # "audio", "iq" oppure "baseband" (vedi sotto)
    # solo per input "baseband":
    "rate": 48000,             # frequenza d'uscita desiderata (si usa la più vicina ottenibile)
    "bw": 25000,               # larghezza del canale da far passare (Hz)
}

class Decoder:
    def __init__(self, freq_hz: float, rate: int):   # rate = frequenza effettiva dei campioni
        ...
    def feed(self, samples) -> dict | None:
        # samples dipende da "input":
        #   "audio"    -> numpy float, audio demodulato a 32 kHz
        #   "iq"       -> numpy complex64, IQ grezzo dell'intero blocco (1.024 MS/s, target NON a 0)
        #   "baseband" -> numpy complex64, canale GIÀ a banda base (target a 0), filtrato a "bw" e
        #                 decimato a ~"rate": pronto per demodulare π/4-DQPSK, FSK, PSK, ecc.
        # ritorna un dizionario con una o più di:
        #   "html":  str   -> pannello HTML (mostrato in un iframe sandbox)
        #   "text":  str   -> testo semplice (accodato)
        #   "audio": bytes -> PCM int16 mono 32 kHz da far sentire al posto dell'audio demodulato
        return {"html": "..."}
```

Vedi `esempio_livello.py` (audio) ed `esempio_baseband.py` (canale a banda base) come modelli.
Dopo aver aggiunto o modificato un plugin,
**riavvia Home Assistant** e ricarica il pannello.

## Attenzione (sicurezza)

Un plugin è **codice Python eseguito dentro Home Assistant**, con i suoi permessi: installa solo
plugin di cui ti fidi. L'HTML che restituisci viene mostrato in un **iframe sandbox**, isolato dal
resto del pannello.
