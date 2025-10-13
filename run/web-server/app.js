// ===== DOM refs =====
const pretty         = document.getElementById("pretty");
const intervalInput  = document.getElementById("interval");
const refreshBtn     = document.getElementById("refresh");
const fontSlider     = document.getElementById("fontSlider");
const fontSizeValue  = document.getElementById("fontSizeValue");
const viewSelect     = document.getElementById("viewSelect");
const imgSlider      = document.getElementById("imgSlider");
const imgScaleValue  = document.getElementById("imgScaleValue");
const feed           = document.getElementById("feed");
const cards          = document.getElementById("cards");
const profileSelect  = document.getElementById("profileSelect");
const headerEl       = document.querySelector("header");
const autoRefresh    = document.getElementById("autoRefresh");
const filterToggle   = document.getElementById("filterToggle");

// ===== sticky/fixed header padding sync =====
function syncHeaderPad() {
  const h = headerEl ? headerEl.offsetHeight : 64;
  document.documentElement.style.setProperty("--header-h", h + "px");
}
window.addEventListener("resize", syncHeaderPad);
window.addEventListener("load", syncHeaderPad);

// ===== state =====
let timer = null;
let currentProfile = null;
let currentFilters = []; // for LIVE view

// ===== persistence =====
const getSaved = (k, d) => {
  const v = localStorage.getItem(k);
  if (v === null) return d;
  if (v === "true" || v === "false") return v === "true";
  return v;
};
const setSaved = (k, v) => localStorage.setItem(k, typeof v === "boolean" ? String(v) : v);
function getSavedProfile()  { return getSaved("viewer.profile", null); }
function setSavedProfile(p) { setSaved("viewer.profile", p || ""); }

// ===== hard show/hide helpers =====
function forceShow(el) { if (el) { el.style.display = ""; el.classList.remove("hidden"); } }
function forceHide(el) { if (el) { el.style.display = "none"; el.classList.add("hidden"); } }

// ===== font size (applies to JSON + Live + Cards) =====
function applyFontSize(px) {
  const size = px + "px";
  fontSizeValue && (fontSizeValue.textContent = size);
  pretty && (pretty.style.fontSize = size);
  cards  && (cards.style.fontSize  = size);
  document.querySelectorAll("#feed .card pre").forEach(el => { el.style.fontSize = size; });
}
if (fontSlider) {
  const minPx = parseInt(fontSlider.min || "10", 10);
  const maxPx = parseInt(fontSlider.max || "28", 10);
  let savedPx = parseInt(getSaved("viewer.fontPx", fontSlider.value || "13"), 10);
  if (isNaN(savedPx)) savedPx = 13;
  savedPx = Math.max(minPx, Math.min(maxPx, savedPx));
  fontSlider.value = String(savedPx);
  fontSlider.addEventListener("input", () => {
    let px = parseInt(fontSlider.value || String(savedPx), 10);
    px = Math.max(minPx, Math.min(maxPx, px));
    setSaved("viewer.fontPx", px);
    applyFontSize(px);
  });
  applyFontSize(savedPx);
}

// ===== util: safe text + row-highlighter (JSON/Live) =====
function escapeHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function rowHighlight(text) {
  const esc = escapeHtml(text);
  return esc.split("\n").map(line =>
    line.replace(/^([^:]*:)(.*)$/, (_, left, right) =>
      `<span class="key">${left}</span><span class="val">${right}</span>`
    )
  ).join("\n");
}

// ===== JSON view =====
async function fetchJSON() {
  const url = "/api/data?profile=" + encodeURIComponent(currentProfile || "");
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  const raw = JSON.stringify(data, null, 2);
  if (pretty) pretty.innerHTML = rowHighlight(raw);
  applyFontSize(parseInt(fontSlider.value || "13", 10));
}

// ===== Live filters =====
async function fetchFilters() {
  const url = "/api/filters?profile=" + encodeURIComponent(currentProfile || "");
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error();
    const payload = await res.json();
    currentFilters = Array.isArray(payload.filters) ? payload.filters : [];
  } catch { currentFilters = []; }
}
function applyFilters(text, filters) {
  if (!filters || !filters.length) return text;
  let out = text;
  for (const rule of filters) {
    const type = (rule.type || "literal").toLowerCase();
    const find = String(rule.find ?? "");
    const rep  = String(rule.replace ?? "");
    if (!find) continue;
    if (type === "regex") {
      try { out = out.replace(new RegExp(find, "g"), rep); } catch {}
    } else {
      out = out.split(find).join(rep);
    }
  }
  return out;
}

