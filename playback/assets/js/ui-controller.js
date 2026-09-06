// ui-controller.js — v3: Card accumulation, provenance panel, PDF viewer, download links

import { resolveUrl, padBundleId } from './config-loader.js';

const $ = (sel) => document.querySelector(sel);

// Master provenance file list (uniform across all sessions)
const PROVENANCE_FILES = {
  'Video': ['video_full.mp4', 'video_web.mp4'],
  'PDF Reports': [
    '01__bundle_sha256_ocr.pdf', '02__bundle_downloads.pdf', '03__bundle_overview.pdf',
    '04__bundle_http_streams.pdf', '05__bundle_http_events.pdf', '06__bundle_net_streams.pdf',
    '07__bundle_ots_report.pdf', '08__bundle_rt_report.pdf',
    '09__har_case_details.pdf', '10__har_mcro_form.pdf', '11__har_case_search.pdf',
  ],
  'CSV + ZIP Data': [
    '00__report_master.csv', '00__report_master.zip',
    '00__timestamp_master.csv', '00__timestamp_master.zip',
    '01__bundle_sha256_ocr.csv', '01__bundle_sha256_ocr.zip',
    '02__bundle_downloads.csv', '02__bundle_downloads.zip',
    '03__bundle_overview.csv', '03__bundle_overview.zip',
    '04__bundle_http_streams.csv', '04__bundle_http_streams.zip',
    '05__bundle_http_events.csv', '05__bundle_http_events.zip',
    '06__bundle_net_streams.csv', '06__bundle_net_streams.zip',
    '07__bundle_ots_report.csv', '07__bundle_ots_report.zip',
    '08__bundle_rt_report.csv', '08__bundle_rt_report.zip',
    '09__har_case_details.csv', '09__har_case_details.zip',
    '10__har_mcro_form.csv', '10__har_mcro_form.zip',
    '11__har_case_search.csv', '11__har_case_search.zip',
  ],
  'Archives': [
    'downloads.zip', 'reports.zip', 'bundles.json', 'bundles.json.zip',
    'bundles.zip', 'csv.zip', 'har.zip', 'mcro.zip', 'ocr_png.zip', 'ots_upgraded.zip',
  ],
  'Master Workbooks': [
    'reports_master.xlsx', 'reports_master.xlsx.zip',
  ],
  'Scripts': [
    '00__bundle_verification_and_reporting_script.zip',
  ],
  'Playback': [
    'playback_seq_master_w_timecode.xlsx', 'playback_seq_master_w_timecode.xlsx.zip',
  ],
};

export class UIController {
  constructor({ config, sessionConfig, slug }) {
    this.config = config;
    this.session = sessionConfig;
    this.slug = slug;
    this.lastTickState = null;
    this.init();
  }

  init() {
    const nameEl = $('#session-name');
    if (nameEl) nameEl.textContent = this.session?.name || this.slug;
  }

  buildProvenanceList(slug) {
    const listEl = $('#prov-list');
    if (!listEl) return;
    let html = '';
    for (const [section, files] of Object.entries(PROVENANCE_FILES)) {
      html += `<div class="prov-section"><div class="prov-section-title">${this.esc(section)}</div>`;
      for (const f of files) {
        const url = resolveUrl('session', slug, f);
        html += `<a class="prov-file" href="${url}" target="_blank">${this.esc(f)}</a>`;
      }
      html += '</div>';
    }
    listEl.innerHTML = html;
  }

  onTick(state) {
    this.lastTickState = state;

    const tcEl = $('#transport-tc');
    if (tcEl) tcEl.textContent = state.timecode;

    this.updateSummaryLines(state);
  }

  updateSummaryLines(state) {
    // HTTP
    const httpLine = $('#summary-http');
    if (httpLine && state.har.event) {
      const e = state.har.event;
      httpLine.innerHTML = `<span class="summary-arrow">&#x25BD;</span><span class="summary-label">HTTP</span><span class="summary-value">${this.esc(e.url || '')}</span>`;
    }

    // Network
    const netLine = $('#summary-network');
    if (netLine && state.network?.rows.length) {
      const cols = state.network.cols;
      const last = state.network.rows[state.network.rows.length - 1];
      const proto = last[cols.indexOf('proto')] || '';
      const src = last[cols.indexOf('src')] || '';
      const dst = last[cols.indexOf('dst')] || '';
      netLine.innerHTML = `<span class="summary-arrow">&#x25BD;</span><span class="summary-label">NETWORK</span><span class="summary-value">${this.esc(src)} &nbsp; ${this.esc(proto)} &nbsp; ${this.esc(dst)}</span>`;
    }

    // HTTP Events
    const httpEvtLine = $('#summary-http-events');
    if (httpEvtLine && state.http?.rows.length) {
      const cols = state.http.cols;
      const last = state.http.rows[state.http.rows.length - 1];
      const url = last[cols.indexOf('url')] || '';
      httpEvtLine.innerHTML = `<span class="summary-arrow">&#x25BD;</span><span class="summary-label">HTTP EVENTS</span><span class="summary-value">${this.esc(url)}</span>`;
    }

    // Downloads
    const dlLine = $('#summary-downloads');
    if (dlLine) {
      const dl = state.downloads.sticky;
      if (dl && (dl.downloaded_file || dl.vault_file)) {
        const file = dl.downloaded_file || '';
        const vault = dl.vault_file || '';
        let html = `<span class="summary-arrow">&#x25BD;</span><span class="summary-label">DOWNLOADS</span>`;
        if (file) {
          const fileUrl = resolveUrl('session', this.slug, file);
          html += `<a class="summary-link dl-link" href="${fileUrl}" target="_blank">${this.esc(file)}</a>`;
        }
        if (vault) {
          const vaultUrl = resolveUrl('session', this.slug, vault);
          html += ` | Vault File: <a class="summary-link dl-link" href="${vaultUrl}" target="_blank">${this.esc(vault)}</a>`;
        }
        dlLine.innerHTML = html;
      }
    }
  }

