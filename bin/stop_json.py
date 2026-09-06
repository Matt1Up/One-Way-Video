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
from pathlib import Path

# ========= Shared modules =========
from evidence_capture.paths import ROOT, BIN, RUN, PY, ensure_runtime_dirs
from evidence_capture.state import state_read

# Must match capture_randomized_save_json.py's pidfile location
CAPTURE_PID_FILE = RUN / "capture.pid"

# Defaults (can be overridden via env or CLI)
DEFAULT_OUT_DIR    = Path(os.environ.get("EVCAP_DUMP_DIR", str(ROOT / "run" / "dumps")))
DEFAULT_HAR_OUTDIR = Path(os.environ.get("EVCAP_HAR_DIR",  str(ROOT / "run" / "dumps" / "processed")))

# Dedicated interpreter for mitm dump -> HAR conversion.
#
# mitmproxy lives in its own venv (~/.venvs/mitm) because of dependency pins.
# When stop_json is launched from a shell without that venv on PATH, falling
# back to the parent PY means /usr/bin/python3 — which has no mitmproxy and
# fails the HAR conversion with exit 2. Discover the dedicated venv up front
# so the chain works regardless of which shell launched the controller.
def _resolve_mitm_py() -> Path:
    override = os.environ.get("EVCAP_MITM_PY")
    if override:
        return Path(override).expanduser()
    candidates = [
        Path.home() / ".venvs" / "mitm" / "bin" / "python",
        Path.home() / ".venvs" / "mitm" / "bin" / "python3",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return Path(str(PY))  # last-resort: hope mitmproxy is in this interpreter


MITM_PY = _resolve_mitm_py()

# Converter path
HAR_CONVERTER = BIN / "dump_to_redacted_har_sanitized.py"


# ---------- Helpers ----------
def run_cmd(cmd):
    print(f"[RUN] {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True)


def ensure_dump_ext(name: str) -> str:
    return name if name.lower().endswith(".dump") else f"{name}.dump"


def wait_for_ready(out_dir: Path, filename_dump: str, timeout: int = 600, poll: float = 0.5) -> bool:
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

    left = []
    if CAPTURE_PID_FILE.exists():
        left.append("pidfile")
    if pid is not None and pid_is_running(pid):
        left.append(f"pid={pid}")
    note = " & ".join(left) if left else "unknown reason"
    print(f"[WARN] image grabber did not stop within {timeout}s ({note}); continuing.")
    return False


# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser(description="Stop recorders and convert dump to HAR (session_name-aware).")
    ap.add_argument("--session-name", help="If provided, used as dump base name.")
    ap.add_argument("--filename", help="Optional explicit dump base name (overrides session-name/state).")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--har-out-dir", type=Path, default=DEFAULT_HAR_OUTDIR)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--capture-timeout", type=float, default=30.0)
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
            st = state_read()
            sess = st.get("session_name") or "session"
        base = sess

    filename_dump = ensure_dump_ext(base)
    dump_path = out_dir / filename_dump

    # Each teardown step is best-effort — we don't want a failure in
    # step 2 to prevent OBS from stopping recording in step 4.
    errors = []

    def try_step(label, fn):
        try:
            fn()
        except Exception as e:
            msg = f"[WARN] {label}: {e}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    # 1) STOP image grabber FIRST, then wait for it to fully exit
    try_step("Stop image grabber",
             lambda: run_cmd([str(PY), str(BIN / "capture_randomized_save_json.py"), "stop"]))
    wait_for_capture_stop(timeout=args.capture_timeout)

    # 2) STOP network stream (quiet, fire-and-forget)
    try_step("Stop netstream",
             lambda: run_cmd([str(PY), str(BIN / "netstream_ctrl_json.py"), "STOP", "--quiet"]))

    # 3) STOP HTTP recording (mitm)
    try_step("Stop mitm",
             lambda: run_cmd([str(PY), str(BIN / "mitm_dump_control.py"), "stop"]))

    # 4) STOP OBS STUDIO RECORDING — always attempted regardless of prior errors
    try_step("Stop OBS recording",
             lambda: run_cmd([str(PY), str(BIN / "obs_studio_ctrl.py"), "stop"]))

    # 5) WAIT for <SESSION>.dump.ready (only if mitm was running)
    has_dump = False
    if dump_path.exists():
        if wait_for_ready(out_dir, filename_dump, timeout=args.timeout):
            has_dump = True
        else:
            # .ready didn't appear but dump exists — mitm may have stopped uncleanly
            print(f"[WARN] No .ready sentinel, but dump exists: {dump_path}", file=sys.stderr)
            has_dump = True
    else:
        print(f"[WARN] Dump file not found: {dump_path}", file=sys.stderr)

    # 6) Convert to HAR (if we have a dump)
    if has_dump:
        try_step("HAR conversion",
                 lambda: run_cmd([
                     str(MITM_PY), str(HAR_CONVERTER),
                     "--in", str(dump_path),
                     "--out-dir", str(har_out_dir),
                     "--keep-bodies-hash",
                     "--include-bodies",
                     "--no-body-sanitize",
                 ]))

    if errors:
        print(f"\n[DONE with {len(errors)} warning(s)]")
        for e in errors:
            print(f"  {e}")
    else:
        print("[DONE] Clean shutdown and conversion complete.")


if __name__ == "__main__":
    main()
