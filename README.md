<p align="center">
  <img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/logo.png" width="200" alt="Spettro SDR">
</p>

# Spettro SDR

Integrazione custom per **Home Assistant** che trasforma una chiavetta RTL-SDR condivisa con **rtl_tcp** in un analizzatore di spettro: aggiunge la voce **Spettro** nella **barra laterale** con il grafico di tutte le frequenze dell'intervallo scelto, il waterfall e l'elenco dei segnali trovati.

## Funzionalità

- Pannello **Spettro** nella barra laterale, con grafico dello spettro in dBFS, traccia **max hold**, linea della soglia di rilevamento e **waterfall** delle ultime scansioni.
- Scansione di qualsiasi intervallo tra 24 e 1766 MHz (limiti del tuner R820T): la chiavetta viene sintonizzata a passi e lo spettro ricomposto in un unico grafico.
- Parametri dal pannello: **da / a (MHz)**, **ogni quanto ripetere** la scansione (0 = continua), **guadagno** (automatico o in dB), **soglia picchi**; nelle *Avanzate* sample rate, punti FFT, tempo di assestamento e correzione ppm.
- Preset pronti: radio FM, banda aerea, satelliti meteo, 2 m, marina VHF, 70 cm, LPD/ISM 433, PMR446, ISM 868, GSM 900, ADS-B, tutto.
- **Zoom** trascinando sul grafico (doppio clic per tornare indietro), lettura frequenza/livello sotto il cursore, pulsante **Scansiona zona** per usare la zona ingrandita come nuovo intervallo.
- **Analisi segnale**: tocca un picco sul grafico (o una riga della tabella) e premi **Analizza**. La scansione si sospende per qualche secondo, la chiavetta registra il segnale a frequenza fissa e il pannello mostra una cascata ad alta risoluzione (~1 kHz, ~5 ms per riga) con le misure: frequenza centrale, banda occupata, SNR, toni distinti, trasmissione continua o a burst (durata e ripetizione), frequenza che sale/scende (chirp) o due toni (FSK), più un'ipotesi sul **tipo di segnale** ricavata dalle misure. L'ultima analisi è anche un'entità immagine (**Ultima analisi**).
- **RDS delle radio FM**: per le stazioni FM broadcast l'analisi decodifica anche i dati digitali RDS trasmessi sulla sottoportante a 57 kHz: nome della stazione (PS), testo (RadioText, di solito titolo e artista), tipo di programma, codice PI, frequenze alternative, ora trasmessa, informazioni sul traffico e se la stazione è stereo (pilota a 19 kHz). Il nome compare anche nel tipo rilevato e nell'archivio. Il nome arriva già con 1 s di registrazione, il testo completo di solito con 2–5 s (*Durata analisi*); con segnale debole i caratteri non ricevuti sono indicati con «·».
- **DAB / DAB+ (radio digitale)**: i multiplex della banda III (174–240 MHz, canali 5A…13F, in Italia soprattutto il 12) compaiono come blocchi piatti larghi 1,5 MHz e l'analisi li riconosce con il loro canale. Sintonizzandosi al centro del canale decodifica il canale di servizio (FIC: sincronismo sulle trame da 96 ms, demodulazione OFDM/DQPSK, Viterbi, CRC) e mostra **nome del multiplex**, codice EId e l'**elenco delle radio** con il tipo (DAB o DAB+); il nome finisce anche nel tipo rilevato e nell'archivio. Serve il sample rate di 2,048 MS/s (quello predefinito). Per ogni radio si vede anche la velocità (kbit/s).
- **Ascolto delle radio DAB+**: il ▶ accanto a una radio DAB+ avvia l'analisi continua sul suo multiplex. L'integrazione demodula in Python tutto il multiplex, estrae il sottocanale della radio (de-interleaving temporale, Viterbi, Reed-Solomon) e manda al pannello i pacchetti audio HE-AAC, che **suona il decoder AAC del browser** (WebCodecs: Chrome, Edge, Firefox recenti); nessun programma esterno. Il pannello mostra il **testo DLS** (titolo, artista, programma) con lo storico, e la qualità: pacchetti audio integri e correzioni Reed-Solomon. La chiavetta va a 2,048 MS/s (circa 4 MB/s in rete). Le radio DAB classiche (MP2) non si ascoltano.
- **Preferiti**: con il cuoricino ♡ accanto ad *Analizza* la frequenza selezionata (dal grafico o dall'archivio) entra nei preferiti e il cuore diventa ❤. La scheda *Preferiti* li elenca con il nome: quello RDS per le radio FM, altrimenti *NONAME 1*, *NONAME 2*…; con ✏ lo cambi a mano (un nome scritto da te non viene mai sostituito dall'RDS; lasciandolo vuoto torna automatico). Toccando la riga si seleziona la frequenza, ▶ avvia l'analisi continua, ❤ la toglie. I preferiti sono salvati in Home Assistant, quindi uguali su telefono e computer.
- **Analisi continua (in diretta)**: dal pulsante *▶ Analisi continua* o dai preferiti la chiavetta resta sintonizzata fissa sulla frequenza (la scansione si sospende e alla fine riparte, solo se era attiva) e il pannello mostra in diretta la cascata che scorre, il livello del segnale e l'**audio** (FM larga, FM stretta o AM, con *Ascolta*/*Muto*). Per le radio FM l'**RDS si aggiorna** ogni secondo, con lo storico dei testi ricevuti (titoli, conduttori…). In diretta si usa 1,024 MS/s (circa 2 MB/s in rete); l'elaborazione avviene sul computer di Home Assistant. Se tutti i pannelli vengono chiusi l'analisi continua si ferma da sola dopo 30 s, per liberare la chiavetta.
- **Ascolto**: la registrazione viene demodulata in FM larga (radio), FM stretta (voce) e AM; il pannello propone la più adatta in un lettore audio e si può passare alle altre.
- **Analisi automatica dei picchi** (opzione *Analizza subito i picchi trovati*): con le stesse impostazioni della scansione, quando un passo trova un picco sopra la soglia la ricerca si interrompe, il picco viene registrato e analizzato (per la *Durata analisi* delle Avanzate, la stessa del pulsante Analizza; ogni secondo in più allunga la scansione per ogni picco nuovo), poi la scansione riprende da dove era rimasta. Una frequenza già analizzata non viene rianalizzata prima di *Rianalizza dopo (min)* (predefinito 30, 0 = ogni volta). Facoltativi, con il loro checkbox, *Max picchi per passo* (3) e *Max per scansione* (20) limitano quante analisi si fanno per non rallentare troppo la ricerca; di default sono spenti, cioè nessun limite.
- **Archivio spike**: ogni analisi, automatica o manuale, viene salvata con misure, cascata e audio nella cartella `spettro_archivio` della configurazione di Home Assistant, e cancellata quando diventa più vecchia di *Archivio: cancella dopo (h)* ore (predefinito 24). La scheda mostra quante analisi ci sono e quanto spazio occupano su disco, e ha il pulsante **Svuota archivio**. Toccando sul grafico un picco già analizzato compare subito la sua analisi. Nella scheda *Archivio spike* le analisi si ordinano per ora, frequenza o SNR toccando l'intestazione della colonna (un secondo tocco inverte l'ordine); toccando una riga si ingrandisce il segnale sul grafico e si riapre l'analisi (✋ = avviata a mano).
- **Chiedi all'AI**: invia misure e cascata all'entità **AI Task** di Home Assistant (Anthropic, OpenAI, Google Gemini, Ollama…) per un'ipotesi su che segnale è e, se è pubblico e in chiaro, con quale programma decodificarlo. Serve Home Assistant 2025.8 o successivo e un utente amministratore; se il modello non accetta immagini riceve solo le misure.
- Durante le scansioni lunghe il grafico si aggiorna man mano, con la barra di avanzamento.
- Sensori per automazioni: **Picco principale** (MHz, con la lista dei picchi negli attributi), **Rumore di fondo** (dBFS) e **Segnali rilevati**.
- Interruttore **Scansione** (`switch.spettro_sdr_scansione`, anche nel pannello) per mettere in pausa la scansione a mano o da un'automazione.
- Opzione **Scansiona solo con il pannello aperto**: con il pannello chiuso l'integrazione non si collega al server, utile se sullo stesso computer girano altri servizi (Pi-hole, DNS…). In questa modalità i sensori si aggiornano solo mentre il pannello è aperto.

