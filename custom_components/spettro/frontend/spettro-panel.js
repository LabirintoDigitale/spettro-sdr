// Pannello "Spettro" per Home Assistant: grafico dello spettro, waterfall e picchi.

// deve coincidere con VERSION in const.py: se il backend ne riporta un'altra manca un riavvio
const PANEL_VERSION = "1.18.0";

const PRESETS = [
  ["Personalizzato", null, null],
  ["Radio FM (87.5–108)", 87.5, 108],
  ["Banda aerea (118–137)", 118, 137],
  ["Satelliti meteo (137–138)", 137, 138],
  ["Radioamatori 2 m (144–146)", 144, 146],
  ["Marina VHF (156–162)", 156, 162.1],
  ["Radioamatori 70 cm (430–440)", 430, 440],
  ["LPD / ISM 433 (433–435)", 433.05, 434.8],
  ["PMR446 (446.0–446.2)", 446.0, 446.2],
  ["ISM 868 (863–870)", 863, 870],
  ["GSM 900 (925–960)", 925, 960],
  ["ADS-B (1085–1095)", 1085, 1095],
  ["TV digitale UHF (470–694)", 470, 694],
  ["Satellite con LNB (950–1760)", 950, 1760],
  ["Onde corte (0,5–30)", 0.5, 30],
  ["Tutto (24–1766)", 24, 1766],
];
// immagini, testo e modi digitali: [nome, MHz di sintonia, modo, decoder, larghezza]. Il radiofax si riceve in USB 1,9 kHz sotto la
// frequenza assegnata, l'RTTY DWD 1,5 kHz sotto (i due toni cadono a 1275 e 1725 Hz); in CW si sintonizza la portante
const IMG_PRESETS = [
  ["ISS · SSTV 145,800 MHz (FM)", 145.8, "nfm", "sstv"],
  ["SSTV radioamatori 20 m · 14,230 MHz (USB)", 14.23, "usb", "sstv"],
  ["SSTV radioamatori 40 m · 7,171 MHz (LSB)", 7.171, "lsb", "sstv"],
  ["SSTV radioamatori 80 m · 3,730 MHz (LSB)", 3.73, "lsb", "sstv"],
  ["Radiofax meteo DWD · 3855 kHz", 3.8531, "usb", "fax"],
  ["Radiofax meteo DWD · 7880 kHz", 7.8781, "usb", "fax"],
  ["Radiofax meteo DWD · 13882,5 kHz", 13.8806, "usb", "fax"],
  ["Radiofax meteo Northwood (UK) · 4610 kHz", 4.6081, "usb", "fax"],
  ["Radiofax meteo Northwood (UK) · 8040 kHz", 8.0381, "usb", "fax"],
  ["Morse · radiofari NCDXF 14,100 MHz (CW)", 14.1, "cw", "cw"],
  ["Morse · radioamatori 40 m · 7,025 MHz (CW)", 7.025, "cw", "cw"],
  ["Morse · radioamatori 20 m · 14,040 MHz (CW)", 14.04, "cw", "cw"],
  ["Telescrivente meteo DWD (RTTY 50 baud) · 4583 kHz", 4.5815, "usb", "rtty50"],
  ["Telescrivente meteo DWD (RTTY 50 baud) · 7646 kHz", 7.6445, "usb", "rtty50"],
  ["Telescrivente meteo DWD (RTTY 50 baud) · 10100,8 kHz", 10.0993, "usb", "rtty50"],
  ["RTTY radioamatori 20 m · 14,085 MHz (45 baud)", 14.0835, "usb", "rtty45"],
  ...[["160", 1.84], ["80", 3.573], ["40", 7.074], ["30", 10.136], ["20", 14.074], ["17", 18.1], ["15", 21.074], ["12", 24.915], ["10", 28.074], ["6", 50.313]]
    .map(([b, f]) => [`FT8 ${b} m · ${f.toLocaleString("it-IT", { minimumFractionDigits: 3 })} MHz`, f, "usb", "ft8", 3500]),
  ...[["80", 3.5686], ["40", 7.0386], ["30", 10.1387], ["20", 14.0956], ["17", 18.1046], ["15", 21.0946], ["10", 28.1246], ["6", 50.293]]
    .map(([b, f]) => [`WSPR ${b} m · ${f.toLocaleString("it-IT", { minimumFractionDigits: 4 })} MHz`, f, "usb", "wspr", 3500]),
];
// larghezza del canale in diretta (Hz): predefinita e scelte, come in demod.py
const BANDWIDTHS = {
  nfm: [15000, [6250, 12500, 15000, 25000]],
  am: [10000, [3000, 6000, 10000, 15000]],
  usb: [2800, [1800, 2400, 2800, 3500]],
  lsb: [2800, [1800, 2400, 2800, 3500]],
  cw: [500, [250, 500, 1000]],
};
// decoder dell'audio in diretta: [valore, nome, modi in cui funziona]
const LIVE_DECODERS = [
  ["sstv", "Immagini SSTV", ["nfm", "am", "usb", "lsb"]],
  ["fax", "Radiofax meteo", ["nfm", "am", "usb", "lsb"]],
  ["cw", "Testo Morse (CW)", ["cw", "usb", "lsb"]],
  ["rtty45", "Testo RTTY 45 baud (radioamatori)", ["usb", "lsb", "cw"]],
  ["rtty50", "Testo RTTY 50 baud (meteo DWD)", ["usb", "lsb", "cw"]],
  ["ft8", "Messaggi FT8", ["usb"]],
  ["wspr", "Radiofari WSPR", ["usb"]],
];
const TEXT_DECODERS = ["cw", "rtty45", "rtty50"];
const SLOT_DECODERS = ["ft8", "wspr"]; // periodi UTC: continuano anche a pannello chiuso
const SAMPLE_RATES = [250000, 1024000, 1400000, 1800000, 2048000, 2400000, 2560000, 2880000];
const FFT_SIZES = [256, 512, 1024, 2048, 4096];
const MAX_HISTORY = 60;

// colormap del waterfall (blu notte → ciano → giallo → rosso)
const LUT = (() => {
  const stops = [[0, [8, 8, 32]], [0.3, [20, 50, 160]], [0.55, [30, 180, 200]], [0.78, [245, 210, 40]], [1, [255, 40, 0]]];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [t0, c0] = stops[k], [t1, c1] = stops[k + 1];
    const u = (t - t0) / (t1 - t0);
    for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * u;
  }
  return lut;
})();

function fmtMHz(hz, span) {
  const d = span < 2e5 ? 4 : span < 5e6 ? 3 : span < 5e7 ? 2 : 1;
  return (hz / 1e6).toFixed(d);
}

// --- MP4 frammentato per suonare l'AAC del DAB+ con Media Source (anche su pagine http) ---
function box(type, ...parts) {
  const len = 8 + parts.reduce((t, p) => t + p.length, 0);
  const out = new Uint8Array(len);
  new DataView(out.buffer).setUint32(0, len);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  let o = 8;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
function u8(...v) { return new Uint8Array(v); }
function u32(v) { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v >>> 0); return b; }
function u16(v) { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, v); return b; }
function zeros(n) { return new Uint8Array(n); }
const MATRIX = new Uint8Array([0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x40, 0, 0, 0]);

function mp4Init(asc, rate, channels) {
  const desc = (tag, body) => new Uint8Array([tag, body.length, ...body]);
  const dsi = desc(0x05, asc);
  const dcd = desc(0x04, [0x40, 0x15, 0, 0, 0, ...u32(0), ...u32(0), ...dsi]);
  const es = desc(0x03, [0, 1, 0, ...dcd, ...desc(0x06, [0x02])]);
  const esds = box("esds", zeros(4), es);
  const mp4a = box("mp4a", zeros(6), u16(1), zeros(8), u16(channels), u16(16), zeros(4), u32(rate * 65536), esds);
  const stbl = box("stbl", box("stsd", zeros(4), u32(1), mp4a), box("stts", zeros(8)), box("stsc", zeros(8)),
    box("stsz", zeros(12)), box("stco", zeros(8)));
  const minf = box("minf", box("smhd", zeros(8)), box("dinf", box("dref", zeros(4), u32(1), box("url ", u8(0, 0, 0, 1)))), stbl);
  const mdia = box("mdia", box("mdhd", zeros(12), u32(rate), zeros(4), u16(0x55c4), zeros(2)),
    box("hdlr", zeros(8), new TextEncoder().encode("soun"), zeros(12), new TextEncoder().encode("Spettro\0")), minf);
  const trak = box("trak", box("tkhd", u8(0, 0, 0, 7), zeros(8), u32(1), zeros(4), zeros(4), zeros(8), zeros(4), u16(0x0100), zeros(2), MATRIX, zeros(8)), mdia);
  const mvhd = box("mvhd", zeros(12), u32(1000), zeros(4), u32(0x00010000), u16(0x0100), zeros(10), MATRIX, zeros(24), u32(2));
  const mvex = box("mvex", box("trex", zeros(4), u32(1), u32(1), zeros(12)));
  return concatU8([box("ftyp", new TextEncoder().encode("iso6"), zeros(4), new TextEncoder().encode("iso6mp41")),
    box("moov", mvhd, trak, mvex)]);
}

function mp4Fragment(seq, baseTime, aus, duration) {
  const n = aus.length;
  const entries = new Uint8Array(8 * n);
  const dv = new DataView(entries.buffer);
  aus.forEach((a, i) => { dv.setUint32(8 * i, duration); dv.setUint32(8 * i + 4, a.length); });
  const tfdt = new Uint8Array(12);
  const tv = new DataView(tfdt.buffer);
  tv.setUint32(0, 0x01000000);
  tv.setUint32(4, Math.floor(baseTime / 2 ** 32));
  tv.setUint32(8, baseTime >>> 0);
  const build = (offset) => box("moof", box("mfhd", zeros(4), u32(seq)),
    box("traf", box("tfhd", u8(0, 2, 0, 0), u32(1)), box("tfdt", tfdt),
      box("trun", u8(0, 0, 3, 1), u32(n), u32(offset), entries)));
  const moof = build(build(0).length + 8);
  return concatU8([moof, box("mdat", concatU8(aus))]);
}

