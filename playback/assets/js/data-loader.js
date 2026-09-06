// data-loader.js — Fetch meta.json and all event stream files for a session

import { resolveUrl } from './config-loader.js';

async function fetchJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`fetch ${url}: ${r.status}`);
  return r.json();
}

export async function loadSessionData(slug) {
  const metaUrl = resolveUrl('session', slug, 'data/meta.json');
  const meta = await fetchJSON(metaUrl);

  const base = (file) => resolveUrl('session', slug, 'data/' + file);

  const [har, downloads, bundles, netIndex, httpIndex] = await Promise.all([
    fetchJSON(base(meta.outputs.har.file)),
    fetchJSON(base(meta.outputs.downloads.file)),
    fetchJSON(base(meta.outputs.bundles.file)),
    fetchJSON(base(meta.outputs.network_stream.index)),
    fetchJSON(base(meta.outputs.http_events.index)),
  ]);

  return { meta, har, downloads, bundles, netIndex, httpIndex };
}