## Come si legge il pannello

Esempio reale: scansione da 600 a 1500 MHz, poi ingrandita sulla banda GSM 900.

<p align="center">
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/complete/spettro.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/spettro.jpg" width="260" alt="Grafico dello spettro"></a>
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/complete/analisi.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/analisi.jpg" width="260" alt="Analisi di un segnale"></a>
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/complete/impostazioni-segnali.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/impostazioni-segnali.jpg" width="260" alt="Impostazioni e segnali rilevati"></a>
</p>

<sub>Tocca un'immagine per vederla intera, a piena risoluzione (schermate originali dal telefono in <code>docs/images/screenshots/complete/</code>).</sub>

**1. Spettro** (prima schermata)

- In alto lo **stato**: avanzamento della scansione in corso (37%), tuner della chiavetta, durata e ora dell'ultima scansione completa. La barra azzurra è l'avanzamento.
- Il grafico mostra il livello in **dBFS** per ogni frequenza. Qui è ingrandito su 930–950 MHz (trascinando il dito sul grafico): ogni picco stretto è la portante di una **cella GSM** (larga 200 kHz), il blocco piatto a destra largo vari MHz è una portante **LTE/UMTS**.
- I **triangoli rossi** sono i segnali rilevati, con la frequenza scritta sopra ai più forti; la **linea rossa tratteggiata** è la soglia di rilevamento (rumore di fondo + *Soglia picchi*).
- La **linea verde tratteggiata** è la frequenza scelta per l'analisi (qui 944,80 MHz): si sceglie toccando un picco.
- **Max hold** aggiunge la traccia arancione con il massimo raggiunto in ogni punto (*Azzera max* la riparte); **Scansiona zona** usa la zona ingrandita come nuovo intervallo; **Zoom 100%** torna all'intervallo completo.
- Sotto, la **cascata**: una riga per scansione completa, la più recente in alto (*ora*); i colori vanno dal blu (debole) al giallo e rosso (forte).

