# Plugin di Spettro

I plugin **inclusi nella release** (gli esempi e quelli pronti all'uso, come `rtl433.py`) stanno qui,
in `custom_components/spettro/plugins/`, e vengono **caricati direttamente da qui**: si aggiornano da
soli a ogni aggiornamento di Spettro, senza che tu debba copiare nulla.

I **tuoi** plugin vanno invece nella cartella **`spettro_plugins/` dentro la configurazione di Home
Assistant** (di solito `/config/spettro_plugins/`). Quella cartella è **persistente**: i tuoi plugin
restano tra un aggiornamento e l'altro. Se metti lì un file con lo **stesso id** di un plugin incluso,
il tuo ha la precedenza (puoi così personalizzarne uno).

Dopo aver aggiunto o modificato un plugin, premi 🔄 **Ricarica plugin** nel pannello (o riavvia Home
Assistant) e ricarica la pagina.

> Nota: le versioni fino alla 1.45 copiavano i plugin inclusi dentro `spettro_plugins/`. Dalla 1.47
> quelle vecchie copie vengono rimosse in automatico (i tuoi plugin non si toccano), così la versione
> aggiornata inclusa nella release non resta coperta da una copia datata.

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