// ===== Live view =====
function applyImageScale(scale) {
  document.querySelectorAll(".live-img").forEach(img => {
    const nw = img.naturalWidth || parseInt(img.dataset.nw || "0", 10);
    if (nw) img.style.width = (nw * scale) + "px";
    img.style.height = "auto";
  });
  if (imgScaleValue) imgScaleValue.textContent = Math.round(scale * 100) + "%";
}
if (imgSlider) {
  imgSlider.value = String(getSaved("viewer.imgScale", 1));
  imgSlider.addEventListener("input", () => {
    setSaved("viewer.imgScale", imgSlider.value);
    applyImageScale(parseFloat(imgSlider.value || "1"));
  });
}
function renderLive(payload) {
  if (!feed) return;
  feed.innerHTML = "";
  const items = (payload && payload.items) || [];
  const useFilters = !!(filterToggle && filterToggle.checked);
  items.forEach(it => {
    if (it.type === "text") {
      const card = document.createElement("div");
      card.className = "card";
      card.className = "card text-card";
      const pre = document.createElement("pre");
      const text = useFilters ? applyFilters(it.text, currentFilters) : it.text;
      pre.innerHTML = rowHighlight(text);
      card.appendChild(pre);
      feed.appendChild(card);
    } else if (it.type === "image") {
      const wrap = document.createElement("div");
      wrap.className = "img-wrap";
      const img = document.createElement("img");
      img.className = "live-img";
      img.alt = it.name || "image";
      img.src = it.url;
      img.addEventListener("load", () => {
        img.dataset.nw = img.naturalWidth;
        applyImageScale(parseFloat(imgSlider.value || "1"));
      });
      wrap.appendChild(img);
      feed.appendChild(wrap);
    }
  });
  applyImageScale(parseFloat(imgSlider.value || "1"));
  applyFontSize(parseInt(fontSlider.value || "13", 10));
}
async function fetchLive() {
  const url = "/api/live?profile=" + encodeURIComponent(currentProfile || "");
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const payload = await res.json();
  renderLive(payload);
}

// ===== Cards view =====
function labelize(k) { return String(k).replace(/_/g, " "); }
function addKV(parent, key, val) {
  const row = document.createElement("div");
  row.className = "kv";
  row.innerHTML = `<div class="k">${escapeHtml(labelize(key))}</div><div class="v">${escapeHtml(val)}</div>`;
  parent.appendChild(row);
}
function makeToggle(text, expanded=false, className="toggle-btn") {
  const btn = document.createElement("button");
  btn.className = className;
  btn.textContent = labelize(text);
  btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  return btn;
}

// Render an object's primitives as kv rows; complex values as small pre blocks.
function appendKVObject(parent, obj) {
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    for (const [k, v] of Object.entries(obj)) {
      if (v !== null && typeof v === "object") {
        const pre = document.createElement("pre");
        pre.className = "obj";
        pre.textContent = `${k}: ${JSON.stringify(v, null, 2)}`;
        parent.appendChild(pre);
      } else {
        addKV(parent, k, String(v));
      }
    }
  } else if (Array.isArray(obj)) {
    const pre = document.createElement("pre");
    pre.className = "obj";
    pre.textContent = JSON.stringify(obj, null, 2);
    parent.appendChild(pre);
  } else {
    addKV(parent, "value", String(obj));
  }
}

