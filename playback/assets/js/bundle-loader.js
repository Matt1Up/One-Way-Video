// bundle-loader.js — Loads ALL bundles from bundles.json in one fetch.
// No more individual per-bundle loading. Everything in memory.

import { resolveUrl } from './config-loader.js';

export class BundleLoader {
  constructor(slug) {
    this.slug = slug;
    this.bundles = [];      // array indexed by bundle number
    this.bundleMap = new Map(); // id (string like "0042") -> bundle object
    this.loaded = false;
    this.loading = null;
    this.currentId = -1;
  }

  // Load the combined bundles.json once
  async loadAll() {
    if (this.loaded) return;
    if (this.loading) return this.loading;

    const url = resolveUrl('session', this.slug, 'bundles.json');
    this.loading = fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`bundles.json: ${r.status}`);
        return r.json();
      })
      .then(arr => {
        this.bundles = arr;
        for (const b of arr) {
          this.bundleMap.set(String(b.id), b);
          this.bundleMap.set(parseInt(b.id, 10), b);
        }
        this.loaded = true;
        this.loading = null;
      })
      .catch(err => {
        console.warn('Failed to load bundles.json:', err);
        this.loading = null;
      });

    return this.loading;
  }

  onBundleChange(newId) {
    this.currentId = newId;
  }

  getCurrentBundle() {
    return this.getBundle(this.currentId);
  }

  getBundle(id) {
    return this.bundleMap.get(id) || this.bundleMap.get(String(id).padStart(4, '0')) || null;
  }

  // Kept for compatibility — just returns from memory now
  async fetchBundle(id) {
    if (!this.loaded) await this.loadAll();
    return this.getBundle(id);
  }

  fileUrl(filename) {
    return resolveUrl('session', this.slug, filename);
  }
}
