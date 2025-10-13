#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
start_json.py — JSON-native bootstrap for the evidence-capture pipeline (portable)

What changed vs your original:
- Anchors all paths to the repo via `evidence_capture.paths` (no ~ or /media hard-codes)
- Uses your current Python (sys.executable) for all subprocesses; override via EVCAP_PY
- MITM dump output defaults to RUN/dumps (override with --dump-out-dir or EVCAP_DUMP_DIR)
- Optional CLI args: --v4l-device, --dump-out-dir
- Keeps your clear-logs / clear-state behaviors & bundle/hash/OTS sequence

Safe defaults: nothing in your local flow changes; this should "just work" in your venv.
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, os, sys, time, subprocess, shutil, glob, fcntl, hashlib
from pathlib import Path
from datetime import datetime, timezone

# ========= Feature toggles (defaults; CLI can override) =========
ENABLE_PNG_WATCHER_DEFAULT = False
ENABLE_PLACEHOLDER_A_DEFAULT = False
ENABLE_PLACEHOLDER_B_DEFAULT = False

# ---------- Portable paths ----------
from evidence_capture.paths import ROOT, RUN, BIN, ensure_runtime_dirs

TIME_DIR  = ROOT / "time"
BUNDLES   = RUN / "bundles"
LOG_DIR   = RUN / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Legacy helper file that roughtime_client.py uses/maintains
LAST_RT_TXT = RUN / "last_roughtime.txt"

# JSON state + lock
STATE_JSON = RUN / "state.json"
STATE_LOCK = RUN / "state.lock"

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

# Logging
LAST_HASH_LOG = LOG_DIR / "last_hash.log"

# Timing
OTS_WAIT_SECONDS = 240
OTS_POLL = 0.25

# Python interpreter for subprocesses (your venv by default)
PY = Path(os.environ.get("EVCAP_PY", sys.executable))

# Default dumps dir (override with --dump-out-dir or EVCAP_DUMP_DIR)
DEFAULT_DUMP_DIR = Path(os.environ.get("EVCAP_DUMP_DIR", str(RUN / "dumps")))