**2. Analisi segnale** (seconda schermata)

- *Selezionato* è la frequenza scelta; **Analizza** sospende la scansione, registra e la fa ripartire. Quanto registrare (1, 2 o 5 s) si sceglie in *Avanzate → Durata analisi*, e vale anche per l'analisi automatica dei picchi.
- L'immagine è la **cascata ad alta risoluzione** attorno al segnale: frequenza in orizzontale (estremi scritti sotto), tempo che scorre verso il basso. Qui si vede una banda piatta e sempre accesa, larga circa 250 kHz: la "firma" di una portante GSM.
- **Tipo probabile** è l'ipotesi ricavata dalle misure (qui *portante GSM, telefonia digitale cifrata*): non serve l'AI.
- **Frequenza centrale** e **Picco**: dove sta il segnale e quanto è forte; **Sopra il rumore**: di quanto spicca (SNR).
- **Banda occupata 99%**: larghezza che contiene quasi tutta la potenza (la voce analogica sta sotto i 25 kHz, i segnali digitali vanno da decine di kHz a vari MHz); **Banda a −3 dB**: larghezza della sola parte più forte.
- **Nel tempo**: trasmissione continua oppure percentuale di attività, numero, durata e ripetizione dei burst.
- Sotto, la banda di appartenenza dalla tabella delle frequenze italiane e il **lettore audio** con la registrazione demodulata (FM larga, FM stretta o AM); per i segnali digitali come questo si sente solo rumore.
- Con un'entità **AI Task** configurata compare anche **Chiedi all'AI**.

**3. Impostazioni e segnali rilevati** (terza schermata)

- **Da / A** è l'intervallo scansionato (qui 600–1500 MHz, circa 80 s a 2 MS/s), **Ripeti ogni** la pausa tra due scansioni, **Guadagno** fisso in dB (vuoto = automatico), **Soglia picchi** quanti dB sopra il rumore deve stare un segnale per essere elencato.
- **Applica** salva e riparte; **Scansiona ora** non aspetta la pausa.
- **Segnali rilevati**: frequenza, livello e dB sopra il rumore, dal più forte. Qui in testa ci sono le celle GSM 900 (933–956 MHz) e poi le portanti LTE 800 (760–821 MHz). Dalla versione 1.4.5 questa tabella è sostituita dalla scheda **Archivio spike**, che elenca i segnali analizzati con le loro analisi.

**4. RDS di una radio FM** (finestra di Spettro, analisi automatica di 5 s su 99,6 MHz)

<p align="center">
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/rds.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/rds.jpg" width="800" alt="Analisi di una radio FM con i dati RDS decodificati"></a>
</p>

- Oltre all'audio, le radio FM trasmettono dati digitali **RDS** su una sottoportante a 57 kHz (1187,5 bit/s): l'analisi li decodifica da sola, senza AI né programmi esterni. Qui la stazione si presenta come **M DUE O** e il testo trasmesso (RadioText) è il nome del conduttore, *Davide Rizzi*.
- Il nome ricevuto finisce nel **Tipo probabile** (*radio broadcast «M DUE O»*), quindi anche nell'Archivio spike.
- **Programma** è il tipo dichiarato dalla stazione (qui *varietà*), **Codice PI** l'identificativo unico della stazione (in Italia inizia con 5), **Altre frequenze** quelle su cui la stessa radio trasmette in altre zone, **Stereo** indica il pilota a 19 kHz e **Traffico** se la radio dà informazioni sul traffico. Se la stazione trasmette l'ora, compare anche **Ora trasmessa**.
- *57 gruppi RDS ricevuti* in 5 s: nome e testo vengono confermati sulle ripetizioni, così un errore isolato non li sporca; con segnale debole i caratteri mancanti compaiono come «·».
- A destra l'**Archivio spike** con le analisi automatiche fatte scansionando la banda FM: ora, frequenza, SNR e, sotto, il tipo rilevato (le righe *nessun segnale chiaro sopra il rumore* sono picchi troppo deboli per essere misurati).
- Nella cascata si vede l'audio: la larghezza del segnale cambia con il volume della musica e della voce, e a circa un terzo dall'alto una breve pausa lo stringe quasi alla sola portante. Con il lettore sotto l'immagine si ascoltano i 5 s registrati.

