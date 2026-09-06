// animation-engine.js — Hash-loop animation system (v3)
// Driven by actual sys_time_key events from bundle data.
// Creates red hash clones that split and fly to their destinations.

import { padBundleId, resolveUrl } from './config-loader.js';

export class AnimationEngine {
  constructor(config) {
    this.offsets = config.animation_offsets || {};
    this.currentSpeed = config.playback_speed_default || 1.0;
    this.pngEl = null;
    this.hashZoneEl = null;
    this.cardStackEl = null;
    this.overlayEl = null;
    this.currentBundleId = -1;
    this.slug = null;
    this.pngLoader = null;
  }

  setElements({ pngEl, hashZoneEl, cardStackEl, overlayEl }) {
    this.pngEl = pngEl;
    this.hashZoneEl = hashZoneEl;
    this.cardStackEl = cardStackEl;
    this.overlayEl = overlayEl;
  }

  setSpeed(speed) {
    this.currentSpeed = speed;
    document.documentElement.style.setProperty('--anim-scale', String(1 / speed));
  }

  scaledMs(ms) {
    return ms / this.currentSpeed;
  }

  // Called by playback engine on each bundle lifecycle event
  onBundleLifecycle(sysTimeKey, bundleId, eventData) {
    switch (sysTimeKey) {
      case 'prev.last_hash.sys_time':
        this.onChainLink(bundleId, eventData);
        break;
      case 'sys_time_in':
        this.onBundleOpen(bundleId, eventData);
        break;
      case 'image.sha256.sys_time':
        this.onImageCaptured(bundleId, eventData);
        break;
      case 'sys_time_out':
        this.onBundleClose(bundleId, eventData);
        break;
    }
  }

  // 1. CHAIN LINK — previous bundle's hash deposited into new bundle
  onChainLink(bundleId, data) {
    this.currentBundleId = bundleId;
    const hash = data.last_bundle_hash;
    if (!hash) return;

    // Update the card hash display if it exists
    const cardHash = document.getElementById(`card-hash-${bundleId}`);
    if (cardHash) cardHash.textContent = hash.substring(0, 32) + '...';
  }

  // 2. BUNDLE OPENS — animation only (card creation handled by ensureCards)
  onBundleOpen(bundleId, data) {
    // Animation-only — the card was already created by ensureCards()
  }

  // Called from main tick handler — ensures all cards from 0..bundleId exist
  ensureCards(bundleId, bundleLoader) {
    if (!this.cardStackEl) return;

    // Find highest existing card
    let highestExisting = -1;
    this.cardStackEl.querySelectorAll('.bundle-card').forEach(c => {
      const bid = parseInt(c.dataset.bundleId);
      if (bid > highestExisting) highestExisting = bid;
    });

    // Create any missing cards from highestExisting+1 to bundleId
    for (let id = highestExisting + 1; id <= bundleId; id++) {
      // Collapse previous current card
      const prevCurrent = this.cardStackEl.querySelector('.bundle-card.current');
      if (prevCurrent) {
        prevCurrent.classList.remove('current');
        prevCurrent.classList.add('collapsed');
        const toggle = prevCurrent.querySelector('.card-toggle');
        if (toggle) toggle.innerHTML = '&#x25B6;';
      }

      const padId = padBundleId(id);
      const card = document.createElement('div');
      card.className = 'bundle-card collapsed';
      card.dataset.bundleId = id;

      const raw = bundleLoader ? bundleLoader.getBundle(id) : null;
      const hashText = raw?.prev?.last_hash?.value || '';
      const hashShort = hashText ? hashText.substring(0, 32) + '...' : '';

      card.innerHTML = `
        <div class="card-header">
          <span class="card-toggle">&#x25B6;</span>
          <span class="card-title">Bundle <a href="${resolveUrl('session', this.slug, padId + '.json')}" target="_blank" class="card-file-link" onclick="event.stopPropagation()">${padId}.json</a></span>
          <span class="card-hash" id="card-hash-${id}">${hashShort}</span>
        </div>
        <div class="card-body" id="card-body-${id}"></div>
      `;
      this.cardStackEl.prepend(card);
    }

    // Mark the top card as current — but keep it COLLAPSED.
    // Only a user click should expand a card.
    const topCard = this.cardStackEl.querySelector(`.bundle-card[data-bundle-id="${bundleId}"]`);
    if (topCard && !topCard.classList.contains('current')) {
      // Remove current from any previous card
      const prev = this.cardStackEl.querySelector('.bundle-card.current');
      if (prev) prev.classList.remove('current');
      topCard.classList.add('current');
      // Keep it collapsed — current is just a highlight, not an expand
    }
  }

