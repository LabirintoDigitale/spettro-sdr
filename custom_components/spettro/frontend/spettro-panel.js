// Pannello "Spettro" per Home Assistant: grafico dello spettro, waterfall e picchi.

// deve coincidere con VERSION in const.py: se il backend ne riporta un'altra manca un riavvio
const PANEL_VERSION = "1.7.2";

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
  ["Tutto (24–1766)", 24, 1766],
];
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
      this._spec = { ...m, cursor: null };
    } else {
      // scansione in corso: si mostra la parte nuova + il resto della scansione precedente
      let values = m.values, peaks = m.peaks;
      if (grid(m, this._lastComplete) && this._lastComplete.values.length > values.length) {
        values = values.concat(this._lastComplete.values.slice(values.length));
        peaks = this._lastComplete.peaks;
      }
      this._spec = { ...m, values, peaks, cursor: m.f_start + m.values.length * m.bin_hz };
    }
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
        #archive table td { cursor:pointer; }
        #archive th.sort { cursor:pointer; user-select:none; white-space:nowrap; }
        #archive th.sort:hover { color:var(--primary-text-color); }
        #archive tbody.entry:hover td { background:var(--secondary-background-color); }
        #archive td.kind { color:var(--secondary-text-color); font-size:12px; }
        /* le due righe di un'analisi (ora/frequenza e tipo) si evidenziano insieme */
        #archive tbody.entry tr:first-child td { border-bottom:none; }
        #archive tbody.entry.sel td { background:rgba(3,169,244,.16); }
        #archive tbody.entry.sel td:first-child { box-shadow:inset 3px 0 var(--primary-color); }
        .anahead { font-size:12px; color:var(--secondary-text-color); }
        button.heart { font-size:20px; line-height:1; padding:6px 10px; color:#e53935; }
        #favs table td { cursor:pointer; }
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
        #livewf { height:160px; border-radius:4px; background:#080820; }
        .liveaxis { display:flex; justify-content:space-between; font-size:11px; color:var(--secondary-text-color); }
        #livert div { font-size:13px; padding:2px 0; border-bottom:1px solid var(--divider-color); }
        #livert span { color:var(--secondary-text-color); font-size:11px; margin-right:6px; }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title"><span>👻 Spettro</span><span class="ver">versione ${PANEL_VERSION}</span></div></div>
      <div id="verwarn" class="err" style="display:none;padding:10px 16px;font-size:14px"></div>
      <div class="content">
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
              <div class="hint" id="selinfo" style="flex:1;min-width:200px">Tocca un picco sul grafico o una riga dell'Archivio spike per sceglierlo.</div>
              <button class="primary" id="analyze" disabled>Analizza</button>
              <button id="livebtn" disabled title="Resta sintonizzato su questa frequenza: audio, cascata e RDS in diretta (la scansione si sospende)">▶ Analisi continua</button>
              <button class="heart" id="fav" disabled title="Aggiungi ai preferiti">♡</button>
            </div>
            <div id="live" style="display:none">
              <div class="livehead">
                <span class="dot">● IN DIRETTA</span>
                <span class="title" id="livetitle"></span>
                <select id="livemode" title="Demodulazione"><option value="wfm">FM larga (radio)</option><option value="nfm">FM stretta (voce)</option><option value="am">AM</option></select>
                <button id="livelisten">🔇 Ascolta</button>
                <button class="primary" id="livestop">■ Ferma</button>
              </div>
              <canvas id="livewf" style="margin-top:8px"></canvas>
              <div class="liveaxis"><span id="livelo"></span><span id="livelevel"></span><span id="livehi"></span></div>
              <div id="liverds"></div>
            </div>
            <div id="anaout"></div>
          </div>
        </div>
        <div class="side">
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
                <label title="Quanto registrare per ogni analisi, sia con Analizza sia per i picchi analizzati in automatico (ogni secondo in più allunga la scansione per ogni picco nuovo)">Durata analisi
                  <select id="anadur"><option value="1">1 s</option><option value="2">2 s</option><option value="5">5 s</option></select>
                </label>
              </div>
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

    const form = ["start", "stop", "interval", "gain", "threshold", "viewonly", "autoana", "reana", "archhours", "limstep", "maxstep", "limsweep", "maxsweep",
      "rate", "fft", "settle", "ppm", "anadur"];
    for (const id of form) $(id).addEventListener("input", () => {
      this._formDirty = true;
      if (id === "start" || id === "stop") $("preset").value = "0";
    });
    $("preset").addEventListener("change", (e) => {
      const p = PRESETS[+e.target.value];
      if (p[1] === null) return;
      $("start").value = p[1];
      $("stop").value = p[2];
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
    $("fav").addEventListener("click", () => this._toggleFav(this._sel));
    $("livebtn").addEventListener("click", () => this._liveStart(this._sel / 1e6));
    $("livestop").addEventListener("click", async () => {
      try { await this._hass.callWS({ type: "spettro/live_stop" }); } catch (err) { this._msg(err.message || String(err), true); }
    });
    $("livelisten").addEventListener("click", () => this._setListen(!this._listen));
    $("livemode").addEventListener("change", (e) => { if (this._live) this._liveStart(this._live.freq_mhz, e.target.value); });
    $("resume").addEventListener("click", async () => {
      try {
        await this._hass.callWS({ type: "spettro/set_enabled", enabled: true });
        this._msg("Scansione riattivata.");
      } catch (err) {
        this._msg(err.message || String(err), true);
      }
    });
    for (const id of ["autoana", "limstep", "limsweep"]) $(id).addEventListener("change", () => this._syncLimits());
    $("archclear").addEventListener("click", async () => {
      if (!this._archive.length || !confirm("Cancellare tutte le analisi archiviate?")) return;
      try {
        await this._hass.callWS({ type: "spettro/archive_delete" });
        await this._loadArchive();
      } catch (err) {
        this._msg(err.message || String(err), true);
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
    $("anadur").value = String(s.analysis_s ?? 2);
    $("viewonly").checked = !!s.only_when_viewed;
    $("autoana").checked = !!s.auto_analyze;
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
      analysis_s: parseFloat($("anadur").value) || 2,
      only_when_viewed: $("viewonly").checked,
      auto_analyze: $("autoana").checked,
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
        text = s.paused === "analysis" ? "Analisi del segnale in corso, scansione sospesa…"
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

    // soglia di rilevamento
    if (s.floor_db != null && this._status && this._status.settings) {
      const y = vToY(s.floor_db + this._status.settings.threshold_db);
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(219,68,55,.6)";
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
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

  _favFor(mhz) {
    return mhz == null ? null : this._favs.find((f) => Math.abs(f.freq_mhz - mhz) <= 0.03) || null;
  }

  _updateFavBtn() {
    const b = this.shadowRoot.getElementById("fav");
    if (!b) return;
    const on = this._sel !== null && !!this._favFor(this._sel / 1e6);
    b.textContent = on ? "❤" : "♡";
    b.title = on ? "Togli dai preferiti" : "Aggiungi ai preferiti";
  }

  async _toggleFav(hz, name = null) {
    if (hz == null) return;
    const mhz = hz / 1e6, fav = this._favFor(mhz);
    // nome iniziale: quello RDS dell'analisi mostrata, se è di questa frequenza
    const ana = this._ana;
    if (!name && ana && ana.rds && ana.rds.ps_complete && Math.abs(ana.target_mhz - mhz) <= 0.1) name = ana.rds.ps;
    try {
      if (fav) await this._hass.callWS({ type: "spettro/favorite_remove", freq_mhz: fav.freq_mhz });
      else await this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: mhz, name, ...(name ? { source: "rds" } : {}) });
    } catch (err) {
      this._msg(err.message || String(err), true);
    }
  }

  async _renameFav(mhz) {
    const fav = this._favFor(mhz);
    if (!fav) return;
    const name = prompt(`Nome per ${fav.freq_mhz.toFixed(4)} MHz (vuoto = nome automatico):`, fav.name || "");
    if (name === null) return;
    try {
      await this._hass.callWS({ type: "spettro/favorite_set", freq_mhz: fav.freq_mhz, name, source: "user" });
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
    el.innerHTML = `<table><tbody>${this._favs.map((f) => `<tr data-f="${f.freq_mhz}">
        <td class="name">${esc(f.name || "—")}</td><td>${f.freq_mhz.toFixed(4)} MHz</td>
        <td class="act"><button class="edit" title="Cambia nome">✏</button><button class="play" title="Analisi continua">▶</button><button class="heart" title="Togli dai preferiti">❤</button></td></tr>`).join("")}
      </tbody></table>`;
    el.querySelectorAll("tr[data-f]").forEach((tr) => {
      const mhz = parseFloat(tr.dataset.f);
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
      tr.querySelector("button.play").addEventListener("click", () => { this._select(mhz * 1e6); this._liveStart(mhz); });
      tr.querySelector("button.heart").addEventListener("click", () => this._toggleFav(mhz * 1e6));
      tr.querySelector("button.edit").addEventListener("click", () => this._renameFav(mhz));
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
    const b = this.shadowRoot.getElementById("livelisten");
    b.textContent = this._listen ? "🔊 Muto" : "🔇 Ascolta";
  }

  async _liveStart(mhz, mode = null) {
    if (mhz == null) return;
    this._setListen(true); // il tocco sul pulsante permette di avviare l'audio
    this._liveRestart = true; // la diretta precedente si ferma: l'audio resta acceso
    try {
      await this._hass.callWS({ type: "spettro/live_start", freq_mhz: mhz, ...(mode ? { mode } : {}) });
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
      if (was && !this._liveRestart) { this._setListen(false); this._liveRows = null; }
      return;
    }
    if (!was || was.freq_mhz !== live.freq_mhz || was.mode !== live.mode) {
      this._liveRows = null;
      this._rtHistory = [];
      this._liveRds = null;
      $("liverds").innerHTML = live.mode === "wfm" ? `<div class="hint" style="margin-top:6px">In attesa dei dati RDS…</div>` : "";
    }
    $("livemode").value = live.mode;
    this._liveTitle();
  }

  _liveTitle() {
    const live = this._live;
    if (!live) return;
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
    const now = ctx.currentTime;
    // piccola riserva contro i ritardi di rete; se si accumula troppo ritardo si riparte
    if (this._nextT < now + 0.05 || this._nextT > now + 2) this._nextT = now + 0.4;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(this._nextT);
    this._nextT += buf.duration;
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
    $("selinfo").textContent = `Selezionato: ${(f / 1e6).toFixed(4)} MHz`;
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
      el.textContent = "Nessuna analisi archiviata. Attiva \"Analizza subito i picchi trovati\" o usa Analizza.";
      return;
    }
    const fmt = (t) => {
      const d = new Date(t * 1000), today = new Date().toDateString() === d.toDateString();
      return (today ? "" : d.toLocaleDateString([], { day: "2-digit", month: "2-digit" }) + " ")
        + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    };
    const { key, desc } = this._archSort;
    const rows = this._archive.slice().sort((a, b) => (desc ? b[key] - a[key] : a[key] - b[key]));
    const th = (k, label, cls = "") => `<th class="sort ${cls}" data-key="${k}">${label}${k === key ? (desc ? " ▼" : " ▲") : ""}</th>`;
    el.className = "";
    el.innerHTML = `<table><thead><tr>${th("timestamp", "Ora")}${th("target_mhz", "MHz")}${th("snr_db", "SNR", "num")}</tr></thead>
      ${rows.map((e) => `<tbody class="entry${e.id === this._archSel ? " sel" : ""}" data-id="${esc(e.id)}"><tr>
        <td>${fmt(e.timestamp)}${e.auto ? "" : " ✋"}</td><td>${e.target_mhz.toFixed(4)}</td><td class="num">${e.snr_db} dB</td></tr>
        <tr><td colspan="3" class="kind">${esc(e.kind)}</td></tr></tbody>`).join("")}
      </table>
      <div class="hint" style="margin-top:6px">✋ = analisi avviata a mano.</div>`;
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
        </div>
      </div>
      ${r.audio ? `<div class="row">
          <select id="audmode">${[["wfm", "FM larga (radio)"], ["nfm", "FM stretta (voce)"], ["am", "AM"]]
            .map(([k, t]) => `<option value="${k}"${k === (r.audio_mode || "nfm") ? " selected" : ""}>${t}</option>`).join("")}</select>
          <audio id="player" controls preload="auto" style="flex:1;min-width:200px;height:36px"></audio>
        </div>
        ${r.audio_mode ? "" : `<div class="hint">Segnale digitale: demodulato si sente solo rumore.</div>`}` : ""}
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
