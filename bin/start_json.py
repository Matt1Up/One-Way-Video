#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
start_json.py — JSON-native bootstrap for the evidence-capture pipeline (portable)

What it does:
- Purges previous outputs, clears logs/state
- Starts OBS, mitmproxy, netstream, image grabber (fire-and-forget)
- Seeds start_time.json (100-row system-time sample)
- OTS + Roughtime stamps the seed
- Builds the initial bundle 0000.json with its hash chain
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, os, sys, time, subprocess, shutil, glob
from pathlib import Path

# ========= Shared modules =========
from evidence_capture.paths import ROOT, RUN, BIN, PY, ensure_runtime_dirs
from evidence_capture.timeutil import ts_local_ns, now_utc_iso
from evidence_capture.hashing import sha256_file, sha256_bytes
from evidence_capture.state import (
    state_read, state_write, state_get, state_set_with_time, state_update,
    state_clear_streams_and_files_recent,
    snapshot_last_files, snapshot_stream,
    log_last_hash_change, LAST_HASH_LOG,
)
from evidence_capture.process import log, run_cmd, wait_for_file
from evidence_capture.evidence import roughtime_bind, ots_stamp, LAST_RT_TXT

# ========= Feature toggles (defaults; CLI can override) =========

# ---------- Paths ----------
BUNDLES   = RUN / "bundles"
LOG_DIR   = RUN / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Bootstrap artifacts
START_JSON         = BUNDLES / "start_time.json"
START_JSON_OTS     = BUNDLES / "start_time.json.ots"
START_JSON_OTS_TS  = BUNDLES / "start_time.json.ots__time-stamp.json"

# Logging targets to optionally clear
LOGS_TO_CLEAR = [
    LOG_DIR / "capture_events.log",
    LOG_DIR / "download_watcher.log",
    LOG_DIR / "last_hash.log",
    LOG_DIR / "loop.log",
    LOG_DIR / "mitm_dump.log",
]

# Timing
OTS_WAIT_SECONDS = 240
OTS_POLL = 0.25

# Default dumps dir (override with --dump-out-dir or EVCAP_DUMP_DIR)
DEFAULT_DUMP_DIR = Path(os.environ.get("EVCAP_DUMP_DIR", str(RUN / "dumps")))


# ---------- Purge / Clear ----------
def _safe_remove(p: Path):
    try:
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p)
    except Exception:
        pass


def purge_previous_outputs(aggressive: bool):
    BUNDLES.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    if aggressive:
        log("[PURGE] Aggressively clearing bundles/*")
        for p in BUNDLES.glob("*"):
            _safe_remove(p)
    else:
        log("[PURGE] Cleaning typical previous outputs (conservative)")
        for t in [START_JSON, START_JSON_OTS, BUNDLES / "0000.json",
                  BUNDLES / "0000.json.ots", BUNDLES / "0000.json.ots__time-stamp.json",
                  START_JSON_OTS_TS]:
            _safe_remove(t)
        for pat in [str(BUNDLES / "roughtime_proof_*.json"), str(BUNDLES / "start_time*.json")]:
            for p in glob.glob(pat):
                _safe_remove(Path(p))
    # reset JSON state keys (baseline)
    now = ts_local_ns()
    state_update({
        "last_index": 0,
        "last_index_sys_time": now,
        "last_hash": "",
        "last_hash_sys_time": now,
        "last_img_hash": "",
        "last_img_hash_sys_time": now,
    })
    # rotate last_hash.log (simple)
    if LAST_HASH_LOG.exists() and LAST_HASH_LOG.stat().st_size > 5 * 1024 * 1024:
        LAST_HASH_LOG.replace(LAST_HASH_LOG.with_suffix(".log.1"))


def clear_logs():
    for p in LOGS_TO_CLEAR:
        try:
            if p.exists():
                p.unlink()
                log(f"[CLEAR] removed log: {p}")
        except Exception as e:
            log(f"[WARN] failed removing {p}: {e}")


# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser(description="JSON bootstrapper for evidence-capture.")
    ap.add_argument("--no-clean", action="store_true",
                    help="Do NOT clear run/bundles before starting (default: clear everything).")
    ap.add_argument("--interval", type=float, default=8.0,
                    help="Seconds between image captures for capture_randomized_save_json.py.")
    ap.add_argument("--session-name", default=None,
                    help="Session label saved into state.json and passed to background workers.")
    ap.add_argument("--no-obs", action="store_true", help="Skip starting OBS controller.")
    ap.add_argument("--no-net", action="store_true", help="Skip starting netstream.")
    ap.add_argument("--no-http", action="store_true", help="Skip starting mitmproxy/httpstream.")
    ap.add_argument("--no-download-watcher", action="store_true", help="Skip starting download_watcher_json.py.")
    ap.add_argument("--no-clear-logs", action="store_true", help="Do not clear logs on start.")
    ap.add_argument("--no-clear-state", action="store_true", help="Do not clear streams/files_recent on start.")
    ap.add_argument("--v4l-device", default="/dev/video2", help="Device for capture_randomized_save_json.py")
    ap.add_argument("--dump-out-dir", default=str(DEFAULT_DUMP_DIR), help="Output dir for mitm dumps")
    args = ap.parse_args()

    # 0) Purge / init (default: clean everything; --no-clean to keep)
    purge_previous_outputs(aggressive=not args.no_clean)
    BUNDLES.mkdir(parents=True, exist_ok=True)

    # 0.1) Clear logs (default ON)
    if not args.no_clear_logs:
        clear_logs()

    # 0.2) Set/record session name
    sess = args.session_name or state_get("session_name") or "session"
    state_set_with_time("session_name", sess)
    log(f"[SESSION] session_name = {sess}")

    # 0.3) Clear streams/http_events/files_recent (default ON)
    if not args.no_clear_state:
        state_clear_streams_and_files_recent()
        log("[STATE] Cleared streams (http, net) + http_events + files_recent")

    # S0) OBS (optional)
    if not args.no_obs:
        try:
            subprocess.run([str(PY), str(BIN / "obs_studio_ctrl.py"), "start"], check=False)
            log("[STARTUP] obs_studio_ctrl.py")
        except Exception as e:
            log(f"[WARN] couldn't start OBS controller: {e}")

    # S1) mitm_dump_control.py + httpstream_json.py (optional)
    #     Always kill any existing mitm first — stale instances from prior sessions
    #     are the #1 cause of "no dump file" on stop.
    if not args.no_http:
        try:
            # Force-stop any existing mitm (standby or recording)
            log("[STARTUP] Stopping any existing mitm instance...")
            subprocess.run(
                [str(PY), str(BIN / "mitm_dump_control.py"), "stop"],
                check=False, capture_output=True,
            )
            time.sleep(0.5)  # let port release

            dump_dir = Path(args.dump_out_dir).expanduser().resolve()
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_name = f"{sess}.dump" if not str(sess).endswith(".dump") else str(sess)

            # Remove stale dump with this session name (fresh start)
            stale_dump = dump_dir / dump_name
            stale_ready = dump_dir / f"{dump_name}.ready"
            for f in (stale_dump, stale_ready):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass

            # Start fresh mitm in recording mode
            subprocess.run([
                str(PY), str(BIN / "mitm_dump_control.py"), "start",
                "--out-dir", str(dump_dir),
                "--filename", dump_name,
                "--script", str(BIN / "httpstream_json.py"),
            ], check=True)
            log(f"[STARTUP] mitm recording started (filename={dump_name}, out={dump_dir})")
        except Exception as e:
            log(f"[WARN] couldn't start mitm_dump_control: {e}")

    # S2) netstream_ctrl_json.py (optional)
    if not args.no_net:
        try:
            subprocess.Popen([str(PY), str(BIN / "netstream_ctrl_json.py"), "START", "--quiet"])
            log("[STARTUP] netstream_ctrl_json START (quiet)")
        except Exception as e:
            log(f"[WARN] couldn't start netstream_ctrl_json: {e}")

    # S3) image grabber (always)
    try:
        subprocess.Popen([
            str(PY), str(BIN / "capture_randomized_save_json.py"), "start",
            "--reset-counter", "--no-meta", "--no-overlay",
            "--v4l-device", str(args.v4l_device),
            "--interval", str(args.interval),
        ])
        log(f"[STARTUP] capture_randomized_save_json started (dev={args.v4l_device}, interval={args.interval}s)")
    except Exception as e:
        log(f"[WARN] couldn't start capture_randomized_save_json: {e}")

    # Small pause for daemons to come up
    time.sleep(0.5)

    # ---- PART 1: Seed start_time.json (nonce) ----
    run_cmd([str(PY), str(BIN / "get_start_time_json.py"), "--save-path", str(START_JSON)],
            label="get_start_time_json")
    log(f"[OK] wrote {START_JSON.name}")

    # SHA-256 of start_time.json -> state.json
    start_sha = sha256_file(START_JSON)
    state_set_with_time("start_time_sha256", start_sha)
    log(f"[OK] start_time.json sha256 = {start_sha}")

    # OTS of start_time.json (wait)
    ots = ots_stamp(START_JSON, BUNDLES, log_fn=log)
    if not wait_for_file(ots, timeout=OTS_WAIT_SECONDS, poll=OTS_POLL):
        log(f"ERROR: Timed out waiting for OTS: {ots}")
        sys.exit(1)
    log(f"[OK] OTS for start_time.json: {ots.name}")

    # Roughtime bind the start_time.json .ots
    ots_rt_json = roughtime_bind(ots, BUNDLES, log_fn=log)
    if ots_rt_json:
        log(f"[OK] Roughtime bind JSON: {ots_rt_json.name}")
        try:
            if LAST_RT_TXT.exists():
                val = LAST_RT_TXT.read_text(encoding="utf-8").strip()
                state_set_with_time("last_roughtime", val)
        except Exception:
            pass

    # ---- PART 2: Build initial bundle 0000.json ----
    sys_time_in = ts_local_ns()
    st_now = state_read()
    http_snap = snapshot_stream(st_now, "http", 5)
    net_snap = snapshot_stream(st_now, "net", 5)
    last_files = snapshot_last_files()

    prev_hash_val = state_get("last_hash") or ""
    prev_img_val = state_get("last_img_hash") or ""
    prev_rt_val = state_get("last_roughtime") or ""

    bundle_obj = {
        "id": "0000",
        "index_raw": "0",
        "sys_time_in": sys_time_in,

        "prev": {
            "last_hash":      {"value": prev_hash_val, "sys_time": state_get("last_hash_sys_time")},
            "last_img_hash":  {"value": prev_img_val,  "sys_time": state_get("last_img_hash_sys_time")},
            "last_timestamp": {"value": prev_rt_val,   "sys_time": state_get("last_roughtime_sys_time")},
        },

        "start": {
            "path": START_JSON.name,
            "sha256": {"value": start_sha, "sys_time": ts_local_ns()},
            "ots": {
                "path": START_JSON_OTS.name,
                "rt_json": START_JSON_OTS_TS.name,
                "sys_time": ts_local_ns(),
            },
        },

        "streams": {
            "http_freeze_sys": http_snap["sys_time_freeze"],
            "http": http_snap["items"],
            "net_freeze_sys": net_snap["sys_time_freeze"],
            "net": net_snap["items"],
        },
        "last_files": last_files,
    }

    bundle_obj["sys_time_out"] = ts_local_ns()

    bundle_path = BUNDLES / "0000.json"
    bundle_bytes = (json.dumps(bundle_obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    bundle_path.write_bytes(bundle_bytes)
    log(f"[OK] wrote {bundle_path.name}")

    before = prev_hash_val
    after = sha256_bytes(bundle_bytes)
    state_set_with_time("last_hash", after)
    log_last_hash_change(
        context="start", src="hash_of_bundle_json", src_path=bundle_path,
        id_hint="0000", before=before, after=after, log_fn=log,
    )

    # OTS + Roughtime for 0000.json
    b_ots = ots_stamp(bundle_path, BUNDLES, log_fn=log)
    if not wait_for_file(b_ots, timeout=OTS_WAIT_SECONDS, poll=OTS_POLL):
        log(f"ERROR: Timed out waiting for bundle OTS: {b_ots}")
        sys.exit(1)
    log(f"[OK] OTS for 0000.json: {b_ots.name}")
    b_ots_rt = roughtime_bind(b_ots, BUNDLES, log_fn=log)
    if b_ots_rt:
        log(f"[OK] Roughtime bind JSON: {b_ots_rt.name}")
        try:
            if LAST_RT_TXT.exists():
                val = LAST_RT_TXT.read_text(encoding="utf-8").strip()
                state_set_with_time("last_roughtime", val)
        except Exception:
            pass

    log("[DONE] start_json bootstrap completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        msg = f"Command failed ({e.returncode}): {' '.join(map(str, e.cmd))}\n{(e.stderr or '').strip()}"
        log(msg)
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as ex:
        log(f"Unhandled error: {ex}")
        sys.exit(1)
