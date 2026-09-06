#!/usr/bin/env python3
# netstream_json.py
"""
tshark -> websocat streamer with:
  1) WebSocket stream to browser ('|' delimited lines — unchanged)
  2) Rolling echo file (NDJSON, newest first, max 5) -> RUN/network_stream_fifo.txt
  3) Optional NDJSON saver controlled via FIFO RUN/netstream.ctrl (START/STOP)
  4) Updates RUN/state.json -> streams.net (keeps latest 5 with net_index)

Only change from the original behavior: paths are now anchored to the repo root
(parent of the 'bin' folder), so nothing gets created under bin/.

ENV overrides (optional):
  STATE_JSON_FILE   (default: <REPO>/run/state.json)
  STATE_LOCK_FILE   (default: <REPO>/run/state.lock)
  NET_FIFO_MAX      (default: 5)
"""

import os, sys, shlex, signal, argparse, threading, subprocess, fcntl, time, zipfile, json, stat
from pathlib import Path
from datetime import datetime, timezone

# -------- Repo paths (via shared module) --------
# --- portable import bootstrap ---
_sys_path_added = str(Path(__file__).resolve().parents[1])
if _sys_path_added not in sys.path:
    sys.path.insert(0, _sys_path_added)

from evidence_capture.paths import RUN, BIN, ensure_runtime_dirs
from evidence_capture.state import json_update_locked
from evidence_capture.timeutil import now_utc_iso

ensure_runtime_dirs()
RUN_DIR = RUN
BIN_DIR = BIN

# -------- Tshark setup (unchanged) --------
TSHARK_FIELDS = [
    "-T", "fields",
    "-e", "frame.time_epoch",
    "-e", "_ws.col.Protocol",
    "-e", "ip.src",
    "-e", "tcp.srcport",
    "-e", "udp.srcport",
    "-e", "ip.dst",
    "-e", "tcp.dstport",
    "-e", "udp.dstport",
    "-e", "frame.len",
    "-E", "separator=|",
    "-E", "header=n",
]
TSHARK_FILTER = "ip || ipv6 || arp || tcp || udp"

IDX_TIME  = 0
IDX_PROTO = 1
IDX_SRC   = 2
IDX_DST   = 5

# -------- STATE JSON config --------
STATE_JSON = Path(os.path.expanduser(os.environ.get("STATE_JSON_FILE", str(RUN_DIR / "state.json"))))
STATE_LOCK = Path(os.path.expanduser(os.environ.get("STATE_LOCK_FILE", str(RUN_DIR / "state.lock"))))
MAX_STATE_LINES = int(os.environ.get("NET_FIFO_MAX", "5"))
STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)

def now_iso():
    return now_utc_iso()

def append_event_to_state_net(event: dict):
    """Prepend one event to streams.net (cap to MAX_STATE_LINES) and bump streams.net_index."""
    def _update(st):
        streams = st.get("streams") or {}
        net_list = streams.get("net") or []
        idx = int(streams.get("net_index", 0)) + 1
        event["idx"] = idx
        streams["net"] = [event] + net_list[: max(0, MAX_STATE_LINES - 1)]
        streams["net_index"] = idx
        st["streams"] = streams
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)

# -------- Echo file helpers (NDJSON, newest-first) --------
def json_compact(o: dict) -> str:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False)

