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
- **Analisi segnale**: tocca un picco sul grafico (o una riga della tabella), oppure scrivi la frequenza nel campo **MHz** (anche con la virgola, Invio per confermare; anche fuori dall'intervallo scansionato), e premi **Analizza** o **▶ Analisi continua**. La scansione si sospende per qualche secondo, la chiavetta registra il segnale a frequenza fissa e il pannello mostra una cascata ad alta risoluzione (~1 kHz, ~5 ms per riga) con le misure: frequenza centrale, banda occupata, SNR, toni distinti, trasmissione continua o a burst (durata e ripetizione), frequenza che sale/scende (chirp) o due toni (FSK), più un'ipotesi sul **tipo di segnale** ricavata dalle misure. L'ultima analisi è anche un'entità immagine (**Ultima analisi**). Le analisi fatte a mano salvano anche la **registrazione grezza** (IQ): con **⬇ WAV IQ** o **⬇ cu8** la scarichi per aprirla con altri programmi (SDR#, SDR++, SDRangel, SatDump, Universal Radio Hacker; il nome del file contiene la frequenza centrale) o per mandarla a qualcuno. Sono circa 4 MB al secondo di registrazione; per le analisi automatiche c'è l'opzione nelle *Avanzate*. Il **tipo probabile** distingue anche i segnali digitali a banda stretta dalla fonia, dalla forma dello spettro e dai livelli di frequenza: TETRA (380–400 e 410–430 MHz), DMR/dPMR/NXDN/P25 (4FSK), dati FSK a due toni.
- **Riconosci e decodifica dati** (ascolto continuo in FM stretta): ogni 1,5 s misura il segnale in ascolto (modulazione 2FSK/4FSK, livelli, deviazione, velocità dei simboli) e dice con quali protocolli è compatibile (per esempio 18000 simboli/s → TETRA, 4FSK a 4800 → DMR/P25/NXDN, 2FSK a 1200 → POCSAG), poi prova i decoder: packet/APRS, POCSAG, DMR/P25/dPMR, AIS. Le trame decodificate (APRS, AIS) restano in un registro sotto la cascata. Le stesse misure compaiono nelle analisi dei segnali dati che non si riescono a decodificare.
- **Protocollo verificato**: nelle analisi (anche automatiche) Spettro non si ferma all'ipotesi dalle misure ma prova a **decodificare** davvero la registrazione, e se ci riesce il tipo diventa *verificato* (✓ nell'archivio) con i dati letti:
  - **APRS / packet radio** (AX.25 a 1200 baud, es. 144,800 MHz): mittente, percorso e messaggio, con la distanza se c'è la posizione;
  - **ADS-B** (1090 MHz): aerei con nominativo del volo, quota, velocità, posizione e distanza;
  - **AIS** (161,975 e 162,025 MHz): navi con MMSI, nome e posizione;
  - **DMR, P25 e dPMR**: radio digitali confermate dalle parole di sincronismo (stazione base o terminale, voce o dati);
  - **POCSAG**: cercapersone a 512, 1200 o 2400 baud;
  - in onde corte, **telescrivente RTTY** (45, 50, 75 baud; spostamento 85, 170, 450, 850 Hz) e **Morse**, con il testo letto; **FT8, FT4 e WSPR** si riconoscono dalla frequenza (per leggerli serve l'ascolto continuo).

  Ogni conferma passa da un controllo forte (CRC delle trame, codice BCH, parole di sincronismo, pochi errori di trama), quindi voce e rumore non vengono scambiati per dati. Delle comunicazioni private (radio digitali, cercapersone) si conferma solo il tipo, senza mostrare il contenuto. Con 2 s di registrazione si leggono pochi caratteri di RTTY o Morse: per il testo conviene alzare la *Durata analisi* a 4–5 s.
- **RDS delle radio FM**: per le stazioni FM broadcast l'analisi decodifica anche i dati digitali RDS trasmessi sulla sottoportante a 57 kHz: nome della stazione (PS), testo (RadioText, di solito titolo e artista), tipo di programma, codice PI, frequenze alternative, ora trasmessa, informazioni sul traffico e se la stazione è stereo (pilota a 19 kHz). Il nome compare anche nel tipo rilevato e nell'archivio. Il nome arriva già con 1 s di registrazione, il testo completo di solito con 2–5 s (*Durata analisi*); con segnale debole i caratteri non ricevuti sono indicati con «·».
- **DAB / DAB+ (radio digitale)**: i multiplex della banda III (174–240 MHz, canali 5A…13F, in Italia soprattutto il 12) compaiono come blocchi piatti larghi 1,5 MHz e l'analisi li riconosce con il loro canale. Sintonizzandosi al centro del canale decodifica il canale di servizio (FIC: sincronismo sulle trame da 96 ms, demodulazione OFDM/DQPSK, Viterbi, CRC) e mostra **nome del multiplex**, codice EId e l'**elenco delle radio** con il tipo (DAB o DAB+); il nome finisce anche nel tipo rilevato e nell'archivio. Serve il sample rate di 2,048 MS/s (quello predefinito). Per ogni radio si vede anche la velocità (kbit/s).
- **TV digitale (DVB-T/T2)**: riconosce i multiplex TV dei canali UHF 21–48 dalla struttura dei simboli OFDM e ne misura standard, modalità, intervallo di guardia e qualità (MER stimato), con una scheda dei canali trovati e un misuratore continuo per puntare l'antenna. Vedi [Misuratore di campo TV](#misuratore-di-campo-tv).
- **TV satellitare**: con la chiavetta sul cavo della parabola riconosce il satellite (Hotbird, Astra…) dalla posizione dei transponder e misura il livello per puntare la parabola. Vedi [Satellite](#satellite-parabola-con-lnb).
- **Satelliti meteo**: calcola i passaggi dei Meteor-M, li riceve da solo e ricostruisce le foto della Terra (LRPT), con anteprima in diretta e archivio. Vedi [Satelliti meteo](#satelliti-meteo-meteor-m).
- **Aerei (ADS-B)**: gli aerei sui 1090 MHz, con radar e distanza da casa. Vedi [Aerei](#aerei-ads-b-1090-mhz).
- **FT8 e WSPR**: i messaggi digitali dei radioamatori di tutto il mondo, con la distanza di chi trasmette. Vedi [Modi digitali](#modi-digitali-ft8-e-wspr).
- **SSTV e radiofax**: immagini dalla ISS e dai radioamatori (SSTV) e carte del tempo in onde corte (radiofax DWD), con ascolto continuo e galleria. Onde corte con il direct sampling della chiavetta. Vedi [Immagini dalla radio](#immagini-dalla-radio-sstv-e-radiofax).
- **Preferiti**: con il cuoricino ♡ accanto ad *Analizza* la frequenza selezionata (dal grafico o dall'archivio) entra nei preferiti e il cuore diventa ❤. La scheda *Preferiti* li elenca con il nome: quello RDS per le radio FM, altrimenti *NONAME 1*, *NONAME 2*…; con ✏ lo cambi a mano (un nome scritto da te non viene mai sostituito dall'RDS; lasciandolo vuoto torna automatico). Toccando la riga si seleziona la frequenza, ▶ avvia l'analisi continua, ❤ la toglie. I preferiti sono salvati in Home Assistant, quindi uguali su telefono e computer.
- **Analisi continua (in diretta)**: dal pulsante *▶ Analisi continua* o dai preferiti la chiavetta resta sintonizzata fissa sulla frequenza (la scansione si sospende e alla fine riparte, solo se era attiva) e il pannello mostra in diretta la cascata che scorre, il livello del segnale e l'**audio** (FM larga, FM stretta o AM, con *Ascolta*/*Muto*). Per le radio FM l'**RDS si aggiorna** ogni secondo, con lo storico dei testi ricevuti (titoli, conduttori…). In diretta si usa 1,024 MS/s (circa 2 MB/s in rete); l'elaborazione avviene sul computer di Home Assistant. Se tutti i pannelli vengono chiusi l'analisi continua si ferma da sola dopo 30 s, per liberare la chiavetta.
- **Ascolto**: la registrazione viene demodulata in FM larga (radio), FM stretta (voce) e AM (in onde corte anche USB, LSB e CW); il pannello propone la più adatta in un lettore audio e si può passare alle altre.
- **Modi e filtri in diretta, come nei ricevitori web**: durante l'analisi continua si sceglie la demodulazione (**FM larga, FM stretta, AM, USB, LSB, CW**) e la **larghezza del filtro** (FM stretta 6,25–25 kHz, AM 3–15 kHz, USB/LSB 1,8–3,5 kHz, CW 250 Hz–1 kHz): un filtro più stretto toglie rumore e stazioni vicine. In CW la portante si sente come un tono a 700 Hz. Vedi [Testo dalla radio](#testo-dalla-radio-morse-e-rtty).
- **Analisi automatica dei picchi** (opzione *Analizza subito i picchi trovati*): con le stesse impostazioni della scansione, quando un passo trova un picco sopra la soglia la ricerca si interrompe, il picco viene registrato e analizzato (per la *Durata analisi* delle Avanzate, la stessa del pulsante Analizza; ogni secondo in più allunga la scansione per ogni picco nuovo), poi la scansione riprende da dove era rimasta. Nell'archivio, accanto all'SNR dell'analisi, c'è in piccolo quanto il picco stava sopra il rumore nella scansione: i due valori di solito sono vicini, ma per un segnale a impulsi l'analisi può trovarlo più debole (o non trovarlo se in quel momento taceva). Una frequenza già analizzata non viene rianalizzata prima di *Rianalizza dopo (min)* (predefinito 30, 0 = ogni volta). Facoltativi, con il loro checkbox, *Max picchi per passo* (3) e *Max per scansione* (20) limitano quante analisi si fanno per non rallentare troppo la ricerca; di default sono spenti, cioè nessun limite.
- **Archivio spike**: ogni analisi, automatica o manuale, viene salvata con misure, cascata e audio nella cartella `spettro_archivio` della configurazione di Home Assistant, e cancellata quando diventa più vecchia di *Archivio: cancella dopo (h)* ore (predefinito 24). La scheda mostra quante analisi ci sono e quanto spazio occupano su disco, e ha il pulsante **Svuota archivio**. Toccando sul grafico un picco già analizzato compare subito la sua analisi. Nella scheda *Archivio spike* le analisi si ordinano per ora, frequenza o SNR toccando l'intestazione della colonna (un secondo tocco inverte l'ordine); il menu sopra la lista mostra un solo tipo di segnale (DAB / DAB+, TV digitale, Radio FM, Voce e analogico, Cellulare, Dati digitali…) con quante analisi ce ne sono. Toccando una riga si ingrandisce il segnale sul grafico e si riapre l'analisi (✋ = avviata a mano); la lista scorre per conto suo, così dopo aver aperto un'analisi si resta al punto in cui si era.
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
- I **triangoli rossi** sono i segnali rilevati, con la frequenza scritta sopra ai più forti; la **linea rossa tratteggiata** è la soglia di rilevamento: il rumore di fondo **della zona** (stimato su circa 16 MHz attorno, perché il rumore cambia molto lungo la banda) più la *Soglia picchi*. Un segnale largo (FM, DAB, TV) conta come un picco solo.
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
- **Applica** salva e riparte; **Scansiona ora** non aspetta la pausa e, se la scansione è spenta, fa un giro solo e poi torna in pausa.
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
- Il nome del multiplex finisce anche nel **Tipo probabile** e quindi nell'Archivio spike. Le radio DAB+ si ascoltano con ▶ (vedi [Ricevitore DAB+](#ricevitore-dab)); il lettore sotto la cascata demodula in analogico e su un segnale digitale fa sentire solo rumore.

## Ricevitore DAB+

Spettro fa anche da **ricevitore di radio digitale DAB+**, tutto dentro Home Assistant e senza programmi esterni: l'integrazione demodula il multiplex in Python e il pannello suona l'audio con il decoder AAC del browser.

**Come si ascolta una radio**

1. Scansiona la banda III (174–240 MHz; in Italia i multiplex nazionali stanno soprattutto nel canale 12 e in alcuni canali 5–10, come DAB Italia sul 7D).
2. Tocca il blocco piatto largo 1,5 MHz di un multiplex e premi **Analizza** (o aprilo dall'Archivio spike): compare l'elenco delle radio con tipo e velocità (kbit/s).
3. Premi **▶** accanto a una radio **DAB+**: parte l'analisi continua sul multiplex (la scansione si sospende) e dopo 2–3 secondi si sente l'audio. **🔇 Ascolta / 🔊 Muto** lo accende e lo spegne, **■ Ferma** torna alla scansione.
4. Con il **cuore ♡** la radio va nei **Preferiti** con il suo nome e il canale (*DAB+ 7D*): da lì ▶ la fa ripartire con un tocco.

**Cosa mostra la diretta**

- **Slideshow**: le immagini che la stazione trasmette con l'audio (copertine, loghi, programmi), aggiornate man mano.
- **Testo (DLS)**: il testo scorrevole della radio (titolo, artista, conduttore) e lo storico dei testi ricevuti.
- **Radio, multiplex e velocità** (kbit/s e protezione del sottocanale).
- **Pacchetti audio integri** e **correzioni Reed-Solomon**: la qualità di ricezione (vicino al 100% = audio pulito).
- **Carico su Home Assistant**: quanto calcolo serve rispetto al tempo reale (oltre il 90% l'audio va a scatti) e la **riserva audio**, che cresce da sola fino a 4 s se la rete o il computer rallentano.

**Come funziona**

La chiavetta resta sintonizzata al centro del canale a 2,048 MS/s (circa 4 MB/s in rete dal computer con rtl_tcp a quello di Home Assistant). L'integrazione segue le trame da 96 ms, demodula solo i simboli OFDM della radio scelta, ricostruisce il suo sottocanale (de-interleaving temporale, decodifica di Viterbi, Reed-Solomon, superframe DAB+) e manda al pannello i pacchetti audio HE-AAC, il testo DLS e le immagini della slideshow (MOT). Il browser decodifica l'audio: con Home Assistant in **https** usa WebCodecs, in **http** (es. `http://192.168.x.x:8123`) blocchi di mezzo secondo con Web Audio. Se il decoder del browser non applica l'SBR del DAB+ (blocchi da 960 campioni) l'audio si sente comunque alla velocità giusta, a qualità ridotta, e il pannello lo segnala.

**Limiti**: una radio alla volta; le radio DAB classiche (MP2) non si ascoltano; su iPhone/iPad l'audio dipende dal supporto del browser.

### Ricerca di tutti i multiplex

Nello spettro i multiplex vicini (per esempio 12A, 12B, 12C e 12D) si fondono in un blocco unico e la scansione può non vederli tutti come picchi. Nella scheda **📻 Radio DAB+** il pulsante **🔍 Cerca tutti i multiplex DAB** prova uno per uno i 38 canali della banda III (5A–13F, circa un minuto, scansione sospesa): per ogni multiplex trovato mostra nome, qualità, livello e l'elenco delle radio, con ▶ per ascoltarle. Il risultato resta salvato (file `spettro_dab.json` nella configurazione).

## Misuratore di campo TV

La TV digitale terrestre (DVB-T/DVB-T2) non si può guardare con la chiavetta in modalità rtl_tcp: un multiplex è largo 7–8 MHz e la chiavetta ne riceve circa 2, e in ogni caso i 20–40 Mbit/s di un multiplex non si decodificano in Python. Spettro però lo **misura**, e diventa un piccolo misuratore di campo per puntare e controllare l'antenna, sempre senza programmi esterni.

- **Canali TV**: con una scansione della banda UHF (es. 470–694 MHz, canali 21–48) compare la scheda *Canali TV* con i multiplex trovati e quanto stanno sopra il rumore.
- **Analisi**: anche se lo spettro di un multiplex è piatto come il rumore, Spettro lo riconosce dalla struttura dei simboli OFDM, che ripetono in testa la propria parte finale (intervallo di guardia). Ne ricava lo **standard** (DVB-T o DVB-T2), la **modalità** (2k…32k), l'**intervallo di guardia** e una **stima del MER**, la qualità del segnale che decide se l'immagine si vede senza blocchi. Indicativamente servono ~18 dB per il DVB-T e ~17 dB per il DVB-T2.
- **▶ Misura continua**: la chiavetta resta sul canale e aggiorna più volte al secondo **qualità** e **livello**, con le barre e il grafico degli ultimi due minuti. Si muove l'antenna lentamente e ci si ferma dove la qualità è più alta. Un livello vicino a 0 dBFS vuol dire chiavetta saturata (abbassa il guadagno).

**Esempio** (finestra di Spettro, misura continua sul canale UHF 42, 642 MHz)

<p align="center">
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/tv.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/tv.jpg" width="800" alt="Misuratore di campo su un multiplex DVB-T del canale UHF 42"></a>
</p>

- **TV UHF 42 · 642,0 MHz**: la chiavetta è al centro del canale e la cascata mostra i 2 MHz centrali del multiplex, una fascia uniforme di portanti OFDM. Le strisce verticali più chiare e più scure sono portanti attenuate dalle riflessioni (percorsi multipli), che si spostano lentamente nel tempo.
- Lo standard è riconosciuto come **DVB-T** in modalità **8k** con intervallo di guardia **1/4**: simboli da 1,120 ms con portanti ogni 1116 Hz, la configurazione tipica dei multiplex DVB-T italiani.
- **Qualità (MER stimato) 18,8 dB, discreta**: appena sopra la tacca dei ~18 dB che servono per vedere senza blocchi. Il grafico sotto mostra la qualità degli ultimi due minuti, tra circa 20 e 25 dB con qualche calo: è la traccia da seguire mentre si orienta l'antenna.
- **Livello in ingresso −20,8 dBFS**: lontano dalla saturazione (0 dBFS), quindi il guadagno va bene.

I nomi dei canali (Rai 1, Canale 5…) non si vedono: viaggiano dentro il flusso dati, che senza l'intero multiplex non si decodifica. Con una guardia corta (es. DVB-T2 32k 1/128) la stima del MER resta per difetto e il pannello la indica con «≥».

## Satellite (parabola con LNB)

Collegando la chiavetta al cavo della parabola, Spettro diventa un **sat finder** che riconosce il satellite: utile per puntare la parabola su **Hotbird 13°E**, **Astra 19,2°E** o gli altri satelliti visibili dall'Italia (Eutelsat 5°W, 7°E, 9°E, 16°E, Astra 4,8°E, 23,5°E, 28,2°E).

**Collegamento**

- La parabola riceve 10,7–12,75 GHz; l'LNB li abbassa sul cavo a **950–2150 MHz** e la chiavetta arriva a circa **1766 MHz**. Si vedono quindi quasi tutta la *banda bassa* (10,70–11,52 GHz) e una parte della *banda alta* (11,70–12,37 GHz, con il tono a 22 kHz).
- L'LNB va **alimentato** a 13 V (polarizzazione verticale) o 18 V (orizzontale): da un decoder con uno splitter satellitare, oppure con un alimentatore da linea (*power inserter*).
- ⚠ **La tensione dell'LNB non deve arrivare alla chiavetta**, che si brucerebbe: usa uno splitter con il passaggio di corrente solo dal lato del decoder, oppure un condensatore di blocco (*DC block*). Il cavo ha l'attacco F e la chiavetta di solito SMA o MCX: serve un adattatore.

**Uso**

1. Nella scheda *Scansione* scegli il preset **Satellite con LNB (950–1760)**: si spunta anche **LNB satellitare sul cavo**. Poi premi *Applica*.
2. I transponder (larghi 27–36 MHz) compaiono nello spettro come **gobbe piatte**. A fine scansione la scheda **Satellite** dice quale satellite è, con la **banda** e la **polarizzazione** in uso e di quanto è spostato l'oscillatore dell'LNB. Se non è sicura, indica anche le alternative.
3. Sotto c'è l'elenco dei transponder di quel satellite nella banda vista, con la frequenza vera (GHz), quella sul cavo, il symbol rate e quanto stanno sopra il rumore.
4. Con **▶** su un transponder parte la **misura continua del livello**, con il massimo raggiunto e il grafico degli ultimi due minuti: si sposta la parabola di pochi millimetri alla volta e ci si ferma dove il livello è più alto.

**Come funziona**: dentro un transponder non si legge nulla (la chiavetta ne riceve 2 MHz su 30, e il DVB-S non ha l'intervallo di guardia usato per la TV terrestre), ma ogni satellite ha il suo **piano di frequenze**. Spettro confronta le gobbe della scansione con i transponder di ciascun satellite, per entrambe le bande e polarizzazioni e con l'oscillatore dell'LNB spostato fino a ±8 MHz, e sceglie quello che spiega meglio dove cadono le gobbe. I piani vengono dal file `satellites.xml` dei decoder Enigma2 (ottobre 2026): se un satellite cambia i transponder il riconoscimento può peggiorare. Programmi, nomi dei canali e qualità del segnale (MER) non si possono misurare.

## Satelliti meteo (Meteor-M)

Spettro riceve le **foto della Terra** trasmesse dai satelliti meteo russi **Meteor-M2 3** e **Meteor-M2 4**, che passano sopra l'Italia 2–4 volte al giorno ciascuno. Tutto avviene dentro l'integrazione, in Python, senza programmi esterni: calcolo delle orbite, ricezione e decodifica dell'immagine.

**Antenna**: serve un'antenna per i 137 MHz **all'aperto**, con il cielo libero il più possibile. Va bene un **V-dipolo**, due bracci da ~53 cm aperti a 120° in orizzontale, o una QFH. L'antenna della TV o un'antennina da interno non bastano.

**Uso**

1. Nella scheda **🛰️ Satelliti meteo e immagini**, sezione *Satelliti in passaggio*, compaiono i **prossimi passaggi** di Meteor e ISS sopra la tua posizione (presa da Home Assistant): ora, satellite, elevazione massima, direzione e durata.
2. Con **Meteor: ricevi da solo le foto**, quando un Meteor passa sopra l'**elevazione minima** (predefinita 20°) la scansione si sospende e la chiavetta lo riceve per tutto il passaggio. **▶ Ricevi**, accanto a un passaggio in corso, lo riceve subito.
3. Durante la ricezione si vedono:
   - elevazione del satellite, Doppler e aggancio del segnale;
   - le trame valide;
   - l'**immagine che cresce** riga per riga.

   **■ Ferma e salva** interrompe la ricezione e tiene quanto ricevuto fin lì.
4. A fine passaggio l'immagine va nell'elenco **Immagini ricevute**, nella cartella `spettro_meteo` e nell'entità **Ultima immagine meteo**. Da lì si può usare in dashboard o mandare in una notifica.
   - Di giorno l'immagine è a **colori quasi naturali** (canali visibile e vicino infrarosso). Se il satellite trasmette il canale termico, c'è anche la versione **infrarosso**, utile di notte.
   - Il nord è sempre in alto. Le immagini più vecchie di *Conserva (giorni)* vengono cancellate.

**Come funziona**

- **Orbite**: gli elementi orbitali (TLE) si scaricano da CelesTrak due volte al giorno e restano salvati se manca la rete. Il modello SGP4 è scritto nell'integrazione: calcola i passaggi e il **Doppler**, fino a ±3,5 kHz, che viene corretto mentre il satellite passa.
- **Ricezione**: la chiavetta sta a 1,024 MS/s vicino a 137,1 o 137,9 MHz. Se dopo una ventina di secondi non arrivano dati, prova l'altra frequenza, perché ogni tanto i satelliti le scambiano.
- **Demodulazione**: segnale **OQPSK a 72 ksym/s**. Portante, fase e tempo dei simboli si ricavano a blocchi dal segnale elevato al quadrato, che ha due righe a ±72 kHz.
- **Decodifica**:
  - Viterbi (codice convoluzionale 1/2, K=7);
  - decodifica differenziale;
  - sincronismo delle trame;
  - Reed-Solomon (255,223) ×4;
  - pacchetti CCSDS;
  - blocchi JPEG 8×8 dello strumento MSU-MR, ricomposti in righe da 1568 pixel (circa 1 km per pixel).
- Sui segnali di prova la ricezione funziona fino a un rapporto segnale/rumore di circa 2 dB e richiede circa metà del tempo reale di calcolo.

I satelliti geostazionari **Meteosat** non si possono ricevere con la chiavetta: trasmettono a 3,75 Msym/s, oltre la sua banda, e servirebbe una parabola da 1,5–2 m.

## Immagini dalla radio: SSTV e radiofax

Oltre ai Meteor, Spettro decodifica le immagini trasmesse come suoni:

- **SSTV della Stazione spaziale (ISS)**: durante gli eventi ARISS, più volte l'anno, la ISS trasmette serie di immagini su **145,800 MHz in FM**. Con **ISS: ascolta da sola l'SSTV** Spettro si sintonizza a ogni passaggio, calcolato come per i Meteor, e salva le immagini. Va bene la stessa antenna dei 137 MHz.
- **SSTV dei radioamatori** in onde corte, per esempio 14,230 MHz (USB) o 7,171 e 3,730 MHz (LSB). Modi riconosciuti dal codice VIS: **PD50…PD290, Martin 1/2, Scottie 1/2/DX, Robot 36/72**. L'inclinazione dovuta all'orologio di chi trasmette viene corretta con gli impulsi di sincronismo.
- **Radiofax meteo**: carte del tempo trasmesse ogni giorno dal servizio meteorologico tedesco **DWD** (3855, 7880, 13882,5 kHz) e da **Northwood**, Regno Unito (4610, 8040 kHz). Spettro riconosce il tono di avvio, allinea le righe con la fasatura e chiude la carta al tono di stop: 120 righe al minuto, 1810 pixel per riga.

**Uso**: nella scheda **🛰️ Satelliti meteo e immagini**, sezione *Stazioni radio: immagini, testo e modi digitali*, scegli una stazione dall'elenco e premi **▶ Ascolta**. La chiavetta resta su quella frequenza: si sente l'audio, l'immagine si disegna mentre arriva e a fine immagine viene salvata in **Immagini ricevute** (e nell'entità *Ultima immagine meteo*). L'ascolto continua anche a pannello chiuso, finché non premi **■ Ferma**. Durante qualsiasi analisi continua si può anche scegliere a mano la decodifica, *Immagini SSTV* o *Radiofax meteo*, accanto al modo (FM stretta, AM, **USB**, **LSB**). Per il Morse e la telescrivente vedi [Testo dalla radio](#testo-dalla-radio-morse-e-rtty).

## Testo dalla radio: Morse e RTTY

Durante l'analisi continua, accanto al modo, si può scegliere anche una decodifica in **testo**, che compare sotto la cascata mentre arriva (con **📋 Copia** e *Pulisci*):

- **Testo Morse (CW)**: si usa in modo **CW** (o USB/LSB). Il tono e la velocità (5–60 parole al minuto) si trovano da soli e la velocità segue chi trasmette. Basta sintonizzare la portante; con il filtro CW a 500 o 250 Hz si legge meglio. Per provare: i **radiofari NCDXF** su 14,100 MHz (ogni 10 secondi una stazione diversa del mondo dice il suo nominativo) o i radioamatori su 7,025 e 14,040 MHz.
- **Testo RTTY 50 baud**: la telescrivente meteo del **DWD** (Pinneberg, 4583, 7646, 10100,8 kHz, spostamento 450 Hz) trasmette di continuo bollettini e osservazioni (gruppi SYNOP).
- **Testo RTTY 45 baud**: la telescrivente dei radioamatori (spostamento 170 Hz), per esempio intorno a 14,085 MHz, soprattutto durante i contest.

Per l'RTTY si ascolta in **USB**: i due toni e la polarità (normale o invertita) si trovano da soli, basta che cadano nel filtro. Le stazioni pronte sono nell'elenco *Stazioni radio: immagini, testo e modi digitali* della scheda **🛰️ Satelliti meteo e immagini**. A differenza delle immagini, il testo si decodifica solo con il pannello aperto.

## Aerei: ADS-B (1090 MHz)

<p align="center">
  <a href="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/complete/aerei.jpg"><img src="https://raw.githubusercontent.com/LabirintoDigitale/spettro-sdr/main/docs/images/screenshots/aerei.jpg" width="340" alt="Radar ADS-B con le scie degli aerei, foto, compagnia e rotta"></a>
</p>

Gli aerei trasmettono in chiaro la loro posizione sui 1090 MHz (ADS-B), e si ricevono anche con un'antennina corta (~6,9 cm). Nella scheda **✈️ Aerei (ADS-B)** il pulsante **▶ Avvia** tiene la chiavetta su 1090 MHz (scansione sospesa) e mostra gli aerei in vista aggiornati in tempo reale:

- un **radar** centrato su di te (nord in alto) con ogni aereo alla sua direzione e distanza, colorato per quota (basso rosso → alto blu) e con una freccia per la rotta;
- una **tabella** con volo, quota, velocità, distanza e direzione da casa.

Per ogni aereo, **senza internet**, si ricavano anche la **compagnia** (dalle prime 3 lettere della sigla del volo), il **numero del volo**, il **paese di immatricolazione** (dal codice ICAO dell'aereo), la **categoria** (leggero, pesante, elicottero…) ed eventuali **emergenze** dichiarate. Con l'opzione **Dettagli da internet** (spenta di default) si aggiungono **modello esatto, immatricolazione, operatore e rotta partenza → arrivo**, scaricati da adsbdb.com (API pubblica e gratuita, senza chiave; serve che Home Assistant possa uscire su internet). La rotta viene mostrata **nel verso giusto**: dato che il database conosce solo la coppia di aeroporti della sigla del volo, Spettro guarda verso quale dei due l'aereo sta volando davvero (dalla sua rotta e posizione) e, se è la tratta di ritorno, inverte partenza e arrivo. Nella riga, «vola verso» indica la direzione di volo (diversa da «Direz.», che è dove l'aereo si trova rispetto a te).

Sul radar ogni aereo lascia una **scia** che svanisce dietro di sé, così si vede da dove arriva e dove sta andando; con i dettagli online compare anche la **foto reale** dell'aereo accanto al volo. Un aereo sparisce dopo un minuto che non si sente. Se mentre il radar è attivo inizia un passaggio di un satellite meteo (Meteor automatico, o ISS), l'ADS-B **si sospende da solo** per lasciare la chiavetta al satellite e **riprende automaticamente** alla fine del passaggio.

**Storico dei voli.** Con la spunta **Registra storico** i voli intercettati (con la loro traccia) vengono salvati; si cancellano da soli dopo 24 ore. Nella sezione **Storico voli** li rivedi con compagnia, rotta, orario, distanza minima e numero di punti, puoi **mostrare sul radar** le tracce dei voli che selezioni (con **Mostra tutti sul radar** / **Nessuno**) — anche a ricevitore spento — e **🗑 Svuota storico** cancella tutto. Lo storico è salvato su disco (`spettro_adsb_history.json`); la traccia registra **ogni posizione ricevuta** (fedele come la scia in diretta). In pratica occupa pochi MB (un volo tipico qualche decina di KB); limiti: 24 ore, 800 voli, 3000 punti per traccia. La posizione viene calcolata da un solo messaggio usando la posizione della stazione (da Home Assistant). Con l'opzione **Resta attivo a pannello chiuso** la chiavetta resta su 1090 MHz finché non premi **■ Ferma**; altrimenti si ferma da sola poco dopo aver chiuso il pannello. Due **sensori** — *Aerei in vista* e *Aereo più vicino* (con volo, quota e distanza negli attributi) — si possono usare in dashboard e automazioni. Il decoder (preambolo a impulsi, 112 bit, CRC a 24 bit, posizione CPR) è lo stesso usato nelle analisi.

## Puntamento dell'antenna e satelliti UHF

Il pulsante **📡 Punta antenna** (accanto a *▶ Analisi continua*) tiene la chiavetta sulla frequenza scelta e mostra un **misuratore di livello** grande, con il picco più forte della banda, un indicatore ↑/↓ («più forte / più debole») e un trattino che **segna il massimo raggiunto** (azzerabile). Si gira lentamente l'antenna cercando di far salire il numero; per i satelliti geostazionari, trovato il picco, l'antenna resta ferma lì. Funziona per qualsiasi antenna: i 137 MHz dei meteo, un ponte ripetitore, una parabola, ecc.

Tra i preset di scansione c'è **UHF SatCom militari (243–270)**: sono vecchi satelliti militari in UHF con transponder «a specchio» non cifrati, su cui a volte si sentono comunicazioni non autorizzate (voce in FM stretta). Dall'Italia solo i satelliti sopra l'Atlantico/Africa sono raggiungibili, bassi verso sud, con un'antenna direttiva. ⚠️ Ricevere queste trasmissioni è un terreno legalmente delicato (comunicazioni non destinate al pubblico, su satelliti militari) e **trasmettere è reato**: trattalo come curiosità tecnica.

## Modi digitali: FT8 e WSPR

I radioamatori usano molto due modi digitali "deboli", che si ricevono anche con antenne modeste da tutto il mondo:

- **FT8**: ogni 15 secondi, allineati all'ora UTC, centinaia di stazioni si scambiano messaggi brevi (nominativi, locatore, rapporto di segnale). Spettro trova i segnali nella banda audio (100–3000 Hz), li sincronizza con i blocchi Costas e li decodifica (codice LDPC e CRC, come WSJT-X), anche quelli sovrapposti, togliendo man mano quelli già letti. Sulle registrazioni di prova di ft8_lib trova circa il 70% dei messaggi di WSJT-X (quasi tutti sopra −10 dB).
- **WSPR**: radiofari a bassissima potenza (spesso meno di 1 W): ogni 2 minuti pari ognuno trasmette nominativo, locatore e potenza. Si decodificano fino a circa −26 dB. Per ogni stazione si vede la distanza da casa (dal locatore e dalla posizione di Home Assistant).

**Uso**: dall'elenco *Stazioni radio: immagini, testo e modi digitali* scegli per esempio *FT8 20 m · 14,074 MHz* o *WSPR 40 m · 7,0386 MHz* e premi **▶ Ascolta**; oppure, in un'analisi continua in **USB**, scegli *Messaggi FT8* o *Radiofari WSPR*. I messaggi compaiono in una tabella sotto la cascata (con il filtro *solo chi chiama (CQ)*); l'ascolto continua anche a pannello chiuso e riaprendo il pannello si ritrovano gli ultimi 600 messaggi. Serve che l'**ora del computer di Home Assistant sia giusta**, al secondo (di solito lo è, sincronizzata da internet).

**Onde corte**: sotto i 24 MHz Spettro attiva da solo il **direct sampling** della chiavetta, che riceve senza passare dal sintonizzatore. È impostato sul ramo Q, quello delle Nooelec NESDR SMArt v5 e RTL-SDR Blog V3, e si cambia nelle impostazioni avanzate (*Onde corte*: Q, I o spento, per esempio con la RTL-SDR Blog V4, che le onde corte le ha già). Serve un **filo lungo 5–10 m** come antenna, meglio all'aperto: le antennine in dotazione non bastano. C'è anche il preset di scansione *Onde corte (0,5–30)*.

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

---

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/W4M829BGBC)
