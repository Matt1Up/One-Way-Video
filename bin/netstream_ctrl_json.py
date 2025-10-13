#!/usr/bin/env python3
"""
netstream_ctrl_json.py — minimal controller for netstream_json.py via FIFO (portable)

Usage:
  python3 netstream_ctrl_json.py START [--path /full/path/to/file.tsv] [--fifo ...] [--wait-reply] [--timeout 1.0] [--quiet]
  python3 netstream_ctrl_json.py STOP  [--fifo ...] [--wait-reply] [--timeout 1.0] [--quiet]
  python3 netstream_ctrl_json.py STATUS[--fifo ...] [--wait-reply] [--timeout 1.0] [--quiet]

Defaults:
- Fire-and-forget (no reply read) unless --wait-reply is given.
- Non-blocking reply read with timeout (won't freeze the CLI).
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import os
import sys
import time
import argparse
from pathlib import Path

# Repo-anchored default FIFO (matches your repo layout: run/network_stream_fifo.txt)
from evidence_capture.paths import RUN, ensure_runtime_dirs
ensure_runtime_dirs()

DEFAULT_CTRL_FIFO = RUN / "netstream.ctrl"  # was: ~/evidence-capture/run/netstream.ctrl
DEFAULT_REPLY_FIFO = Path(str(DEFAULT_CTRL_FIFO) + ".out")  # optional reply channel

def expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))

def fifo_paths(base_fifo: str):
    ctrl = expand(base_fifo)
    reply = ctrl + ".out"
    return ctrl, reply

def send_line(fifo_path: str, line: str) -> None:
    with open(fifo_path, "w", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")

def try_read_reply(reply_fifo: str, timeout: float) -> str | None:
    path = expand(reply_fifo)
    if not os.path.exists(path):
        return None
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                data = os.read(fd, 4096)
            finally:
                os.close(fd)
            if data:
                return data.decode("utf-8", "ignore").splitlines()[0].strip()
            time.sleep(0.05)
        except FileNotFoundError:
            return None
        except (BlockingIOError, OSError):
            time.sleep(0.05)
    return None

def log(msg: str, quiet: bool):
    if not quiet:
        print(msg)

def main():
    ap = argparse.ArgumentParser(description="Control netstream_json.py via FIFO")
    ap.add_argument("command", choices=["START", "STOP", "STATUS"], help="Control command")
    ap.add_argument("--path", help="TSV path for START (optional; uses netstream default if omitted)")
    ap.add_argument(
        "--fifo",
        default=str(DEFAULT_CTRL_FIFO),
        help=f"Control FIFO path (default: {DEFAULT_CTRL_FIFO})"
    )
    ap.add_argument("--wait-reply", action="store_true", help="Wait briefly for a reply message from the daemon")
    ap.add_argument("--timeout", type=float, default=1.0, help="Seconds to wait when --wait-reply is used (default: 1.0)")
    ap.add_argument("--quiet", action="store_true", help="Suppress log output")
    args = ap.parse_args()

    ctrl_fifo, reply_fifo = fifo_paths(args.fifo)

    if not os.path.exists(ctrl_fifo):
        log(f"[ERR] Control FIFO not found: {ctrl_fifo}\n      Make sure netstream_json.py is running (it creates the FIFO).", args.quiet)
        sys.exit(2)

    if args.command == "START":
        line = "START " + (args.path or "")
        log(f"[->] {line}", args.quiet)
        send_line(ctrl_fifo, line)
        if args.wait_reply:
            resp = try_read_reply(reply_fifo, args.timeout)
            if resp and not args.quiet:
                print(f"[<-] {resp}")

    elif args.command == "STOP":
        log("[->] STOP", args.quiet)
        send_line(ctrl_fifo, "STOP")
        if args.wait_reply:
            resp = try_read_reply(reply_fifo, args.timeout)
            if resp and not args.quiet:
                print(f"[<-] {resp}")

    elif args.command == "STATUS":
        log("[->] STATUS", args.quiet)
        send_line(ctrl_fifo, "STATUS")
        if args.wait_reply:
            resp = try_read_reply(reply_fifo, args.timeout)
            if resp and not args.quiet:
                print(f"[<-] {resp}")

if __name__ == "__main__":
    main()
