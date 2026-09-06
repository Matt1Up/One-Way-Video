// playback-engine.js — Core timecode sync engine (v2)
// Polls video.currentTime, binary-searches event arrays,
// fires callbacks on state changes including bundle lifecycle events.

import { resolveUrl } from './config-loader.js';

export function bisectRight(events, t_ms) {
  let lo = 0, hi = events.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (events[mid].t_ms <= t_ms) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function findChunk(chunks, t_ms) {
  let lo = 0, hi = chunks.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (chunks[mid].start_t_ms <= t_ms) lo = mid + 1;
    else hi = mid;
  }
  return chunks[Math.max(0, lo - 1)];
}

export function tc30(t_ms, fps = 30) {
  if (t_ms < 0) t_ms = 0;
  const totalFrames = Math.round((t_ms / 1000) * fps);
  const frames = totalFrames % fps;
  const totalSeconds = Math.floor(totalFrames / fps);
  const s = totalSeconds % 60;
  const m = Math.floor(totalSeconds / 60) % 60;
  const h = Math.floor(totalSeconds / 3600);
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(frames)}`;
}

function isEmpty(v) {
  return v === null || v === undefined ||
    (typeof v === 'string' && v.trim() === '') ||
    (typeof v === 'number' && Number.isNaN(v));
}

function safePick(obj, key) {
  const v = obj?.[key];
  return isEmpty(v) ? null : v;
}

export class PlaybackEngine {
  constructor({ data, config, slug, onTick, onBundleLifecycle }) {
    this.data = data;
    this.config = config;
    this.slug = slug;
    this.onTick = onTick;
    this.onBundleLifecycle = onBundleLifecycle; // (eventType, bundleId, eventData)
    this.chunkCache = new Map();

    this.last = {
      t_ms: 0, wall_ms: 0,
      harIdx: -1, dlIdx: -1, bIdx: -1, bundleId: -1,
      dl_file: null, vault_file: null, write_freeze: 0,
      sysTimeKey: null,
    };

    this.lists = {
      har_html: [], har_json: [],
      downloaded: [], downloaded_meta: [],
      vault: [], vault_meta: [],
      bundle_files: [], img_files: [], ts_files: [],
    };

    this.isPlaying = false;
    this.videoEl = null;
    this.tickTimer = null;
    this.inflight = false;
    this.queuedMs = null;
  }

  resetLists() {
    for (const k of Object.keys(this.lists)) this.lists[k] = [];
  }

  rebuildListsTo(t_ms) {
    this.resetLists();
    const { har, downloads, bundles } = this.data;

    for (const e of har.events) {
      if (e.t_ms > t_ms) break;
      const h = safePick(e, 'html_filename');
      const j = safePick(e, 'json_filename');
      if (h) this.lists.har_html.push(h);
      if (j) this.lists.har_json.push(j);
    }

    let lastDl = null, lastDlMeta = null, lastV = null, lastVMeta = null;
    for (const e of downloads.events) {
      if (e.t_ms > t_ms) break;
      let d = safePick(e, 'downloaded_file');
      const dm = safePick(e, 'downloaded_file_metadata');
      let v = safePick(e, 'vault_file');
      const vm = safePick(e, 'vault_file_metadata');
      if (d) { lastDl = d; if (dm) lastDlMeta = dm; }
      if (v) { lastV = v; if (vm) lastVMeta = vm; }
      if (d && /^\d{4}__/.test(d)) {
        lastV = d; if (dm) lastVMeta = dm;
        lastDl = null; lastDlMeta = null;
      }
    }
    if (lastDl) { this.lists.downloaded.push(lastDl); if (lastDlMeta) this.lists.downloaded_meta.push(lastDlMeta); }
    if (lastV) { this.lists.vault.push(lastV); if (lastVMeta) this.lists.vault_meta.push(lastVMeta); }

    for (const e of bundles.events) {
      if (e.t_ms > t_ms) break;
      for (const [k, listName] of [
        ['add_to_bundle_file_list', 'bundle_files'],
        ['add_to_img_file_list', 'img_files'],
        ['add_to_timestamp_file_list', 'ts_files'],
      ]) {
        for (const kk of Object.keys(e)) {
          if (kk === k || kk.startsWith(k + '.')) {
            const v = safePick(e, kk);
            if (v) this.lists[listName].push(v);
          }
        }
      }
    }
  }

  analyze(t_ms) {
    const now = performance.now();
    const dtVideo = t_ms - this.last.t_ms;
    const dtWall = now - this.last.wall_ms;
    let direction = 0;
    if (dtVideo > 0) direction = 1;
    else if (dtVideo < 0) direction = -1;
    const jumped = Math.abs(dtVideo) >= (this.config.jump_threshold_ms ?? 500);
    const nonstandard = jumped || direction !== 1;
    const speed_est = dtWall > 0 ? dtVideo / dtWall : 1;
    this.last.t_ms = t_ms;
    this.last.wall_ms = now;
    return { direction, jumped, nonstandard, speed_est };
  }

  async loadChunk(kind, file) {
    const key = `${kind}:${file}`;
    if (this.chunkCache.has(key)) return this.chunkCache.get(key);
    const url = resolveUrl('session', this.slug, `data/${kind}/${file}`);
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`chunk ${kind}/${file}: ${resp.status}`);
    const data = await resp.json();
    this.chunkCache.set(key, data);
    return data;
  }

  async fifoWindow(kind, indexObj, t_ms, wantRows) {
    const ch = findChunk(indexObj.chunks, t_ms);
    const cur = await this.loadChunk(kind, ch.file);
    let prev = null;
    const chIdx = indexObj.chunks.findIndex(x => x.file === ch.file);
    if (chIdx > 0) prev = await this.loadChunk(kind, indexObj.chunks[chIdx - 1].file);

    const merged = [];
    const cols = cur.cols;
    const pushRows = (chunk) => {
      if (!chunk) return;
      for (let i = 0; i < chunk.t_ms.length; i++) {
        if (chunk.t_ms[i] <= t_ms) merged.push([chunk.t_ms[i], ...chunk.rows[i]]);
      }
    };
    pushRows(prev);
    pushRows(cur);

    const tail = merged.slice(Math.max(0, merged.length - wantRows));
    return {
      cols: ['t_ms', ...cols],
      rows: tail.map(r => r.map(v => isEmpty(v) ? '' : v)),
    };
  }

  async updateAt(t_ms) {
    const pb = this.analyze(t_ms);
    if (pb.nonstandard) this.rebuildListsTo(t_ms);

    const { har, downloads, bundles } = this.data;
    const harPos = bisectRight(har.events, t_ms) - 1;
    const dlPos = bisectRight(downloads.events, t_ms) - 1;
    const bPos = bisectRight(bundles.events, t_ms) - 1;

    const harE = harPos >= 0 ? har.events[harPos] : null;
    const dlE = dlPos >= 0 ? downloads.events[dlPos] : null;
    const bE = bPos >= 0 ? bundles.events[bPos] : null;

    const harPulse = harPos !== this.last.harIdx;
    this.last.harIdx = harPos;

    let dlNewFile = false, dlNewVault = false;
    if (dlPos !== this.last.dlIdx) {
      this.last.dlIdx = dlPos;
      let downloaded = safePick(dlE, 'downloaded_file');
      let vault = safePick(dlE, 'vault_file');
      if (downloaded && /^\d{4}__/.test(downloaded)) { vault = downloaded; downloaded = null; }
      if (downloaded && downloaded !== this.last.dl_file) { dlNewFile = true; this.last.dl_file = downloaded; }
      if (vault && vault !== this.last.vault_file) { dlNewVault = true; this.last.vault_file = vault; }
    }

    const bundleId = bE ? Number(bE.bundle_id ?? 0) : 0;
    const bundleChanged = bundleId !== this.last.bundleId;
    this.last.bundleId = bundleId;

    // Emit bundle lifecycle events for animation engine
    if (bE && bPos !== this.last.bIdx) {
      this.last.bIdx = bPos;
      const sysTimeKey = String(bE.sys_time_key ?? '');

      if (sysTimeKey !== this.last.sysTimeKey) {
        this.last.sysTimeKey = sysTimeKey;
        if (this.onBundleLifecycle) {
          this.onBundleLifecycle(sysTimeKey, bundleId, bE);
        }
      }
    }

    // Sticky download display
    const dlSticky = Object.assign({}, dlE || {});
    if (isEmpty(dlSticky.downloaded_file)) dlSticky.downloaded_file = this.last.dl_file;
    if (isEmpty(dlSticky.vault_file)) dlSticky.vault_file = this.last.vault_file;

    // Streaming tables
    let netWindow = null, httpWindow = null;
    try { netWindow = await this.fifoWindow('network_stream', this.data.netIndex, t_ms, this.config.network_fifo_rows ?? 100); } catch (e) {}
    try { httpWindow = await this.fifoWindow('http_events', this.data.httpIndex, t_ms, this.config.http_fifo_rows ?? 20); } catch (e) {}

    const tickState = {
      t_ms,
      timecode: tc30(t_ms, this.data.meta?.fps ?? 30),
      playback: pb,
      har: { event: harE, pos: harPos, pulse: harPulse },
      downloads: { event: dlE, pos: dlPos, newFile: dlNewFile, newVault: dlNewVault, sticky: dlSticky },
      bundle: { event: bE, pos: bPos, id: bundleId, changed: bundleChanged },
      lists: this.lists,
      network: netWindow,
      http: httpWindow,
      isPlaying: this.isPlaying,
    };

    if (this.onTick) this.onTick(tickState);
  }

  async safeUpdate(t_ms) {
    if (this.inflight) { this.queuedMs = t_ms; return; }
    this.inflight = true;
    try {
      await this.updateAt(t_ms);
    } finally {
      this.inflight = false;
      if (this.queuedMs !== null) {
        const q = this.queuedMs;
        this.queuedMs = null;
        await this.safeUpdate(q);
      }
    }
  }

  bindVideo(videoEl) {
    this.videoEl = videoEl;
    const tickMs = this.config.tick_ms ?? 33;

    videoEl.addEventListener('play', () => { this.isPlaying = true; });
    videoEl.addEventListener('playing', () => { this.isPlaying = true; });
    videoEl.addEventListener('pause', () => { this.isPlaying = false; });
    videoEl.addEventListener('ended', () => { this.isPlaying = false; });

    videoEl.addEventListener('seeked', async () => {
      const t_ms = Math.round(videoEl.currentTime * 1000);
      this.rebuildListsTo(t_ms);
      await this.safeUpdate(t_ms);
    });

    this.tickTimer = setInterval(() => {
      // Check actual video state — don't rely solely on play event firing
      if (!this.isPlaying && !videoEl.paused && videoEl.currentTime > 0) {
        this.isPlaying = true;
      }
      if (!this.isPlaying) return;
      const t_ms = Math.round(videoEl.currentTime * 1000);
      this.safeUpdate(t_ms).catch(console.error);
    }, tickMs);

    this.rebuildListsTo(0);
    this.safeUpdate(0).catch(console.error);
  }

  destroy() {
    if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null; }
  }

  /**
   * Parse URL time parameter and seek video to that position.
   * Supports: ?t=120 (seconds), ?t=2:00 (M:SS), ?t=1:30:00 (H:MM:SS), ?t=1h30m15s
   * Call after bindVideo() and setting video src.
   */
  static applyUrlTimecode(videoEl) {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get('t');
    if (!raw) return;

    let seconds = 0;

    // Try H:MM:SS or M:SS or SS format
    if (raw.includes(':')) {
      const parts = raw.split(':').map(Number);
      if (parts.length === 3) seconds = parts[0] * 3600 + parts[1] * 60 + parts[2];
      else if (parts.length === 2) seconds = parts[0] * 60 + parts[1];
      else seconds = parts[0] || 0;
    }
    // Try YouTube-style 1h30m15s
    else if (/[hms]/i.test(raw)) {
      const h = raw.match(/(\d+)h/i);
      const m = raw.match(/(\d+)m/i);
      const s = raw.match(/(\d+)s/i);
      if (h) seconds += parseInt(h[1]) * 3600;
      if (m) seconds += parseInt(m[1]) * 60;
      if (s) seconds += parseInt(s[1]);
    }
    // Plain number = seconds
    else {
      seconds = parseFloat(raw) || 0;
    }

    if (seconds <= 0) return;

    // Seek once video metadata is loaded (so duration is known)
    const doSeek = () => {
      if (seconds <= videoEl.duration) {
        videoEl.currentTime = seconds;
      }
    };

    if (videoEl.readyState >= 1) {
      doSeek();
    } else {
      videoEl.addEventListener('loadedmetadata', doSeek, { once: true });
    }
  }
}
