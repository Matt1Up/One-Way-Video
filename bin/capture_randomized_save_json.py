#!/usr/bin/env python3
"""
capture_randomized_save_json.py - corrected behavior

Fixes included:
 - --fixed-scale: when set, the output width will equal img.width * scale_max (no random scaling).
 - --reset-counter: reliably resets the counter to 0 before start/run.
 - --no-meta: disables writing JSON sidecar files (only PNGs saved).
 - --no-overlay: disables drawing the last-hash overlay completely.
 - --no-counter: optional flag to avoid writing/using the numeric counter file (uses timestamp filenames).
 - Minor robustness fixes around counter init & argument parsing.

Usage examples:
  # foreground, reset counter, no meta, use v4l device, no overlay
  python3 capture_randomized_save_json.py run --reset-counter --no-meta --no-overlay --v4l-device /dev/video2 --interval 2

  # start/stop background daemon
  python3 bin/capture_randomized_save_json.py start --reset-counter --no-meta --no-overlay --v4l-device /dev/video2 --interval 2
  python3 bin/capture_randomized_save_json.py stop
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

from pathlib import Path
import argparse
import time
import os
import sys
import signal
import tempfile
import shutil
import subprocess
import json
from datetime import datetime, timezone
from io import BytesIO

# ======= PORTABLE PATHS (only change) =======
from evidence_capture.paths import RUN, BIN, ensure_runtime_dirs
from evidence_capture.state import state_get as _state_get, state_set as _state_set

# Scripts to trigger (non-blocking), same behavior as before:
SCRIPT_A = str(BIN / "script_A.py")     # run once (before first capture)
SCRIPT_B = str(BIN / "loop_json.py")    # run before every subsequent capture

# Logs/state/output all under repo-root run/
CAPTURE_LOG = str((RUN / "logs" / "capture_events.log"))
RUN_DIR     = RUN
PNG_DIR     = RUN / "bundles"

# Unified JSON state
STATE_JSON = RUN / "state.json"

PID_FILE = RUN / "capture.pid"

# ensure dirs exist (repo-root)
ensure_runtime_dirs()
for d in (PNG_DIR, RUN_DIR, RUN / "logs"):
    d.mkdir(parents=True, exist_ok=True)

# optional import mss; we'll handle if it's not available
try:
    import mss
    from mss import mss as mss_mss
except Exception:
    mss = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    print("Missing Pillow. Install with: pip install --user pillow", file=sys.stderr)
    raise

# ----- Utility functions -----
def parse_region(s: str):
    parts = {}
    for part in s.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            continue
        k, v = part.split("=")
        parts[k.strip()] = int(v.strip())
    if not all(k in parts for k in ("left", "top", "width", "height")):
        raise ValueError("Region must contain left,top,width,height")
    return {"left": parts["left"], "top": parts["top"], "width": parts["width"], "height": parts["height"]}

# --- JSON state helpers (delegated to evidence_capture.state) ---
def state_get(key, default=None):
    return _state_get(key, default)

def state_set(key, value):
    return _state_set(key, value)

def read_last_hash(_path_ignored):
    """Return 'last_hash' from state.json, else 'NO-HASH'."""
    val = state_get("last_hash")
    if not val:
        return "NO-HASH"
    return str(val).strip()

def _read_counter():
    v = state_get("last_index")
    try:
        return int(v)
    except Exception:
        return None

def next_index(use_counter=True):
    if not use_counter:
        return None
    cur = _read_counter()
    if cur is None:
        cur = 0
    # Use current value, then bump for next time
    state_set("last_index", cur + 1)
    return cur

def reset_counter():
    state_set("last_index", 0)

def safe_font(font_path=None, size=12):
    try:
        if font_path:
            return ImageFont.truetype(font_path, size=size)
    except Exception:
        pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        pass
    return ImageFont.load_default()

def json_pretty(obj):
    return json.dumps(obj, indent=2, sort_keys=True)

def which_prog(name):
    return shutil.which(name)

# ----- Capture helpers -----
def capture_with_mss(region):
    if mss is None:
        raise RuntimeError("mss not installed")
    with mss_mss() as sct:
        shot = sct.grab(region)
        img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
        return img

def capture_with_v4l2(device, w, h):
    if not device or not Path(device).exists():
        raise RuntimeError(f"v4l2 device not found: {device}")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp_path = Path(tf.name)
    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-video_size", f"{w}x{h}",
        "-i", device,
        "-frames:v", "1",
        "-loglevel", "error",
        str(tmp_path)
    ]
    try:
        subprocess.check_call(cmd)
        img = Image.open(str(tmp_path)).convert("RGB")
        return img
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg v4l2 capture failed: {e}") from e
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

def capture_region_fallback(region, v4l_device=None):
    errs = []
    if v4l_device:
        try:
            w, h = region["width"], region["height"]
            return capture_with_v4l2(v4l_device, w, h)
        except Exception as e:
            errs.append(f"v4l2 device {v4l_device} failed: {e}")
    msg = "No working screen-capture backend found. Tried:\n  - " + "\n  - ".join(errs) + "\n\n"
    msg += "Common causes & fixes:\n"
    msg += " - If you're on Wayland and your compositor does not expose wlr-screencopy, grim may fail.\n"
    msg += " - On X11, ensure $DISPLAY is set and consider installing ffmpeg and scrot:\n"
    msg += "     sudo apt install ffmpeg scrot imagemagick grim\n"
    raise RuntimeError(msg)

# ----- Core capture/save logic -----
def _measure_text(draw, text, font):
    """Measure text dimensions using Pillow's textbbox (Pillow 10+)."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        # Last-resort estimate if textbbox somehow fails
        size = getattr(font, "size", 12)
        return len(text) * (size // 2), size + 2

def _timestamp_fname():
    now = datetime.now(timezone.utc)
    return now.strftime("sample-%Y%m%d-%H%M%S-%f.png")

def capture_once(region, scale_min, scale_max, palette_colors, min_out_w, font_path, font_size,
                 last_hash_file, out_dir, v4l_device=None, fixed_scale=False, write_meta=True,
                 use_counter=True, do_overlay=True):
    img = capture_region_fallback(region, v4l_device=v4l_device)

    # determine output size
    # FIXED-SCALE BEHAVIOR: when fixed_scale is True, use scale_max (no random)
    if fixed_scale:
        scale = float(scale_max)
        out_w = max(int(img.width * scale), min_out_w)
    else:
        scale = float(scale_max)
        out_w = max(int(img.width * scale), min_out_w)
        # (randomized scaling was intentionally commented out in your original)

    aspect = img.height / img.width
    out_h = max(int(out_w * aspect), 32)

    # resize only if needed
    if out_w != img.width or out_h != img.height:
        resized = img.resize((out_w, out_h), resample=Image.LANCZOS)
    else:
        resized = img

    # overlay last_hash if enabled
    last_hash = read_last_hash(last_hash_file) if last_hash_file else "NO-HASH"
    draw = ImageDraw.Draw(resized)
    font = safe_font(font_path, font_size)

    if do_overlay:
        text = last_hash.strip()
        if len(text) > 400:
            text = text[:400] + "..."
        tw, th = _measure_text(draw, text, font)
        pad_x = 6
        pad_y = 4
        rect_w = tw + pad_x * 2
        rect_h = th + pad_y * 2
        if rect_w > resized.width - 4:
            rect_w = max(0, resized.width - 8)
        rect_box = (2, 2, 2 + rect_w, 2 + rect_h)
        draw.rectangle(rect_box, fill=(255,255,255))
        draw.text((2 + pad_x, 2 + pad_y), text, font=font, fill=(0,0,0))

    # convert to limited palette
    pal = resized.convert("P", palette=Image.ADAPTIVE, colors=palette_colors)
    buf = BytesIO()
    pal.save(buf, format="PNG", optimize=False)
    png_bytes = buf.getvalue()

    # filename and counter handling
    idx = next_index(use_counter=use_counter)
    if use_counter and idx is not None:
        fname = f"{idx:04d}.png"
    else:
        fname = _timestamp_fname()
    out_path = Path(out_dir) / fname
    out_path.write_bytes(png_bytes)

    meta = {
        "file": fname,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "orig_w": img.width,
        "orig_h": img.height,
        "out_w": out_w,
        "out_h": out_h,
        "palette_colors": palette_colors,
        "last_hash_snapshot": last_hash[:256],
    }
    if not fixed_scale:
        meta["scale_min"] = scale_min
        meta["scale_max"] = scale_max

    if write_meta:
        (Path(out_dir) / f"{fname.rsplit('.',1)[0]}.json").write_text(json_pretty(meta))

    return out_path, meta

def _trigger_script_nonblocking(script_path):
    """Launch a python script with the same interpreter without blocking.
    Returns subprocess.Popen object or None if skipped."""
    if not script_path:
        return None
    path = os.path.expanduser(os.path.expandvars(script_path))
    if not os.path.exists(path):
        print(f"trigger: not found, skipping {path}")
        return None
    try:
        proc = subprocess.Popen([sys.executable, path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        print(f"trigger: spawned {path} (pid {proc.pid})")
        return proc
    except Exception as e:
        print(f"trigger: failed to spawn {path}: {e}", file=sys.stderr)
        return None

def run_loop(region, interval, capture_offset, scale_min, scale_max, palette_colors, min_out_w,
             font_path, font_size, last_hash_file, out_dir, stop_event, v4l_device=None,
             fixed_scale=False, write_meta=True, use_counter=True, do_overlay=True):
    """
    Main capture loop (unchanged timing) but with two non-blocking triggers:
      - SCRIPT_A runs once (before the very first capture)
      - SCRIPT_B runs before every subsequent capture
    Triggers are non-blocking and will not delay the capture loop.
    """
    print(f"capture: starting loop interval={interval}s capture_offset={capture_offset}s out={out_dir} v4l_device={v4l_device} fixed_scale={fixed_scale} write_meta={write_meta} use_counter={use_counter} overlay={do_overlay}")
    step = 0
    try:
        while True:
            if hasattr(stop_event, "is_set") and stop_event.is_set():
                print("capture: stop requested")
                break

            loop_start = time.time()
            capture_time = loop_start + max(0.0, interval - capture_offset)

            # Fire the appropriate trigger (non-blocking). Do NOT wait for it.
            try:
                if step == 0:
                    if SCRIPT_A:
                        _trigger_script_nonblocking(SCRIPT_A)
                else:
                    if SCRIPT_B:
                        _trigger_script_nonblocking(SCRIPT_B)
            except Exception as e:
                print("capture: error launching trigger:", e, file=sys.stderr)

            # Wait until capture_time (if any left)
            to_sleep = capture_time - time.time()
            if to_sleep > 0:
                time.sleep(to_sleep)

            # Perform capture
            step += 1
            try:
                path, meta = capture_once(region, scale_min, scale_max, palette_colors, min_out_w,
                                          font_path, font_size, last_hash_file, out_dir,
                                          v4l_device=v4l_device, fixed_scale=fixed_scale,
                                          write_meta=write_meta, use_counter=use_counter, do_overlay=do_overlay)
                print(f"[{step}] saved {path.name} out={meta.get('out_w')}x{meta.get('out_h')}")
            except Exception as e:
                print("capture: error during capture:", e, file=sys.stderr)

            # log capture event
            t = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                open(CAPTURE_LOG, "a").write(f"{t}\t{step}\t{path.name}\t{meta.get('out_w')}x{meta.get('out_h')}\n")
            except Exception as e:
                print("capture: failed to write capture log:", e)

            # End-of-loop sleep until the next interval (preserve original logic)
            elapsed = time.time() - loop_start
            to_sleep2 = max(0.0, interval - elapsed)
            end = time.time() + to_sleep2
            while time.time() < end:
                if hasattr(stop_event, "is_set") and stop_event.is_set():
                    break
                time.sleep(0.2)
            if hasattr(stop_event, "is_set") and stop_event.is_set():
                print("capture: stop requested (end of loop)")
                break

    except KeyboardInterrupt:
        print("capture: interrupted by keyboard")
    finally:
        print("capture: exiting loop")

def start_daemon(args):
    """Launch the capture loop as a background subprocess (not a fork-daemon).

    Logs go to run/logs/capture.out and capture.err so you can debug issues.
    The PID is recorded in run/capture.pid for stop_daemon().
    """
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"capture: already running (pid {pid})")
            return
        except Exception:
            PID_FILE.unlink()

    # Re-invoke ourselves with "run" in a background process group
    log_dir = RUN / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_f = open(log_dir / "capture.out", "ab")
    stderr_f = open(log_dir / "capture.err", "ab")

    cmd = [sys.executable, __file__, "run",
           "--interval", str(args.interval),
           "--capture-offset", str(args.capture_offset),
           "--region", f"left={args.region['left']},top={args.region['top']},width={args.region['width']},height={args.region['height']}",
           "--out", str(args.out),
           "--scale-min", str(args.scale_min),
           "--scale-max", str(args.scale_max),
           "--palette-colors", str(args.palette_colors),
           "--min-out-w", str(args.min_out_w),
           "--font", str(args.font),
           "--font-size", str(args.font_size),
           "--last-hash-file", str(args.last_hash_file)]
    if args.v4l_device:
        cmd += ["--v4l-device", str(args.v4l_device)]
    if args.fixed_scale:
        cmd.append("--fixed-scale")
    if getattr(args, "reset_counter", False):
        cmd.append("--reset-counter")
    if args.no_meta:
        cmd.append("--no-meta")
    if args.no_overlay:
        cmd.append("--no-overlay")
    if getattr(args, "no_counter", False):
        cmd.append("--no-counter")

    proc = subprocess.Popen(
        cmd,
        stdout=stdout_f,
        stderr=stderr_f,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        preexec_fn=os.setpgrp,  # new process group so stop_daemon can killpg
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"capture: started background (pid {proc.pid})")
    print(f"capture: logs -> {log_dir / 'capture.out'}")

# --- stop_daemon() (unchanged behavior) ---
def stop_daemon():
    if not PID_FILE.exists():
        print("capture: no pid file; not running?")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        print("capture: pid file invalid; removing")
        try:
            PID_FILE.unlink()
        except Exception:
            pass
        return

    try:
        print(f"capture: sending SIGTERM to pid {pid} and its process group")
        # 1) polite stop
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.killpg(pid, signal.SIGTERM)  # kill the whole group (includes ffmpeg)
        except Exception:
            pass

        # 2) brief grace period
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                os.kill(pid, 0)  # still alive?
            except OSError:
                break
            time.sleep(0.2)
        else:
            # 3) hard stop if still alive
            print("capture: process did not exit; escalating to SIGKILL on pid and process group")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass

        print(f"capture: stopped pid {pid}")
    except ProcessLookupError:
        print("capture: process not found; removing stale pid file")
    except PermissionError:
        print("capture: permission error killing process")
    finally:
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass

def run_foreground(args):
    import threading
    stop_evt = threading.Event()

    # Write PID so stop_daemon() can find us
    PID_FILE.write_text(str(os.getpid()))

    def onint(signum, frame):
        stop_evt.set()
    signal.signal(signal.SIGINT, onint)
    signal.signal(signal.SIGTERM, onint)
    try:
        run_loop(args.region, args.interval, args.capture_offset, args.scale_min, args.scale_max,
                 args.palette_colors, args.min_out_w, args.font, args.font_size,
                 args.last_hash_file, args.out, stop_evt, v4l_device=args.v4l_device,
                 fixed_scale=args.fixed_scale, write_meta=not args.no_meta, use_counter=not args.no_counter, do_overlay=not args.no_overlay)
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

def main():
    p = argparse.ArgumentParser(description="Randomized capture -> paletted PNG saver (v4l2/ffmpeg support)")
    sub = p.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--interval", type=float, default=8.0, help="seconds between saves (default 8)")
    common.add_argument("--capture-offset", type=float, default=0.0, help="seconds before tick to capture")
    common.add_argument("--region", type=str, default="left=120,top=120,width=1920,height=1080", help="screen region")
    common.add_argument("--out", type=str, default=str(PNG_DIR), help="output dir (default repo-root/run/bundles)")
    common.add_argument("--scale-min", type=float, default=0.364583333, help="minimum random scale factor")
    common.add_argument("--scale-max", type=float, default=0.364583333, help="maximum random scale factor")
    common.add_argument("--palette-colors", type=int, default=16, help="palette color count")
    common.add_argument("--min-out-w", type=int, default=700, help="minimum output width in pixels (ignored with --fixed-scale)")
    # keep your original default here (path may vary by distro; unchanged logic)
    common.add_argument("--font", type=str, default="/usr/share/fonts/truetype/dejavu/LiberationMono-Bold.ttf", help="font path for overlay text")
    common.add_argument("--font-size", type=int, default=14, help="font size for overlay text")
    common.add_argument("--last-hash-file", dest="last_hash_file", type=str, default=str(STATE_JSON), help="path to state.json (contains 'last_hash' key; default repo-root/run/state.json)")
    common.add_argument("--v4l-device", type=str, default="", help="path to v4l2 device (e.g. /dev/video2). If provided, use ffmpeg v4l2 capture as primary backend.")
    common.add_argument("--fixed-scale", action="store_true", dest="fixed_scale", help="Output at captured image width (no random scaling).")
    common.add_argument("--reset-counter", action="store_true", dest="reset_counter", help="Reset counter to 0 before starting (so numbering begins at 1)")
    common.add_argument("--no-meta", action="store_true", dest="no_meta", help="Do not write JSON sidecar metadata files (only PNGs will be saved)")
    common.add_argument("--no-overlay", action="store_true", dest="no_overlay", help="Do not draw the last-hash overlay on the saved images")
    common.add_argument("--no-counter", action="store_true", dest="no_counter", help="Do not use / update the numeric counter file (use timestamp filenames)")
    sub.add_parser("start", parents=[common], help="start background daemon")
    sub.add_parser("stop", help="stop background daemon")
    sub.add_parser("run", parents=[common], help="run in foreground (for testing)")
    args = p.parse_args()

    if args.cmd in ("start", "run"):
        try:
            args.region = parse_region(args.region)
        except Exception as e:
            print("Bad region:", e, file=sys.stderr)
            sys.exit(2)
        args.out = Path(args.out)
        args.out.mkdir(parents=True, exist_ok=True)

        # reset counter if requested (this writes 0 to state.json)
        if getattr(args, "reset_counter", False):
            reset_counter()

        # ensure counter file exists unless user requested no_counter
        if not getattr(args, "no_counter", False) and state_get("last_index") is None:
            state_set("last_index", 0)

    if args.cmd == "start":
        start_daemon(args)
    elif args.cmd == "stop":
        stop_daemon()
    elif args.cmd == "run":
        run_foreground(args)
    else:
        p.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