**5. Multiplex DAB+** (finestra di Spettro, analisi automatica di 5 s sul canale 7D, 194,064 MHz)

<p align="center">
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/dab.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/dab.jpg" width="800" alt="Analisi di un multiplex DAB+ con l'elenco delle radio decodificato"></a>
</p>

- Un multiplex DAB+ è un **blocco piatto largo 1,5 MHz** (*Banda occupata 99%* 1517 kHz, trasmissione continua) fatto di 1536 portanti OFDM: nella cascata si vede come una fascia uniforme, con i bordi netti. Le strisce verticali più chiare e più scure sono le portanti attenuate dalle riflessioni del segnale (percorsi multipli), normali in ricezione da interno.
- Il canale viene riconosciuto (**7D**) e l'analisi si sintonizza al suo centro; poi decodifica il canale di servizio del multiplex: nome (**DAB Italia**), codice **EId** e lo **scarto di frequenza** della chiavetta (qui −0,1 kHz).
- **Servizi (20)**: le radio contenute nel multiplex con il tipo, **DAB+** (audio HE-AAC) o **DAB** (MP2), più i servizi di **dati** come *EPG-SPI* (la guida ai programmi). I nomi sono quelli trasmessi dalle stazioni, anche quelli con i simboli (*\* RDS \**).
- *612/612 blocchi informativi integri in 51 trame*: ogni trama dura 96 ms e porta 12 blocchi con controllo CRC; se il segnale è al limite il conteggio scende e il pannello lo segnala.
- Il nome del multiplex finisce anche nel **Tipo probabile** e quindi nell'Archivio spike. L'audio delle radio DAB+ non viene decodificato: il lettore sotto la cascata, su un segnale digitale, fa sentire solo rumore.

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

- Dopo un aggiornamento da HACS **riavvia Home Assistant**: finché non lo fai il pannello è già nuovo ma l'integrazione no, e il pannello lo segnala con un avviso rosso in alto.

- rtl_tcp accetta **un solo client alla volta**. L'integrazione si collega solo durante la scansione e poi si stacca, quindi tra una scansione e l'altra puoi usare SDR#, SDR++ o altri programmi. Se un altro client resta collegato, la scansione va in errore e riprova dopo almeno 10 secondi.
- Tempi indicativi a 2,048 MS/s: banda FM (20 MHz) circa 2 s, tutto 24–1766 MHz circa 2–3 minuti.
- Mentre è collegata, la chiavetta invia dati in continuo: a 2,048 MS/s sono circa 4 MB/s (33 Mbit/s) e un po' di carico CPU/USB sul server. Se il server ospita anche altri servizi e li rallenta, usa l'interruttore **Scansione**, l'opzione *solo con il pannello aperto* o un intervallo più lungo.
- Meglio collegare server e Home Assistant via cavo; con il Wi-Fi conviene abbassare il sample rate (Avanzate).
- I livelli sono in **dBFS** (relativi al fondo scala della chiavetta), non in dBm calibrati.
- Le stazioni FM sono larghe: a volte la stessa stazione compare con due picchi vicini.
- L'analisi **riconosce** i segnali, non li decifra: le comunicazioni cifrate restano tali e in Italia intercettare comunicazioni private altrui è vietato. Le decodifiche lecite (FM/RDS, ADS-B, AIS, APRS, sensori con rtl_433…) si fanno con i programmi dedicati che l'AI suggerisce.

## Rilasci

HACS mostra come versione l'ultima **release** GitHub. Per pubblicarne una basta aumentare `version` in `custom_components/spettro/manifest.json`, `VERSION` in `custom_components/spettro/const.py` e `PANEL_VERSION` in `custom_components/spettro/frontend/spettro-panel.js` (devono coincidere) e fare push su `main`: il workflow *Release* crea il tag `vX.Y.Z` e la release se non esistono già.

## Licenza

MIT
