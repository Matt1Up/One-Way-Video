#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
overlay_vcam_hash.py
- Push a 1920x72 (default) hash overlay into a v4l2loopback device for OBS input.
- EXACT font+size cadence: Liberation Mono Bold 36, refresh 0.5s.
- Reads run/state.json (key=last_hash) under shared lock.
- Requires: ffmpeg and LiberationMono-Bold.ttf (package: fonts-liberation).
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, os, time, fcntl, signal, subprocess
from pathlib import Path

# ---------- Portable paths ----------
from evidence_capture.paths import RUN, ensure_runtime_dirs

STATE_JSON = RUN / "state.json"
STATE_LOCK = RUN / "state.lock"

def state_read_last_hash(key: str = "last_hash") -> str:
    """Read a value from state.json under a shared lock; return '' if missing."""
    try:
        STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_LOCK, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                if not STATE_JSON.exists():
                    return ""
                with STATE_JSON.open("r", encoding="utf-8") as f:
                    st = json.load(f)
                val = st.get(key) or ""
                return val if isinstance(val, str) else str(val)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        return ""

def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/video20")
    ap.add_argument("--w", type=int, default=1920)
    ap.add_argument("--h", type=int, default=72)
    ap.add_argument("--refresh", type=float, default=0.5)
    ap.add_argument("--font-file",
        default="/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf")
    ap.add_argument("--font-size", type=int, default=36)
    ap.add_argument("--font-color", default="white")
    ap.add_argument("--bg", default="black")  # v4l2 has no alpha; key/compose in OBS if needed
    ap.add_argument("--key", default="last_hash")
    ap.add_argument("--placeholder", default="")
    args = ap.parse_args()

    # Text file that ffmpeg's drawtext will reload
    textfile = RUN / "web-server" / "overlay_text.txt"
    textfile.parent.mkdir(parents=True, exist_ok=True)

    def write_text(s: str):
        tmp = textfile.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(s + "\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, textfile)

    # Seed with current state
    write_text(state_read_last_hash(args.key) or args.placeholder)

    # ffmpeg pipeline -> v4l2
    # vertically center the text: y=(h-text_h)/2
    draw = (f"drawtext=fontfile='{args.font_file}':"
            f"textfile='{textfile}':reload=1:"
            f"fontcolor={args.font_color}:fontsize={args.font_size}:"
            f"x=10:y=(h-text_h)/2")
    cmd = [
        "ffmpeg", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", f"color=c={args.bg}:s={args.w}x{args.h}:r=30",
        "-vf", draw,
        "-pix_fmt", "yuv420p",
        "-f", "v4l2", args.device
    ]

    proc = subprocess.Popen(cmd)

    stop = False
    def _sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    last = None
    try:
        while not stop and proc.poll() is None:
            cur = state_read_last_hash(args.key) or args.placeholder
            if cur != last:
                write_text(cur)
                last = cur
            time.sleep(max(0.05, args.refresh))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

if __name__ == "__main__":
    main()
