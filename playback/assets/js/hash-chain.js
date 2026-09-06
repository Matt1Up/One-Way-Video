// hash-chain.js — Hash chain state tracking and display (v2)
// Full 64-char SHA-256 display, NEVER truncated.

export class HashChain {
  constructor() {
    this.currentBundleId = 0;
    this.totalBundles = 0;
    this.imgHash = null;
    this.bundleHash = null;
    this.prevBundleHash = null;
    this.prevImgHash = null;
  }

  updateFromBundleEvents(bundleEvents, currentPos) {
    if (currentPos < 0 || !bundleEvents?.length) return;

    const currentEvent = bundleEvents[currentPos];
    this.currentBundleId = Number(currentEvent.bundle_id ?? 0);

    // Scan backward then forward within this bundle to collect hashes
    for (let i = currentPos; i >= 0; i--) {
      const e = bundleEvents[i];
      if (Number(e.bundle_id) !== this.currentBundleId) break;
      if (e.last_bundle_hash && !this.prevBundleHash) this.prevBundleHash = e.last_bundle_hash;
      if (e.last_img_hash && !this.prevImgHash) this.prevImgHash = e.last_img_hash;
    }
    for (let i = currentPos + 1; i < bundleEvents.length; i++) {
      const e = bundleEvents[i];
      if (Number(e.bundle_id) !== this.currentBundleId) break;
      if (e.last_bundle_hash && !this.prevBundleHash) this.prevBundleHash = e.last_bundle_hash;
      if (e.last_img_hash && !this.prevImgHash) this.prevImgHash = e.last_img_hash;
    }
  }

  updateFromRawBundle(rawBundle) {
    if (!rawBundle) return;
    if (rawBundle.image?.sha256?.value) this.imgHash = rawBundle.image.sha256.value;
    if (rawBundle.prev?.last_hash?.value) this.bundleHash = rawBundle.prev.last_hash.value;
    if (rawBundle.prev?.last_img_hash?.value) this.prevImgHash = rawBundle.prev.last_img_hash.value;
  }
}
