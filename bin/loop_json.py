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

import argparse, json, subprocess, sys, time, hashlib, fcntl, os
from pathlib import Path
from datetime import datetime

# ---------- Portable repo paths ----------
from evidence_capture.paths import ROOT, RUN, BIN, ensure_runtime_dirs

TIME_DIR = ROOT / "time"
BUNDLES  = RUN / "bundles"
PNG_DIR  = BUNDLES

# Legacy helper files that external tools still expect
LAST_INDEX_TXT = RUN / "last_index.txt"
LAST_RT_TXT    = RUN / "last_roughtime.txt"  # roughtime_client.py uses/maintains this

# ---------- JSON state (globals) ----------
STATE_JSON = RUN / "state.json"
STATE_LOCK = RUN / "state.lock"

# ---------- Logging ----------
LOG_DIR        = RUN / "logs"
LOG_FILE       = LOG_DIR / "loop.log"
LAST_HASH_LOG  = LOG_DIR / "last_hash.log"
MAX_LOG_BYTES  = 5 * 1024 * 1024

# ---------- Timing ----------
WAIT_OTS_SECS = 240
POLL = 0.25

# ---------- Globals ----------
DEBUG_ENABLED = True

# ---------- Interpreter (use current venv unless overridden) ----------
PY = Path(os.environ.get("EVCAP_PY", sys.executable))

# ---------- Utilities ----------
def _ts_local_ns() -> str:
    ns = time.time_ns()
    sec = ns // 1_000_000_000
    rem = ns % 1_000_000_000
    dt = datetime.fromtimestamp(sec, tz=datetime.now().astimezone().tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{rem:09d} {dt.tzname() or ''}"

def log(msg: str):
    if not DEBUG_ENABLED:
        return
    line = f"[{_ts_local_ns()}] {msg}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def run_cmd(args, **kwargs):
    log(f"RUN: {' '.join(map(str, args))}")
    if DEBUG_ENABLED:
        res = subprocess.run(args, text=True, capture_output=True, **kwargs)
        if res.stdout: log(f"STDOUT: {res.stdout.strip()}")
        if res.stderr: log(f"STDERR: {res.stderr.strip()}")
        log(f"EXIT: {res.returncode}")
    else:
        res = subprocess.run(args, **kwargs)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, args)
    return res