function buildSectionCardBody(key, obj) {
  const body = document.createElement("div");
  body.className = "sect-body";

  if (key === "prev") {
    ["last_hash","last_img_hash","last_timestamp"].forEach(f => {
      const node = obj?.[f];
      const val = (node && (node.value ?? node.path ?? JSON.stringify(node))) || "";
      addKV(body, f, String(val));
    });
} else if (key === "image") {
  // sha256 (value or raw)
  const sha = (obj?.sha256 && (obj.sha256.value ?? obj.sha256)) ?? "";
  addKV(body, "sha256", String(sha));

  // ots: render as a proper key with its children as kv rows
  const ots = obj?.ots;
  if (ots !== undefined) {
    // header row: "ots"
    const row = document.createElement("div");
    row.className = "kv";
    row.innerHTML = `<div class="k">ots</div><div class="v"></div>`;
    body.appendChild(row);

    // children under an indented sub-body
    const sub = document.createElement("div");
    sub.className = "sub-body";

    if (ots && typeof ots === "object") {
      for (const [k, v] of Object.entries(ots)) {
        if (v && typeof v === "object") {
          // complex child -> pretty JSON block, still indented
          const pre = document.createElement("pre");
          pre.className = "obj";
          pre.textContent = `${k}: ${JSON.stringify(v, null, 2)}`;
          sub.appendChild(pre);
        } else {
          // simple child -> kv row (wraps nicely, scales with slider)
          addKV(sub, k, String(v));
        }
      }
    } else {
      // primitive ots (rare) -> single kv row
      addKV(sub, "value", String(ots));
    }

    body.appendChild(sub);
  }


  } else if (key === "streams") {
    ["http_freeze_sys","net_freeze_sys","http_events_freeze_sys"].forEach(k => {
      if (k in obj) addKV(body, k, String(obj[k]));
    });
function renderStreamArray(arrKey) {
  const arr = obj?.[arrKey];
  if (!Array.isArray(arr) || arr.length === 0) return;

  // label row for the array (e.g., "http", "net", "http_events")
  const label = document.createElement("div");
  label.className = "kv";
  label.innerHTML = `<div class="k">${labelize(arrKey)}</div><div class="v"></div>`;
  body.appendChild(label);

  // show up to 5 entries with sub-toggles; each entry renders as kv rows (scales with slider)
  arr.slice(0, 5).forEach((item, idx) => {
    const subBtn = makeToggle(String(idx), false, "sub-toggle");

    const row = document.createElement("div");
    row.className = "kv";
    const left = document.createElement("div"); left.className = "k";
    const right = document.createElement("div"); right.className = "v";
    left.appendChild(subBtn);
    row.appendChild(left); row.appendChild(right);
    body.appendChild(row);

    const subWrap = document.createElement("div");
    subWrap.className = "sub-body";
    subWrap.style.display = "none";

    // render the object neatly into kv rows / compact pre blocks
    appendKVObject(subWrap, item);

    body.appendChild(subWrap);
  });
}

    renderStreamArray("http");
    renderStreamArray("net");
    renderStreamArray("http_events");
  } else if (key === "last_files") {
    if ("sys_time_freeze" in obj) addKV(body, "sys_time_freeze", String(obj.sys_time_freeze));
    if (Array.isArray(obj.rows) && obj.rows.length) {
      const pre = document.createElement("pre");
      pre.className = "obj";
      pre.textContent = JSON.stringify(obj.rows.slice(0, 10), null, 2);
      body.appendChild(pre);
    }
  } else if (key === "downloads") {
    if ("files_recent_freeze_sys" in obj) addKV(body, "files_recent_freeze_sys", String(obj.files_recent_freeze_sys));
    if (Array.isArray(obj.files_recent) && obj.files_recent.length) {
      const pre = document.createElement("pre");
      pre.className = "obj";
      pre.textContent = JSON.stringify(obj.files_recent.slice(0, 10), null, 2);
      body.appendChild(pre);
    }
  } else {
    const pre = document.createElement("pre");
    pre.className = "obj";
    pre.textContent = JSON.stringify(obj, null, 2);
    body.appendChild(pre);
  }
  return body;
}
function buildSection(key, obj) {
  const sect = document.createElement("div");
  sect.className = "sect collapsed";      // collapsed by default
  const head = document.createElement("div");
  head.className = "sect-head";
  const btn = makeToggle(key, false, "toggle-btn");
  head.appendChild(btn);
  sect.appendChild(head);
  sect.appendChild(buildSectionCardBody(key, obj));
  return sect;
}
function renderCardsPayload(data) {
  const list = Array.isArray(data) ? data : (data?.items || []);
  cards.innerHTML = "";
  list.forEach((obj, idx) => {
    const card = document.createElement("div");
    card.className = "card text-card";   // <- add text-card

    const top = document.createElement("div");
    addKV(top, "id", String(obj.id ?? idx));
    addKV(top, "index raw", String(obj.index_raw ?? ""));
    addKV(top, "sys time in", String(obj.sys_time_in ?? ""));
    card.appendChild(top);

    ["prev","image","streams","last_files","downloads"].forEach(k => {
      if (k in obj) card.appendChild(buildSection(k, obj[k]));
    });

    const bottom = document.createElement("div");
    bottom.style.marginTop = "10px";
    bottom.style.borderTop = "1px dashed #2a2f3a";
    bottom.style.paddingTop = "10px";
    addKV(bottom, "sys time out", String(obj.sys_time_out ?? ""));
    card.appendChild(bottom);

    cards.appendChild(card);
  });
  applyFontSize(parseInt(fontSlider.value || "13", 10));
}
async function fetchCards() {
  const url = "/api/data?profile=" + encodeURIComponent(currentProfile || "");
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  renderCardsPayload(data);
}

