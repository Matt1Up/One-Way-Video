#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stop_json.py — orderly shutdown + HAR conversion (session_name-aware)

Order:
1) STOP image grabber loop (capture_randomized_save_json.py) and WAIT for it to exit
2) STOP network stream recorder (quiet)
3) STOP HTTP recording (mitm)
4) STOP OBS recording (video)
5) WAIT for <SESSION>.dump.ready in --out-dir
6) Convert <SESSION>.dump -> redacted HAR

Filename resolution priority:
    --filename  >  --session-name  >  state.json["session_name"]  >  "session"
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse
import subprocess
import sys as _sys
import time
import os
import json
import fcntl
from pathlib import Path

# ---------- Paths (portable via evidence_capture) ----------
from evidence_capture.paths import (
    ROOT, BIN, RUN, WEB, ensure_runtime_dirs, resolve
)

# Must match capture_randomized_save_json.py's pidfile location
CAPTURE_PID_FILE = RUN / "capture.pid"

# State file (for session_name)
STATE_JSON = RUN / "state.json"
STATE_LOCK = RUN / "state.lock"

# Defaults (can be overridden via env or CLI)
DEFAULT_OUT_DIR    = Path(os.environ.get("EVCAP_DUMP_DIR", str(ROOT / "run" / "dumps")))
DEFAULT_HAR_OUTDIR = Path(os.environ.get("EVCAP_HAR_DIR",  str(ROOT / "run" / "dumps" / "processed")))

# Interpreter selection
# PY  -> use current Python for all the stop/teardown helpers
# MITM_PY -> dedicated interpreter for mitm dump -> HAR conversion
PY        = Path(os.environ.get("EVCAP_PY", _sys.executable))
MITM_PY   = Path(os.environ.get("EVCAP_MITM_PY", "~/.pyenv/versions/mitm-3.13/bin/python")).expanduser()

# Converter path
HAR_CONVERTER = BIN / "dump_to_redacted_har_sanitized.py"

# ---------- Small utils ----------
def run_cmd(cmd):
    print(f"[RUN] {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True)

def ensure_dump_ext(name: str) -> str:
    return name if name.lower().endswith(".dump") else f"{name}.dump"

def wait_for_ready(out_dir: Path, filename_dump: str, timeout: int = 600, poll: float = 0.5) -> bool:
    """Wait for <filename_dump>.ready to appear in out_dir."""
    ready = out_dir / f"{filename_dump}.ready"
    print(f"[WAIT] ready signal: {ready}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready.exists():
            print("[WAIT] ready file detected.")
            return True
        time.sleep(poll)
    return False

def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def wait_for_capture_stop(timeout: float = 30.0, poll: float = 0.25):
    """Wait until capture_randomized_save_json.py fully stops (pid file removed or pid gone)."""
    pid = None
    if CAPTURE_PID_FILE.exists():
        try:
            pid = int(CAPTURE_PID_FILE.read_text().strip())
        except Exception:
            pid = None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not CAPTURE_PID_FILE.exists():
            print("[WAIT] image grabber stopped (pidfile removed).")
            return True
        if pid is not None and not pid_is_running(pid):
            print("[WAIT] image grabber pid exited.")
            try:
                CAPTURE_PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        time.sleep(poll)

    # Timed out; continue anyway per your preference
    left = []
    if CAPTURE_PID_FILE.exists():
        left.append("pidfile")
    if pid is not None and pid_is_running(pid):
        left.append(f"pid={pid}")
    note = " & ".join(left) if left else "unknown reason"
    print(f"[WARN] image grabber did not stop within {timeout}s ({note}); continuing.")
    return False

# ---------- Locked state read ----------
class Flock:
    def __init__(self, lock_path: Path): self.lock_path = lock_path; self.fd = None
    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *a):
        try: fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally: os.close(self.fd); self.fd = None

def read_state() -> dict:
    if not STATE_JSON.exists():
        return {}
    try:
        with Flock(STATE_LOCK):
            with STATE_JSON.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {}

# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser(description="Stop recorders and convert dump to HAR (session_name-aware).")
    ap.add_argument("--session-name",
                    help="If provided, used as dump base name (e.g., <session-name>.dump).")
    ap.add_argument("--filename",
                    help="Optional explicit dump base name (overrides session-name/state). .dump appended if missing.")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="Directory where mitm dumps are stored (default: %(default)s)")
    ap.add_argument("--har-out-dir", type=Path, default=DEFAULT_HAR_OUTDIR,
                    help="Directory where processed HAR files are written (default: %(default)s)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="Seconds to wait for <name>.dump.ready (default: 600)")
    ap.add_argument("--capture-timeout", type=float, default=30.0,
                    help="Seconds to wait for image grabber to stop before proceeding (default: 30)")
    args = ap.parse_args()

    out_dir: Path = Path(args.out_dir).expanduser().resolve()
    har_out_dir: Path = Path(args.har_out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    har_out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve dump base name
    if args.filename:
        base = args.filename
    else:
        sess = args.session_name
        if not sess:
            st = read_state()
            sess = st.get("session_name") or "session"
        base = sess

    filename_dump = ensure_dump_ext(base)
    dump_path = out_dir / filename_dump

    try:
        # 1) STOP image grabber FIRST, then wait for it to fully exit
        run_cmd([str(PY), str(BIN / "capture_randomized_save_json.py"), "stop"])
        wait_for_capture_stop(timeout=args.capture_timeout)

        # 2) STOP network stream (quiet, fire-and-forget)
        run_cmd([str(PY), str(BIN / "netstream_ctrl_json.py"), "STOP", "--quiet"])

        # 3) STOP HTTP recording (mitm)
        run_cmd([str(PY), str(BIN / "mitm_dump_control.py"), "stop"])

        # 4) STOP OBS STUDIO RECORDING
        run_cmd([str(PY), str(BIN / "obs_studio_ctrl.py"), "stop"])

        # 5) WAIT for <SESSION>.dump.ready
        if not wait_for_ready(out_dir, filename_dump, timeout=args.timeout):
            print(f"ERROR: Timed out waiting for {filename_dump}.ready in {out_dir}", file=sys.stderr)
            sys.exit(1)

        if not dump_path.exists():
            print(f"ERROR: Expected dump not found: {dump_path}", file=sys.stderr)
            sys.exit(1)

        # 6) Convert to HAR using the dedicated mitm venv
        run_cmd([
            str(MITM_PY), str(HAR_CONVERTER),
            "--in", str(dump_path),
            "--out-dir", str(har_out_dir),
            "--keep-bodies", "--keep-bodies-hash"
        ])
        print("[DONE] Conversion complete.")

    except subprocess.CalledProcessError as e:
        print(f"Command failed (exit {e.returncode}): {e.cmd}", file=sys.stderr)
        sys.exit(e.returncode)
    except Exception as ex:
        print(f"Unhandled error: {ex}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