  // 3. PNG CAPTURED (image.sha256.sys_time) — display the PNG NOW
  // This fires ~1.5s BEFORE sys_time_out (when the video hash changes).
  // The PNG shows what the video looks like RIGHT NOW, before the hash updates.
  // No slide animation — just swap the image cleanly.
  onImageCaptured(bundleId, data) {
    if (!this.pngEl) return;
    // Display the PNG at the exact moment it was captured
    if (this.pngLoader) this.pngLoader.display(bundleId);

    // Update image hash in zone
    const imgHash = data.last_img_hash;
    if (imgHash && this.hashZoneEl) {
      const imgVal = this.hashZoneEl.querySelector('.hash-img-value');
      const imgLabel = this.hashZoneEl.querySelector('.hash-img-label');
      const padId = padBundleId(bundleId);
      if (imgVal) imgVal.textContent = imgHash;
      if (imgLabel) {
        const pngUrl = this.slug ? resolveUrl('session', this.slug, `${padId}.png`) : '#';
        imgLabel.innerHTML = `<a class="hash-file-link" href="${pngUrl}" target="_blank">${padId}.png</a> | OBS Screen Capture SHA256 Hash`;
      }
    }
  }

  // 4. BUNDLE CLOSES — hash pops out, red clones fly up and down
  onBundleClose(bundleId, data) {
    if (!this.hashZoneEl || !this.overlayEl) return;

    const bundleHashEl = this.hashZoneEl.querySelector('.hash-bundle-value');
    const bundleLabelEl = this.hashZoneEl.querySelector('.hash-bundle-label');
    const padId = padBundleId(bundleId);
    const prevPadId = padBundleId(Math.max(0, bundleId - 1));

    // Get the hash that was just computed (this bundle's hash will be the NEXT bundle's last_hash)
    // For display: show the previous bundle's hash that's stored in THIS bundle
    const hash = data.last_bundle_hash || '';

    // Flash the bundle hash zone red
    if (bundleHashEl && hash) {
      bundleHashEl.textContent = hash;
      bundleHashEl.classList.remove('hash-flash-red');
      void bundleHashEl.offsetWidth;
      bundleHashEl.classList.add('hash-flash-red');
    }
    if (bundleLabelEl) {
      const jsonUrl = this.slug ? resolveUrl('session', this.slug, `${prevPadId}.json`) : '#';
      bundleLabelEl.innerHTML = `<a class="hash-file-link" href="${jsonUrl}" target="_blank">${prevPadId}.json</a> | Bundle ${prevPadId} SHA256 Hash`;
    }

    // Create flying red hash clones
    if (hash) {
      this.flyHashClone(hash, 'up', bundleHashEl);
      this.flyHashClone(hash, 'down', bundleHashEl);
    }
  }

  // Create a red hash clone that flies from the card to a destination
  flyHashClone(hash, direction, sourceEl) {
    if (!this.overlayEl || !sourceEl) return;

    const rect = sourceEl.getBoundingClientRect();
    const clone = document.createElement('div');
    clone.className = 'hash-clone';
    clone.textContent = hash.substring(0, 32) + '...';
    clone.style.left = rect.left + 'px';
    clone.style.top = rect.top + 'px';
    clone.style.width = rect.width + 'px';
    this.overlayEl.appendChild(clone);

    // Animate after one frame
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        clone.classList.add(direction === 'up' ? 'fly-up' : 'fly-down');
        setTimeout(() => {
          clone.remove();
        }, this.scaledMs(500));
      });
    });
  }

  // Update hash zone from loaded raw bundle JSON
  updateFromRawBundle(rawBundle) {
    if (!rawBundle || !this.hashZoneEl) return;

    const imgHashEl = this.hashZoneEl.querySelector('.hash-img-value');
    const bundleHashEl = this.hashZoneEl.querySelector('.hash-bundle-value');
    const imgLabelEl = this.hashZoneEl.querySelector('.hash-img-label');
    const bundleLabelEl = this.hashZoneEl.querySelector('.hash-bundle-label');

    const padId = padBundleId(rawBundle.id || 0);
    const prevPadId = padBundleId(Math.max(0, parseInt(rawBundle.id || 0) - 1));

    if (rawBundle.image?.sha256?.value && imgHashEl) {
      imgHashEl.textContent = rawBundle.image.sha256.value;
    }
    if (imgLabelEl) {
      const pngUrl = this.slug ? resolveUrl('session', this.slug, `${padId}.png`) : '#';
      imgLabelEl.innerHTML = `<a class="hash-file-link" href="${pngUrl}" target="_blank">${padId}.png</a> | OBS Screen Capture SHA256 Hash`;
    }
    if (rawBundle.prev?.last_hash?.value && bundleHashEl) {
      bundleHashEl.textContent = rawBundle.prev.last_hash.value;
    }
    if (bundleLabelEl) {
      const jsonUrl = this.slug ? resolveUrl('session', this.slug, `${prevPadId}.json`) : '#';
      bundleLabelEl.innerHTML = `<a class="hash-file-link" href="${jsonUrl}" target="_blank">${prevPadId}.json</a> | Bundle ${prevPadId} SHA256 Hash`;
    }
  }
}