def prepend_with_max(file_path: Path, lock_path: Path, new_line: str, max_lines: int):
    file_path = Path(file_path)
    lock_path = Path(lock_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path  = file_path.with_suffix(file_path.suffix + ".tmp")

    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            existing = []
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8", errors="replace") as rf:
                    existing = [line.rstrip("\n") for line in rf.readlines()]
            new_lines = [new_line.rstrip("\n")] + existing
            new_lines = new_lines[:max_lines]
            with open(tmp_path, "w", encoding="utf-8", newline="") as wf:
                for ln in new_lines:
                    wf.write(ln + "\n")
            os.replace(tmp_path, file_path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

class Saver:
    """Start/Stop NDJSON saver (STOP -> zip + delete)."""
    def __init__(self, default_out: Path | None):
        self.default_out = Path(default_out).expanduser() if default_out else None
        self.fp = None
        self.saving = False
        self.current_path = None
        self.lock = threading.Lock()

    def start(self, outpath: str | None):
        path = Path(outpath).expanduser() if outpath else self.default_out
        if not path:
            print("[ERROR] START requested but no output path and no --default-out set", flush=True)
            return "ERR no output path"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if self.fp:
                try: self.fp.close()
                except Exception: pass
            self.fp = open(path, "w", encoding="utf-8", newline="")
            self.fp.flush()
            self.saving = True
            self.current_path = str(path)
        print(f"[INFO] START saving (NDJSON) -> {path}", flush=True)
        return f"OK STARTED {path}"

    def stop(self):
        path = None
        with self.lock:
            if self.fp:
                path = self.current_path
                try:
                    self.fp.close()
                    print(f"[INFO] STOP saving -> closed {path}", flush=True)
                except Exception:
                    pass
            self.fp = None
            self.saving = False
            self.current_path = None

        if path and os.path.exists(path):
            zip_path = path + ".zip"
            try:
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(path, arcname=os.path.basename(path))
                os.remove(path)
                print(f"[INFO] Compressed to {zip_path} and removed original", flush=True)
            except Exception as e:
                print(f"[WARN] Failed to zip/delete ({path}): {e}", flush=True)
        return "OK STOPPED"

    def append_from_pipe_line(self, pipe_line: str):
        if not self.saving:
            return
        obj = line_to_obj(pipe_line)
        js = json_compact(obj) + "\n"
        with self.lock:
            if not self.fp:
                return
            try:
                self.fp.write(js)
                self.fp.flush()
            except Exception as e:
                print("[ERROR] write failed:", e, flush=True)

def line_to_obj(pipe_line: str) -> dict:
    """Convert a '|' tshark line into an event dict; add write-time 'ts'."""
    parts = pipe_line.split("|")
    def g(i): return parts[i] if i < len(parts) else ""
    t_raw = g(IDX_TIME)
    try:
        t_val = float(t_raw)
    except Exception:
        t_val = t_raw
    return {
        "t": t_val,             # frame.time_epoch from tshark
        "proto": g(IDX_PROTO),  # Protocol
        "src": g(IDX_SRC),      # ip.src
        "dst": g(IDX_DST),      # ip.dst
        "ts": now_iso()         # system write time (UTC ISO-8601)
    }

def ensure_fifo(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists():
            if not stat.S_ISFIFO(path.stat().st_mode):
                raise RuntimeError(f"{path} exists and is not a FIFO")
        else:
            os.mkfifo(path, 0o600)
    except Exception as e:
        print(f"[ERROR] creating FIFO {path}: {e}", flush=True)
        raise
    return path

def run_control_fifo(fifo_path: Path, saver: Saver):
    fifo_path = ensure_fifo(fifo_path)
    print(f"[INFO] Control FIFO ready: {fifo_path}", flush=True)
    while True:
        try:
            with open(fifo_path, "r") as fr:
                for line in fr:
                    cmdline = line.strip()
                    if not cmdline:
                        continue
                    parts = cmdline.split(None, 1)
                    cmd = parts[0].upper()
                    arg = parts[1] if len(parts) > 1 else None
                    if cmd == "START":
                        resp = saver.start(arg)
                    elif cmd == "STOP":
                        resp = saver.stop()
                    elif cmd == "STATUS":
                        state = "SAVING" if saver.saving else "STOPPED"
                        resp = f"STATUS {state} {saver.current_path}"
                        print(resp, flush=True)
                    else:
                        resp = "ERR unknown command (use START [path] / STOP / STATUS)"
                    print(f"[CTRL] {cmdline} -> {resp}", flush=True)
        except FileNotFoundError:
            time.sleep(0.5)
        except Exception as e:
            print(f"[WARN] FIFO loop error: {e}", flush=True)
            time.sleep(0.5)

def main():
    ap = argparse.ArgumentParser(description="tshark -> websocat with NDJSON echo, saver, and state.json updates")
    ap.add_argument("--interface", "-i", default="1", help="tshark interface index/name (default 1)")
    ap.add_argument("--ws-host", default="127.0.0.1", help="websocat host (default 127.0.0.1)")
    ap.add_argument("--ws-port", type=int, default=9001, help="websocat port (default 9001)")

    # Default paths are repo-anchored:
    ap.add_argument("--default-out", default=str(RUN_DIR / "bundles" / "network_stream.jsonl"),
                    help="default NDJSON output path for START if none given")
    ap.add_argument("--control-fifo", default=str(RUN_DIR / "netstream.ctrl"),
                    help="path to control FIFO (START/STOP/STATUS)")

    ap.add_argument("--echo-file", default=str(RUN_DIR / "network_stream_fifo.txt"),
                    help="rolling echo file (newest first, NDJSON)")
    ap.add_argument("--echo-lock", default=str(BIN_DIR / "network_stream_fifo.lock"),
                    help="lock file used to protect the echo file updates")
    ap.add_argument("--echo-max", type=int, default=5, help="max rows kept in echo file (default 5)")
    args = ap.parse_args()

    saver = Saver(Path(args.default_out))

    # Control FIFO thread (repo anchored)
    ctrl_thr = threading.Thread(target=run_control_fifo, args=(Path(args.control_fifo), saver), daemon=True)
    ctrl_thr.start()

    # websocat server (stdin gets tshark lines; browser connects to ws://host:port)
    ws_cmd = ["websocat", "-s", f"{args.ws_host}:{args.ws_port}"]
    try:
        ws_proc = subprocess.Popen(ws_cmd, stdin=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        print("[ERROR] websocat not found. Install it or adjust PATH.", flush=True)
        sys.exit(1)

    # tshark reader
    tshark_cmd = ["tshark", "-i", str(args.interface), "-l", "-n", "-Q"] + TSHARK_FIELDS + ["-Y", TSHARK_FILTER]
    print("[INFO] Launching:", " ".join(shlex.quote(x) for x in tshark_cmd), flush=True)
    try:
        tshark = subprocess.Popen(tshark_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        print("[ERROR] tshark not found. Install it or adjust PATH.", flush=True)
        ws_proc.terminate()
        sys.exit(1)

    stop_flag = {"stop": False}
    def _shutdown(sig, _frm):
        print(f"[INFO] Signal {sig} received, shutting down...", flush=True)
        stop_flag["stop"] = True
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _shutdown)

    echo_file = Path(args.echo_file)
    echo_lock = Path(args.echo_lock)
    echo_max  = int(args.echo_max)

    try:
        while not stop_flag["stop"]:
            line = tshark.stdout.readline()
            if line == "":
                break
            line = line.rstrip("\r\n")

            # 1) Stream to browser (unchanged)
            if ws_proc.stdin:
                try:
                    ws_proc.stdin.write(line + "\n")
                    ws_proc.stdin.flush()
                except Exception:
                    pass

            # Construct object once
            obj = line_to_obj(line)

            # 2) Echo file (NDJSON), newest first, keep max
            try:
                prepend_with_max(echo_file, echo_lock, json_compact(obj), echo_max)
            except Exception:
                pass

            # 3) Save NDJSON if enabled
            saver.append_from_pipe_line(line)

            # 4) Update shared state.json (streams.net)
            try:
                append_event_to_state_net(obj)
            except Exception as e:
                print(f"[WARN] state.json update failed: {e}", flush=True)

    finally:
        try: saver.stop()
        except Exception: pass
        try:
            if tshark.poll() is None: tshark.terminate()
        except Exception: pass
        try:
            if ws_proc.stdin:
                try: ws_proc.stdin.close()
                except Exception: pass
            if ws_proc.poll() is None: ws_proc.terminate()
        except Exception: pass
        print("[INFO] netstream_json stopped", flush=True)

if __name__ == "__main__":
    main()