def wait_for_file(path: Path, timeout=WAIT_OTS_SECS, poll=POLL) -> bool:
    log(f"WAIT for file: {path} (timeout {timeout}s)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.exists() and path.stat().st_size > 0:
                log(f"FOUND file: {path} ({path.stat().st_size} bytes)")
                return True
        except Exception:
            pass
        time.sleep(poll)
    log(f"TIMEOUT waiting for: {path}")
    return False

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

# ---------- JSON state helpers ----------
def _state_read() -> dict:
    try:
        STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_LOCK, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                if not STATE_JSON.exists():
                    return {}
                with STATE_JSON.open("r", encoding="utf-8") as f:
                    return json.load(f)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        return {}

def _state_write(obj: dict) -> None:
    tmp = STATE_JSON.with_suffix(STATE_JSON.suffix + ".tmp")
    with open(STATE_LOCK, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(obj or {}, f, ensure_ascii=False, separators=(",", ":"))
                f.write("\n")
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, STATE_JSON)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

def state_get(key, default=None):
    return _state_read().get(key, default)

def state_get_with_time(key):
    st = _state_read()
    return st.get(key), st.get(f"{key}_sys_time")

def state_set(key, value):
    st = _state_read()
    st[key] = value
    _state_write(st)
    return value

def state_set_with_time(key, value, sys_time=None):
    if sys_time is None:
        sys_time = _ts_local_ns()
    st = _state_read()
    st[key] = value
    st[f"{key}_sys_time"] = sys_time
    _state_write(st)
    return value, sys_time

# ---------- last_hash LOG (unchanged) ----------
def log_last_hash_change(context: str, src: str, src_path: Path, id_hint: str | None,
                         before: str, after: str):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = _ts_local_ns()
        line = {"ts": ts, "context": context, "source": src, "id": id_hint,
                "in_path": str(src_path), "before": before, "after": after}
        with LAST_HASH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        if DEBUG_ENABLED: log(f"LAST_HASH_LOG: {line}")
    except Exception as e:
        if DEBUG_ENABLED: log(f"WARN: failed to write last_hash log: {e}")

# ---------- Streams snapshot from state.json ----------
def snapshot_stream(st: dict, key: str, limit: int = 5) -> dict:
    now = _ts_local_ns()
    streams = st.get("streams") or {}
    items = streams.get(key) or []
    snap = items[:limit]
    return {"sys_time_freeze": now, "items": snap}

# >>> Snapshot list (e.g., http_events) with "last 5" semantics
def snapshot_tail_list(st: dict, key: str, limit: int = 5) -> dict:
    now = _ts_local_ns()
    items = st.get(key) or []
    tail = items[-limit:] if isinstance(items, list) else []
    return {"sys_time_freeze": now, "items": tail}

# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    global DEBUG_ENABLED
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

    DEBUG_ENABLED = bool(args.debug)
    if DEBUG_ENABLED: log("==== LOOP START ====")
    time.sleep(max(0.0, args.sleep))

    # 0) Determine effective_id (prefer state.json; fallback to last_index.txt)
    st = _state_read()
    if "last_index" in st:
        last_index_raw = str(st["last_index"])
    else:
        if not LAST_INDEX_TXT.exists():
            if DEBUG_ENABLED: log(f"ERROR: {LAST_INDEX_TXT} not found and state.json missing 'last_index'")
            sys.exit(2)
        last_index_raw = LAST_INDEX_TXT.read_text(encoding="utf-8").strip()

    try:
        n = int(last_index_raw)
    except Exception:
        n = 0
    effective = max(n - 1, 0)
    effective_id = f"{effective:04d}"
    if DEBUG_ENABLED:
        log(f"last_index raw='{last_index_raw}' -> effective_id='{effective_id}'")

    # Skip bootstrap (0000 handled elsewhere)
    if effective == 0:
        if DEBUG_ENABLED: log("effective_id is 0000; bootstrap handled elsewhere. Skipping.")
        sys.exit(0)

    # 0.1) Record sys_time_in to state.json immediately
    sys_time_in = _ts_local_ns()
    state_set_with_time("sys_time_in", sys_time_in)

    # 1) Ensure PNG exists
    img_path = PNG_DIR / f"{effective_id}.png"
    if DEBUG_ENABLED: log(f"Resolved image path: {img_path}")
    deadline = time.time() + float(args.image_wait)
    while (not img_path.exists()) or img_path.stat().st_size == 0:
        if time.time() >= deadline:
            if DEBUG_ENABLED: log(f"ERROR: image still missing after {args.image_wait}s: {img_path}")
            print(f"ERROR: image not found for effective_id {effective_id}: {img_path}", file=sys.stderr)
            sys.exit(2)
        time.sleep(0.1)
    if DEBUG_ENABLED:
        try: log(f"FOUND image: {img_path} ({img_path.stat().st_size} bytes)")
        except Exception: pass

    # 2) Compute image hash (no *_hash.txt files)
    img_hash_sys_time = _ts_local_ns()
    img_sha = sha256_file(img_path)
    state_set_with_time("last_img_hash", img_sha, img_hash_sys_time)

    # 3) OTS the PNG
    BUNDLES.mkdir(parents=True, exist_ok=True)
    run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(img_path), str(BUNDLES)])
    img_ots = BUNDLES / f"{effective_id}.png.ots"
    if not wait_for_file(img_ots):
        print(f"ERROR: Timed out waiting for image OTS: {img_ots}", file=sys.stderr)
        sys.exit(1)

    # 4) Roughtime bind the PNG .ots (bind the .ots itself)
    run_cmd([
        str(PY), str(TIME_DIR / "roughtime_client.py"), "query", "-v",
        "--server", "roughtime.cloudflare.com", "--port", "2003",
        "--pubkey-base64", "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=",
        "--reveal-hash",
        "--bind-file", str(img_ots),
        "--last-time", str(LAST_RT_TXT),
        "--json-out", str(BUNDLES),
    ])
    img_ots_json = BUNDLES / f"{effective_id}.png.ots__time-stamp.json"
    # Mirror last roughtime to state.json (+ time of write)
    try:
        if LAST_RT_TXT.exists():
            state_set_with_time("last_roughtime", LAST_RT_TXT.read_text(encoding="utf-8").strip(), _ts_local_ns())
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
        if DEBUG_ENABLED: log(f"WARN parsing roughtime JSON (image .ots): {e}")

    # 5) Snapshot streams + events + last_files from state.json
    st_now = _state_read()
    http_snap = snapshot_stream(st_now, "http", 5)  # {sys_time_freeze, items}
    net_snap  = snapshot_stream(st_now, "net",  5)

    # >>> last five http_events (tail)
    http_events_snap = snapshot_tail_list(st_now, "http_events", 5)

    # 6) last_files snapshot (text → JSON rows) with freeze time
    last_files_rows = []
    last_files_freeze_sys = _ts_local_ns()
    lf_path = RUN / "last_files.txt"
    lf_lock = RUN / "last_files.lock"
    if lf_path.exists():
        lf_lock.parent.mkdir(parents=True, exist_ok=True)
        lf_lock.touch(exist_ok=True)
        with lf_lock.open("rb") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                with lf_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        ln = ln.rstrip("\r\n")
                        if not ln: continue
                        parts = ln.split("\t")
                        if len(parts) == 3:
                            last_files_rows.append({"name": parts[0], "time": parts[1], "sha256": parts[2]})
                        else:
                            last_files_rows.append({"raw": ln})
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # >>> snapshot files_recent objects and plan to clear them post-hash
    files_recent_list = st_now.get("files_recent") or []
    files_recent_freeze_sys = _ts_local_ns()

    # 7) Build bundle JSON (include sys_time_out NOW, before writing)
    prev_hash_val, prev_hash_time = state_get_with_time("last_hash")
    prev_img_val,  prev_img_time  = state_get_with_time("last_img_hash")
    prev_rt_val,   prev_rt_time   = state_get_with_time("last_roughtime")

    # >>> scoped snapshot of streams.http_events tail (last 5) + freeze time
    _http_events_all = (st_now.get("streams") or {}).get("http_events") or []
    _http_events_tail = _http_events_all[-5:] if isinstance(_http_events_all, list) else []
    _http_events_freeze_sys = _ts_local_ns()

    bundle_obj = {
        "id": effective_id,
        "index_raw": last_index_raw,
        "sys_time_in": sys_time_in,

        "prev": {
            "last_hash":      {"value": prev_hash_val or "", "sys_time": prev_hash_time or ""},
            "last_img_hash":  {"value": prev_img_val  or "", "sys_time": prev_img_time  or ""},
            "last_timestamp": {"value": prev_rt_val   or "", "sys_time": prev_rt_time   or ""},
        },

        "image": {
            "path": str(img_path),
            "sha256": {"value": img_sha, "sys_time": img_hash_sys_time},
            "ots": {
                "path": str(img_ots),
                "rt_json": str(img_ots_json),
                "bind_sha256_hex": img_bind_hex,
                "sys_time": _ts_local_ns()
            }
        },

        "streams": {
            "http_freeze_sys": http_snap["sys_time_freeze"],
            "http": http_snap["items"],
            "net_freeze_sys":  net_snap["sys_time_freeze"],
            "net":  net_snap["items"],
            # last five http_events from streams
            "http_events_freeze_sys": _http_events_freeze_sys,
            "http_events": _http_events_tail,
        },

        # (top-level "http_events" block not used; kept within "streams")

        "last_files": {
            "sys_time_freeze": last_files_freeze_sys,
            "rows": last_files_rows
        },

        # include files_recent (to be cleared immediately after seal)
        "downloads": {
            "files_recent_freeze_sys": files_recent_freeze_sys,
            "files_recent": files_recent_list
        }
    }

    # add sys_time_out BEFORE first/only write so hashing seals this content
    bundle_obj["sys_time_out"] = _ts_local_ns()

    # 8) Write bundle JSON, then hash the actual bytes
    bundle_file = BUNDLES / f"{effective_id}.json"
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_bytes = (json.dumps(bundle_obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with bundle_file.open("wb") as f:
        f.write(bundle_bytes)
    if DEBUG_ENABLED: log(f"WROTE bundle JSON: {bundle_file}")

    before = prev_hash_val or ""
    after  = sha256_bytes(bundle_bytes)
    state_set_with_time("last_hash", after, _ts_local_ns())  # overlay source for next frame
    log_last_hash_change(context="loop", src="hash_of_bundle_json",
                         src_path=bundle_file, id_hint=effective_id,
                         before=before, after=after)

    # clear files_recent NOW (after bundle is sealed & last_hash updated)
    try:
        state_set_with_time("files_recent", [], _ts_local_ns())
        if DEBUG_ENABLED:
            log("Cleared state.json: files_recent=[] (post-bundle seal)")
    except Exception as e:
        if DEBUG_ENABLED:
            log(f"WARN: failed to clear files_recent: {e}")

    # 9) OTS of bundle JSON (file)
    run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(bundle_file), str(BUNDLES)])
    bundle_ots = BUNDLES / f"{effective_id}.json.ots"
    if not wait_for_file(bundle_ots):
        print(f"ERROR: Timed out waiting for bundle OTS: {bundle_ots}", file=sys.stderr)
        sys.exit(1)

    # 10) Roughtime bind the bundle .ots (bind the .ots itself; no post-hash edits to bundle)
    run_cmd([
        str(PY), str(TIME_DIR / "roughtime_client.py"), "query", "-v",
        "--server", "roughtime.cloudflare.com", "--port", "2003",
        "--pubkey-base64", "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=",
        "--reveal-hash",
        "--bind-file", str(bundle_ots),
        "--last-time", str(LAST_RT_TXT),
        "--json-out", str(BUNDLES),
    ])
    # mirror any updated roughtime again (safe)
    try:
        if LAST_RT_TXT.exists():
            state_set_with_time("last_roughtime", LAST_RT_TXT.read_text(encoding="utf-8").strip(), _ts_local_ns())
    except Exception:
        pass

    if DEBUG_ENABLED:
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
