#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
loop_json.py — one capture+bundle iteration, all-JSON artifacts, hash-first discipline.

Per-interval outputs (under run/bundles):
  <ID>.png.ots
  <ID>.png.ots__time-stamp.json
  <ID>.json
  <ID>.json.ots
  <ID>.json.ots__time-stamp.json
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, sys, time

# ========= Shared modules =========
from evidence_capture.paths import RUN, BIN, PY, ensure_runtime_dirs
from evidence_capture.timeutil import ts_local_ns
from evidence_capture.hashing import sha256_file, sha256_bytes
from evidence_capture.state import (
    state_read, state_get_with_time, state_set_with_time,
    snapshot_stream, snapshot_last_files,
    log_last_hash_change,
)
from evidence_capture.process import make_logger, run_cmd, wait_for_file
from evidence_capture.evidence import LAST_RT_TXT

# ---------- Paths ----------
BUNDLES  = RUN / "bundles"
PNG_DIR  = BUNDLES
LOG_DIR  = RUN / "logs"
LOG_FILE = LOG_DIR / "loop.log"

# Legacy helper files
LAST_INDEX_TXT = RUN / "last_index.txt"

# Timing
WAIT_OTS_SECS = 240
POLL = 0.25

# Module-level logger (configured in main)
log = make_logger(log_file=LOG_FILE)


# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    global log
    ap = argparse.ArgumentParser(description="One capture+bundle iteration (all JSON, sealed-before-hash).")
    ap.add_argument("--sleep", type=float, default=0.5)
    try:
        from argparse import BooleanOptionalAction
        ap.add_argument("--debug", action=BooleanOptionalAction, default=True)
    except Exception:
        ap.add_argument("--debug", dest="debug", action="store_true", default=True)
        ap.add_argument("--no-debug", dest="debug", action="store_false")
    ap.add_argument("--index-debug", action="store_true")
    ap.add_argument("--image-wait", type=float, default=10.0)
    args = ap.parse_args()

    debug = bool(args.debug)
    log = make_logger(log_file=LOG_FILE, enabled=debug)
    log("==== LOOP START ====")
    time.sleep(max(0.0, args.sleep))

    # 0) Determine effective_id (prefer state.json; fallback to last_index.txt)
    st = state_read()
    if "last_index" in st:
        last_index_raw = str(st["last_index"])
    else:
        if not LAST_INDEX_TXT.exists():
            log(f"ERROR: {LAST_INDEX_TXT} not found and state.json missing 'last_index'")
            sys.exit(2)
        last_index_raw = LAST_INDEX_TXT.read_text(encoding="utf-8").strip()

    try:
        n = int(last_index_raw)
    except Exception:
        n = 0
    effective = max(n - 1, 0)
    effective_id = f"{effective:04d}"
    log(f"last_index raw='{last_index_raw}' -> effective_id='{effective_id}'")

    # Skip bootstrap (0000 handled elsewhere)
    if effective == 0:
        log("effective_id is 0000; bootstrap handled elsewhere. Skipping.")
        sys.exit(0)

    # 0.1) Record sys_time_in to state.json immediately
    sys_time_in = ts_local_ns()
    state_set_with_time("sys_time_in", sys_time_in)

    # 1) Ensure PNG exists
    img_path = PNG_DIR / f"{effective_id}.png"
    log(f"Resolved image path: {img_path}")
    deadline = time.time() + float(args.image_wait)
    while (not img_path.exists()) or img_path.stat().st_size == 0:
        if time.time() >= deadline:
            log(f"ERROR: image still missing after {args.image_wait}s: {img_path}")
            print(f"ERROR: image not found for effective_id {effective_id}: {img_path}", file=sys.stderr)
            sys.exit(2)
        time.sleep(0.1)
    try:
        log(f"FOUND image: {img_path} ({img_path.stat().st_size} bytes)")
    except Exception:
        pass

    # 2) Compute image hash
    img_hash_sys_time = ts_local_ns()
    img_sha = sha256_file(img_path)
    state_set_with_time("last_img_hash", img_sha, img_hash_sys_time)

    # 3) OTS the PNG
    BUNDLES.mkdir(parents=True, exist_ok=True)
    run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(img_path), str(BUNDLES)], log_fn=log)
    img_ots = BUNDLES / f"{effective_id}.png.ots"
    if not wait_for_file(img_ots, timeout=WAIT_OTS_SECS, poll=POLL, log_fn=log):
        print(f"ERROR: Timed out waiting for image OTS: {img_ots}", file=sys.stderr)
        sys.exit(1)

    # 4) Roughtime bind the PNG .ots
    run_cmd([
        str(PY), str(BIN / "time" / "roughtime_client.py"), "query", "-v",
        "--server", "roughtime.cloudflare.com", "--port", "2003",
        "--pubkey-base64", "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=",
        "--reveal-hash",
        "--bind-file", str(img_ots),
        "--last-time", str(LAST_RT_TXT),
        "--json-out", str(BUNDLES),
    ], log_fn=log)
    img_ots_json = BUNDLES / f"{effective_id}.png.ots__time-stamp.json"
    try:
        if LAST_RT_TXT.exists():
            state_set_with_time("last_roughtime", LAST_RT_TXT.read_text(encoding="utf-8").strip(), ts_local_ns())
    except Exception:
        pass

    # Extract bind hash (best-effort)
    img_bind_hex = None
    try:
        if img_ots_json.exists():
            with img_ots_json.open("r", encoding="utf-8") as jf:
                data = json.load(jf)
            v = data.get("bind_sha256_hex")
            if isinstance(v, str) and len(v) >= 64:
                img_bind_hex = v
    except Exception as e:
        log(f"WARN parsing roughtime JSON (image .ots): {e}")

    # 5) Snapshot streams + events + last_files from state.json
    st_now = state_read()
    http_snap = snapshot_stream(st_now, "http", 5)
    net_snap = snapshot_stream(st_now, "net", 5)

    # 6) last_files snapshot
    last_files_data = snapshot_last_files()
    last_files_rows = last_files_data["rows"]
    last_files_freeze_sys = last_files_data["sys_time_freeze"]

    # files_recent from state
    files_recent_list = st_now.get("files_recent") or []
    files_recent_freeze_sys = ts_local_ns()

    # scoped snapshot of the NEWEST streams.http_events entries.
    # httpstream_json.py prepends (newest first, capped at HTTP_EVENTS_MAX), so the
    # head of the list is the most recent activity, same as snapshot_stream() for
    # streams.http / streams.net. Before v2.0 this took the tail ([-5:]), i.e. the
    # OLDEST entries, which together with the missing reset in
    # state_clear_streams_and_files_recent() put a previous session's events into
    # new bundles.
    _http_events_all = (st_now.get("streams") or {}).get("http_events") or []
    _http_events_tail = _http_events_all[:5] if isinstance(_http_events_all, list) else []
    _http_events_freeze_sys = ts_local_ns()

    # 7) Build bundle JSON
    prev_hash_val, prev_hash_time = state_get_with_time("last_hash")
    prev_img_val, prev_img_time = state_get_with_time("last_img_hash")
    prev_rt_val, prev_rt_time = state_get_with_time("last_roughtime")

    bundle_obj = {
        "id": effective_id,
        "index_raw": last_index_raw,
        "sys_time_in": sys_time_in,

        "prev": {
            "last_hash":      {"value": prev_hash_val or "", "sys_time": prev_hash_time or ""},
            "last_img_hash":  {"value": prev_img_val or "",  "sys_time": prev_img_time or ""},
            "last_timestamp": {"value": prev_rt_val or "",   "sys_time": prev_rt_time or ""},
        },

        "image": {
            "path": str(img_path),
            "sha256": {"value": img_sha, "sys_time": img_hash_sys_time},
            "ots": {
                "path": str(img_ots),
                "rt_json": str(img_ots_json),
                "bind_sha256_hex": img_bind_hex,
                "sys_time": ts_local_ns(),
            },
        },

        "streams": {
            "http_freeze_sys": http_snap["sys_time_freeze"],
            "http": http_snap["items"],
            "net_freeze_sys": net_snap["sys_time_freeze"],
            "net": net_snap["items"],
            "http_events_freeze_sys": _http_events_freeze_sys,
            "http_events": _http_events_tail,
        },

        "last_files": {
            "sys_time_freeze": last_files_freeze_sys,
            "rows": last_files_rows,
        },

        "downloads": {
            "files_recent_freeze_sys": files_recent_freeze_sys,
            "files_recent": files_recent_list,
        },
    }

    bundle_obj["sys_time_out"] = ts_local_ns()

    # 8) Write bundle JSON, then hash the actual bytes
    bundle_file = BUNDLES / f"{effective_id}.json"
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_bytes = (json.dumps(bundle_obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with bundle_file.open("wb") as f:
        f.write(bundle_bytes)
    log(f"WROTE bundle JSON: {bundle_file}")

    before = prev_hash_val or ""
    after = sha256_bytes(bundle_bytes)
    state_set_with_time("last_hash", after, ts_local_ns())
    log_last_hash_change(
        context="loop", src="hash_of_bundle_json",
        src_path=bundle_file, id_hint=effective_id,
        before=before, after=after, log_fn=log,
    )

    # Clear files_recent NOW (after bundle is sealed)
    try:
        state_set_with_time("files_recent", [], ts_local_ns())
        log("Cleared state.json: files_recent=[] (post-bundle seal)")
    except Exception as e:
        log(f"WARN: failed to clear files_recent: {e}")

    # 9) OTS of bundle JSON
    run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(bundle_file), str(BUNDLES)], log_fn=log)
    bundle_ots = BUNDLES / f"{effective_id}.json.ots"
    if not wait_for_file(bundle_ots, timeout=WAIT_OTS_SECS, poll=POLL, log_fn=log):
        print(f"ERROR: Timed out waiting for bundle OTS: {bundle_ots}", file=sys.stderr)
        sys.exit(1)

    # 10) Roughtime bind the bundle .ots
    run_cmd([
        str(PY), str(BIN / "time" / "roughtime_client.py"), "query", "-v",
        "--server", "roughtime.cloudflare.com", "--port", "2003",
        "--pubkey-base64", "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=",
        "--reveal-hash",
        "--bind-file", str(bundle_ots),
        "--last-time", str(LAST_RT_TXT),
        "--json-out", str(BUNDLES),
    ], log_fn=log)
    try:
        if LAST_RT_TXT.exists():
            state_set_with_time("last_roughtime", LAST_RT_TXT.read_text(encoding="utf-8").strip(), ts_local_ns())
    except Exception:
        pass

    existing = [p for p in [
        BUNDLES / f"{effective_id}.png.ots",
        BUNDLES / f"{effective_id}.png.ots__time-stamp.json",
        bundle_file,
        bundle_ots,
        BUNDLES / f"{effective_id}.json.ots__time-stamp.json",
    ] if p.exists()]
    log("ARTIFACTS present after loop:\n  - " + ("\n  - ".join(map(str, existing)) if existing else "(none)"))
    log("==== LOOP END ====")


if __name__ == "__main__":
    main()
