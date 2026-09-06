// png-loader.js — PNG screenshot preloading, buffer management, and display
// PNG display is triggered by image.sha256.sys_time lifecycle events
// so the PNG appears ~1.5s BEFORE the video hash changes (matching real capture timing).

import { resolveUrl, padBundleId } from './config-loader.js';

export class PngLoader {
  constructor(slug, preloadAhead = 5, bufferBehind = 3) {
    this.slug = slug;
    this.preloadAhead = preloadAhead;
    this.bufferBehind = bufferBehind;
    this.cache = new Map();
    this.loading = new Set();
    this.currentId = -1;
    this.displayedId = -1;
    this.displayEl = null;
  }

  pngUrl(id) {
    return resolveUrl('session', this.slug, `${padBundleId(id)}.png`);
  }

  preload(id) {
    if (this.cache.has(id) || this.loading.has(id)) return;
    this.loading.add(id);
    const img = new Image();
    img.onload = () => {
      this.cache.set(id, img);
      this.loading.delete(id);
      // If this is the current PNG and it hasn't been displayed yet, show it
      if (id === this.currentId && this.displayedId !== id && this.displayEl) {
        this.display(id);
      }
    };
    img.onerror = () => { this.loading.delete(id); };
    img.src = this.pngUrl(id);
  }

  // Set the displayed image
  display(id) {
    if (!this.displayEl) return;
    const img = this.cache.get(id);
    if (img) {
      this.displayEl.src = img.src;
      this.displayEl.classList.remove('png-loading');
      this.displayedId = id;
    } else {
      this.displayEl.src = this.pngUrl(id);
      this.displayEl.classList.add('png-loading');
      this.preload(id);
    }
  }

  // Called on bundle change — preloads only, does NOT display.
  // Display is triggered by onImageCaptured lifecycle event (image.sha256.sys_time)
  // which fires ~1.5s before the video hash changes — matching real capture timing.
  onBundleChange(newId, maxBundleId) {
    this.currentId = newId;

    const minKeep = newId - this.bufferBehind;
    const maxKeep = newId + this.preloadAhead;
    for (const id of this.cache.keys()) {
      if (id < minKeep || id > maxKeep) this.cache.delete(id);
    }

    // Preload current + ahead (but don't display — wait for lifecycle event)
    this.preload(newId);
    const max = maxBundleId ?? newId + this.preloadAhead;
    for (let i = newId + 1; i <= Math.min(newId + this.preloadAhead, max); i++) {
      this.preload(i);
    }
  }

  // Called on seek/jump — display immediately
  displayImmediate(id) {
    this.currentId = id;
    this.display(id);
  }

  setDisplayElement(el) { this.displayEl = el; }

  flush() {
    this.cache.clear();
    this.loading.clear();
    this.currentId = -1;
    this.displayedId = -1;
  }
}