  // Render bundle tree into a card body
  renderBundleTree(bundleId, rawBundle) {
    const bodyEl = document.getElementById(`card-body-${bundleId}`);
    if (!bodyEl || !rawBundle) return;
    bodyEl.innerHTML = '<div class="json-tree">' + this.buildTree(rawBundle, 0) + '</div>';
    bodyEl.querySelectorAll('.tree-toggle').forEach(toggle => {
      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        const content = toggle.nextElementSibling;
        if (content) {
          content.classList.toggle('collapsed');
          toggle.classList.toggle('open');
        }
      });
    });
  }

  renderCardHash(bundleId, hash) {
    const el = document.getElementById(`card-hash-${bundleId}`);
    if (el && hash) el.textContent = hash.substring(0, 32) + '...';
  }

  // PDF viewer: load a non-vault PDF by filename
  loadPdf(slug, filename) {
    const url = resolveUrl('session', slug, filename);
    this.loadPdfFromUrl(url);
  }

  loadPdfFromUrl(url) {
    const wrap = document.getElementById('pdf-wrap');
    if (!wrap) return;
    // Use iframe for PDF rendering (simpler than pdf.js, works everywhere)
    wrap.innerHTML = `<iframe src="${url}" style="width:100%;height:100%;border:none"></iframe>`;
  }

  loadImageInViewer(url) {
    const wrap = document.getElementById('pdf-wrap');
    if (!wrap) return;
    wrap.innerHTML = `<img src="${url}" style="max-width:100%;display:block;margin:0 auto">`;
  }

  buildTree(obj, depth) {
    if (obj === null) return '<span class="json-null">null</span>';
    if (typeof obj === 'boolean') return `<span class="json-bool">${obj}</span>`;
    if (typeof obj === 'number') return `<span class="json-num">${obj}</span>`;
    if (typeof obj === 'string') {
      if (/\.(json|png|pdf|ots|csv|zip)(\.|$)/i.test(obj) || /^\d{4}__/.test(obj) || /\.ots__time-stamp\.json$/.test(obj)) {
        // Strip to filename only — paths like /home/.../bundles/0064.png become 0064.png
        const filename = obj.includes('/') ? obj.split('/').pop() : obj;
        const url = resolveUrl('session', this.slug, filename);
        return `<a href="${url}" target="_blank" class="json-file-link">${this.esc(filename)}</a>`;
      }
      if (/^[0-9a-f]{64}$/i.test(obj)) {
        return `<span class="json-hash">${obj}</span>`;
      }
      return `<span class="json-str">"${this.esc(obj)}"</span>`;
    }
    if (Array.isArray(obj)) {
      if (obj.length === 0) return '<span class="json-empty">[]</span>';
      const collapsed = depth > 1 ? ' collapsed' : '';
      const arrow = depth > 1 ? '&#x25B6;' : '&#x25BC;';
      let html = `<span class="tree-toggle${depth <= 1 ? ' open' : ''}">${arrow} [${obj.length}]</span><div class="tree-content${collapsed}">`;
      obj.forEach((item, i) => {
        html += `<div class="tree-item">${this.buildTree(item, depth + 1)}${i < obj.length - 1 ? ',' : ''}</div>`;
      });
      html += '</div>';
      return html;
    }
    if (typeof obj === 'object') {
      const keys = Object.keys(obj);
      if (keys.length === 0) return '<span class="json-empty">{}</span>';
      const collapsed = depth > 1 ? ' collapsed' : '';
      const arrow = depth > 1 ? '&#x25B6;' : '&#x25BC;';
      let html = `<span class="tree-toggle${depth <= 1 ? ' open' : ''}">${arrow} {${keys.length}}</span><div class="tree-content${collapsed}">`;
      keys.forEach((key, i) => {
        html += `<div class="tree-item"><span class="json-key">"${this.esc(key)}"</span>: ${this.buildTree(obj[key], depth + 1)}${i < keys.length - 1 ? ',' : ''}</div>`;
      });
      html += '</div>';
      return html;
    }
    return String(obj);
  }

  esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
}
