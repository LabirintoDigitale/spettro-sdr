# Spettro SDR

Integrazione custom per **Home Assistant** che trasforma una chiavetta RTL-SDR condivisa con **rtl_tcp** in un analizzatore di spettro: aggiunge la voce **Spettro** nella **barra laterale** con il grafico di tutte le frequenze dell'intervallo scelto, il waterfall e l'elenco dei segnali trovati.

## Funzionalità

- Pannello **Spettro** nella barra laterale, con grafico dello spettro in dBFS, traccia **max hold**, linea della soglia di rilevamento e **waterfall** delle ultime scansioni.
- Scansione di qualsiasi intervallo tra 24 e 1766 MHz (limiti del tuner R820T): la chiavetta viene sintonizzata a passi e lo spettro ricomposto in un unico grafico.
- Parametri dal pannello: **da / a (MHz)**, **ogni quanto ripetere** la scansione (0 = continua), **guadagno** (automatico o in dB), **soglia picchi**; nelle *Avanzate* sample rate, punti FFT, tempo di assestamento e correzione ppm.
- Preset pronti: radio FM, banda aerea, satelliti meteo, 2 m, marina VHF, 70 cm, LPD/ISM 433, PMR446, ISM 868, GSM 900, ADS-B, tutto.
- **Zoom** trascinando sul grafico (doppio clic per tornare indietro), lettura frequenza/livello sotto il cursore, pulsante **Scansiona zona** per usare la zona ingrandita come nuovo intervallo.
- Tabella dei **segnali rilevati** (frequenza, livello, dB sopra il rumore): un clic ingrandisce il segnale.
- Durante le scansioni lunghe il grafico si aggiorna man mano, con la barra di avanzamento.
- Sensori per automazioni: **Picco principale** (MHz, con la lista dei picchi negli attributi), **Rumore di fondo** (dBFS) e **Segnali rilevati**.
- Interruttore **Scansione** (`switch.spettro_sdr_scansione`, anche nel pannello) per mettere in pausa la scansione a mano o da un'automazione.
- Opzione **Scansiona solo con il pannello aperto**: con il pannello chiuso l'integrazione non si collega al server, utile se sullo stesso computer girano altri servizi (Pi-hole, DNS…). In questa modalità i sensori si aggiornano solo mentre il pannello è aperto.

## Requisiti

- Un server **rtl_tcp** raggiungibile in rete (porta predefinita `1234`), ad esempio su un mini PC o un Raspberry Pi con la chiavetta RTL-SDR collegata:

  ```bash
  rtl_tcp -a 0.0.0.0 -p 1234
  ```

## Installazione

### HACS (consigliato)

1. HACS → menu ⋮ → **Repository personalizzati**
2. URL: `https://github.com/LabirintoDigitale/spettro-sdr`, categoria **Integrazione**
3. Installa **Spettro SDR** e riavvia Home Assistant.

### Manuale

Copia la cartella `custom_components/spettro` dentro `config/custom_components/` e riavvia Home Assistant.

### Configurazione

**Impostazioni → Dispositivi e servizi → Aggiungi integrazione → Spettro SDR**, poi inserisci indirizzo IP e porta del server rtl_tcp. Comparirà la voce *Spettro* nella barra laterale. I parametri di scansione si cambiano direttamente dal pannello (serve un utente amministratore) e vengono salvati.

## Note

- rtl_tcp accetta **un solo client alla volta**. L'integrazione si collega solo durante la scansione e poi si stacca, quindi tra una scansione e l'altra puoi usare SDR#, SDR++ o altri programmi. Se un altro client resta collegato, la scansione va in errore e riprova dopo almeno 10 secondi.
- Tempi indicativi a 2,048 MS/s: banda FM (20 MHz) circa 2 s, tutto 24–1766 MHz circa 2–3 minuti.
- Mentre è collegata, la chiavetta invia dati in continuo: a 2,048 MS/s sono circa 4 MB/s (33 Mbit/s) e un po' di carico CPU/USB sul server. Se il server ospita anche altri servizi e li rallenta, usa l'interruttore **Scansione**, l'opzione *solo con il pannello aperto* o un intervallo più lungo.
- Meglio collegare server e Home Assistant via cavo; con il Wi-Fi conviene abbassare il sample rate (Avanzate).
- I livelli sono in **dBFS** (relativi al fondo scala della chiavetta), non in dBm calibrati.
- Le stazioni FM sono larghe: a volte la stessa stazione compare con due picchi vicini.

## Rilasci

HACS mostra come versione l'ultima **release** GitHub. Per pubblicarne una basta aumentare `version` in `custom_components/spettro/manifest.json` e `VERSION` in `custom_components/spettro/const.py` (devono coincidere) e fare push su `main`: il workflow *Release* crea il tag `vX.Y.Z` e la release se non esistono già.

## Licenza

MIT
