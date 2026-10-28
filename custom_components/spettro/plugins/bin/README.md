# Programmi di appoggio (bin/)

Qui stanno i programmi esterni usati da alcuni plugin. Vengono consegnati nella cartella persistente
`/config/spettro_plugins/bin/` da soli, al primo avvio in cui compaiono (vedi il README dei plugin),
e resi eseguibili.

## rtl_433

Binario di [rtl_433](https://github.com/merbanan/rtl_433), usato dal plugin **Sensori ISM (rtl_433)**
per decodificare i dispositivi a 433/868/315 MHz.

- **Versione:** 24.10
- **Architettura:** Linux **x86-64**, **compilato staticamente** (gira anche dentro Home Assistant OS,
  che è basato su Alpine/musl, senza dipendere dalle librerie di sistema).
- **Compilazione:** dai sorgenti ufficiali, senza supporto SDR (serve solo l'input da file):
  `cmake -DENABLE_RTLSDR=OFF -DENABLE_SOAPYSDR=OFF -DENABLE_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXE_LINKER_FLAGS=-static`
- **sha256:** `0c5c25ad9df7657fd93629c8e9ec9999967d4535a923697ea5834ee255036b16`

### Attenzione: solo x86-64

Questo binario funziona **solo su CPU x86-64** (PC/NUC). Su Raspberry Pi o altri ARM **non parte**:
in quel caso cancellalo e metti al suo posto un `rtl_433` statico per la tua architettura
(il plugin cerca anche in `PATH` e in `/usr/bin`, `/usr/local/bin`).

### Licenza

rtl_433 è distribuito sotto **GNU GPL v2**. Il codice sorgente completo è disponibile su
<https://github.com/merbanan/rtl_433> (tag `24.10`). La ridistribuzione del binario è conforme alla
GPL v2 in quanto il sorgente corrispondente è pubblicamente reperibile allo stesso indirizzo.
