// overlay.js — simple, robust overlay with single CONFIG block.
// Expects server messages like: { "title": "...", "source": "...", "pdf_page": ... }

// ===================== CONFIG — EDIT THIS BLOCK =====================
const CONFIG = {
  // WebSocket endpoint
  ws: 'ws://127.0.0.1:8765',

  // Show/Hide fields
  showTitle:  false,
  showSource: true,
  showPage:   true,

  // Layout options:
  //   'stack'         -> Title (row1), Source (row2), Page (row3)
  //   'inlineTitle'   -> Title — Page — Source (single row)
  //   'inlineSource'  -> Source — Page — Title (single row)
  layout: 'stack',

  // Spacing / padding
  inlineGapPx: 24,   // horizontal space between inline items
  paddingPx:   20,   // padding inside overlay

  // Font sizes / weights (applied via CSS variables that your style.css reads)
  titleSizePx:   44,
  titleWeight:  800,
  sourceSizePx:  44,
  sourceWeight: 800,
  pageSizePx:    38,
  pageWeight:   800,

  // Colors / font family (also pushed into CSS variables)
  // colorFG:  '#e6e6e6',
  // colorDim: '#a8a8a8',
  // colorBG:  'rgba(0,0,0,0)', // keep transparent for OBS
  colorFG:  '#e6e6e6',
  colorDim: '#e6e6e6',
  colorBG:  'rgba(0,0,0,0.9)', // keep transparent for OBS
  fontFamily:
    'system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Noto Sans","Apple Color Emoji","Segoe UI Emoji"',

  // Status ribbon (errors always show; set true to also show idle/connected text)
  showStatusIdle: false,

  // URL remap:
  // 1) Inline map (preferred under OBS file://). Case-insensitive prefix match;
  //    replacement keeps your exact casing; the rest of the original URL is preserved.
  //    Example preserves “MnCourtFraud.com” casing:
  urlMapInline: [
    // ["https://mncourtfraud.com/", "https://MnCourtFraud.com/"],
    // ["https://storage.courtlistener.com/", "https://CourtListener-CDN/"]
  ],

  // 2) External JSON map (optional). If inline map is empty, we try to fetch this.
  //    (Works when the overlay is served over http(s) or when CSP allows file:// fetch.)
  urlMapPath: 'url_map.json',
  reloadMapMs: 0, // >0 to auto-reload periodically; 0 = load once
};
// =================== END CONFIG — NOTHING BELOW NEEDS EDITS ===================