# ---------- Small utilities ----------
def _ts_local_ns() -> str:
    ns = time.time_ns()
    sec, rem = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(sec, tz=datetime.now().astimezone().tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{rem:09d} " + (dt.tzname() or "")

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def log(msg: str):
    print(f"[{_ts_local_ns()}] {msg}", flush=True)

def run_cmd(argv, check=True, capture=True, cwd=None, label: str | None = None):
    lab = f" [{label}]" if label else ""
    log(f"RUN{lab}: " + " ".join(map(str, argv)))
    p = subprocess.run(
        argv, check=False, cwd=str(cwd) if cwd else None, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture:
        if p.stdout:
            log(f"STDOUT{lab}: {p.stdout.strip()}")
        if p.stderr:
            log(f"STDERR{lab}: {p.stderr.strip()}")
    log(f"EXIT{lab}: {p.returncode}")
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, argv, p.stdout, p.stderr)
    return p

def wait_for_file(path: Path, timeout_sec=OTS_WAIT_SECONDS, poll=OTS_POLL) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if path.exists() and path.stat().st_size > 0:
                return True
        except FileNotFoundError:
            pass
        time.sleep(poll)
    return False

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------- Locked JSON state I/O ----------
class Flock:
    def __init__(self, lock: Path): self.lock = lock; self.fd = None
    def __enter__(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *args):
        try: fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally: os.close(self.fd); self.fd = None

def _state_read() -> dict:
    if not STATE_JSON.exists(): return {}
    try:
        with Flock(STATE_LOCK):
            with STATE_JSON.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {}

def _state_write(obj: dict) -> None:
    tmp = STATE_JSON.with_suffix(STATE_JSON.suffix + ".tmp")
    with Flock(STATE_LOCK):
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(obj or {}, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, STATE_JSON)

def state_set_with_time(key: str, value, sys_time: str | None = None):
    if sys_time is None: sys_time = _ts_local_ns()
    st = _state_read()
    st[key] = value
    st[f"{key}_sys_time"] = sys_time
    _state_write(st)
    return value, sys_time

def state_get(key, default=None):
    return _state_read().get(key, default)

def state_clear_streams_and_files_recent():
    st = _state_read()
    # Clear streams: http, net, and http_events list
    streams = st.get("streams") or {}
    streams["http"] = []
    streams["net"] = []
    st["streams"] = streams
    st["http_events"] = []
    st["files_recent"] = []
    st["streams_sys_time"] = _ts_local_ns()
    st["files_recent_sys_time"] = _ts_local_ns()
    _state_write(st)
    log("[STATE] Cleared streams (http, net) + http_events + files_recent")

# ---------- last_hash log ----------
def log_last_hash_change(context: str, src: str, src_path: Path, id_hint: str | None,
                         before: str, after: str):
    try:
        entry = {
            "ts": _ts_local_ns(), "context": context, "source": src, "id": id_hint,
            "in_path": str(src_path), "before": before, "after": after
        }
        with LAST_HASH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log(f"[last_hash.log] {entry}")
    except Exception as e:
        log(f"[WARN] failed to write last_hash.log: {e}")

# ---------- Streams snapshot from state.json ----------
def snapshot_streams(limit: int = 5) -> dict:
    st = _state_read()
    now = _ts_local_ns()
    streams = st.get("streams") or {}
    http = (streams.get("http") or [])[:limit]
    net  = (streams.get("net")  or [])[:limit]
    return {
        "http_freeze_sys": now, "http": http,
        "net_freeze_sys":  now, "net":  net
    }

# ---------- last_files snapshot (compat: safe if missing) ----------
def snapshot_last_files() -> dict:
    rows = []
    freeze = _ts_local_ns()
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
                            rows.append({"name": parts[0], "time": parts[1], "sha256": parts[2]})
                        else:
                            rows.append({"raw": ln})
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return {"sys_time_freeze": freeze, "rows": rows}

# ---------- Roughtime & OTS helpers ----------
def roughtime_bind(target: Path, json_out_dir: Path) -> Path | None:
    json_out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd([
        str(PY), str(TIME_DIR / "roughtime_client.py"), "query", "-v",
        "--server", "roughtime.cloudflare.com", "--port", "2003",
        "--pubkey-base64", "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=",
        "--reveal-hash",
        "--bind-file", str(target),
        "--last-time", str(LAST_RT_TXT),
        "--json-out", str(json_out_dir),
    ], check=True, capture=True, label=f"roughtime {target.name}")
    newest = None
    for p in json_out_dir.glob("*.json"):
        if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
            newest = p
    return newest

def ots_stamp(target: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(target), str(out_dir)],
            check=True, capture=True, label=f"ots {target.name}")
    for cand in (out_dir / f"{target.name}.ots", out_dir / (target.stem + ".ots")):
        if cand.exists(): return cand
    raise RuntimeError(f"OTS not created for {target}")

# ---------- Purge / Clear ----------
def _safe_remove(p: Path):
    try:
        if p.is_file() or p.is_symlink(): p.unlink(missing_ok=True)
        elif p.is_dir(): shutil.rmtree(p)
    except Exception: pass

