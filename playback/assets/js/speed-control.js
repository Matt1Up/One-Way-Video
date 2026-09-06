// speed-control.js — Playback speed UI (0.25x to 4.0x)

export class SpeedControl {
  constructor(config, videoEl, animationEngine) {
    this.min = config.playback_speed_min || 0.25;
    this.max = config.playback_speed_max || 4.0;
    this.step = 0.25;
    this.current = config.playback_speed_default || 1.0;
    this.videoEl = videoEl;
    this.animationEngine = animationEngine;
    this.displayEl = null;
    this.apply();
  }

  setDisplayElement(el) {
    this.displayEl = el;
    this.render();
  }

  apply() {
    if (this.videoEl) this.videoEl.playbackRate = this.current;
    if (this.animationEngine) this.animationEngine.setSpeed(this.current);
    this.render();
  }

  increase() {
    this.current = Math.min(this.max, +(this.current + this.step).toFixed(2));
    this.apply();
  }

  decrease() {
    this.current = Math.max(this.min, +(this.current - this.step).toFixed(2));
    this.apply();
  }

  set(speed) {
    this.current = Math.max(this.min, Math.min(this.max, speed));
    this.apply();
  }

  render() {
    if (this.displayEl) {
      this.displayEl.textContent = this.current.toFixed(2);
    }
  }
}