(function () {
  // Grab DOM
  const $ = id => document.getElementById(id);
  const elOverlay = $('overlay');
  const elStatus  = $('status');
  const elTitle   = $('title');
  const elSource  = $('source');
  const elPage    = $('page');

  // Apply layout class and spacing
  elOverlay.className = CONFIG.layout === 'inlineTitle'
    ? 'inline-title'
    : CONFIG.layout === 'inlineSource'
      ? 'inline-source'
      : 'stack';
  elOverlay.style.padding = `${CONFIG.paddingPx}px`;
  document.documentElement.style.setProperty('--inline-gap', `${CONFIG.inlineGapPx}px`);

  // Push CONFIG values into CSS vars that your style.css uses
  document.documentElement.style.setProperty('--title-size',   `${CONFIG.titleSizePx}px`);
  document.documentElement.style.setProperty('--source-size',  `${CONFIG.sourceSizePx}px`);
  document.documentElement.style.setProperty('--page-size',    `${CONFIG.pageSizePx}px`);
  document.documentElement.style.setProperty('--title-weight',  String(CONFIG.titleWeight));
  document.documentElement.style.setProperty('--source-weight', String(CONFIG.sourceWeight));
  document.documentElement.style.setProperty('--page-weight',   String(CONFIG.pageWeight));
  document.documentElement.style.setProperty('--color-fg',  CONFIG.colorFG);
  document.documentElement.style.setProperty('--color-dim', CONFIG.colorDim);
  document.documentElement.style.setProperty('--color-bg',  CONFIG.colorBG);
  elOverlay.style.fontFamily = CONFIG.fontFamily;

  // Initial visibility per toggles
  elTitle.style.display  = CONFIG.showTitle  ? '' : 'none';
  elSource.style.display = CONFIG.showSource ? '' : 'none';
  elPage.style.display   = CONFIG.showPage   ? '' : 'none';

  // ---------- URL remap ----------
  // Case-insensitive prefix match; replacement keeps casing; rest of URL is preserved.
  let urlMap = Array.isArray(CONFIG.urlMapInline) && CONFIG.urlMapInline.length > 0
    ? CONFIG.urlMapInline.slice()
    : [];

  async function loadMapOnce() {
    if (urlMap.length > 0) return; // inline overrides external
    try {
      const res = await fetch(CONFIG.urlMapPath, { cache: 'no-store' });
      const m = await res.json();
      urlMap = Array.isArray(m) ? m : [];
      if (CONFIG.showStatusIdle) okStatus(`loaded ${CONFIG.urlMapPath}`);
    } catch {
      // If external map fails, we just use an empty map. Overlay still works.
      if (CONFIG.showStatusIdle) errStatus(`failed to load ${CONFIG.urlMapPath}`);
    }
  }
  if (CONFIG.reloadMapMs > 0) setInterval(loadMapOnce, CONFIG.reloadMapMs);
  loadMapOnce();

  // >>> FIXED remap: case-insensitive match, preserve replacement casing (host + path), keep query/hash.
  function remap(urlText) {
    if (!urlText) return '';

    // Try robust URL-based matching first (preserves query/hash and your replacement casing)
    try {
      const u = new URL(urlText);
      // normalized candidate prefix (origin + pathname), ensure trailing slash behavior matches
      const candidate = (u.origin + u.pathname);

      for (const [from, to] of urlMap) {
        if (!from || !to) continue;

        // Build a normalized "from" prefix with origin + pathname
        const f = new URL(from, u.origin);
        const fromPrefix = (f.origin + f.pathname);

        // Case-insensitive prefix match
        if (candidate.toLowerCase().startsWith(fromPrefix.toLowerCase())) {
          // Remainder AFTER the matched prefix (path remainder only)
          const remainder = candidate.slice(fromPrefix.length);
          // Use your replacement EXACTLY as given (casing preserved),
          // then append the original remainder + search + hash
          return to + remainder + (u.search || '') + (u.hash || '');
        }
      }
      return urlText; // no mapping matched; return original
    } catch {
      // Fallback for non-URL strings (keep previous behavior, but case-insensitive)
      const lower = urlText.toLowerCase();
      for (const [from, to] of urlMap) {
        if (!from || !to) continue;
        const fl = String(from).toLowerCase();
        if (lower.startsWith(fl)) {
          return to + urlText.slice(from.length);
        }
      }
      return urlText;
    }
  }

  // ---------- Status helpers ----------
  function errStatus(msg) {
    elStatus.textContent = msg;
    elStatus.classList.add('error');  // shown regardless of showStatusIdle
  }
  function okStatus(msg) {
    if (!CONFIG.showStatusIdle) {
      elStatus.textContent = '';
      elStatus.classList.remove('error');
      return;
    }
    elStatus.textContent = msg;
    elStatus.classList.remove('error');
  }

  // ---------- Page handling ----------
  // Normalize to integer; renders as "Page N". Avoid double "Page".
  function normalizePage(v) {
    if (v == null) return null;
    if (typeof v === 'number') return v;
    const m = String(v).match(/(\d+)/);
    return m ? parseInt(m[1], 10) : null;
  }

  // Sticky page (prevents null flicker after scroll)
  let lastPdf = { isPdf:false, title:'', source:'', page:null };

  function isPdfTitle(t) {
    return t === 'Web PDF File' || t === 'Local PDF File';
  }

  function render(msg) {
    // Server sends exactly what you wanted: { title, source, pdf_page }
    const titleRaw  = msg.title  || '';
    const sourceRaw = msg.source || '';
    const pageNum   = normalizePage(msg.pdf_page);

    // Apply URL remap to source (for web URLs); local filenames remain unchanged
    const source = remap(sourceRaw);

    if (CONFIG.showTitle)  elTitle.textContent  = titleRaw;
    if (CONFIG.showSource) elSource.textContent = source;

    const inPdf = isPdfTitle(titleRaw);

    // Sticky logic
    if (inPdf && pageNum != null) {
      lastPdf = { isPdf:true, title:titleRaw, source, page:pageNum };
    } else if (inPdf && pageNum == null && lastPdf.isPdf &&
               lastPdf.title === titleRaw && lastPdf.source === source) {
      // keep last page
    } else if (!inPdf) {
      lastPdf = { isPdf:false, title:'', source:'', page:null };
    }

    const effectivePage = inPdf ? (pageNum ?? lastPdf.page) : null;

    if (CONFIG.showPage) {
      if (effectivePage != null) {
        elPage.textContent = `Page ${effectivePage}`;
        elPage.style.display = '';
      } else {
        elPage.textContent = '';
        elPage.style.display = 'none';
      }
    }

    // Ensure DOM order for inline layouts
    if (CONFIG.layout === 'inlineTitle') {
      elOverlay.className = 'inline-title';
      elOverlay.replaceChildren(elStatus, elTitle, elPage, elSource);
    } else if (CONFIG.layout === 'inlineSource') {
      elOverlay.className = 'inline-source';
      elOverlay.replaceChildren(elStatus, elSource, elPage, elTitle);
    } else {
      elOverlay.className = 'stack';
      elOverlay.replaceChildren(elStatus, elTitle, elSource, elPage);
    }
  }

  // ---------- WS client with clean auto-reconnect ----------
  let ws;
  function connect() {
    try { ws = new WebSocket(CONFIG.ws); }
    catch (e) {
      errStatus(`connect error: ${e.message}`);
      return setTimeout(connect, 1000);
    }

    ws.onopen    = () => okStatus(`connected ${CONFIG.ws}`);
    ws.onerror   = () => { /* onclose will follow */ };
    ws.onclose   = () => { errStatus('disconnected'); setTimeout(connect, 1000); };
    ws.onmessage = (ev) => {
      try { render(JSON.parse(ev.data)); }
      catch { errStatus('bad JSON from server'); }
    };
  }
  connect();
})();