def purge_previous_outputs(aggressive: bool):
    BUNDLES.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    if aggressive:
        log("[PURGE] Aggressively clearing bundles/*")
        for p in BUNDLES.glob("*"): _safe_remove(p)
    else:
        log("[PURGE] Cleaning typical previous outputs (conservative)")
        for t in [START_JSON, START_JSON_OTS, BUNDLES / "0000.json",
                  BUNDLES / "0000.json.ots", BUNDLES / "0000.json.ots__time-stamp.json",
                  START_JSON_OTS_TS]:
            _safe_remove(t)
        for pat in [str(BUNDLES / "roughtime_proof_*.json"), str(BUNDLES / "start_time*.json")]:
            for p in glob.glob(pat): _safe_remove(Path(p))
    # reset JSON state keys (baseline)
    st = _state_read()
    st.update({
        "last_index": 0,
        "last_index_sys_time": _ts_local_ns(),
        "last_hash": "",
        "last_hash_sys_time": _ts_local_ns(),
        "last_img_hash": "",
        "last_img_hash_sys_time": _ts_local_ns(),
    })
    _state_write(st)
    # rotate last_hash.log (simple)
    if LAST_HASH_LOG.exists() and LAST_HASH_LOG.stat().st_size > 5*1024*1024:
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
    ap.add_argument("--aggressive", action="store_true",
                    help="Aggressively remove ALL files under run/bundles before running.")
    ap.add_argument("--interval", type=float, default=8.0,
                    help="Seconds between image captures for capture_randomized_save_json.py.")
    ap.add_argument("--session-name", default=None,
                    help="Session label saved into state.json and passed to background workers. Also used as mitm dump filename ('.dump').")
    ap.add_argument("--no-obs", action="store_true", help="Skip starting OBS controller.")
    ap.add_argument("--no-net", action="store_true", help="Skip starting netstream.")
    ap.add_argument("--no-http", action="store_true", help="Skip starting mitmproxy/httpstream.")
    ap.add_argument("--no-download-watcher", action="store_true", help="Skip starting download_watcher_json.py.")
    ap.add_argument("--no-clear-logs", action="store_true", help="Do not clear logs on start.")
    ap.add_argument("--no-clear-state", action="store_true", help="Do not clear streams/files_recent on start.")
    # Optional extras (off by default)
    ap.add_argument("--png-watcher", action="store_true", help="Start png_watcher_json.py (optional).")
    ap.add_argument("--placeholder-a", action="store_true", help="Start placeholder_a_json.py (optional).")
    ap.add_argument("--placeholder-b", action="store_true", help="Start placeholder_b_json.py (optional).")
    # New quality-of-life overrides
    ap.add_argument("--v4l-device", default="/dev/video2", help="Device for capture_randomized_save_json.py")
    ap.add_argument("--dump-out-dir", default=str(DEFAULT_DUMP_DIR), help="Output dir for mitm dumps")
    args = ap.parse_args()

    # 0) Purge / init
    purge_previous_outputs(args.aggressive)
    BUNDLES.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)

    # 0.1) Clear logs (default ON)
    if not args.no_clear_logs:
        clear_logs()
    else:
        log("[SKIP] log clearing (per flag)")

    # 0.2) Set/record session name (write to state.json so others see it)
    sess = args.session_name or state_get("session_name") or "session"
    state_set_with_time("session_name", sess)
    log(f"[SESSION] session_name = {sess}")

    # 0.3) Clear streams/http_events/files_recent (default ON)
    if not args.no_clear_state:
        state_clear_streams_and_files_recent()
    else:
        log("[SKIP] state.json streams/files_recent clearing (per flag)")

    # S0) OBS (optional)
    if not args.no_obs:
        try:
            subprocess.Popen([str(PY), str(BIN / "obs_studio_ctrl.py"), "start"])
            log("[STARTUP] obs_studio_ctrl.py")
        except Exception as e:
            log(f"[WARN] couldn't start OBS controller: {e}")

    # S1) mitm_dump_control.py + httpstream_json.py (optional)
    if not args.no_http:
        try:
            dump_dir = Path(args.dump_out_dir).expanduser().resolve()
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_name = f"{sess}.dump" if not str(sess).endswith(".dump") else str(sess)
            subprocess.Popen([
                str(PY), str(BIN / "mitm_dump_control.py"), "start",
                "--out-dir", str(dump_dir),
                "--filename", dump_name,
                "--script", str(BIN / "httpstream_json.py"),
            ])
            log(f"[STARTUP] mitm_dump_control + httpstream_json (filename={dump_name}, out={dump_dir})")
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

    # S4) download watcher (default ON previously commented; keep off unless you enable flag here)
    # If you want it started automatically, uncomment this block.
    # if not args.no_download_watcher:
    #     try:
    #         subprocess.Popen([str(PY), str(BIN / "download_watcher_json.py"), "--session-name", sess])
    #         log("[STARTUP] download_watcher_json.py")
    #     except Exception as e:
    #         log(f"[WARN] couldn't start download_watcher_json.py: {e}")

    # Optional extras (flags override defaults)
    if args.png_watcher or ENABLE_PNG_WATCHER_DEFAULT:
        try:
            subprocess.Popen([str(PY), str(BIN / "png_watcher_json.py"), "--session-name", sess])
            log("[STARTUP] png_watcher_json.py (optional)")
        except Exception as e:
            log(f"[WARN] couldn't start png_watcher_json.py: {e}")

    if args.placeholder_a or ENABLE_PLACEHOLDER_A_DEFAULT:
        try:
            subprocess.Popen([str(PY), str(BIN / "placeholder_a_json.py"), "--session-name", sess])
            log("[STARTUP] placeholder_a_json.py (optional)")
        except Exception as e:
            log(f"[WARN] couldn't start placeholder_a_json.py: {e}")

    if args.placeholder_b or ENABLE_PLACEHOLDER_B_DEFAULT:
        try:
            subprocess.Popen([str(PY), str(BIN / "placeholder_b_json.py"), "--session-name", sess])
            log("[STARTUP] placeholder_b_json.py (optional)")
        except Exception as e:
            log(f"[WARN] couldn't start placeholder_b_json.py: {e}")

    # Small pause for daemons to come up
    time.sleep(0.5)

    # ---- PART 1: Seed start_time.json (nonce) ----
    run_cmd([str(PY), str(BIN / "get_start_time_json.py"), "--save-path", str(START_JSON)],
            check=True, capture=True, label="get_start_time_json")
    log(f"[OK] wrote {START_JSON.name}")

    # SHA-256 of start_time.json → state.json
    start_sha = sha256_file(START_JSON)
    state_set_with_time("start_time_sha256", start_sha)
    log(f"[OK] start_time.json sha256 = {start_sha}")

    # OTS of start_time.json (wait)
    ots = ots_stamp(START_JSON, BUNDLES)
    if not wait_for_file(ots):
        log(f"ERROR: Timed out waiting for OTS: {ots}"); sys.exit(1)
    log(f"[OK] OTS for start_time.json: {ots.name}")

    # Roughtime bind the start_time.json .ots
    ots_rt_json = roughtime_bind(ots, BUNDLES)
    if ots_rt_json:
        log(f"[OK] Roughtime bind JSON: {ots_rt_json.name}")
        # mirror (best-effort) last_roughtime into state.json
        try:
            if LAST_RT_TXT.exists():
                val = LAST_RT_TXT.read_text(encoding="utf-8").strip()
                state_set_with_time("last_roughtime", val)
        except Exception:
            pass

    # ---- PART 2: Build initial bundle 0000.json ----
    sys_time_in = _ts_local_ns()
    streams_snap = snapshot_streams(limit=5)
    last_files   = snapshot_last_files()

    # Prev pointers (mostly empty at bootstrap)
    prev_hash_val = state_get("last_hash") or ""
    prev_img_val  = state_get("last_img_hash") or ""
    prev_rt_val   = state_get("last_roughtime") or ""

    bundle_obj = {
        "id": "0000",
        "index_raw": "0",
        "sys_time_in": sys_time_in,

        "prev": {
            "last_hash":      {"value": prev_hash_val, "sys_time": state_get("last_hash_sys_time")},
            "last_img_hash":  {"value": prev_img_val,  "sys_time": state_get("last_img_hash_sys_time")},
            "last_timestamp": {"value": prev_rt_val,   "sys_time": state_get("last_roughtime_sys_time")},
        },

        # No PNG yet; seed is start_time.json instead
        "start": {
            "path": START_JSON.name,
            "sha256": {"value": start_sha, "sys_time": _ts_local_ns()},
            "ots": {
                "path": START_JSON_OTS.name,
                "rt_json": START_JSON_OTS_TS.name,
                "sys_time": _ts_local_ns()
            }
        },

        "streams": streams_snap,
        "last_files": last_files,
    }

    # sys_time_out BEFORE writing so hash seals this value
    bundle_obj["sys_time_out"] = _ts_local_ns()

    bundle_path = BUNDLES / "0000.json"
    bundle_bytes = (json.dumps(bundle_obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    bundle_path.write_bytes(bundle_bytes)
    log(f"[OK] wrote {bundle_path.name}")

    # Update last_hash (and log)
    before = prev_hash_val
    after  = hashlib.sha256(bundle_bytes).hexdigest()
    state_set_with_time("last_hash", after)
    log_last_hash_change(context="start", src="hash_of_bundle_json", src_path=bundle_path, id_hint="0000",
                         before=before, after=after)

    # OTS + Roughtime for 0000.json
    b_ots = ots_stamp(bundle_path, BUNDLES)
    if not wait_for_file(b_ots):
        log(f"ERROR: Timed out waiting for bundle OTS: {b_ots}"); sys.exit(1)
    log(f"[OK] OTS for 0000.json: {b_ots.name}")
    b_ots_rt = roughtime_bind(b_ots, BUNDLES)
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
        log(msg); sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as ex:
        log(f"Unhandled error: {ex}"); sys.exit(1)
