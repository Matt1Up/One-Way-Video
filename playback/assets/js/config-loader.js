// config-loader.js — Loads config.json, exposes resolveUrl() globally
// THE single source of truth for all file path resolution.

let CONFIG = null;
let ROOT_PREFIX = '';  // relative path from current page to project root

export async function loadConfig(configPath, rootPrefix) {
  // rootPrefix: the relative path from the calling page to the project root
  // e.g. '../' from pages/foo.html, '' from index.html
  if (rootPrefix !== undefined) {
    ROOT_PREFIX = rootPrefix;
  } else {
    // Auto-detect: if we're in a subdirectory (pages/), prefix with ../
    const depth = window.location.pathname.replace(/\/[^/]*$/, '').split('/').filter(Boolean).length;
    ROOT_PREFIX = configPath ? '' : '../';
  }

  const actualConfigPath = configPath || (ROOT_PREFIX + 'config.json');
  const r = await fetch(actualConfigPath, { cache: 'no-store' });
  if (!r.ok) throw new Error(`Failed to load config: ${r.status}`);
  CONFIG = await r.json();

  // Also wait for StorjSwitch config to be ready so resolveUrl() can
  // synchronously decide whether to rewrite video URLs to the Storj
  // mirror. No-op if storj-switch.js isn't loaded on the page.
  if (typeof window !== 'undefined' && window.StorjSwitch && typeof window.StorjSwitch.load === 'function') {
    try { await window.StorjSwitch.load(); } catch (e) { /* ignore — fall back to local */ }
  }

  return CONFIG;
}

export function getConfig() {
  return CONFIG;
}

// Central path resolver — ALL file references go through this.
// Prepends ROOT_PREFIX so paths resolve correctly regardless of which
// page (root or pages/) is calling.
export function resolveUrl(baseKey, sessionSlug, relativePath) {
  if (!CONFIG) throw new Error('Config not loaded — call loadConfig() first');
  const base = CONFIG.bases[baseKey];
  if (base === undefined) throw new Error(`Unknown base key: ${baseKey}`);

  // If the base is already an absolute URL (http/https), use it directly
  if (/^https?:\/\//.test(base)) {
    return `${base}/${sessionSlug}/${relativePath}`;
  }

  // Compute the local relative URL first (legacy default behavior)
  const localUrl = `${ROOT_PREFIX}${base}/${sessionSlug}/${relativePath}`;

  // For video files, optionally route through StorjSwitch when the global
  // mode is "storj" or "auto". Resolve the relative URL to a site-absolute
  // path (using the URL constructor) so storj_base + path produces a clean
  // CDN URL. Falls back silently if StorjSwitch isn't ready.
  if (baseKey === 'video' && typeof window !== 'undefined' && window.StorjSwitch
      && window.StorjSwitch._config) {
    try {
      const mode = window.StorjSwitch.getMode();
      if (mode === 'storj' || mode === 'auto') {
        const absolutePath = new URL(localUrl, window.location.href).pathname;
        const switched = window.StorjSwitch.resolveUrlSync(absolutePath);
        if (switched && switched !== absolutePath) return switched;
      }
    } catch (e) { /* fall through to local */ }
  }

  return localUrl;
}

export function getSessionConfig(slug) {
  if (!CONFIG) return null;
  return CONFIG.sessions.find(s => s.slug === slug) || null;
}

export function getSessionSlug() {
  const path = window.location.pathname;
  return path.split('/').pop().replace('.html', '');
}

export function padBundleId(id) {
  return String(id).padStart(4, '0');
}