// ===== polling (skip Cards; static) =====
function isAutoRefreshOn() { return !!(autoRefresh && autoRefresh.checked); }
function startPolling() {
  if (timer) clearInterval(timer);
  const ms = parseInt(intervalInput?.value || "0", 10) || 0;
  if (viewSelect?.value === "cards") return;   // no polling on Cards
  if (ms > 0 && isAutoRefreshOn()) {
    timer = setInterval(() => {
      const v = viewSelect?.value;
      if (v === "live") fetchLive().catch(()=>{});
      else fetchJSON().catch(()=>{});
    }, ms);
  }
}
if (intervalInput) intervalInput.addEventListener("change", startPolling);
if (refreshBtn)    refreshBtn.addEventListener("click", () => {
  const v = viewSelect?.value;
  if (v === "live") fetchLive().catch(()=>{});
  else if (v === "cards") fetchCards().catch(()=>{});
  else fetchJSON().catch(()=>{});
});
if (autoRefresh) {
  const savedAR = getSaved("viewer.autoRefresh", true);
  autoRefresh.checked = !!savedAR;
  autoRefresh.addEventListener("change", () => {
    setSaved("viewer.autoRefresh", autoRefresh.checked);
    if (autoRefresh.checked) startPolling();
    else if (timer) { clearInterval(timer); timer = null; }
  });
}

// ===== view & profile switching =====
function setFilterToggleDisabled(disabled) {
  if (!filterToggle) return;
  filterToggle.disabled = disabled;
  filterToggle.parentElement.style.opacity = disabled ? 0.5 : 1;
}
async function showJSON() {
  forceHide(feed);  feed?.replaceChildren();
  forceHide(cards); cards?.replaceChildren();
  forceShow(pretty);
  setFilterToggleDisabled(true);
  await fetchJSON();
  syncHeaderPad();
}
async function showLive() {
  forceHide(pretty);
  forceHide(cards); cards?.replaceChildren();
  forceShow(feed);
  setFilterToggleDisabled(false);
  if (filterToggle?.checked) await fetchFilters();
  await fetchLive();
  syncHeaderPad();
}
async function showCards() {
  forceHide(pretty);
  forceHide(feed); feed?.replaceChildren();
  forceShow(cards);
  setFilterToggleDisabled(true);
  if (timer) { clearInterval(timer); timer = null; }  // stop polling on Cards
  await fetchCards();
  syncHeaderPad();
}
if (viewSelect) {
  const onViewChange = async () => {
    const v = viewSelect.value;
    if (v === "live") await showLive();
    else if (v === "cards") await showCards();
    else await showJSON();
    startPolling();
  };
  viewSelect.addEventListener("change", onViewChange);
}

// Bind Cards toggles once (outside render)
if (cards && !cards.__toggleBound) {
  cards.addEventListener("click", (e) => {
    const btn = e.target.closest("button.toggle-btn");
    if (btn) {
      e.preventDefault(); e.stopPropagation();
      const sect = btn.closest(".sect");
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      sect.classList.toggle("collapsed", open);
      return;
    }
    const sub = e.target.closest("button.sub-toggle");
    if (sub) {
      e.preventDefault(); e.stopPropagation();
      const open = sub.getAttribute("aria-expanded") === "true";
      sub.setAttribute("aria-expanded", open ? "false" : "true");
      const wrap = sub.closest(".kv")?.nextElementSibling;
      if (wrap && wrap.classList.contains("sub-body")) {
        wrap.style.display = open ? "none" : "block";
      }
    }
  });
  cards.__toggleBound = true;
}
if (profileSelect) {
  const onProfileChange = async () => {
    currentProfile = profileSelect.value || null;
    setSavedProfile(currentProfile || "");
    const v = viewSelect?.value;
    if (v === "live") await showLive();
    else if (v === "cards") await showCards();
    else await showJSON();
  };
  profileSelect.addEventListener("change", onProfileChange);
}
if (filterToggle) {
  const savedF = getSaved("viewer.filterOn", true);
  filterToggle.checked = !!savedF;
  filterToggle.addEventListener("change", async () => {
    setSaved("viewer.filterOn", filterToggle.checked);
    if (viewSelect?.value === "live") {
      if (filterToggle.checked) await fetchFilters();
      await fetchLive();
    }
  });
}

// ===== boot =====
(async function init() {
  try {
    const cfg = await fetch("/api/config", { cache: "no-store" }).then(r=>r.json());
    const profiles = cfg.profiles || [];
    const active = getSavedProfile() || cfg.active || (profiles[0] && profiles[0].name) || "";
    if (profileSelect) {
      profileSelect.innerHTML = "";
      profiles.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p.name; opt.textContent = p.name;
        if (p.name === active) opt.selected = true;
        profileSelect.appendChild(opt);
      });
    }
    currentProfile = active;

    viewSelect && (viewSelect.value = "json");
    await showJSON();
    startPolling();
  } catch (e) { console.error(e); }
})();