function concatU8(parts) {
  const out = new Uint8Array(parts.reduce((t, p) => t + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

// canali UHF della TV digitale in Italia (21–48, 8 MHz): centro in Hz
const TV_UHF = Array.from({ length: 28 }, (_, i) => [String(21 + i), (474 + 8 * i) * 1e6]);

// tipi di segnale per il filtro dell'archivio, riconosciuti dal testo del tipo rilevato
const ARCH_CATS = [
  ["dab", "DAB / DAB+"], ["tv", "TV digitale"], ["fm", "Radio FM"], ["voce", "Voce e analogico"],
  ["cell", "Cellulare (GSM)"], ["dati", "Dati digitali"], ["vuoto", "Nessun segnale chiaro"],
];
function archCat(kind) {
  const k = kind || "";
  if (/^nessun segnale/.test(k)) return "vuoto";
  if (/^segnale digitale a banda larga/.test(k)) return "dati"; // OFDM generico (LTE, DAB+ o TV)
  if (/^DAB/.test(k)) return "dab";
  if (/^TV digitale/.test(k)) return "tv";
  if (/^FM a banda larga/.test(k)) return "fm";
  if (/GSM/.test(k)) return "cell";
  if (/fonia|^AM|portante stabile|non classificato/.test(k)) return "voce";
  return "dati";
}

function tvRows(t) {
  return [
    ["Standard", t.standard === "DVB-T2" ? "DVB-T2 (HD/HEVC)" : "DVB-T"],
    ["Qualità (MER stimato)", `${t.mer_at_least ? "≥ " : ""}${t.mer_db} dB`],
    ["Modalità FFT", `${t.mode} (portanti ogni ${t.carrier_spacing_hz} Hz)`],
    ["Intervallo di guardia", `${t.guard} · simbolo ${(t.symbol_us / 1000).toFixed(3)} ms`],
    ["Larghezza canale", `${t.bandwidth_mhz} MHz`],
    ["Livello", `${t.level_db} dBFS`],
  ];
}

function esc(t) {
  return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function niceStep(span, target) {
  const raw = span / target;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * p) return m * p;
  return 10 * p;
}

class SpettroPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._spec = null; // spettro mostrato (eventualmente parziale)
    this._lastComplete = null;
    this._maxHold = null;
    this._history = [];
    this._status = null;
    this._view = null; // [lo, hi] in Hz quando si è fatto zoom
    this._hoverX = null;
    this._drag = null;
    this._formDirty = false;
    this._showMax = true;
    this._sel = null; // frequenza (Hz) scelta per l'analisi
    this._ana = null; // risultato dell'ultima analisi
    this._archive = []; // analisi archiviate (solo indice), dalla più recente
    this._archHours = null;
    this._archFilter = "all"; // tipo di segnale mostrato nell'archivio
    try { this._archFilter = localStorage.getItem("spettro.archFilter") || "all"; } catch (e) { /* non disponibile */ }
    this._archSort = { key: "timestamp", desc: true }; // colonna e verso dell'ordinamento dell'archivio
    this._archSel = null; // id dell'analisi dell'archivio mostrata
    this._favs = []; // frequenze preferite
    this._live = null; // analisi continua in corso: {freq_mhz, mode}
    this._listen = false; // audio della diretta attivo
    this._audioCtx = null;
    this._nextT = 0;
    this._rtHistory = []; // testi RDS ricevuti in diretta, dal più recente
    try {
      const saved = JSON.parse(localStorage.getItem("spettro.archSort"));
      if (saved && ["timestamp", "target_mhz", "snr_db"].includes(saved.key)) this._archSort = saved;
    } catch (e) { /* memoria del browser non disponibile */ }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._menu) this._menu.hass = hass;
    if (first) this._init();
  }
  get hass() { return this._hass; }

  set narrow(v) {
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
  }

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
  }

  disconnectedCallback() {
    this._unsubscribe();
    if (this._audioCtx) { this._audioCtx.close(); this._audioCtx = null; }
    clearInterval(this._tick);
    this._tick = null;
    if (this._ro) this._ro.disconnect();
  }

  _init() {
    this._render();
    this._subscribe();
  }

  async _subscribe() {
    if (this._unsub || this._subscribing) return;
    this._subscribing = true;
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (m) => this._onMessage(m), { type: "spettro/subscribe" });
    } catch (err) {
      this._setStatusText(`Errore: ${err.message || err.code || err}`, true);
    } finally {
      this._subscribing = false;
    }
    if (!this._tick) this._tick = setInterval(() => this._updateStatus(), 1000);
    this._loadArchive();
    this._loadMeteo();
    if (this._ro && this.$spec) { this._ro.observe(this.$spec); this._ro.observe(this.$wf); }
  }

  _unsubscribe() {
    if (this._unsub) {
      try { this._unsub(); } catch (e) { /* connessione già chiusa */ }
      this._unsub = null;
    }
  }

  // ---------------------------------------------------------------- dati

  _onMessage(m) {
    if (m.kind === "status") {
      this._status = m;
      this._setLive(m.live || null);
      const vw = this.shadowRoot.getElementById("verwarn");
      const mismatch = m.version !== PANEL_VERSION;
      vw.style.display = mismatch ? "block" : "none";
      if (mismatch) {
        vw.textContent = `Aggiornamento non completato: il pannello è la versione ${PANEL_VERSION} ma l'integrazione è `
          + `${m.version || "precedente alla 1.4.3"}. Riavvia Home Assistant (Impostazioni → Sistema → Riavvia), poi ricarica la pagina.`;
      }
      this.shadowRoot.getElementById("enabled").checked = m.enabled !== false;
      if (!this._formDirty) this._fillForm(m.settings);
      this._updateStatus();
      return;
    }
    if (m.kind === "favorites") { this._favs = m.favorites; this._renderFavs(); this._updateFavBtn(); return; }
    if (m.kind === "live") { this._onLive(m); return; }
    if (m.kind === "live_rds") { this._onLiveRds(m.rds); return; }
    if (m.kind === "live_dab") { this._onLiveDab(m); return; }
    if (m.kind === "live_tv") { this._onLiveTv(m); return; }
    if (m.kind === "live_sat") { this._onLiveSat(m); return; }
    if (m.kind === "live_image") { this._onLiveImage(m); return; }
    if (m.kind === "meteo_passes") { this._meteoPasses = m; this._renderMeteoPasses(); return; }
    if (m.kind === "meteo_live") { this._onMeteoLive(m); return; }
    if (m.kind === "meteo_log") { this._meteoLog = m.log; this._renderMeteoLog(); return; }
    if (m.kind === "digi_log") { this._digiLog = m.log; this._renderDigi(); return; }
    if (m.kind === "live_digi") {
      this._digiLog = (this._digiLog || []).concat(m.decodes).slice(-600);
      this._digiStatus = m;
      this._renderDigi(m.decodes.length ? null : `nessun messaggio nell'ultimo periodo`);
      return;
    }
    if (m.kind === "meteo_image" || m.kind === "meteo_pruned") { this._loadMeteo(m.entry ? m.entry.id : null); return; }
    if (m.kind === "live_error") { this._msg(`Analisi continua interrotta: ${m.error}`, true); return; }
    if (m.kind === "analysis") {
      if (m.entry && !this._archive.some((e) => e.id === m.entry.id)) {
        this._archive.unshift(m.entry);
        this._renderArchive();
      }
      return;
    }
    if (m.kind === "archive_pruned") { this._loadArchive(); return; }
    if (m.kind !== "spectrum") return;
    const grid = (a, b) => a && b && a.f_start === b.f_start && a.bin_hz === b.bin_hz;
    if (m.complete) {
      // all'iscrizione il server rimanda le scansioni recenti: quelle già viste si saltano
      if (this._lastComplete && m.timestamp <= this._lastComplete.timestamp) return;
      const sameGrid = grid(m, this._lastComplete) && this._lastComplete.values.length === m.values.length;
      if (!sameGrid) { this._history = []; this._maxHold = null; }
      if (!this._history.length || this._history[0].sweep_id !== m.sweep_id) {
        this._history.unshift(m);
        if (this._history.length > MAX_HISTORY) this._history.pop();
      }
      this._maxHold = this._maxHold && sameGrid
        ? this._maxHold.map((v, i) => Math.max(v, m.values[i] ?? -999))
        : m.values.slice();
      this._lastComplete = m;
      this._sat = m.sat || null;
      this._renderSat();
      this._spec = { ...m, cursor: null };
    } else {
      // scansione in corso: si mostra la parte nuova + il resto della scansione precedente
      let values = m.values, peaks = m.peaks;
      if (grid(m, this._lastComplete) && this._lastComplete.values.length > values.length) {
        values = values.concat(this._lastComplete.values.slice(values.length));
        if (m.floor_values && this._lastComplete.floor_values) {
          m.floor_values = m.floor_values.concat(this._lastComplete.floor_values.slice(m.floor_values.length));
        }
        peaks = this._lastComplete.peaks;
      }
      this._spec = { ...m, values, peaks, cursor: m.f_start + m.values.length * m.bin_hz };
    }
    this._renderTv();
    if (this._view && (this._view[1] < m.range_start || this._view[0] > m.range_stop)) this._view = null;
    this._draw();
  }

  // ---------------------------------------------------------------- UI

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100%; background:var(--primary-background-color); color:var(--primary-text-color);
                font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .toolbar { display:flex; align-items:center; height:var(--header-height,56px); padding:0 12px; gap:8px;
                   background:var(--app-header-background-color, var(--primary-color)); color:var(--app-header-text-color, #fff);
                   font-size:20px; box-sizing:border-box; }
        .toolbar .title { flex:1; display:flex; flex-direction:column; line-height:1.15; }
        .toolbar .ver { font-size:12px; opacity:.75; }
        .content { padding:12px; display:grid; gap:12px; grid-template-columns: minmax(0,1fr) 300px; max-width:1800px; margin:0 auto; }
        @media (max-width: 1000px) { .content { grid-template-columns: minmax(0,1fr); } }
        .card { background:var(--card-background-color,#fff); border-radius:var(--ha-card-border-radius,12px);
                box-shadow:var(--ha-card-box-shadow,none); border:1px solid var(--divider-color); padding:12px; min-width:0; }
        .charts { display:flex; flex-direction:column; gap:6px; }
        label.selfreq { display:flex; align-items:center; gap:6px; white-space:nowrap; }
        label.selfreq input { width:130px; }
        h4.sub { margin:14px 0 2px; font-size:14px; font-weight:500; border-top:1px solid var(--divider-color); padding-top:10px; }
        #meteopasses button { padding:2px 8px; font-size:12px; }
        .maincol { display:flex; flex-direction:column; gap:12px; min-width:0; }
        #meteopasses table td, #meteolist table td { padding:4px 6px; }
        #meteolist tr { cursor:pointer; }
        #meteolist tr:hover td { background:var(--secondary-background-color); }
        #meteolist tr.sel td { background:rgba(3,169,244,.16); }
        .meteoimg { width:100%; height:auto; display:block; border-radius:6px; margin-top:8px; background:#000; image-rendering:auto; }
        .digitab { width:100%; border-collapse:collapse; font-size:13px; }
        .digitab th, .digitab td { text-align:left; padding:3px 6px; border-bottom:1px solid var(--divider-color,#e0e0e0); white-space:nowrap; }
        .digitab td.msg { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; white-space:normal; }
        .digitab tr.cq td.msg { color:var(--success-color,#43a047); font-weight:600; }
        .digi { max-height:22em; overflow:auto; }
        .livetext { white-space:pre-wrap; word-break:break-word; font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:var(--secondary-background-color,#f5f5f5); border-radius:6px; padding:8px; margin:6px 0; min-height:4.5em; max-height:16em; overflow:auto; }
        .meteogrid { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:4px 12px; margin:6px 0; }
        canvas { display:block; width:100%; }
        #spec { height:340px; cursor:crosshair; touch-action:none; }
        #wf { height:220px; border-radius:4px; }
        .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
        .status { font-size:13px; color:var(--secondary-text-color); flex:1; min-width:200px; }
        .status.err { color:var(--error-color,#db4437); }
        .bar { height:4px; background:var(--divider-color); border-radius:2px; overflow:hidden; }
        .bar > div { height:100%; width:0; background:var(--primary-color); transition:width .4s; }
        label { display:flex; flex-direction:column; font-size:12px; color:var(--secondary-text-color); gap:3px; }
        input, select { font:inherit; font-size:14px; padding:7px 8px; border-radius:6px; border:1px solid var(--divider-color);
                background:var(--primary-background-color); color:var(--primary-text-color); min-width:0; box-sizing:border-box; width:100%; }
        .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
        .side { display:flex; flex-direction:column; gap:12px; }
        h3 { margin:0 0 8px; font-size:15px; font-weight:500; }
        button { font:inherit; font-size:14px; padding:8px 12px; border-radius:6px; border:1px solid var(--divider-color);
                 background:transparent; color:var(--primary-text-color); cursor:pointer; }
        button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); }
        button:disabled { opacity:.5; cursor:default; }
        details summary { cursor:pointer; font-size:13px; color:var(--secondary-text-color); margin:4px 0; }
        table { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums; }
        th { text-align:left; font-weight:500; color:var(--secondary-text-color); padding:4px; border-bottom:1px solid var(--divider-color); }
        td { padding:4px; border-bottom:1px solid var(--divider-color); }
        td.num, th.num { text-align:right; }
        .hint { font-size:12px; color:var(--secondary-text-color); }
        .chk { flex-direction:row; align-items:center; gap:6px; font-size:13px; width:fit-content; max-width:100%; }
        .chk input { width:auto; }
        .msg { font-size:12px; min-height:16px; }
        .msg.err { color:var(--error-color,#db4437); }
        .ana { border-top:1px solid var(--divider-color); padding-top:10px; margin-top:4px; display:flex; flex-direction:column; gap:10px; }
        .ana select, .ana input { width:auto; }
        .ana input#question { flex:1; min-width:160px; }
        .anaimg img { display:block; width:100%; max-width:640px; image-rendering:pixelated; border-radius:4px; }
        .anaimg .axis { display:flex; justify-content:space-between; max-width:640px; font-size:11px; color:var(--secondary-text-color); }
        .anagrid { display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:12px; align-items:start; }
        .answer { white-space:pre-wrap; font-size:14px; line-height:1.45; background:var(--secondary-background-color);
                  border-radius:6px; padding:10px; }
        .err { color:var(--error-color,#db4437); }
        /* la lista scorre da sola: aprire un'analisi non la riporta in cima */
        #archive { max-height:min(70vh, 720px); overflow-y:auto; }
        #archive thead th { position:sticky; top:0; z-index:1; background:var(--card-background-color, var(--primary-background-color)); }
        #archfilter { width:auto; margin-bottom:6px; }
        #archive table td { cursor:pointer; }
        #archive th.sort { cursor:pointer; user-select:none; white-space:nowrap; }
        #archive th.sort:hover { color:var(--primary-text-color); }
        #archive tbody.entry:hover td { background:var(--secondary-background-color); }
        #archive span.scan { color:var(--secondary-text-color); font-size:11px; }
        #archive td.kind { color:var(--secondary-text-color); font-size:12px; }
        /* le due righe di un'analisi (ora/frequenza e tipo) si evidenziano insieme */
        #archive tbody.entry tr:first-child td { border-bottom:none; }
        #archive tbody.entry.sel td { background:rgba(3,169,244,.16); }
        #archive tbody.entry.sel td:first-child { box-shadow:inset 3px 0 var(--primary-color); }
        .anahead { font-size:12px; color:var(--secondary-text-color); }
        button.heart { font-size:20px; line-height:1; padding:6px 10px; color:#e53935; }
        #favs table td { cursor:pointer; }
        #tvlist .tvrow { display:grid; grid-template-columns:64px 1fr 52px 34px; align-items:center; gap:6px; padding:3px 0;
                         cursor:pointer; font-size:13px; }
        #tvlist .tvrow:hover { background:var(--secondary-background-color); }
        #tvlist .tvbar, .meter { height:10px; background:var(--divider-color); border-radius:5px; overflow:hidden; position:relative; }
        #tvlist .tvbar > div, .meter > div { height:100%; border-radius:5px; }
        #tvlist button, #satout button { padding:2px 6px; font-size:12px; }
        #satout .satname { font-size:20px; font-weight:500; margin:2px 0; }
        #satout table td { padding:3px 4px; }
        .meter { height:18px; margin:4px 0 2px; }
        .meter .mark { position:absolute; top:0; bottom:0; width:2px; background:var(--primary-text-color, #000); opacity:.7; border-radius:0; }
        .bignum { font-size:28px; font-weight:500; }
        canvas.trend { width:100%; height:70px; display:block; margin-top:6px; background:#080820; border-radius:6px; }
        #favs tr:hover td { background:var(--secondary-background-color); }
        #favs td.name { font-weight:500; }
        #favs td.act { text-align:right; white-space:nowrap; }
        #favs td.act button { padding:4px 8px; font-size:13px; }
        #favs td.act button.heart { font-size:16px; padding:2px 6px; }
        .livehead { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
        .livehead .dot { color:#e53935; font-weight:600; animation:blink 1.2s infinite; }
        @keyframes blink { 50% { opacity:.3; } }
        .livehead .title { font-size:15px; font-weight:500; flex:1; min-width:180px; }
        .livehead select { width:auto; }
        img.slide { display:block; max-width:min(100%, 320px); margin:10px 0 0; border-radius:6px; }
        #livewf { height:160px; border-radius:4px; background:#080820; }
        .liveaxis { display:flex; justify-content:space-between; font-size:11px; color:var(--secondary-text-color); }
        #livert div { font-size:13px; padding:2px 0; border-bottom:1px solid var(--divider-color); }
        #livert span { color:var(--secondary-text-color); font-size:11px; margin-right:6px; }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title"><span>👻 Spettro</span><span class="ver">versione ${PANEL_VERSION}</span></div></div>
      <div id="verwarn" class="err" style="display:none;padding:10px 16px;font-size:14px"></div>
      <div class="content">
        <div class="maincol">
        <div class="card charts">
          <div class="row">
            <div class="status" id="status">Collegamento…</div>
            <button class="primary" id="resume" style="display:none">Riattiva scansione</button>
            <label class="chk"><input type="checkbox" id="showmax" checked> Max hold</label>
            <button id="clearmax">Azzera max</button>
            <button id="zoomscan" disabled title="Usa la zona ingrandita come nuovo intervallo di scansione">Scansiona zona</button>
            <button id="resetzoom" disabled>Zoom 100%</button>
          </div>
          <div class="bar"><div id="bar"></div></div>
          <canvas id="spec"></canvas>
          <canvas id="wf"></canvas>
          <div class="hint">Trascina sul grafico per ingrandire, doppio clic per tornare all'intervallo completo.</div>
          <div class="ana">
            <h3 style="margin:0">Analisi segnale</h3>
            <div class="row">
              <label class="selfreq" title="Frequenza da analizzare: si prende dal clic sul grafico o si scrive qui (Invio per confermare)">MHz
                <input id="selfreq" type="text" inputmode="decimal" placeholder="es. 99.6" autocomplete="off"></label>
              <div class="hint" id="selinfo" style="flex:1;min-width:160px">Tocca un picco sul grafico, una riga dell'Archivio spike, o scrivi la frequenza.</div>
              <button class="primary" id="analyze" disabled>Analizza</button>
              <button id="livebtn" disabled title="Resta sintonizzato su questa frequenza: audio, cascata e RDS in diretta (la scansione si sospende)">▶ Analisi continua</button>
              <button class="heart" id="fav" disabled title="Aggiungi ai preferiti">♡</button>
            </div>
            <div id="live" style="display:none">
              <div class="livehead">
                <span class="dot">● IN DIRETTA</span>
                <span class="title" id="livetitle"></span>
                <select id="livemode" title="Demodulazione"><option value="wfm">FM larga (radio)</option><option value="nfm">FM stretta (voce)</option><option value="am">AM</option><option value="usb">USB</option><option value="lsb">LSB</option><option value="cw">CW (Morse)</option></select>
                <select id="livebw" title="Larghezza del filtro: più stretto toglie rumore e disturbi vicini, più largo fa passare tutto il segnale"></select>
                <select id="livedec" title="Decodifica dell'audio: immagini o testo"><option value="">Nessuna decodifica</option>${LIVE_DECODERS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
                <button id="livelisten">🔇 Ascolta</button>
                <button class="primary" id="livestop">■ Ferma</button>
              </div>
              <canvas id="livewf" style="margin-top:8px"></canvas>
              <div class="liveaxis"><span id="livelo"></span><span id="livelevel"></span><span id="livehi"></span></div>
              <img id="liveslide" class="slide" style="display:none" alt="Slideshow DAB+">
              <div id="liveimg"></div>
              <div id="liverds"></div>
            </div>
            <div id="anaout"></div>
          </div>
        </div>
        <div class="card" id="meteocard">
          <h3 style="margin:0">🛰️ Satelliti meteo e immagini</h3>
          <div id="meteolive"></div>
          <h4 class="sub">Satelliti in passaggio</h4>
          <div class="hint">Meteor e ISS si ricevono solo quando passano sopra di te: ecco i prossimi passaggi. Con le caselle la
            chiavetta li riceve da sola; ▶ su un passaggio in corso lo riceve subito.</div>
          <div class="row" style="margin-top:4px">
            <label class="chk" title="Quando un Meteor passa sopra l'elevazione minima la scansione si sospende e la chiavetta riceve le foto (LRPT, 137 MHz)"><input type="checkbox" id="meteoauto"> Meteor: ricevi da solo le foto</label>
            <label class="chk" title="La ISS trasmette immagini SSTV su 145,800 MHz solo durante gli eventi ARISS (qualche volta l'anno): ascoltando ogni passaggio le immagini si salvano quando ci sono, altrimenti non succede nulla"><input type="checkbox" id="isssstv"> ISS: ascolta da sola l'SSTV</label>
          </div>
          <div class="row" style="margin-top:4px">
            <label style="width:150px">Elevazione minima (°)<input id="meteoel" type="number" min="0" max="90" step="5"></label>
            <label style="width:150px">Conserva immagini (giorni)<input id="meteodays" type="number" min="1" max="365" step="1"></label>
          </div>
          <div id="meteopasses" class="hint">Calcolo dei passaggi…</div>
          <div id="meteolog"></div>
          <h4 class="sub">Stazioni radio: immagini, testo e modi digitali</h4>
          <div class="hint">Trasmettono da terra a orari propri: radiofax e telescrivente meteo quasi sempre, FT8 e WSPR dei
            radioamatori di continuo (di giorno sui 20 m, di sera sui 40 e 80 m), SSTV e Morse quando qualcuno trasmette.
            ▶ Ascolta resta in ascolto: immagini e messaggi FT8/WSPR si raccolgono anche a pannello chiuso, il testo Morse/RTTY
            compare sotto la cascata mentre arriva.</div>
          <div class="row" style="margin-top:4px">
            <select id="imgpreset" style="flex:1;min-width:220px">${IMG_PRESETS.filter((p) => !p[0].startsWith("ISS")).map((p) => `<option value="${IMG_PRESETS.indexOf(p)}">${p[0]}</option>`).join("")}</select>
            <button id="imglisten" title="Resta in ascolto su questa frequenza e salva le immagini che arrivano (la scansione si sospende)">▶ Ascolta</button>
          </div>
          <h4 class="sub">Immagini ricevute</h4>
          <div id="meteolist" class="hint">Nessuna immagine ancora.</div>
          <div id="meteoview"></div>
          <div class="hint" style="margin-top:8px">Serve un'antenna per i 137 MHz all'aperto con il cielo libero (V-dipolo con due bracci da
            ~53 cm a 120°, o QFH): quella della TV non va bene. I Meteor-M trasmettono le foto della Terra in digitale (LRPT) su 137,1 o 137,9 MHz
            per 10–15 minuti a passaggio; orbite da CelesTrak, posizione della stazione da Home Assistant.</div>
        </div>
        </div>
        <div class="side">
          <div class="card" id="tvcard" style="display:none">
            <h3>Canali TV</h3>
            <div id="tvlist"></div>
            <div class="hint" style="margin-top:6px">Multiplex del digitale terrestre trovati nell'ultima scansione: quanto
              ciascuno sta sopra il rumore. ▶ apre il misuratore continuo (modalità, guardia e qualità del segnale).</div>
          </div>
          <div class="card" id="satcard" style="display:none">
            <h3>Satellite</h3>
            <div id="satout"></div>
          </div>
          <div class="card">
            <h3>Preferiti <span style="color:#e53935">❤</span></h3>
            <div id="favs" class="hint">Nessun preferito: seleziona una frequenza e tocca ♡ nell'Analisi segnale.</div>
          </div>
          <div class="card">
            <div class="row" style="justify-content:space-between">
              <h3 style="margin:0">Archivio spike</h3>
              <button id="archclear" title="Cancella tutte le analisi archiviate">Svuota archivio</button>
            </div>
            <div class="hint" id="archinfo" style="margin:6px 0"></div>
            <select id="archfilter" title="Mostra solo un tipo di segnale" style="display:none"></select>
            <div id="archive" class="hint">Nessuna analisi archiviata.</div>
          </div>
          <div class="card">
            <h3>Scansione</h3>
            <label class="chk" style="margin-bottom:8px"><input type="checkbox" id="enabled" checked> Scansione attiva</label>
            <label>Preset
              <select id="preset">${PRESETS.map((p, i) => `<option value="${i}">${p[0]}</option>`).join("")}</select>
            </label>
            <div class="grid2" style="margin-top:8px">
              <label>Da (MHz)<input id="start" type="number" step="0.001" min="0.5" max="2200"></label>
              <label>A (MHz)<input id="stop" type="number" step="0.001" min="0.5" max="2200"></label>
              <label>Ripeti ogni (s)<input id="interval" type="number" step="1" min="0" title="0 = scansione continua"></label>
              <label>Guadagno (dB)<input id="gain" type="text" placeholder="auto" title="Vuoto = automatico"></label>
              <label>Soglia picchi (dB)<input id="threshold" type="number" step="1" min="1" max="60"></label>
            </div>
            <label class="chk" style="margin-top:8px" title="Con il pannello chiuso il server rtl_tcp resta libero (niente traffico di rete)">
              <input type="checkbox" id="viewonly"> Scansiona solo con il pannello aperto</label>
            <label class="chk" style="margin-top:4px" title="Quando un passo della scansione trova un picco sopra la soglia, la scansione si ferma, il picco viene registrato e analizzato (per la «Durata analisi» delle Avanzate) e poi si riprende">
              <input type="checkbox" id="autoana"> Analizza subito i picchi trovati</label>
            <label class="chk" style="margin-top:4px" title="La chiavetta è collegata al cavo della parabola: tra 950 e 2150 MHz ci sono i transponder della TV satellitare, e Spettro riconosce il satellite">
              <input type="checkbox" id="lnb"> LNB satellitare sul cavo (parabola)</label>
            <div class="hint" id="lnbwarn" style="display:none;margin:2px 0 0 24px">⚠ L'LNB va alimentato a 13/18 V da un decoder o da un
              alimentatore da linea, ma quella tensione <b>non deve arrivare alla chiavetta</b> (la brucia): usa uno splitter con
              l'uscita protetta o un condensatore di blocco. La chiavetta arriva a ~1766 MHz.</div>
            <div class="grid2" style="margin-top:8px">
              <label title="Un picco già analizzato non viene rianalizzato in automatico prima di questo tempo (0 = ogni volta)">Rianalizza dopo (min)<input id="reana" type="number" step="1" min="0"></label>
              <label title="Le analisi archiviate più vecchie vengono cancellate">Archivio: cancella dopo (h)<input id="archhours" type="number" step="1" min="1"></label>
            </div>
            <div class="grid2" style="margin-top:8px">
              <label title="Se spuntato, in ogni passo della scansione si analizzano al massimo questi picchi">
                <span class="chk" style="display:flex"><input type="checkbox" id="limstep"> Max picchi per passo</span>
                <input id="maxstep" type="number" step="1" min="1"></label>
              <label title="Se spuntato, in una scansione completa si analizzano al massimo questi picchi">
                <span class="chk" style="display:flex"><input type="checkbox" id="limsweep"> Max per scansione</span>
                <input id="maxsweep" type="number" step="1" min="1"></label>
            </div>
            <details>
              <summary>Avanzate</summary>
              <div class="grid2">
                <label>Sample rate
                  <select id="rate">${SAMPLE_RATES.map((r) => `<option value="${r}">${(r / 1e6).toFixed(3)} MS/s</option>`).join("")}</select>
                </label>
                <label>Punti FFT
                  <select id="fft">${FFT_SIZES.map((n) => `<option value="${n}">${n}</option>`).join("")}</select>
                </label>
                <label>Assestamento (ms)<input id="settle" type="number" step="10" min="0" max="1000"></label>
                <label>Correzione (ppm)<input id="ppm" type="number" step="1" min="-200" max="200"></label>
                <label title="Sotto i 24 MHz la chiavetta riceve senza sintonizzatore (direct sampling): Nooelec SMArt v5 e RTL-SDR Blog V3 sul ramo Q; la Blog V4 ha già le onde corte (spento)">Onde corte
                  <select id="hfs"><option value="2">Direct sampling Q</option><option value="1">Direct sampling I</option><option value="0">Spento</option></select>
                </label>
                <label title="Quanto registrare per ogni analisi, sia con Analizza sia per i picchi analizzati in automatico (ogni secondo in più allunga la scansione per ogni picco nuovo)">Durata analisi
                  <select id="anadur"><option value="1">1 s</option><option value="2">2 s</option><option value="5">5 s</option></select>
                </label>
              </div>
              <label class="chk" style="margin-top:6px" title="Le analisi fatte a mano salvano sempre la registrazione grezza (circa 4 MB al secondo); le automatiche solo con questa opzione, perché riempirebbero il disco">
                <input type="checkbox" id="saveiq"> Salva la registrazione IQ anche delle analisi automatiche</label>
            </details>
            <div class="row" style="margin-top:10px">
              <button class="primary" id="apply">Applica</button>
              <button id="scannow">Scansiona ora</button>
            </div>
            <div class="msg" id="msg"></div>
          </div>
        </div>
      </div>`;

    const $ = (id) => this.shadowRoot.getElementById(id);
    this.$spec = $("spec");
    this.$wf = $("wf");
    this._menu = this.shadowRoot.querySelector("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;

    const form = ["start", "stop", "interval", "gain", "threshold", "viewonly", "autoana", "lnb", "reana", "archhours", "limstep", "maxstep", "limsweep", "maxsweep",
      "rate", "fft", "settle", "ppm", "anadur", "hfs", "saveiq"];
    for (const id of form) $(id).addEventListener("input", () => {
      this._formDirty = true;
      if (id === "start" || id === "stop") $("preset").value = "0";
    });
    $("preset").addEventListener("change", (e) => {
      const p = PRESETS[+e.target.value];
      if (p[1] === null) return;
      $("start").value = p[1];
      $("stop").value = p[2];
      if (p[1] === 950) { $("lnb").checked = true; $("lnbwarn").style.display = ""; }
      this._formDirty = true;
    });
    $("apply").addEventListener("click", () => this._apply());
    $("enabled").addEventListener("change", async (e) => {
      const enabled = e.target.checked;
      // spegnerla per sbaglio (un tocco mentre la pagina scorre) ferma tutto: si chiede conferma
      if (!enabled && !confirm("Mettere in pausa la scansione?")) { e.target.checked = true; return; }
      try {
        await this._hass.callWS({ type: "spettro/set_enabled", enabled });
        this._msg(enabled ? "Scansione riattivata." : "Scansione in pausa.");
      } catch (err) {
        e.target.checked = !enabled;
        this._msg(err.message || String(err), true);
      }
    });
    $("scannow").addEventListener("click", async () => {
      try { await this._hass.callWS({ type: "spettro/scan_now" }); this._msg("Scansione avviata."); }
      catch (err) { this._msg(err.message || String(err), true); }
    });
    $("showmax").addEventListener("change", (e) => { this._showMax = e.target.checked; this._draw(); });
    $("clearmax").addEventListener("click", () => {
      this._maxHold = this._lastComplete ? this._lastComplete.values.slice() : null;
      this._draw();
    });
    $("resetzoom").addEventListener("click", () => { this._view = null; this._draw(); });
    $("analyze").addEventListener("click", () => this._analyze());
    $("fav").addEventListener("click", () => this._toggleFav());
    $("livebtn").addEventListener("click", () => this._liveStart(this._sel / 1e6));
    $("livestop").addEventListener("click", async () => {
      try { await this._hass.callWS({ type: "spettro/live_stop" }); } catch (err) { this._msg(err.message || String(err), true); }
    });
    $("livelisten").addEventListener("click", () => this._setListen(!this._listen));
    $("livemode").addEventListener("change", (e) => {
      if (!this._live) return;
      const dec = LIVE_DECODERS.find(([v]) => v === this._live.decoder);
      this._liveStart(this._live.freq_mhz, e.target.value, null, dec && dec[2].includes(e.target.value) ? dec[0] : null);
    });
    $("livebw").addEventListener("change", (e) => {
      if (this._live) this._liveStart(this._live.freq_mhz, this._live.mode, null, this._live.decoder || null, this._live.title, +e.target.value);
    });
    const typed = () => {
      // frequenza scritta a mano (anche con la virgola): si seleziona e si inquadra sul grafico
      const v = parseFloat($("selfreq").value.trim().replace(",", "."));
      if (!(v >= 0.5 && v <= 2200)) {
        $("selinfo").textContent = "Frequenza non valida: scrivila in MHz, tra 0,5 e 2200 (es. 7.8781 o 145,8).";
        return;
      }
      const f = v * 1e6;
      this._select(f);
      const sp = this._spec;
      if (sp && f >= sp.range_start && f <= sp.range_stop) {
        const half = Math.max(50e3, (sp.range_stop - sp.range_start) * 0.02);
        this._view = [Math.max(sp.range_start, f - half), Math.min(sp.range_stop, f + half)];
        this._draw();
      }
      $("selfreq").value = v.toFixed(4);
    };
    $("selfreq").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); typed(); $("selfreq").blur(); } });
    $("selfreq").addEventListener("change", typed);
    $("livedec").addEventListener("change", (e) => {
      if (this._live) this._liveStart(this._live.freq_mhz, this._live.mode, null, e.target.value || null, null, this._live.bw);
    });
    $("resume").addEventListener("click", async () => {
      try {
        await this._hass.callWS({ type: "spettro/set_enabled", enabled: true });
        this._msg("Scansione riattivata.");
      } catch (err) {
        this._msg(err.message || String(err), true);
      }
    });
    for (const id of ["autoana", "limstep", "limsweep"]) $(id).addEventListener("change", () => this._syncLimits());
    $("lnb").addEventListener("change", (e) => { $("lnbwarn").style.display = e.target.checked ? "" : "none"; });
    const meteoCfg = async (changes) => {
      try { await this._hass.callWS({ type: "spettro/meteo_config", ...changes }); } catch (err) { this._msg(err.message || String(err), true); }
    };
    $("meteoauto").addEventListener("change", (e) => meteoCfg({ meteo_auto: e.target.checked }));
    $("isssstv").addEventListener("change", (e) => meteoCfg({ iss_sstv: e.target.checked }));
    $("imglisten").addEventListener("click", () => {
      const [name, mhz, mode, dec, bw] = IMG_PRESETS[+$("imgpreset").value];
      this._select(mhz * 1e6);
      this._liveStart(mhz, mode, null, dec, name.startsWith("ISS") ? "ISS" : null, bw || null);
    });
    $("meteoel").addEventListener("change", (e) => meteoCfg({ meteo_min_el: Math.max(0, Math.min(90, parseFloat(e.target.value) || 0)) }));
    $("meteodays").addEventListener("change", (e) => meteoCfg({ meteo_days: Math.max(1, parseFloat(e.target.value) || 7) }));

    $("archfilter").addEventListener("change", (ev) => {
      this._archFilter = ev.target.value;
      try { localStorage.setItem("spettro.archFilter", this._archFilter); } catch (e) { /* non disponibile */ }
      this.shadowRoot.getElementById("archive").scrollTop = 0;
      this._renderArchive();
    });
    $("archclear").addEventListener("click", async () => {
      if (!this._archive.length || !confirm(`Cancellare tutte le ${this._archive.length} analisi archiviate?`)) return;
      const btn = $("archclear");
      btn.disabled = true;
      btn.textContent = "Cancellazione…";
      try {
        const res = await this._hass.callWS({ type: "spettro/archive_delete" });
        this._msg(`Archivio svuotato: ${res.removed} analisi cancellate.`);
        await this._loadArchive();
      } catch (err) {
        this._msg(err.message || String(err), true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Svuota archivio";
      }
    });
    $("zoomscan").addEventListener("click", () => {
      if (!this._view) return;
      $("start").value = (this._view[0] / 1e6).toFixed(3);
      $("stop").value = (this._view[1] / 1e6).toFixed(3);
      $("preset").value = "0";
      this._view = null;
      this._apply();
    });

    this._bindChart();
    this._ro = new ResizeObserver(() => this._draw());
    this._ro.observe(this.$spec);
    this._ro.observe(this.$wf);
  }

  _fillForm(s) {
    if (!s) return;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("start").value = s.start_mhz;
    $("stop").value = s.stop_mhz;
    $("interval").value = s.interval_s;
    $("gain").value = s.gain_db === null || s.gain_db === undefined ? "" : s.gain_db;
    $("threshold").value = s.threshold_db;
    $("rate").value = String(s.sample_rate);
    $("fft").value = String(s.fft_size);
    $("settle").value = s.settle_ms;
    $("ppm").value = s.ppm;
    $("hfs").value = String(s.hf_sampling ?? 2);
    $("anadur").value = String(s.analysis_s ?? 2);
    $("viewonly").checked = !!s.only_when_viewed;
    $("autoana").checked = !!s.auto_analyze;
    $("lnb").checked = !!s.lnb;
    $("saveiq").checked = !!s.save_iq_auto;
    $("meteoauto").checked = !!s.meteo_auto;
    $("isssstv").checked = !!s.iss_sstv;
    if (this.shadowRoot.activeElement !== $("meteoel")) $("meteoel").value = s.meteo_min_el ?? 20;
    if (this.shadowRoot.activeElement !== $("meteodays")) $("meteodays").value = s.meteo_days ?? 7;
    $("lnbwarn").style.display = s.lnb ? "" : "none";
    this._lnb = !!s.lnb;
    $("reana").value = s.reanalyze_min ?? 30;
    $("archhours").value = s.archive_hours ?? 24;
    $("limstep").checked = !!s.auto_limit_step;
    $("maxstep").value = s.auto_max_per_step || 3;
    $("limsweep").checked = !!s.auto_limit_sweep;
    $("maxsweep").value = s.auto_max_per_sweep || 20;
    this._syncLimits();
    const idx = PRESETS.findIndex((p) => p[1] === s.start_mhz && p[2] === s.stop_mhz);
    $("preset").value = String(idx > 0 ? idx : 0);
  }

  async _apply() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const start = parseFloat($("start").value), stop = parseFloat($("stop").value);
    if (!(start > 0) || !(stop > start)) { this._msg("Intervallo non valido: 'A' deve essere maggiore di 'Da'.", true); return; }
    const gainTxt = $("gain").value.trim().toLowerCase();
    const gain = gainTxt === "" || gainTxt === "auto" ? null : parseFloat(gainTxt.replace(",", "."));
    if (gain !== null && isNaN(gain)) { this._msg("Guadagno non valido (numero in dB o vuoto per automatico).", true); return; }
    const msg = {
      type: "spettro/set_config",
      start_mhz: start,
      stop_mhz: stop,
      interval_s: Math.max(0, parseFloat($("interval").value) || 0),
      gain_db: gain,
      threshold_db: parseFloat($("threshold").value) || 10,
      sample_rate: parseInt($("rate").value, 10),
      fft_size: parseInt($("fft").value, 10),
      settle_ms: parseInt($("settle").value, 10) || 0,
      ppm: parseInt($("ppm").value, 10) || 0,
      hf_sampling: parseInt($("hfs").value, 10),
      analysis_s: parseFloat($("anadur").value) || 2,
      only_when_viewed: $("viewonly").checked,
      auto_analyze: $("autoana").checked,
      lnb: $("lnb").checked,
      save_iq_auto: $("saveiq").checked,
      reanalyze_min: Math.max(0, parseFloat($("reana").value) || 0),
      archive_hours: Math.max(1, parseFloat($("archhours").value) || 24),
      auto_limit_step: $("limstep").checked,
      auto_max_per_step: Math.max(1, parseInt($("maxstep").value, 10) || 3),
      auto_limit_sweep: $("limsweep").checked,
      auto_max_per_sweep: Math.max(1, parseInt($("maxsweep").value, 10) || 20),
    };
    $("apply").disabled = true;
    try {
      await this._hass.callWS(msg);
      this._formDirty = false;
      this._view = null;
      this._msg("Impostazioni salvate, nuova scansione in corso.");
    } catch (err) {
      this._msg(err.message || String(err), true);
    } finally {
      $("apply").disabled = false;
    }
  }

  // i valori dei limiti si modificano solo se il loro checkbox è spuntato
  _syncLimits() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const auto = $("autoana").checked;
    for (const id of ["reana", "limstep", "limsweep"]) $(id).disabled = !auto;
    $("maxstep").disabled = !auto || !$("limstep").checked;
    $("maxsweep").disabled = !auto || !$("limsweep").checked;
  }

  _msg(text, err = false) {
    const el = this.shadowRoot.getElementById("msg");
    el.textContent = text;
    el.className = "msg" + (err ? " err" : "");
    clearTimeout(this._msgT);
    this._msgT = setTimeout(() => { el.textContent = ""; }, err ? 20000 : 6000);
  }

  _setStatusText(text, err) {
    const el = this.shadowRoot && this.shadowRoot.getElementById("status");
    if (!el) return;
    el.textContent = text;
    el.className = "status" + (err ? " err" : "");
  }

  _updateStatus() {
    const s = this._status;
    if (!s || !this.shadowRoot.getElementById("status")) return;
    const bar = this.shadowRoot.getElementById("bar");
    const extra = [];
    if (s.tuner) extra.push(`tuner ${s.tuner}`);
    if (s.sweep_s != null) extra.push(`ultima scansione ${s.sweep_s} s`);
    if (s.auto_count != null) extra.push(`${s.auto_count} picchi analizzati`);
    if (s.last_scan) extra.push(new Date(s.last_scan * 1000).toLocaleTimeString());
    const tail = extra.length ? ` · ${extra.join(" · ")}` : "";
    let text, err = false, prog = 0;
    switch (s.state) {
      case "connecting": text = `Collegamento a ${s.host}:${s.port}…`; break;
      case "scanning":
        prog = s.progress;
        text = `Scansione in corso ${Math.round(s.progress * 100)}%`
          + (s.auto_mhz ? ` · analisi automatica ${s.auto_mhz.toFixed(4)} MHz` : "");
        break;
      case "idle": {
        prog = 1;
        const left = s.next_scan ? Math.max(0, Math.round(s.next_scan - Date.now() / 1000)) : null;
        text = left === null ? "In attesa" : left > 0 ? `Prossima scansione tra ${left} s` : "Avvio scansione…";
        break;
      }
      case "paused":
        text = s.paused === "satellite" ? "Ricezione di un satellite meteo in corso, scansione sospesa…"
          : s.paused === "analysis" ? "Analisi del segnale in corso, scansione sospesa…"
          : s.paused === "live" ? `Analisi continua su ${s.live ? s.live.freq_mhz.toFixed(4) : "?"} MHz, scansione sospesa`
          : s.paused === "disabled" ? "In pausa (interruttore Scansione spento)" : "In pausa: nessun pannello aperto";
        break;
      case "error": {
        err = true;
        const left = s.next_scan ? Math.max(0, Math.round(s.next_scan - Date.now() / 1000)) : 0;
        text = `Errore: ${s.error} — nuovo tentativo tra ${left} s`;
        break;
      }
      default: text = "Avvio…";
    }
    this._setStatusText(text + tail, err);
    this.shadowRoot.getElementById("resume").style.display = s.state === "paused" && s.paused === "disabled" ? "" : "none";
    bar.style.width = `${Math.round(prog * 100)}%`;
  }

  // ---------------------------------------------------------------- grafico

  _bindChart() {
    const c = this.$spec;
    const fx = (e) => { const r = c.getBoundingClientRect(); return e.clientX - r.left; };
    c.addEventListener("pointerdown", (e) => {
      if (!this._plot) return;
      c.setPointerCapture(e.pointerId);
      this._drag = { x0: fx(e), x1: fx(e) };
    });
    c.addEventListener("pointermove", (e) => {
      this._hoverX = fx(e);
      if (this._drag) this._drag.x1 = this._hoverX;
      this._draw();
    });
    c.addEventListener("pointerleave", () => { this._hoverX = null; this._draw(); });
    c.addEventListener("pointerup", () => {
      const d = this._drag;
      this._drag = null;
      if (d && this._plot && Math.abs(d.x1 - d.x0) > 6) {
        const a = this._plot.xToF(Math.min(d.x0, d.x1)), b = this._plot.xToF(Math.max(d.x0, d.x1));
        this._view = [a, b];
      } else if (d && this._plot && this._spec) {
        // tocco: sceglie il picco più vicino (entro 14 px), altrimenti la frequenza sotto il dito
        const { xToF, fToX } = this._plot;
        let best = null, bestDx = 14;
        for (const p of this._spec.peaks) {
          const dx = Math.abs(fToX(p.freq_mhz * 1e6) - d.x0);
          if (dx < bestDx) { best = p; bestDx = dx; }
        }
        this._select(best ? best.freq_mhz * 1e6 : xToF(d.x0));
        if (best && best.aid) this._openArchived(best.aid);
      }
      this._draw();
    });
    c.addEventListener("dblclick", () => { this._view = null; this._draw(); });
  }

  _draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = null;
      this._drawSpectrum();
      this._drawWaterfall();
      const z = this.shadowRoot.getElementById("resetzoom");
      if (z) z.disabled = !this._view;
      const zs = this.shadowRoot.getElementById("zoomscan");
      if (zs) zs.disabled = !this._view;
    });
  }

  _setupCanvas(c) {
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return null;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  // massimo dei valori che cadono nella colonna [fa, fb)
  static _colMax(values, f0, bin, fa, fb) {
    let ia = Math.floor((fa - f0) / bin + 0.5), ib = Math.floor((fb - f0) / bin + 0.5);
    if (ib <= ia) ib = ia + 1;
    ia = Math.max(0, ia);
    ib = Math.min(values.length, ib);
    let m = null;
    for (let i = ia; i < ib; i++) { const v = values[i]; if (v !== null && (m === null || v > m)) m = v; }
    return m;
  }

  _drawSpectrum() {
    const s = this._spec;
    const g = this._setupCanvas(this.$spec);
    if (!g) return;
    const { ctx, w, h } = g;
    const css = getComputedStyle(this);
    const txt = css.getPropertyValue("--secondary-text-color").trim() || "#888";
    const grid = css.getPropertyValue("--divider-color").trim() || "rgba(128,128,128,.25)";
    const prim = css.getPropertyValue("--primary-color").trim() || "#03a9f4";
    ctx.font = "11px Roboto, sans-serif";

    if (!s) {
      ctx.fillStyle = txt;
      ctx.textAlign = "center";
      ctx.fillText("In attesa della prima scansione…", w / 2, h / 2);
      this._plot = null;
      return;
    }

    const L = 46, R = 10, T = 10, B = 24;
    const pw = w - L - R, ph = h - T - B;
    const lo = this._view ? this._view[0] : s.range_start;
    const hi = this._view ? this._view[1] : s.range_stop;
    const span = hi - lo;
    const xToF = (x) => lo + ((x - L) / pw) * span;
    const fToX = (f) => L + ((f - lo) / span) * pw;

    // valori per colonna di pixel
    const cols = Math.max(1, Math.floor(pw));
    const line = new Array(cols), maxl = new Array(cols);
    let vmin = Infinity, vmax = -Infinity;
    for (let x = 0; x < cols; x++) {
      const fa = lo + (x / cols) * span, fb = lo + ((x + 1) / cols) * span;
      const v = SpettroPanel._colMax(s.values, s.f_start, s.bin_hz, fa, fb);
      line[x] = v;
      if (v !== null) { vmin = Math.min(vmin, v); vmax = Math.max(vmax, v); }
      if (this._showMax && this._maxHold && this._lastComplete) {
        const mv = SpettroPanel._colMax(this._maxHold, this._lastComplete.f_start, this._lastComplete.bin_hz, fa, fb);
        maxl[x] = mv;
        if (mv !== null) vmax = Math.max(vmax, mv);
      }
    }
    if (!isFinite(vmin)) { vmin = -80; vmax = -20; }
    if (s.floor_values && s.floor_values.length && this._status && this._status.settings) {
      vmax = Math.max(vmax, Math.max(...s.floor_values) + this._status.settings.threshold_db);  // la soglia resta visibile
    }
    const floor = s.floor_db ?? vmin;
    let yMin = Math.floor((Math.min(floor - 10, vmin) - 2) / 10) * 10;
    let yMax = Math.ceil((vmax + 3) / 10) * 10;
    if (yMax - yMin < 20) yMax = yMin + 20;
    this._yRange = [Math.max(floor - 5, yMin), yMax];
    const vToY = (v) => T + ph - ((v - yMin) / (yMax - yMin)) * ph;

    // griglia e assi
    ctx.strokeStyle = grid;
    ctx.fillStyle = txt;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const yStep = yMax - yMin > 60 ? 20 : 10;
    for (let v = yMin; v <= yMax; v += yStep) {
      const y = Math.round(vToY(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
      ctx.fillText(`${v}`, L - 6, y);
    }
    ctx.save();
    ctx.translate(11, T + ph / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center"; ctx.fillText("dBFS", 0, 0);
    ctx.restore();
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const xStep = niceStep(span, Math.max(2, Math.floor(pw / 90)));
    for (let f = Math.ceil(lo / xStep) * xStep; f <= hi; f += xStep) {
      const x = Math.round(fToX(f)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
      ctx.fillText(fmtMHz(f, span), x, T + ph + 6);
    }

    ctx.save();
    ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();

    // max hold
    if (this._showMax && this._maxHold) {
      ctx.strokeStyle = "rgba(255,152,0,.75)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      let pen = false;
      for (let x = 0; x < cols; x++) {
        const v = maxl[x];
        if (v === null || v === undefined) { pen = false; continue; }
        const px = L + x + 0.5, py = vToY(v);
        pen ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
    }

    // spettro con riempimento
    let seg = [];
    const flush = () => {
      if (seg.length < 2) { seg = []; return; }
      ctx.beginPath();
      ctx.moveTo(seg[0][0], T + ph);
      for (const [px, py] of seg) ctx.lineTo(px, py);
      ctx.lineTo(seg[seg.length - 1][0], T + ph);
      ctx.closePath();
      ctx.fillStyle = prim;
      ctx.globalAlpha = 0.22;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath();
      seg.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
      ctx.strokeStyle = prim;
      ctx.lineWidth = 1.2;
      ctx.stroke();
      seg = [];
    };
    for (let x = 0; x < cols; x++) {
      const v = line[x];
      if (v === null) { flush(); continue; }
      seg.push([L + x + 0.5, vToY(v)]);
    }
    flush();

    // soglia di rilevamento: rumore della zona + soglia (segue il fondo lungo la banda)
    if (s.floor_db != null && this._status && this._status.settings) {
      const th = this._status.settings.threshold_db;
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(219,68,55,.6)";
      ctx.beginPath();
      if (s.floor_values && s.floor_values.length) {
        for (let x = 0; x <= pw; x += 2) {
          const f = lo + ((hi - lo) * x) / pw;
          const i = Math.max(0, Math.min(s.floor_values.length - 1, Math.round((f - s.f_start) / s.bin_hz)));
          const y = vToY(s.floor_values[i] + th);
          if (x === 0) ctx.moveTo(L + x, y); else ctx.lineTo(L + x, y);
        }
      } else {
        const y = vToY(s.floor_db + th);
        ctx.moveTo(L, y); ctx.lineTo(L + pw, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // posizione della scansione in corso
    if (s.cursor != null && s.cursor >= lo && s.cursor <= hi) {
      const x = fToX(s.cursor);
      ctx.fillStyle = "rgba(128,128,128,.12)";
      ctx.fillRect(x, T, L + pw - x, ph);
      ctx.strokeStyle = prim;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
    }

    // picchi
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    const labeled = [];
    s.peaks.forEach((p, i) => {
      const f = p.freq_mhz * 1e6;
      if (f < lo || f > hi) return;
      const x = fToX(f), y = vToY(p.db);
      ctx.fillStyle = "#e53935";
      ctx.beginPath(); ctx.moveTo(x, y - 3); ctx.lineTo(x - 4, y - 10); ctx.lineTo(x + 4, y - 10); ctx.closePath(); ctx.fill();
      if (i < 12 && labeled.every((lx) => Math.abs(lx - x) > 52)) {
        labeled.push(x);
        ctx.fillStyle = txt;
        const lx = Math.min(Math.max(x, L + 22), L + pw - 22);
        ctx.fillText(p.freq_mhz.toFixed(span < 5e6 ? 3 : 2), lx, Math.max(T + 12, y - 12));
      }
    });

    // frequenza scelta per l'analisi
    if (this._sel !== null && this._sel >= lo && this._sel <= hi) {
      const x = Math.round(fToX(this._sel)) + 0.5;
      ctx.strokeStyle = "#43a047";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 3]);
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
    }

    // selezione zoom
    if (this._drag) {
      const a = Math.min(this._drag.x0, this._drag.x1), b = Math.max(this._drag.x0, this._drag.x1);
      ctx.fillStyle = "rgba(3,169,244,.15)";
      ctx.fillRect(a, T, b - a, ph);
    }
    ctx.restore();

    // cursore con lettura
    const hx = this._hoverX;
    if (hx !== null && hx >= L && hx <= L + pw && !this._drag) {
      const col = Math.min(cols - 1, Math.max(0, Math.floor(hx - L)));
      const v = line[col];
      ctx.strokeStyle = txt;
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(hx + 0.5, T); ctx.lineTo(hx + 0.5, T + ph); ctx.stroke();
      ctx.setLineDash([]);
      const label = `${fmtMHz(xToF(hx), span / 10)} MHz${v !== null ? `  ${v.toFixed(1)} dBFS` : ""}`;
      ctx.font = "12px Roboto, sans-serif";
      const tw = ctx.measureText(label).width + 12;
      const bx = Math.min(Math.max(L, hx + 8), L + pw - tw);
      ctx.fillStyle = css.getPropertyValue("--card-background-color").trim() || "#fff";
      ctx.strokeStyle = grid;
      ctx.fillRect(bx, T + 4, tw, 20);
      ctx.strokeRect(bx + 0.5, T + 4.5, tw, 20);
      ctx.fillStyle = css.getPropertyValue("--primary-text-color").trim() || "#000";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(label, bx + 6, T + 14);
    }

    this._plot = { L, pw, xToF, fToX };
  }

  // ---------------------------------------------------------------- preferiti

  // un preferito è una frequenza, o per il DAB+ una radio (SId) di un multiplex
  _favFor(mhz, sid = null) {
    return mhz == null ? null
      : this._favs.find((f) => Math.abs(f.freq_mhz - mhz) <= 0.03 && (f.sid || null) === (sid || null)) || null;
  }

  // a cosa si riferisce il cuore: la radio DAB+ in ascolto, altrimenti la frequenza selezionata
  _favTarget() {
    const live = this._live;
    if (live && live.mode === "dab" && live.sid) return { mhz: live.freq_mhz, sid: live.sid, name: live.service || null };
    return this._sel === null ? null : { mhz: this._sel / 1e6, sid: null, name: null };
  }

  _updateFavBtn() {
    const b = this.shadowRoot.getElementById("fav");
    if (!b) return;
    const t = this._favTarget();
    const on = !!t && !!this._favFor(t.mhz, t.sid);
    b.disabled = !t;
    b.textContent = on ? "❤" : "♡";
    b.title = on ? "Togli dai preferiti" : t && t.sid ? "Aggiungi la radio DAB+ ai preferiti" : "Aggiungi ai preferiti";
  }

  async _toggleFav(target = null) {
    const t = target || this._favTarget();
    if (!t) return;
    const fav = this._favFor(t.mhz, t.sid);
    let name = t.name, source = t.sid ? "dab" : "rds";
    // nome iniziale: quello RDS dell'analisi mostrata, se è di questa frequenza
    const ana = this._ana;
    if (!name && !t.sid && ana && ana.rds && ana.rds.ps_complete && Math.abs(ana.target_mhz - t.mhz) <= 0.1) name = ana.rds.ps;
    try {
      if (fav) await this._hass.callWS({ type: "spettro/favorite_remove", freq_mhz: fav.freq_mhz, sid: fav.sid || null });
      else await this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: t.mhz, sid: t.sid, name, ...(name ? { source } : {}) });
    } catch (err) {
      this._msg(err.message || String(err), true);
    }
  }

  async _renameFav(fav) {
    if (!fav) return;
    const name = prompt(`Nome per ${fav.freq_mhz.toFixed(4)} MHz (vuoto = nome automatico):`, fav.name || "");
    if (name === null) return;
    try {
      await this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: fav.freq_mhz, sid: fav.sid || null, name, source: "user" });
    } catch (err) {
      this._msg(err.message || String(err), true);
    }
  }

  _renderFavs() {
    const el = this.shadowRoot.getElementById("favs");
    if (!this._favs.length) {
      el.className = "hint";
      el.textContent = "Nessun preferito: seleziona una frequenza e tocca ♡ nell'Analisi segnale.";
      return;
    }
    el.className = "";
    el.innerHTML = `<table><tbody>${this._favs.map((f, i) => `<tr data-i="${i}">
        <td class="name">${esc(f.name || "—")}</td><td>${f.sid ? `DAB+ ${esc(f.channel || f.freq_mhz.toFixed(3))}` : `${f.freq_mhz.toFixed(4)} MHz`}</td>
        <td class="act"><button class="edit" title="Cambia nome">✏</button><button class="play" title="Analisi continua">▶</button><button class="heart" title="Togli dai preferiti">❤</button></td></tr>`).join("")}
      </tbody></table>`;
    el.querySelectorAll("tr[data-i]").forEach((tr) => {
      const fav = this._favs[+tr.dataset.i];
      const mhz = fav.freq_mhz;
      tr.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        this._select(mhz * 1e6);
        const sp = this._spec, f = mhz * 1e6;
        if (sp && f >= sp.range_start && f <= sp.range_stop) {
          const half = Math.max(50e3, (sp.range_stop - sp.range_start) * 0.02);
          this._view = [Math.max(sp.range_start, f - half), Math.min(sp.range_stop, f + half)];
          this._draw();
        }
        // ultima analisi archiviata di questa frequenza, se c'è
        const e2 = this._archive.filter((a) => Math.abs(a.target_mhz - mhz) <= 0.1).sort((a, b) => b.timestamp - a.timestamp)[0];
        if (e2 && !this._live) this._openArchived(e2.id, true);
      });
      tr.querySelector("button.play").addEventListener("click", () => {
        this._select(mhz * 1e6);
        if (fav.sid) this._liveStart(mhz, "dab", fav.sid);
        else this._liveStart(mhz);
      });
      tr.querySelector("button.heart").addEventListener("click", () => this._toggleFav({ mhz, sid: fav.sid || null, name: fav.name }));
      tr.querySelector("button.edit").addEventListener("click", () => this._renameFav(fav));
    });
  }

  // ---------------------------------------------------------------- analisi continua

  _ensureAudio() {
    // va creato dentro un tocco dell'utente, altrimenti il browser lo tiene muto
    if (!this._audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      this._audioCtx = new AC();
    }
    if (this._audioCtx.state === "suspended") this._audioCtx.resume();
    return this._audioCtx;
  }

  _setListen(on) {
    this._listen = on && !!this._ensureAudio();
    this._nextT = 0;
    this._lead = 0;
    this._underruns = 0;
    const b = this.shadowRoot.getElementById("livelisten");
    b.textContent = this._listen ? "🔊 Muto" : "🔇 Ascolta";

  }

  async _liveStart(mhz, mode = null, sid = null, decoder = null, title = null, bw = null) {
    if (mhz == null) return;
    this._setListen(true); // il tocco sul pulsante permette di avviare l'audio
    this._liveRestart = true; // la diretta precedente si ferma: l'audio resta acceso
    try {
      await this._hass.callWS({ type: "spettro/live_start", freq_mhz: mhz, ...(mode ? { mode } : {}), ...(sid ? { sid } : {}),
        ...(decoder ? { decoder } : {}), ...(title ? { title } : {}), ...(bw ? { bw } : {}) });
      this.shadowRoot.querySelector(".ana").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      this._setListen(false);
      this._msg(err.message || String(err), true);
    } finally {
      this._liveRestart = false;
    }
  }

  _setLive(live) {
    const was = this._live;
    this._live = live;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("live").style.display = live ? "" : "none";
    $("anaout").style.display = live ? "none" : "";
    $("analyze").disabled = this._sel === null || !!this._analyzing || !!live;
    if (!live) {
      if (was && !this._liveRestart) { this._setListen(false); this._liveRows = null; this._closeDec(); }
      this._updateFavBtn();
      return;
    }
    if (!was || was.freq_mhz !== live.freq_mhz || was.mode !== live.mode || was.sid !== live.sid) {
      this._liveRows = null;
      this._rtHistory = [];
      this._liveRds = null;
      this._dabState = null;
      this._tvTrend = [];
      this._satMax = null;
      this._dabFallback = false;
      $("liveslide").style.display = "none";
      $("liveslide").removeAttribute("src");
      this._closeDec();
      $("liverds").innerHTML = live.mode === "wfm" ? `<div class="hint" style="margin-top:6px">In attesa dei dati RDS…</div>`
        : live.mode === "dab" ? `<div class="hint" style="margin-top:6px">Aggancio del multiplex DAB…</div>`
        : live.mode === "tv" ? `<div class="hint" style="margin-top:6px">Ricerca del multiplex TV…</div>`
        : live.mode === "sat" ? `<div class="hint" style="margin-top:6px">Misura del transponder…</div>` : "";
    }
    const digital = live.mode === "dab" || live.mode === "tv" || live.mode === "sat";
    $("livemode").style.display = digital ? "none" : "";
    $("livelisten").style.display = live.mode === "tv" || live.mode === "sat" ? "none" : "";
    $("livedec").style.display = ["nfm", "am", "usb", "lsb", "cw"].includes(live.mode) ? "" : "none";
    for (const o of $("livedec").options) {
      const d = LIVE_DECODERS.find(([v]) => v === o.value);
      o.hidden = o.disabled = !!d && !d[2].includes(live.mode);
    }
    $("livedec").value = live.decoder || "";
    const bws = BANDWIDTHS[live.mode];
    $("livebw").style.display = bws ? "" : "none";
    if (bws) {
      $("livebw").innerHTML = bws[1].map((b) => `<option value="${b}">${b >= 1000 ? `${(b / 1000).toLocaleString("it-IT")} kHz` : `${b} Hz`}</option>`).join("");
      $("livebw").value = String(live.bw || bws[0]);
    }
    if (!live.decoder) { $("liveimg").innerHTML = ""; this._liveImgPreview = null; this._liveText = null; }
    else if (SLOT_DECODERS.includes(live.decoder)) {
      if (!was || was.decoder !== live.decoder || was.freq_mhz !== live.freq_mhz) { this._digiNote = null; $("liveimg").innerHTML = ""; this._renderDigi(); }
    } else if (TEXT_DECODERS.includes(live.decoder)) {
      if (!was || was.decoder !== live.decoder || was.freq_mhz !== live.freq_mhz || this._liveText == null) {
        this._liveText = live.text || "";
        this._renderLiveText("in cerca del segnale…");
      }
    } else if (!was || was.decoder !== live.decoder || was.freq_mhz !== live.freq_mhz) {
      this._liveText = null;
      this._liveImgPreview = null;
      $("liveimg").innerHTML = `<div class="hint" style="margin-top:6px">In ascolto: ${live.decoder === "fax" ? "una carta inizia con il tono di avvio" : "ogni immagine SSTV inizia con il codice VIS"}…</div>`;
    }
    this._updateFavBtn();
    if (!digital) $("livemode").value = live.mode;
    this._liveTitle();
  }

  _liveTitle() {
    const live = this._live;
    if (!live) return;
    if (live.mode === "dab") {
      const ens = this._dabState && this._dabState.info && this._dabState.info.ensemble;
      this.shadowRoot.getElementById("livetitle").textContent =
        `DAB ${live.channel || ""} · «${live.service || live.sid}»${ens ? ` (${ens})` : ""}`;
      return;
    }
    if (live.mode === "sat") {
      this.shadowRoot.getElementById("livetitle").textContent = live.ku_mhz
        ? `SAT ${live.satellite} · ${(live.ku_mhz / 1000).toFixed(3)} GHz ${live.polarization ? live.polarization.split(" ")[0] : ""} (cavo ${live.freq_mhz.toFixed(0)} MHz)`
        : `SAT · cavo ${live.freq_mhz.toFixed(1)} MHz`;
      return;
    }
    if (live.mode === "tv") {
      this.shadowRoot.getElementById("livetitle").textContent = `TV ${live.channel || ""} · ${live.freq_mhz.toFixed(1)} MHz`;
      return;
    }
    const fav = this._favFor(live.freq_mhz);
    const name = (fav && fav.source === "user" && fav.name)
      || (this._liveRds && this._liveRds.ps_complete && this._liveRds.ps) || (fav && fav.name) || "";
    this.shadowRoot.getElementById("livetitle").textContent = `${live.freq_mhz.toFixed(4)} MHz${name ? ` «${name}»` : ""}`;
  }

  _onLive(m) {
    if (!this._live) this._setLive({ freq_mhz: m.freq_mhz, mode: m.mode });
    const $ = (id) => this.shadowRoot.getElementById(id);
    const half = m.span_hz / 2;
    $("livelo").textContent = `${((m.freq_mhz * 1e6 - half) / 1e6).toFixed(3)}`;
    $("livehi").textContent = `${((m.freq_mhz * 1e6 + half) / 1e6).toFixed(3)}`;
    $("livelevel").textContent = `livello ${m.level_db} dBFS`;
    this._drawLiveRow(m.row);
    if (this._listen && this._audioCtx) this._playPcm(m.audio);
    if (m.digi && this._live && SLOT_DECODERS.includes(this._live.decoder)) { this._digiStatus = m.digi; this._renderDigiStatus(); }
    if (m.text_info !== undefined && this._live && TEXT_DECODERS.includes(this._live.decoder)) {
      if (this._liveText == null) this._liveText = "";
      if (m.text) this._liveText = (this._liveText + m.text).slice(-4000);
      if (m.text || m.text_info !== this._liveTextInfo) this._renderLiveText(m.text_info);
    }
  }

  _renderDigi(note) {
    const live = this._live;
    if (!live || !SLOT_DECODERS.includes(live.decoder)) return;
    if (note !== undefined) this._digiNote = note;
    const el = this.shadowRoot.getElementById("liveimg");
    const wspr = live.decoder === "wspr";
    if (!el.querySelector(".digi")) {
      el.innerHTML = `<h3 style="margin:10px 0 4px">${wspr ? "Radiofari WSPR" : "Messaggi FT8"}</h3>
        <div class="hint" id="digistat"></div>
        <div class="row" style="margin:4px 0">${wspr ? "" : `<label class="chk"><input type="checkbox" id="digicq"> solo chi chiama (CQ)</label>`}
          <span class="hint">${wspr ? "Ogni 2 minuti pari: nominativo, locatore e potenza di chi trasmette."
            : "Ogni 15 secondi: i collegamenti dei radioamatori."} L'ora del computer di Home Assistant deve essere giusta (al secondo);
            l'ascolto continua anche a pannello chiuso.</span></div>
        <div class="digi"></div>`;
      const cq = el.querySelector("#digicq");
      if (cq) cq.addEventListener("change", () => this._renderDigi());
    }
    this._renderDigiStatus();
    const onlyCq = el.querySelector("#digicq")?.checked;
    const rows = (this._digiLog || []).filter((d) => d.mode === live.decoder && Math.abs(d.rf_hz - d.freq_hz - live.freq_mhz * 1e6) < 50
      && (!onlyCq || d.cq)).slice(-250).reverse();
    const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: wspr ? undefined : "2-digit", hour12: false });
    const km = (d) => (d.km != null ? `${d.km.toLocaleString("it-IT")} km` : "");
    const watt = (dbm) => { const w = 10 ** (dbm / 10) / 1000; return w >= 1 ? `${+w.toFixed(1)} W` : `${+(w * 1000).toPrecision(2)} mW`; };
    el.querySelector(".digi").innerHTML = rows.length ? `<table class="digitab"><thead><tr>${wspr
      ? "<th>Ora</th><th>SNR</th><th>MHz</th><th>Nominativo</th><th>Locatore</th><th>Potenza</th><th>Distanza</th>"
      : "<th>Ora</th><th>SNR</th><th>DT</th><th>Hz</th><th>Messaggio</th><th>Distanza</th>"}</tr></thead><tbody>${rows.map((d) => wspr
      ? `<tr><td>${hm(d.time)}</td><td>${d.snr_db}</td><td>${(d.rf_hz / 1e6).toFixed(6)}</td><td><b>${esc(d.call)}</b></td><td>${esc(d.grid || "")}</td>
          <td>${d.dbm} dBm (${watt(d.dbm)})</td><td>${km(d)}</td></tr>`
      : `<tr${d.cq ? ' class="cq"' : ""}><td>${hm(d.time)}</td><td>${d.snr_db}</td><td>${d.dt_s.toFixed(1)}</td><td>${d.freq_hz}</td>
          <td class="msg">${esc(d.message)}</td><td>${km(d)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="hint">Nessun messaggio ancora: il primo periodo completo si decodifica ${wspr ? "dopo al massimo 4 minuti" : "entro 30 secondi"}.</div>`;
  }

  _renderDigiStatus() {
    const el = this.shadowRoot.getElementById("digistat");
    const st = this._digiStatus;
    if (!el || !st) return;
    const left = st.next_decode ? Math.max(0, Math.round(st.next_decode - Date.now() / 1000)) : null;
    el.textContent = `${st.slots || 0} periodi decodificati · ${st.decoded || 0} messaggi${left != null ? ` · prossima decodifica tra ${left} s` : ""}`
      + (this._digiNote ? ` · ${this._digiNote}` : "");
  }

  _renderLiveText(info) {
    this._liveTextInfo = info;
    const el = this.shadowRoot.getElementById("liveimg");
    const dec = LIVE_DECODERS.find(([v]) => v === this._live.decoder);
    let box = el.querySelector(".livetext");
    if (!box) {
      el.innerHTML = `<h3 style="margin:10px 0 4px">${dec ? dec[1] : "Testo"}</h3>
        <div class="hint" id="livetextinfo"></div>
        <pre class="livetext"></pre>
        <div class="row"><button id="livetextcopy">📋 Copia</button><button id="livetextclear">Pulisci</button>
          <span class="hint">${this._live.decoder === "cw" ? "Tono e velocità si trovano da soli: sintonizza la portante, in CW stretto (500 Hz) si legge meglio."
            : "I due toni e la polarità si trovano da soli: basta che cadano nel filtro (USB, 2,8 kHz)."}</span></div>`;
      box = el.querySelector(".livetext");
      el.querySelector("#livetextcopy").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(this._liveText || ""); this._msg("Testo copiato."); }
        catch (err) { this._msg("Copia non riuscita: seleziona il testo a mano.", true); }
      });
      el.querySelector("#livetextclear").addEventListener("click", () => { this._liveText = ""; box.textContent = ""; });
    }
    el.querySelector("#livetextinfo").textContent = info || "";
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    box.textContent = this._liveText || "";
    if (atEnd) box.scrollTop = box.scrollHeight;
  }

  _onLiveTv(m) {
    if (!this._live) this._setLive({ freq_mhz: m.freq_mhz, mode: "tv", channel: m.channel });
    if (this._live.mode !== "tv") return;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("livelo").textContent = ((m.freq_mhz * 1e6 - m.span_hz / 2) / 1e6).toFixed(3);
    $("livehi").textContent = ((m.freq_mhz * 1e6 + m.span_hz / 2) / 1e6).toFixed(3);
    $("livelevel").textContent = `livello ${m.level_db} dBFS`;
    this._drawLiveRow(m.row);
    const t = m.tv;
    const trend = this._tvTrend || (this._tvTrend = []);
    trend.push(t ? t.mer_db : null);
    if (trend.length > 150) trend.shift();
    const el = $("liverds");
    if (!t) {
      el.innerHTML = `<div class="hint" style="margin-top:6px">Nessun multiplex TV riconoscibile su ${esc(m.channel)}
        (livello ${m.level_db} dBFS): canale libero, segnale troppo debole o antenna da orientare.</div>
        <canvas class="trend" id="tvtrend"></canvas>`;
      this._drawTvTrend();
      return;
    }
    const need = t.standard === "DVB-T" ? 18 : 17; // come min_mer() in dvbt.py
    const merPct = Math.max(0, Math.min(100, (t.mer_db / 32) * 100));
    const color = t.mer_db >= need + 3 ? "#43a047" : t.mer_db >= need ? "#fb8c00" : "#e53935";
    const lvlPct = Math.max(0, Math.min(100, ((m.level_db + 45) / 45) * 100));
    el.innerHTML = `<h3 style="margin:10px 0 4px">Misuratore di campo · ${esc(t.standard)}</h3>
      <div class="row" style="justify-content:space-between;align-items:baseline">
        <span>Qualità (MER stimato)</span><span class="bignum" style="color:${color}">${t.mer_at_least ? "≥ " : ""}${t.mer_db} dB</span></div>
      <div class="meter"><div style="width:${merPct}%;background:${color}"></div>
        <div class="mark" style="left:${(need / 32) * 100}%" title="minimo per vedere senza blocchi"></div></div>
      <div class="hint">${esc(m.quality || "")} · la tacca segna il minimo per ${esc(t.standard)} (~${need} dB)</div>
      <div class="row" style="justify-content:space-between;margin-top:8px"><span>Livello in ingresso</span><span>${m.level_db} dBFS</span></div>
      <div class="meter"><div style="width:${lvlPct}%;background:#039be5"></div></div>
      <div class="hint">Vicino a 0 dBFS la chiavetta satura: abbassa il guadagno. Molto in basso: alzalo o controlla l'antenna.</div>
      <canvas class="trend" id="tvtrend"></canvas>
      <div class="hint">Andamento della qualità (ultimi ~2 minuti): muovi l'antenna lentamente e fermati dove è più alta.</div>
      <table style="margin-top:6px"><tbody>${tvRows(t).filter(([k]) => k !== "Qualità (MER stimato)")
        .map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>`;
    this._drawTvTrend();
  }

  _drawTvTrend(vmin = 0, vmax = 32, lines = [10, 20, 30], unit = "dB") {
    const c = this.shadowRoot.getElementById("tvtrend");
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    const data = this._tvTrend || [];
    const y = (v) => h - ((Math.max(vmin, Math.min(vmax, v)) - vmin) / (vmax - vmin)) * h;
    g.strokeStyle = "rgba(255,255,255,.25)"; g.setLineDash([4 * dpr, 4 * dpr]);
    for (const v of lines) { g.beginPath(); g.moveTo(0, y(v)); g.lineTo(w, y(v)); g.stroke(); }
    g.setLineDash([]); g.strokeStyle = "#ffd54f"; g.lineWidth = 2 * dpr; g.beginPath();
    let pen = false;
    data.forEach((v, i) => {
      const x = (i / 149) * w;
      if (v === null) { pen = false; return; }
      if (pen) g.lineTo(x, y(v)); else g.moveTo(x, y(v));
      pen = true;
    });
    g.stroke();
    g.fillStyle = "rgba(255,255,255,.6)"; g.font = `${10 * dpr}px sans-serif`;
    for (const v of lines) g.fillText(`${v} ${unit}`, 4 * dpr, y(v) + 11 * dpr);
  }

  _kuText(mhz) {
    // con l'LNB sul cavo la frequenza vera del satellite è quella del cavo + l'oscillatore dell'LNB
    if (!this._lnb || mhz < 950 || mhz > 2150) return "";
    const r = this._sat;
    if (r && r.found) return ` · ${((mhz + r.lo_mhz - r.lo_offset_mhz) / 1000).toFixed(3)} GHz su ${r.satellite}`;
    return ` · ${((mhz + 9750) / 1000).toFixed(3)} GHz (banda bassa) o ${((mhz + 10600) / 1000).toFixed(3)} GHz (alta)`;
  }

  _renderSat() {
    // satellite riconosciuto dalle gobbe dei transponder nella scansione (LNB sul cavo)
    const card = this.shadowRoot.getElementById("satcard");
    if (!card) return;
    const r = this._sat;
    card.style.display = r ? "" : "none";
    if (!r) return;
    const out = this.shadowRoot.getElementById("satout");
    if (!r.found) {
      out.innerHTML = `<div class="hint">${esc(r.reason || "Nessun satellite riconosciuto.")} Controlla che l'LNB sia alimentato e la
        parabola puntata; con la scansione 950–1760 MHz servono almeno 3 transponder visibili.</div>`;
      return;
    }
    const ghz = (f) => (f / 1000).toFixed(3);
    const tps = r.transponders.slice().sort((a, b) => a.if_mhz - b.if_mhz);
    out.innerHTML = `<div class="satname">${r.confident ? "" : "forse "}${esc(r.satellite)}</div>
      <div class="hint">banda ${esc(r.band)} (oscillatore ${r.lo_mhz} MHz) · polarizzazione ${esc(r.polarization)} ·
        ${r.present}/${r.transponders.length} transponder visibili · LNB spostato di ${r.lo_offset_mhz} MHz</div>
      ${r.confident ? "" : `<div class="hint">Riconoscimento incerto: in alternativa ${esc(r.ranking.filter((x) => x.satellite !== r.satellite).slice(0, 2)
        .map((x) => x.satellite).join(" o "))}.</div>`}
      <table style="margin-top:6px"><thead><tr><th>Ku (GHz)</th><th>Cavo (MHz)</th><th class="num">Msym/s</th><th class="num">sopra rumore</th><th></th></tr></thead>
      <tbody>${tps.map((t) => `<tr data-f="${t.if_mhz}"${t.above_db < 3 ? ' style="opacity:.5"' : ""}><td>${ghz(t.ku_mhz)}</td><td>${t.if_mhz.toFixed(0)}</td>
        <td class="num">${(t.sr / 1000).toFixed(1)}</td><td class="num">${t.above_db.toFixed(0)} dB</td>
        <td class="num"><button title="Misuratore continuo del transponder">▶</button></td></tr>`).join("")}</tbody></table>
      <div class="hint" style="margin-top:6px">Transponder del piano di ${esc(r.satellite)} nella banda vista dalla chiavetta (in grigio quelli
        spenti o troppo deboli). ▶ resta sul transponder e ne misura il livello di continuo, per rifinire il puntamento.</div>`;
    out.querySelectorAll("tr[data-f]").forEach((tr) => {
      const f = Number(tr.dataset.f) * 1e6;
      tr.addEventListener("click", (e) => {
        this._select(f);
        if (e.target.closest("button")) this._liveStart(f / 1e6, "sat");
      });
    });
  }

  // ---------------------------------------------------------------- satelliti meteo

  async _loadMeteo(select = null) {
    try {
      const res = await this._hass.callWS({ type: "spettro/meteo_list" });
      this._meteoEntries = res.entries || [];
      if (res.passes) { this._meteoPasses = res.passes; this._renderMeteoPasses(); }
      this._renderMeteoList();
      if (select) this._openMeteo(select);
    } catch (err) {
      /* integrazione precedente: la scheda resta vuota */
    }
  }

  _renderMeteoPasses() {
    const el = this.shadowRoot.getElementById("meteopasses");
    const m = this._meteoPasses;
    if (!el || !m) return;
    const t = (s) => new Date(s * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const dir = (p) => (p.northbound ? "da sud a nord" : "da nord a sud");
    const loaded = m.satellites.filter((x) => x.loaded).map((x) => x.name);
    let head = `Stazione ${m.location[0]}°, ${m.location[1]}° · `
      + (loaded.length ? `orbite di ${loaded.join(", ")}${m.tle_age_h != null ? ` (aggiornate ${m.tle_age_h} h fa)` : ""}` : "orbite non ancora scaricate");
    if (m.tle_error) head += ` · ⚠ ${esc(m.tle_error)}`;
    if (!m.passes.length) { el.innerHTML = `<div class="hint">${head}<br>Nessun passaggio sopra l'elevazione minima nelle prossime 36 ore.</div>`; return; }
    const now = Date.now() / 1000;
    el.className = "";
    el.innerHTML = `<div class="hint">${head}</div>
      <table style="margin-top:4px"><thead><tr><th>Quando</th><th>Satellite</th><th class="num">Elev. max</th><th>Direzione</th><th class="num">Durata</th><th></th></tr></thead>
      <tbody>${m.passes.slice(0, 10).map((p, i) => {
        const on = p.aos - 180 <= now && now < p.los;
        return `<tr${on ? ' style="font-weight:500"' : ""}>
        <td>${t(p.aos)}${p.aos <= now && now < p.los ? " · ora" : ""}</td>
        <td>${esc(p.satellite)}<span class="hint"> · ${p.kind === "iss" ? "SSTV 145,800 MHz" : "foto LRPT 137 MHz"}</span></td><td class="num">${p.max_el}°</td>
        <td>${dir(p)}</td><td class="num">${Math.round((p.los - p.aos) / 60)} min</td>
        <td class="num">${on ? `<button data-i="${i}" title="Ricevi questo passaggio adesso">▶ Ricevi</button>` : ""}</td></tr>`;
      }).join("")}</tbody></table>`;
    el.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", async () => {
      const p = m.passes[+b.dataset.i];
      try {
        if (p.kind === "iss") await this._liveStart(145.8, "nfm", null, "sstv", "ISS");
        else await this._hass.callWS({ type: "spettro/meteo_receive", norad: p.norad });
      } catch (err) { this._msg(err.message || String(err), true); }
    }));
  }

  _renderMeteoLog() {
    // cosa hanno fatto le ricezioni dei satelliti (anche quelle partite da sole mentre non guardavi)
    const el = this.shadowRoot.getElementById("meteolog");
    const log = this._meteoLog || [];
    if (!el) return;
    if (!log.length) { el.innerHTML = ""; return; }
    const t = (x) => new Date(x * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    el.innerHTML = `<div class="hint" style="margin-top:8px">Ultime ricezioni (✋ = avviata a mano):</div>
      <table><tbody>${log.map((e) => `<tr><td>${t(e.start)}${e.auto ? "" : " ✋"}</td>
        <td>${esc(e.satellite)}</td>
        <td class="num">${e.end ? `${Math.max(1, Math.round((e.end - e.start) / 60))} min` : "in corso"}</td>
        <td>${esc(e.result)}</td></tr>`).join("")}</tbody></table>`;
  }

  _onMeteoLive(m) {
    const el = this.shadowRoot.getElementById("meteolive");
    if (m.done) { el.innerHTML = ""; this._meteoPreview = null; return; }
    if (m.preview) this._meteoPreview = m.preview;
    const s = m.stats, p = m.pass;
    const rows = s ? [
      ["Satellite", `${p.satellite} · ${s.freq_mhz} MHz${s.el != null ? ` · elevazione ${s.el}°` : ""}`],
      ["Segnale", s.locked ? `agganciato (righe ${s.line_db} dB sopra il fondo, simboli ${s.snr_db} dB)` : "in ricerca…"],
      ["Doppler", `${s.doppler_hz > 0 ? "+" : ""}${s.doppler_hz} Hz · scarto misurato ${s.offset_hz} Hz`],
      ["Trame valide", `${s.vcdu_ok}${s.vcdu_bad ? ` · ${s.vcdu_bad} perse` : ""}`],
      ["Immagine", `${m.rows || 0} righe${m.composite ? ` · ${m.composite}` : ""}`],
      ["Tempo", `${Math.floor(s.elapsed_s / 60)}:${String(Math.floor(s.elapsed_s % 60)).padStart(2, "0")} · fine alle ${new Date(p.los * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`],
    ] : [];
    el.innerHTML = `<div class="livehead" style="margin-top:8px"><span class="dot">● IN RICEZIONE</span>
        <span class="title">${esc(p.satellite)}</span><button class="primary" id="meteostop">■ Ferma e salva</button></div>
      ${m.error ? `<div class="err">${esc(m.error)}</div>` : ""}
      <table><tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
      ${this._meteoPreview ? `<img class="meteoimg" src="data:image/png;base64,${this._meteoPreview}" alt="Immagine in arrivo">` : ""}`;
    el.querySelector("#meteostop").addEventListener("click", async () => {
      try { await this._hass.callWS({ type: "spettro/meteo_stop" }); } catch (err) { this._msg(err.message || String(err), true); }
    });
  }

  _renderMeteoList() {
    const el = this.shadowRoot.getElementById("meteolist");
    const list = this._meteoEntries || [];
    if (!list.length) { el.className = "hint"; el.textContent = "Nessuna immagine ancora: attiva la ricezione automatica dei passaggi, premi ▶ su un passaggio in corso o ascolta una stazione."; return; }
    el.className = "";
    el.innerHTML = `<table><tbody>${list.map((e) => `<tr data-id="${esc(e.id)}"${e.id === this._meteoSel ? ' class="sel"' : ""}>
      <td>${new Date(e.timestamp * 1000).toLocaleString([], { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
      <td>${esc(e.satellite)}</td><td class="num">${e.max_el != null ? `${e.max_el}°` : e.source === "fax" ? "radiofax" : "SSTV"}</td>
      <td class="num">${e.rows} righe</td></tr>`).join("")}</tbody></table>`;
    el.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => this._openMeteo(tr.dataset.id)));
  }

  async _openMeteo(id, ir = false) {
    this._meteoSel = id;
    this.shadowRoot.querySelectorAll("#meteolist tr[data-id]").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
    const view = this.shadowRoot.getElementById("meteoview");
    view.innerHTML = `<div class="hint">Caricamento dell'immagine…</div>`;
    try {
      const r = await this._hass.callWS({ type: "spettro/meteo_get", entry_id: id, ir });
      const when = new Date(r.timestamp * 1000).toLocaleString();
      const meteor = r.max_el != null;
      view.innerHTML = `<div class="meteogrid hint">
          <span>${esc(r.satellite)} · ${when}</span>
          ${meteor ? `<span>elevazione max ${r.max_el}° · ${r.northbound ? "da sud a nord" : "da nord a sud"}</span>` : `<span>${r.complete ? "completa" : "interrotta"}</span>`}
          <span>${r.rows} righe · ${esc(ir ? "infrarosso (canale 5)" : r.composite)}</span>
          <span>${meteor ? `${r.vcdu_ok} trame valide su ${r.freq_mhz} MHz` : `${r.freq_mhz} MHz`}</span></div>
        <div class="row">
          ${r.has_ir ? `<button id="meteoir">${ir ? "Visibile" : "Infrarosso"}</button>` : ""}
          <a id="meteodl" download="${esc(id)}${ir ? "-ir" : ""}.png"><button>Scarica</button></a>
          <button id="meteodel">Cancella</button>
        </div>
        <img class="meteoimg" src="data:image/png;base64,${r.png}" alt="Immagine del satellite meteo">`;
      const dl = view.querySelector("#meteodl");
      dl.href = `data:image/png;base64,${r.png}`;
      const irb = view.querySelector("#meteoir");
      if (irb) irb.addEventListener("click", () => this._openMeteo(id, !ir));
      view.querySelector("#meteodel").addEventListener("click", async () => {
        if (!confirm("Cancellare questa immagine?")) return;
        await this._hass.callWS({ type: "spettro/meteo_delete", entry_id: id });
        view.innerHTML = "";
      });
    } catch (err) {
      view.innerHTML = `<div class="err">${esc(err.message || err.code || err)}</div>`;
    }
  }

  _onLiveImage(m) {
    if (!this._live || !this._live.decoder) return;
    if (m.preview) this._liveImgPreview = m.preview;
    const el = this.shadowRoot.getElementById("liveimg");
    const fax = m.decoder === "fax";
    const st = {
      attesa: fax ? "In attesa del tono di avvio di una carta…" : "In attesa di un'immagine (codice VIS)…",
      avvio: "Tono di avvio ricevuto: sta per iniziare una carta",
      fasatura: "Allineamento delle righe (fasatura)…",
      immagine: "Ricezione della carta",
      ricezione: "Ricezione dell'immagine",
    }[m.state] || m.state;
    const prog = m.height ? ` · ${m.rows}/${m.height} righe (${Math.round((100 * m.rows) / m.height)}%)` : m.rows ? ` · ${m.rows} righe` : "";
    const until = this._live.until ? ` · fino alle ${new Date(this._live.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
    el.innerHTML = `<h3 style="margin:10px 0 4px">${fax ? "Radiofax" : "SSTV"}${this._live.title ? ` · ${esc(this._live.title)}` : ""}</h3>
      <div class="hint">${esc(st)}${m.mode && m.state !== "attesa" ? ` · ${esc(m.mode)}` : ""}${prog}${until}</div>
      ${m.saved ? `<div class="hint" style="color:var(--success-color,#43a047)">✓ Immagine salvata in «Immagini ricevute».</div>` : ""}
      ${this._liveImgPreview && m.state !== "attesa" ? `<img class="meteoimg" src="data:image/png;base64,${this._liveImgPreview}" alt="Immagine in arrivo">` : ""}
      <div class="hint" style="margin-top:4px">Continua anche a pannello chiuso: le immagini finiscono nella galleria della scheda Satelliti meteo e immagini.</div>`;
    if (m.saved) this._loadMeteo();
  }

  _onLiveSat(m) {
    if (!this._live) this._setLive({ freq_mhz: m.freq_mhz, mode: "sat" });
    if (this._live.mode !== "sat") return;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("livelo").textContent = ((m.freq_mhz * 1e6 - m.span_hz / 2) / 1e6).toFixed(3);
    $("livehi").textContent = ((m.freq_mhz * 1e6 + m.span_hz / 2) / 1e6).toFixed(3);
    $("livelevel").textContent = `livello ${m.level_db} dBFS`;
    this._drawLiveRow(m.row);
    const trend = this._tvTrend || (this._tvTrend = []);
    trend.push(m.level_db);
    if (trend.length > 150) trend.shift();
    this._satMax = Math.max(this._satMax ?? -999, m.level_db);
    const pct = Math.max(0, Math.min(100, ((m.level_db + 50) / 50) * 100));
    const maxPct = Math.max(0, Math.min(100, ((this._satMax + 50) / 50) * 100));
    $("liverds").innerHTML = `<h3 style="margin:10px 0 4px">Puntamento parabola</h3>
      <div class="row" style="justify-content:space-between;align-items:baseline">
        <span>Livello del transponder</span><span class="bignum">${m.level_db.toFixed(1)} dBFS</span></div>
      <div class="meter"><div style="width:${pct}%;background:#039be5"></div>
        <div class="mark" style="left:${maxPct}%" title="massimo raggiunto"></div></div>
      <div class="hint">La tacca è il massimo raggiunto (${this._satMax.toFixed(1)} dBFS): sposta la parabola di pochi millimetri alla volta,
        aspetta un paio di secondi e torna dove il livello era più alto. Vicino a 0 dBFS la chiavetta satura: abbassa il guadagno.</div>
      <canvas class="trend" id="tvtrend"></canvas>
      <div class="hint">Andamento del livello (ultimi ~2 minuti).</div>`;
    this._drawTvTrend(-50, 0, [-40, -30, -20, -10], "dBFS");
  }

  _renderTv() {
    // multiplex TV nella scansione: livello medio del canale sopra il rumore
    const card = this.shadowRoot.getElementById("tvcard");
    const sp = this._spec;
    if (!card || !sp || !sp.values || !sp.values.length) return;
    const found = [];
    for (const [ch, fc] of TV_UHF) {
      if (fc - 3.5e6 < sp.range_start || fc + 3.5e6 > sp.range_stop) continue;
      const i0 = Math.max(0, Math.round((fc - 3.5e6 - sp.f_start) / sp.bin_hz));
      const i1 = Math.min(sp.values.length, Math.round((fc + 3.5e6 - sp.f_start) / sp.bin_hz));
      if (i1 - i0 < 3) continue;
      const v = sp.values.slice(i0, i1).sort((a, b) => a - b);
      const above = v[Math.floor(v.length / 2)] - sp.floor_db;
      if (above >= 3) found.push([ch, fc, above]);
    }
    const covered = TV_UHF.some(([, fc]) => fc > sp.range_start && fc < sp.range_stop);
    card.style.display = covered ? "" : "none";
    if (!covered) return;
    const list = this.shadowRoot.getElementById("tvlist");
    if (!found.length) { list.innerHTML = `<div class="hint">Nessun multiplex TV sopra il rumore nell'intervallo scansionato.</div>`; return; }
    list.innerHTML = found.map(([ch, fc, a]) => `<div class="tvrow" data-f="${fc}">
        <span>UHF ${ch}</span>
        <div class="tvbar"><div style="width:${Math.min(100, (a / 40) * 100)}%;background:${a >= 15 ? "#43a047" : a >= 8 ? "#fb8c00" : "#e53935"}"></div></div>
        <span class="num">${a.toFixed(0)} dB</span>
        <button title="Misuratore continuo del canale ${ch}">▶</button></div>`).join("");
    list.querySelectorAll(".tvrow").forEach((row) => {
      const f = Number(row.dataset.f);
      row.addEventListener("click", (e) => {
        if (e.target.closest("button")) { this._select(f); this._liveStart(f / 1e6, "tv"); return; }
        this._select(f);
      });
    });
  }

  _playPcm(b64) {
    const ctx = this._audioCtx;
    const bin = atob(b64);
    const n = bin.length >> 1;
    const buf = ctx.createBuffer(1, n, 32000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < n; i++) {
      let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
      if (v >= 32768) v -= 65536;
      ch[i] = v / 32768;
    }
    this._schedule(buf);
  }

  _schedule(buf, offset = 0, speed = 1) {
    const ctx = this._audioCtx;
    const now = ctx.currentTime;
    // riserva contro i ritardi di rete e di calcolo: se l'audio resta senza dati la riserva cresce
    // (fino a 4 s); se il ritardo accumulato è troppo si torna alla riserva scelta
    const live = this._live;
    if (!this._lead) this._lead = live && live.mode === "dab" ? 1.5 : 0.5;
    if (this._nextT < now + 0.02) {
      if (this._nextT > 0) { this._lead = Math.min(this._lead + 0.5, 4); this._underruns = (this._underruns || 0) + 1; }
      this._nextT = now + this._lead;
    } else if (this._nextT > now + this._lead + 2.5) {
      this._nextT = now + this._lead;
    }
    const src = ctx.createBufferSource();
    src.playbackRate.value = speed;
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(this._nextT, offset);
    this._nextT += (buf.duration - offset) / speed;
  }

  // ---------------------------------------------------------------- DAB+ in diretta

  _onLiveDab(m) {
    if (!this._live) this._setLive({ freq_mhz: m.freq_mhz, mode: "dab", sid: m.sid });
    const st = this._dabState || (this._dabState = { info: null, stats: null, dls: null, error: null, audioErr: null, dlsHistory: [] });
    const $ = (id) => this.shadowRoot.getElementById(id);
    if (m.row) {
      $("livelo").textContent = ((m.freq_mhz * 1e6 - 1.024e6) / 1e6).toFixed(3);
      $("livehi").textContent = ((m.freq_mhz * 1e6 + 1.024e6) / 1e6).toFixed(3);
      $("livelevel").textContent = `livello ${m.level_db} dBFS`;
      this._drawLiveRow(m.row);
    }
    if (m.info) {
      st.info = m.info;
      // il preferito della radio prende il nome trasmesso, se non è stato scritto a mano
      const fav = this._live && this._favFor(this._live.freq_mhz, this._live.sid);
      if (fav && fav.source !== "user" && m.info.service && fav.name !== m.info.service) {
        this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: fav.freq_mhz, sid: fav.sid, name: m.info.service, source: "dab" }).catch(() => {});
      }
      if (this._live && m.info.service) { this._live.service = m.info.service; this._updateFavBtn(); }
    }
    if (m.error) st.error = m.error;
    if (m.stats) st.stats = m.stats;
    if (m.dls !== undefined && m.dls !== null) {
      st.dls = m.dls;
      if (!st.dlsHistory.length || st.dlsHistory[0].text !== m.dls) {
        st.dlsHistory.unshift({ text: m.dls, time: new Date() });
        st.dlsHistory = st.dlsHistory.slice(0, 20);
      }
    }
    if (m.aus && m.config) this._dabAudio(m.config, m.aus);
    if (m.slide) {
      const img = this.shadowRoot.getElementById("liveslide");
      img.src = `data:${m.slide.mime};base64,${m.slide.data}`;
      img.style.display = "";
    }
    st.synced = m.synced;
    this._liveTitle();
    this._renderDabLive();
  }

  _renderDabLive() {
    const st = this._dabState;
    const el = this.shadowRoot.getElementById("liverds");
    if (!st) return;
    if (st.error) { el.innerHTML = `<div class="err" style="margin-top:6px">${esc(st.error)}</div>`; return; }
    if (!st.synced || !st.info) { el.innerHTML = `<div class="hint" style="margin-top:6px">Aggancio del multiplex DAB…</div>`; return; }
    const s = st.stats;
    const tot = s ? s.au_ok + s.au_bad : 0;
    const rows = [
      ["Radio", st.info.service || "—"],
      ["Multiplex", st.info.ensemble || "—"],
      ["Velocità", st.info.bitrate ? `${st.info.bitrate} kbit/s · ${st.info.protection || ""}` : "—"],
      ["Testo (DLS)", st.dls || "—"],
      ["Pacchetti audio integri", tot ? `${s.au_ok}/${tot} (${Math.round((100 * s.au_ok) / tot)}%)` : "in attesa…"],
      ["Correzioni Reed-Solomon", s ? `${s.rs_fixed} byte${s.rs_failed ? ` · ${s.rs_failed} blocchi persi` : ""}` : "—"],
      ["Carico su Home Assistant", s && s.load ? `${Math.round(s.load * 100)}% del tempo reale${s.load > 0.9 ? " (al limite: audio a scatti)" : ""}` : "—"],
      ["Riserva audio", this._listen ? `${(this._lead || 1.5).toFixed(1)} s${this._underruns ? ` · ${this._underruns} interruzioni` : ""}` : "—"],
    ];
    el.innerHTML = `<h3 style="margin:10px 0 4px">DAB+ in diretta</h3>
      <table><tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
      ${st.audioErr ? `<div class="err" style="margin-top:6px">${esc(st.audioErr)}</div>` : ""}
      ${st.audioNote && !st.audioErr ? `<div class="hint" style="margin-top:6px">${esc(st.audioNote)}</div>` : ""}
      ${st.dlsHistory.length ? `<h3 style="margin:10px 0 4px">Testi ricevuti</h3><div id="livert">${st.dlsHistory
        .map((r) => `<div><span>${r.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>${esc(r.text)}</div>`).join("")}</div>` : ""}`;
  }

  _closeDec() {
    if (this._dec) { try { this._dec.close(); } catch (e) { /* già chiuso */ } }
    this._wa = null;
    this._decMode = null;
    this._dec = null;
    this._decKey = null;
    this._decQueue = [];
  }

  async _dabAudio(cfg, aus) {
    // il decoder AAC del browser (WebCodecs): AAC con blocchi da 960 campioni, SBR e PS dalla configurazione;
    // se il browser rifiuta l'SBR a 960 campioni si passa al solo nucleo AAC (piano B)
    if (this._dabFallback && cfg.fallback) cfg = cfg.fallback;
    this._dabCfg = cfg;
    const key = cfg.asc + cfg.codec;
    if (this._decKey !== key) {
      this._closeDec();
      this._decKey = key;
      this._decQueue = [];
      this._decReady = false;
      this._setupDec(cfg);
    }
    if (!this._decReady) { this._decQueue.push(...aus); this._decQueue = this._decQueue.slice(-100); return; }
    for (const a of aus) this._decodeAu(a);
  }

  async _setupDec(cfg) {
    const st = this._dabState;
    if (!("AudioDecoder" in window)) {
      // WebCodecs c'è solo su pagine sicure (https): con http si decodifica con decodeAudioData
      if (this._setupWebAudio(cfg)) return;
      if (st) st.audioErr = "Questo browser non può suonare l'audio DAB+.";
      return;
    }
    const desc = new Uint8Array(cfg.asc.match(/../g).map((h) => parseInt(h, 16)));
    let conf = null;
    for (const codec of [...new Set([cfg.codec, "mp4a.40.5", "mp4a.40.2"])]) {
      const c = { codec, sampleRate: cfg.sample_rate, numberOfChannels: cfg.channels, description: desc };
      try { if ((await AudioDecoder.isConfigSupported(c)).supported) { conf = c; break; } } catch (e) { /* prova il prossimo */ }
    }
    if (!conf) {
      if (this._setupWebAudio(cfg)) return;
      if (this._dabTryFallback()) return;
      if (st) st.audioErr = "Il browser non sa decodificare questo audio HE-AAC del DAB+.";
      return;
    }
    this._dec = new AudioDecoder({
      output: (ad) => this._playAudioData(ad),
      error: (e) => {
        this._dec = null; this._decKey = null;
        if (this._dabTryFallback()) return;
        if (this._dabState) this._dabState.audioErr = `Errore del decoder audio del browser: ${e.message}`;
      },
    });
    this._dec.configure(conf);
    this._decTs = 0;
    this._decUs = (cfg.samples_per_au / cfg.sample_rate) * 1e6;
    this._decReady = true;
    const q = this._decQueue; this._decQueue = [];
    for (const a of q) this._decodeAu(a);
  }

  // piano B: il prossimo pacchetto riconfigura il decoder con il solo nucleo AAC
  _dabTryFallback() {
    const cfg = this._dabCfg;
    if (this._dabFallback || !cfg || !cfg.sbr) return false;
    this._dabFallback = true;
    this._closeDec();
    if (this._dabState) this._dabState.audioNote = "Audio a qualità ridotta: il browser non supporta l'SBR del DAB+ (acuti fino a ~12 kHz, mono).";
    return true;
  }

  // senza WebCodecs (pagine http): blocchi di ~0,5 s impacchettati in MP4 e decodificati con decodeAudioData,
  // sovrapposti di 2 pacchetti per non avere stacchi tra un blocco e l'altro
  _setupWebAudio(cfg) {
    if (!window.AudioContext && !window.webkitAudioContext) return false;
    const asc = new Uint8Array(cfg.asc.match(/../g).map((h) => parseInt(h, 16)));
    this._wa = { cfg, init: mp4Init(asc, cfg.sample_rate, cfg.channels), prev: [], pending: [], busy: false };
    this._decMode = "webaudio";
    this._decReady = true;
    const q = this._decQueue; this._decQueue = [];
    for (const a of q) this._decodeAu(a);
    return true;
  }

  async _waDecode() {
    const w = this._wa;
    if (!w || w.busy || w.pending.length < 12) return;
    if (!this._listen || !this._audioCtx) { w.pending = []; w.prev = []; return; }
    w.busy = true;
    const take = w.pending.splice(0);
    const overlap = w.prev;
    w.prev = take.slice(-2);
    const aus = overlap.concat(take);
    const file = concatU8([w.init, mp4Fragment(1, 0, aus, w.cfg.samples_per_au)]);
    try {
      const buf = await this._audioCtx.decodeAudioData(file.buffer);
      if (this._wa === w) this._playDecoded(buf, aus.length, overlap.length, w.cfg);
    } catch (e) {
      if (this._wa === w && !this._dabTryFallback() && this._dabState) {
        this._dabState.audioErr = "Il browser non riesce a decodificare l'audio DAB+.";
      }
    }
    w.busy = false;
    this._waDecode();
  }

  // ogni pacchetto dura sempre samples_per_au / sample_rate (40 ms con SBR): se il decoder salta l'SBR
  // restituisce metà dei campioni e si suona a metà velocità, che è quella giusta
  _speedFor(decodedSeconds, aus, cfg) {
    const ratio = decodedSeconds / (aus * cfg.samples_per_au / cfg.sample_rate);
    if (ratio < 0.75 && this._dabState && !this._dabState.audioNote) {
      this._dabState.audioNote = "Audio a qualità ridotta: il browser non applica l'SBR del DAB+ (acuti fino a ~12 kHz).";
    }
    return ratio < 0.75 ? 0.5 : 1;
  }

  _playDecoded(buf, aus, skipAus, cfg) {
    const speed = this._speedFor(buf.duration, aus, cfg);
    const skip = (skipAus / aus) * buf.duration;
    this._schedule(buf, skip, speed);
  }

  _decodeAu(b64) {
    if (this._decMode === "webaudio") {
      if (!this._wa) return;
      const bin = atob(b64);
      const data = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) data[i] = bin.charCodeAt(i);
      this._wa.pending.push(data);
      this._waDecode();
      return;
    }
    if (!this._dec || this._dec.state !== "configured") return;
    const bin = atob(b64);
    const data = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) data[i] = bin.charCodeAt(i);
    try {
      this._dec.decode(new EncodedAudioChunk({ type: "key", timestamp: this._decTs, data }));
    } catch (e) {
      if (this._dabState) this._dabState.audioErr = `Errore del decoder audio del browser: ${e.message}`;
    }
    this._decTs += this._decUs;
  }

  _playAudioData(ad) {
    if (!this._listen || !this._audioCtx) { ad.close(); return; }
    const n = ad.numberOfFrames, chs = ad.numberOfChannels;
    const cfg = this._dabCfg;
    const speed = cfg ? this._speedFor(n / ad.sampleRate, 1, cfg) : 1;
    const buf = this._audioCtx.createBuffer(chs, n, ad.sampleRate * speed);
    for (let c = 0; c < chs; c++) {
      const arr = new Float32Array(n);
      ad.copyTo(arr, { planeIndex: c, format: "f32-planar" });
      buf.copyToChannel(arr, c);
    }
    ad.close();
    this._schedule(buf);
  }

  _drawLiveRow(row) {
    const c = this.shadowRoot.getElementById("livewf");
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; this._liveRows = null; }
    const ctx = c.getContext("2d");
    if (!this._liveRows) { ctx.fillStyle = "#080820"; ctx.fillRect(0, 0, w, h); this._liveRows = 0; this._liveScale = null; }
    // scala dei colori: dal rumore (mediana) al massimo recente
    const sorted = row.slice().sort((a, b) => a - b);
    const lo = sorted[Math.floor(sorted.length / 2)] - 3, hi = sorted[sorted.length - 1] + 3;
    const sc = this._liveScale;
    this._liveScale = sc ? [0.9 * sc[0] + 0.1 * lo, Math.max(hi, 0.95 * sc[1] + 0.05 * hi)] : [lo, Math.max(hi, lo + 20)];
    const [vmin, vmax] = this._liveScale;
    const rh = Math.max(1, Math.round(3 * dpr)); // 4 righe al secondo: 160 px ≈ 13 s
    ctx.drawImage(c, 0, 0, w, h - rh, 0, rh, w, h - rh); // la cascata scende di una riga
    const img = ctx.createImageData(w, 1);
    for (let x = 0; x < w; x++) {
      const v = row[Math.min(row.length - 1, Math.floor((x / w) * row.length))];
      const t = Math.max(0, Math.min(255, Math.round(((v - vmin) / (vmax - vmin)) * 255)));
      img.data[4 * x] = LUT[3 * t]; img.data[4 * x + 1] = LUT[3 * t + 1]; img.data[4 * x + 2] = LUT[3 * t + 2]; img.data[4 * x + 3] = 255;
    }
    for (let y = 0; y < rh; y++) ctx.putImageData(img, 0, y);
    this._liveRows++;
  }

  _onLiveRds(d) {
    if (!this._live) return;
    const el = this.shadowRoot.getElementById("liverds");
    if (!d) {
      if (!this._liveRds) el.innerHTML = `<div class="hint" style="margin-top:6px">Nessun dato RDS ricevuto (stazione senza RDS o segnale debole).</div>`;
      return;
    }
    this._liveRds = d;
    // storico dei testi: si aggiunge solo un testo completo e diverso dall'ultimo
    if (d.rt && d.rt_complete && (!this._rtHistory.length || this._rtHistory[0].text !== d.rt)) {
      this._rtHistory.unshift({ text: d.rt, time: new Date() });
      this._rtHistory = this._rtHistory.slice(0, 20);
    }
    // il preferito prende il nome RDS della stazione, ma un nome scritto a mano resta
    const fav = this._favFor(this._live.freq_mhz);
    if (fav && fav.source !== "user" && d.ps && d.ps_complete && fav.name !== d.ps) {
      this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: fav.freq_mhz, name: d.ps, source: "rds" }).catch(() => {});
    }
    this._liveTitle();
    const rows = [
      ["Stazione", d.ps ? d.ps + (d.ps_complete ? "" : " (incompleto)") : "—"],
      ["Testo", d.rt ? d.rt + (d.rt_complete ? "" : " (incompleto)") : "—"],
      ["Programma", d.pty],
      ["Codice PI", d.pi || "—"],
      ...(d.ct ? [["Ora trasmessa", d.ct]] : []),
      ...(d.af.length ? [["Altre frequenze", d.af.map((f) => f.toFixed(1)).join(", ") + " MHz"]] : []),
      ["Stereo", d.stereo ? "sì (pilota 19 kHz)" : "no"],
      ["Traffico", d.tp ? (d.ta ? "notiziario sul traffico in corso" : "stazione con info traffico") : "—"],
    ];
    el.innerHTML = `<h3 style="margin:10px 0 4px">RDS in diretta</h3>
      <table><tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
      ${this._rtHistory.length ? `<h3 style="margin:10px 0 4px">Testi ricevuti</h3><div id="livert">${this._rtHistory
        .map((r) => `<div><span>${r.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>${esc(r.text)}</div>`).join("")}</div>` : ""}`;
  }

  // ---------------------------------------------------------------- analisi

  _select(f) {
    this._sel = f;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("selinfo").textContent = `Selezionato: ${(f / 1e6).toFixed(4)} MHz${this._kuText(f / 1e6)}`;
    if (this.shadowRoot.activeElement !== $("selfreq")) $("selfreq").value = (f / 1e6).toFixed(4);
    $("analyze").disabled = !!this._analyzing || !!this._live;
    $("livebtn").disabled = false;
    $("fav").disabled = false;
    this._updateFavBtn();
    this._draw();
  }

  async _analyze() {
    if (this._sel === null || this._analyzing) return;
    const $ = (id) => this.shadowRoot.getElementById(id);
    this._analyzing = true;
    $("analyze").disabled = true;
    $("analyze").textContent = "Registrazione…";
    $("anaout").innerHTML = `<div class="hint">Registrazione e analisi di ${(this._sel / 1e6).toFixed(4)} MHz…</div>`;
    try {
      this._ana = await this._hass.callWS({
        type: "spettro/analyze", freq_mhz: this._sel / 1e6, // durata dalle impostazioni (Avanzate)
      });
      this._renderAnalysis();
      this._archSel = this._ana.id || null; // nell'archivio si evidenzia l'analisi appena fatta
      this._renderArchive();
    } catch (err) {
      $("anaout").innerHTML = `<div class="err">Analisi non riuscita: ${esc(err.message || err.code || err)}</div>`;
    } finally {
      this._analyzing = false;
      $("analyze").disabled = this._sel === null;
      $("analyze").textContent = "Analizza";
    }
  }

  async _loadArchive() {
    try {
      const res = await this._hass.callWS({ type: "spettro/archive_list" });
      this._archive = res.entries;
      this._archHours = res.hours;
      this._renderArchive();
    } catch (err) {
      /* integrazione vecchia o non caricata: l'archivio resta vuoto */
    }
  }

  _renderArchive() {
    const el = this.shadowRoot.getElementById("archive");
    const info = this.shadowRoot.getElementById("archinfo");
    if (!el) return;
    const hours = this._archHours ?? (this._status && this._status.settings && this._status.settings.archive_hours);
    const bytes = this._archive.reduce((t, e) => t + (e.bytes || 0), 0);
    const size = bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1e3)} kB`;
    info.textContent = `${this._archive.length} analisi · ${size} su disco · le più vecchie di ${hours ?? "?"} ore vengono cancellate`;
    if (!this._archive.length) {
      el.className = "hint";
      this.shadowRoot.getElementById("archfilter").style.display = "none";
      el.textContent = "Nessuna analisi archiviata. Attiva \"Analizza subito i picchi trovati\" o usa Analizza.";
      return;
    }
    const fmt = (t) => {
      const d = new Date(t * 1000), today = new Date().toDateString() === d.toDateString();
      return (today ? "" : d.toLocaleDateString([], { day: "2-digit", month: "2-digit" }) + " ")
        + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    };
    // filtro per tipo, con il numero di analisi di ciascuno
    const sel = this.shadowRoot.getElementById("archfilter");
    const counts = {};
    for (const e of this._archive) { const c = archCat(e.kind); counts[c] = (counts[c] || 0) + 1; }
    if (this._archFilter !== "all" && !counts[this._archFilter]) this._archFilter = "all";
    sel.style.display = "";
    sel.innerHTML = `<option value="all">Tutti i tipi (${this._archive.length})</option>`
      + ARCH_CATS.filter(([k]) => counts[k]).map(([k, t]) => `<option value="${k}">${t} (${counts[k]})</option>`).join("");
    sel.value = this._archFilter;
    const list = this._archFilter === "all" ? this._archive : this._archive.filter((e) => archCat(e.kind) === this._archFilter);
    const { key, desc } = this._archSort;
    const rows = list.slice().sort((a, b) => (desc ? b[key] - a[key] : a[key] - b[key]));
    const th = (k, label, cls = "") => `<th class="sort ${cls}" data-key="${k}">${label}${k === key ? (desc ? " ▼" : " ▲") : ""}</th>`;
    const top = el.scrollTop;
    el.className = "";
    el.innerHTML = `<table><thead><tr>${th("timestamp", "Ora")}${th("target_mhz", "MHz")}${th("snr_db", "SNR", "num")}</tr></thead>
      ${rows.map((e) => `<tbody class="entry${e.id === this._archSel ? " sel" : ""}" data-id="${esc(e.id)}"><tr>
        <td>${fmt(e.timestamp)}${e.auto ? "" : " ✋"}</td><td>${e.target_mhz.toFixed(4)}</td><td class="num" title="${e.scan_snr_db != null ? `Nella scansione il picco stava ${e.scan_snr_db} dB sopra il rumore della zona; l'analisi misura il segnale da vicino` : ""}">${e.snr_db} dB${e.scan_snr_db != null ? `<span class="scan"> · ${e.scan_snr_db}</span>` : ""}</td></tr>
        <tr><td colspan="3" class="kind">${esc(e.kind)}</td></tr></tbody>`).join("")}
      </table>
      <div class="hint" style="margin-top:6px">✋ = analisi avviata a mano.</div>`;
    el.scrollTop = top;
    el.querySelectorAll("th.sort").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.key;
      // primo tocco: data dalla più recente, frequenza crescente, SNR dal più forte; secondo tocco: al contrario
      this._archSort = k === this._archSort.key ? { key: k, desc: !this._archSort.desc } : { key: k, desc: k !== "target_mhz" };
      try { localStorage.setItem("spettro.archSort", JSON.stringify(this._archSort)); } catch (e) { /* non disponibile */ }
      this._renderArchive();
    }));
    el.querySelectorAll("tbody[data-id]").forEach((tr) => tr.addEventListener("click", () => {
      const e = this._archive.find((x) => x.id === tr.dataset.id);
      if (e) {
        const f = e.target_mhz * 1e6;
        this._select(f);
        // ingrandisce il grafico sul segnale, se è nell'intervallo scansionato
        const sp = this._spec;
        if (sp && f >= sp.range_start && f <= sp.range_stop) {
          const half = Math.max(50e3, (sp.range_stop - sp.range_start) * 0.02);
          this._view = [Math.max(sp.range_start, f - half), Math.min(sp.range_stop, f + half)];
          this._draw();
        }
      }
      this._openArchived(tr.dataset.id, true);
    }));
  }

  async _openArchived(id, scroll = false) {
    this._archSel = id;
    this.shadowRoot.querySelectorAll("#archive tbody.entry").forEach((b) => b.classList.toggle("sel", b.dataset.id === id));
    const out = this.shadowRoot.getElementById("anaout");
    out.innerHTML = `<div class="hint">Caricamento dell'analisi…</div>`;
    if (scroll) this.shadowRoot.querySelector(".ana").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      this._ana = await this._hass.callWS({ type: "spettro/archive_get", entry_id: id });
      this._renderAnalysis();
    } catch (err) {
      out.innerHTML = `<div class="err">${esc(err.message || err.code || err)}</div>`;
    }
  }

  _renderAnalysis() {
    const r = this._ana;
    const out = this.shadowRoot.getElementById("anaout");
    const im = r.image;
    const pct = (v) => `${Math.round(v * 100)}%`;
    const rows = [
      ["Tipo probabile", r.kind],
      ["Frequenza centrale", `${r.center_mhz.toFixed(4)} MHz`],
      ["Picco", `${r.peak_mhz.toFixed(4)} MHz · ${r.peak_db} dBFS`],
      ["Sopra il rumore", `${r.snr_db} dB${r.detected ? "" : " (nessun segnale chiaro)"}`],
      ...(r.scan_snr_db != null ? [["Nella scansione", `${r.scan_snr_db} dB sopra il rumore della zona`]] : []),
      ["Banda occupata 99%", `${r.obw_99_khz} kHz`],
      ["Banda a −3 dB", `${r.bw_3db_khz} kHz`],
      ...(r.tones >= 1 && r.tones <= 4 ? [["Toni distinti", r.tones === 2 ? "2 (FSK?)" : r.tones]] : []),
      ["Nel tempo", r.continuous ? "trasmissione continua" : `${pct(r.duty_cycle)} attivo · ${r.bursts} burst`],
    ];
    if (!r.continuous && r.bursts) {
      rows.push(["Durata burst", `${r.burst_ms_min}–${r.burst_ms_max} ms (mediana ${r.burst_ms_median})`]);
      if (r.burst_period_ms) rows.push(["Ripetizione", `ogni ~${r.burst_period_ms} ms`]);
    }
    // RDS delle stazioni FM: nome, testo, tipo di programma…
    const d = r.rds;
    const rdsRows = d ? [
      ["Stazione", d.ps ? d.ps + (d.ps_complete ? "" : " (incompleto)") : "—"],
      ["Testo", d.rt ? d.rt + (d.rt_complete === false ? " (incompleto)" : "") : "—"],
      ["Programma", d.pty],
      ["Codice PI", d.pi || "—"],
      ...(d.ct ? [["Ora trasmessa", d.ct]] : []),
      ...(d.af.length ? [["Altre frequenze", d.af.map((f) => f.toFixed(1)).join(", ") + " MHz"]] : []),
      ["Stereo", d.stereo ? "sì (pilota 19 kHz)" : "no"],
      ["Traffico", d.tp ? (d.ta ? "notiziario sul traffico in corso" : "stazione con info traffico") : "—"],
    ] : [];
    const rdsHtml = d ? `<div class="rds"><h3 style="margin:8px 0 4px">RDS</h3>
        <table><tbody>${rdsRows.map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
        <div class="hint" style="margin-top:4px">${d.groups} gruppi RDS ricevuti${d.ps_complete && d.rt ? "" : " · con «Durata analisi» più lunga arrivano più dati"}</div></div>`
      : r.audio_mode === "wfm" ? `<div class="hint" style="margin-top:6px">Nessun dato RDS ricevuto (stazione senza RDS o segnale troppo debole).</div>` : "";
    // DAB: nome del multiplex e radio che contiene
    const m = r.dab;
    let dabHtml = "";
    if (r.dab_channel) {
      const head = `<h3 style="margin:8px 0 4px">DAB · canale ${esc(r.dab_channel)}</h3>`;
      if (!m) dabHtml = head + `<div class="hint">Nessuna trama DAB riconosciuta (segnale debole o non DAB).</div>`;
      else if (m.error) dabHtml = head + `<div class="hint">${esc(m.error)}</div>`;
      else if (!m.fib_ok) dabHtml = head + `<div class="hint">Trame DAB trovate ma dati illeggibili (segnale debole o disturbato).</div>`;
      else {
        dabHtml = head + `<table><tbody>
            <tr><td>Multiplex</td><td class="num">${esc(m.ensemble || "—")}</td></tr>
            <tr><td>Codice EId</td><td class="num">${esc(m.eid || "—")}</td></tr>
            <tr><td>Scarto di frequenza</td><td class="num">${(m.freq_offset_hz / 1000).toFixed(1)} kHz</td></tr>
          </tbody></table>
          ${m.services.length ? `<h3 style="margin:8px 0 4px">Servizi (${m.services.length})</h3>
          <table><tbody>${m.services.map((x) => `<tr><td>${esc(x.name)}</td>
            <td class="num">${esc(x.type)}${x.bitrate ? ` · ${x.bitrate} kbit/s` : ""}</td>
            <td class="num">${x.type === "DAB+"
              ? `<button class="dabplay" data-sid="${esc(x.sid)}" title="Ascolta in diretta">▶</button>` : ""}</td></tr>`).join("")}</tbody></table>
          <div class="hint" style="margin-top:4px">▶ ascolta una radio DAB+ in diretta (scansione sospesa).</div>` : ""}
          <div class="hint" style="margin-top:4px">${m.fib_ok}/${m.fib_total} blocchi informativi integri in ${m.frames} trame${m.fib_ok < m.fib_total * 0.7 ? " · segnale al limite" : ""}</div>`;
      }
    }
    // TV digitale: modalità, guardia e qualità del multiplex
    const t = r.tv;
    const tvHtml = t ? `<h3 style="margin:8px 0 4px">TV digitale · canale ${esc(r.tv_channel)}</h3>
        <table><tbody>${tvRows(t).map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
        <div class="row" style="margin-top:6px"><button id="tvlive">▶ Misura continua</button></div>
        <div class="hint" style="margin-top:4px">La chiavetta riceve ~2 MHz degli 8 del multiplex: si misura il segnale,
          ma i programmi non si possono vedere.</div>` : "";
    const states = this._hass.states;
    const ais = Object.keys(states).filter((e) => e.startsWith("ai_task.")).sort();
    const aiCtl = ais.length
      ? `<div class="row">
          <select id="aient"><option value="">AI predefinita</option>
            ${ais.map((e) => `<option value="${esc(e)}">${esc(states[e].attributes.friendly_name || e)}</option>`).join("")}</select>
          <input id="question" type="text" placeholder="Domanda facoltativa (es. può essere un sensore meteo?)">
          <button class="primary" id="askai">Chiedi all'AI</button>
        </div>
        <div id="aiout"></div>`
      : `<div class="hint">Per l'identificazione con l'AI serve un'entità <b>AI Task</b> (Home Assistant 2025.8+):
          aggiungi un'integrazione come Anthropic, OpenAI, Google Gemini o Ollama e attiva il suo AI Task.</div>`;
    const when = r.timestamp ? new Date(r.timestamp * 1000).toLocaleString() : "";
    out.innerHTML = `
      <div class="anahead">${r.auto ? "Analisi automatica durante la scansione" : "Analisi manuale"}${when ? ` · ${when}` : ""}
        · ${r.target_mhz.toFixed(4)} MHz</div>
      <div class="anagrid">
        <div class="anaimg">
          <img src="data:image/png;base64,${r.png}" alt="Cascata del segnale">
          <div class="axis"><span>${im.f_lo_mhz.toFixed(4)}</span><span>MHz · tempo ↓ ${r.duration_s} s</span><span>${im.f_hi_mhz.toFixed(4)}</span></div>
        </div>
        <div>
          <table><tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>
          <div class="hint" style="margin-top:6px">${r.bands.length ? esc(r.bands.join(" · ")) : "Frequenza non presente nella tabella delle bande."}</div>
          ${rdsHtml}
          ${dabHtml}
          ${tvHtml}
        </div>
      </div>
      ${r.audio ? `<div class="row">
          <select id="audmode">${[["wfm", "FM larga (radio)"], ["nfm", "FM stretta (voce)"], ["am", "AM"], ["usb", "USB (onde corte)"], ["lsb", "LSB (onde corte)"], ["cw", "CW (Morse)"]]
            .filter(([k]) => r.audio[k])
            .map(([k, t]) => `<option value="${k}"${k === (r.audio_mode || (r.audio.usb ? "usb" : "nfm")) ? " selected" : ""}>${t}</option>`).join("")}</select>
          <audio id="player" controls preload="auto" style="flex:1;min-width:200px;height:36px"></audio>
        </div>
        ${r.audio_mode ? "" : `<div class="hint">Segnale digitale: demodulato si sente solo rumore.</div>`}` : ""}
      ${r.iq_bytes && r.id ? `<div class="row">
          <span class="hint">Registrazione grezza (${(r.iq_bytes / 1e6).toFixed(1)} MB, ${(r.sample_rate / 1e6).toFixed(3)} MS/s
            centrata su ${((r.tune_hz || r.target_mhz * 1e6) / 1e6).toFixed(4)} MHz):</span>
          <button class="iqdl" data-fmt="wav" title="WAV IQ a 16 bit: SDR#, SDR++, SDRangel, SatDump, Universal Radio Hacker">⬇ WAV IQ</button>
          <button class="iqdl" data-fmt="cu8" title="Byte grezzi della chiavetta, come rtl_sdr (I e Q a 8 bit senza segno)">⬇ cu8</button>
        </div>` : ""}
      ${aiCtl}`;
    const player = this.shadowRoot.getElementById("player");
    if (player) {
      const sel = this.shadowRoot.getElementById("audmode");
      // Blob invece di un URL data: più affidabile sui browser mobili con file da centinaia di kB
      const load = () => {
        if (player.src) URL.revokeObjectURL(player.src);
        const bin = atob(r.audio[sel.value]);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        player.src = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
      };
      sel.addEventListener("change", () => { load(); player.play().catch(() => {}); });
      load();
    }
    out.querySelectorAll("button.dabplay").forEach((b) => b.addEventListener("click", () =>
      // le analisi delle versioni precedenti non hanno il centro del canale: va bene qualsiasi
      // frequenza del blocco, il backend risale al canale
      this._liveStart(r.dab_channel_mhz || r.center_mhz, "dab", b.dataset.sid)));
    out.querySelectorAll("button.iqdl").forEach((b) => b.addEventListener("click", async () => {
      // link firmato (valido pochi minuti): il file è grande, si scarica fuori dal websocket
      try {
        const res = await this._hass.callWS({ type: "spettro/iq_url", entry_id: r.id, format: b.dataset.fmt });
        const a = document.createElement("a");
        a.href = res.url;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } catch (err) {
        this._msg(err.message || String(err), true);
      }
    }));
    const tvb = this.shadowRoot.getElementById("tvlive");
    if (tvb) tvb.addEventListener("click", () => this._liveStart(r.tv_channel_mhz, "tv"));
    const btn = this.shadowRoot.getElementById("askai");
    if (btn) btn.addEventListener("click", () => this._askAi());
  }

  async _askAi() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const btn = $("askai"), outEl = $("aiout");
    btn.disabled = true;
    btn.textContent = "In attesa dell'AI…";
    outEl.innerHTML = "";
    try {
      const res = await this._hass.callWS({
        type: "spettro/ask_ai", entity_id: $("aient").value || null, question: $("question").value.trim() || null,
      });
      const text = esc(res.answer).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
      outEl.innerHTML = `<div class="answer">${text}</div>`
        + (res.with_image ? "" : `<div class="hint">Il modello scelto non accetta immagini: ha ricevuto solo le misure.</div>`);
    } catch (err) {
      outEl.innerHTML = `<div class="err">${esc(err.message || err.code || err)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Chiedi all'AI";
    }
  }

  _drawWaterfall() {
    const g = this._setupCanvas(this.$wf);
    if (!g) return;
    const { ctx, w, h } = g;
    const rows = this._history;
    const s = this._spec;
    if (!s || !rows.length) {
      ctx.fillStyle = "#080820";
      ctx.fillRect(0, 0, w, h);
      return;
    }
    const L = 46, R = 10;
    const pw = Math.max(1, Math.floor(w - L - R));
    const lo = this._view ? this._view[0] : s.range_start;
    const hi = this._view ? this._view[1] : s.range_stop;
    const span = hi - lo;
    const [vlo, vhi] = this._yRange || [-80, -20];
    const nrows = Math.min(rows.length, MAX_HISTORY);
    const img = ctx.createImageData(pw, nrows);
    for (let r = 0; r < nrows; r++) {
      const row = rows[r];
      for (let x = 0; x < pw; x++) {
        const fa = lo + (x / pw) * span, fb = lo + ((x + 1) / pw) * span;
        const v = SpettroPanel._colMax(row.values, row.f_start, row.bin_hz, fa, fb);
        const o = (r * pw + x) * 4;
        const t = v === null ? 0 : Math.max(0, Math.min(255, Math.round(((v - vlo) / (vhi - vlo)) * 255)));
        img.data[o] = LUT[t * 3]; img.data[o + 1] = LUT[t * 3 + 1]; img.data[o + 2] = LUT[t * 3 + 2]; img.data[o + 3] = 255;
      }
    }
    const off = document.createElement("canvas");
    off.width = pw; off.height = nrows;
    off.getContext("2d").putImageData(img, 0, 0);
    ctx.fillStyle = "#080820";
    ctx.fillRect(L, 0, pw, h);
    const rowH = h / MAX_HISTORY;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, L, 0, pw, Math.max(rowH * nrows, 1));
    ctx.fillStyle = getComputedStyle(this).getPropertyValue("--secondary-text-color").trim() || "#888";
    ctx.font = "11px Roboto, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillText("ora", L - 6, 2);
    ctx.textBaseline = "bottom";
    if (nrows > 1) ctx.fillText(`-${nrows - 1}`, L - 6, Math.max(rowH * nrows, 14));
  }
}

if (!customElements.get("spettro-panel")) customElements.define("spettro-panel", SpettroPanel);
