#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# OPTIONAL ALTERNATE OVERLAY (GStreamer/PipeWire backend). Not launched by
# control_all.py; the default overlay path is overlay_vcam_hash.py (v4l2/PyQt5).
# Requires the system PyGObject packages (python3-gi, gir1.2-gstreamer-1.0), which
# are available to the venv because setup.sh creates it with --system-site-packages.
"""
overlay_pw_hash.py — PipeWire RGBA overlay that renders last_hash (from run/state.json) with transparency.

Portability:
- No hard-coded HOME paths; anchors to repo root via evidence_capture.paths.
- Same visual defaults as before: w=1920, h=72, Liberation Mono Bold 36, refresh=0.5s.
- Publishes PipeWire node “Evidence Overlay (RGBA)” (internal name: ec_overlay_rgba).
- OBS: Add → Video Capture Device (PipeWire) (BETA) → pick “Evidence Overlay (RGBA)”.

Requires:
  sudo apt install python3-gi gir1.2-gst-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import json, fcntl, argparse

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GObject', '2.0')
from gi.repository import Gst, GLib

# ---------- Portable paths ----------
from evidence_capture.paths import RUN, ensure_runtime_dirs

STATE_JSON = RUN / 'state.json'
STATE_LOCK = RUN / 'state.lock'

def state_read_key(key: str) -> str:
    """Read a key from state.json with shared lock; return '' if missing."""
    try:
        STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_LOCK, 'a+') as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                if not STATE_JSON.exists():
                    return ""
                with STATE_JSON.open('r', encoding='utf-8') as f:
                    st = json.load(f)
                v = st.get(key) or ""
                return v if isinstance(v, str) else str(v)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        return ""

def build_pipeline(width: int, height: int, node_name: str, font_desc: str, color_hex: str):
    """
    Create a transparent RGBA pipeline with textoverlay whose 'text' is updated live.
    color_hex: '#FFFFFFFF' (ARGB) or '#FFFFFF' (RGB).
    """
    Gst.init(None)

    pipe_str = (
        "videotestsrc pattern=black is-live=true ! "
        f"video/x-raw,format=RGBA,framerate=30/1,width={width},height={height} ! "
        "alpha alpha=0.0 ! "                                    # fully transparent background
        "textoverlay name=tov "
        f"font-desc=\"{font_desc}\" "
        "valignment=top halignment=left line-alignment=left "
        "shaded-background=false draw-shadow=false draw-outline=false ! "
        "queue ! "
        "pipewiresink "
        f"stream-properties=props,node.name=\"{node_name}\",node.description=\"Evidence Overlay (RGBA)\""
    )
    pipeline = Gst.parse_launch(pipe_str)

    # Color as ARGB guint32
    def parse_argb(hexstr: str) -> int:
        hs = hexstr.lstrip('#')
        if len(hs) == 8:  # ARGB
            a = int(hs[0:2], 16); r = int(hs[2:4], 16); g = int(hs[4:6], 16); b = int(hs[6:8], 16)
        elif len(hs) == 6:  # RGB -> full alpha
            a = 255; r = int(hs[0:2], 16); g = int(hs[2:4], 16); b = int(hs[4:6], 16)
        else:
            a, r, g, b = 255, 255, 255, 255
        return (a << 24) | (r << 16) | (g << 8) | b

    tov = pipeline.get_by_name('tov')
    tov.set_property('color', parse_argb(color_hex))

    return pipeline, tov

def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser()
    ap.add_argument('--key', default='last_hash')
    ap.add_argument('--w', type=int, default=1920)
    ap.add_argument('--h', type=int, default=72)
    ap.add_argument('--refresh', type=float, default=0.5)
    ap.add_argument('--font', default='Liberation Mono')
    ap.add_argument('--font-weight', default='bold', choices=['normal', 'bold'])
    ap.add_argument('--font-size', type=int, default=36)
    ap.add_argument('--color', default='#FFFFFFFF', help='ARGB or RGB hex, e.g., #FFFFFFFF (white)')
    ap.add_argument('--node-name', default='ec_overlay_rgba')
    ap.add_argument('--placeholder', default='')
    args = ap.parse_args()

    weight = ' Bold' if args.font_weight.lower() == 'bold' else ''
    font_desc = f"{args.font}{weight} {args.font_size}"

    pipeline, tov = build_pipeline(args.w, args.h, args.node_name, font_desc, args.color)

    # Initial draw
    last_text = state_read_key(args.key) or args.placeholder
    tov.set_property('text', last_text)

    # Periodic updates (same cadence as the vcam overlay)
    def tick():
        nonlocal last_text
        t = state_read_key(args.key) or args.placeholder
        if t != last_text:
            tov.set_property('text', t)
            last_text = t
        return True
    GLib.timeout_add(int(args.refresh * 1000), tick)

    pipeline.set_state(Gst.State.PLAYING)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == '__main__':
    main()
