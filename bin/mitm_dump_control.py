#!/usr/bin/env python3
"""
mitm_dump_control.py

Controller to start/stop a mitmdump session that writes a raw mitmproxy flow dump to disk.

Examples:

# START a proxy in standby (listens but does NOT write a dump yet)
python3 bin/mitm_dump_control.py start --standby

# RECORD to a dump file (defaults to run/state.json's session_name.dump)
python3 bin/mitm_dump_control.py start \
  --out-dir ./captures \
  --filename Testing-1.dump
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import shutil
import subprocess

# ========= Repo-anchored defaults (portable) =========
from evidence_capture.paths import ROOT, RUN, ensure_runtime_dirs

ensure_runtime_dirs()

DEFAULT_RUN_DIR = RUN
DEFAULT_OUT_DIR = ROOT / "captures"
DEFAULT_FILENAME = "mitm_session.dump"
DEFAULT_PIDFILE = RUN / "mitm_dump.pid"
DEFAULT_LOGFILE = RUN / "logs" / "mitm_dump.log"


def _resolve_mitmdump() -> str:
    """Find the mitmdump executable, preferring the dedicated venv.

    mitmproxy pins many dependencies, so we install it in ~/.venvs/mitm/
    (see requirements-mitm.txt). When the controller is launched from a
    shell that hasn't activated that venv, bare `mitmdump` is not on PATH
    and start fails with `[Errno 2] No such file or directory: 'mitmdump'`.
    Fall back to known install paths before giving up.
    """
    override = os.environ.get("EVCAP_MITMDUMP")
    if override:
        return override
    candidates = [
        Path.home() / ".venvs" / "mitm" / "bin" / "mitmdump",
        Path.home() / ".venvs" / "evidence-capture" / "bin" / "mitmdump",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    on_path = shutil.which("mitmdump")
    if on_path:
        return on_path
    return str(candidates[0])  # let Popen produce the FileNotFoundError


DEFAULT_MITM = _resolve_mitmdump()
DEFAULT_PORT = 18080
DEFAULT_HOST = "127.0.0.1"
DEFAULT_WAIT_AFTER_SIGINT = 10  # seconds to wait for graceful exit


def ensure_dirs(*paths: Path):
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _default_session_name() -> str:
    """Read session_name from run/state.json; fallback to 'session'."""
    try:
        st_path = RUN / "state.json"
        if st_path.exists():
            with st_path.open("r", encoding="utf-8") as f:
                st = json.load(f)
            sn = st.get("session_name")
            if isinstance(sn, str) and sn.strip():
                return sn.strip()
    except Exception:
        pass
    return "session"


def read_pidfile(pidpath: Path) -> Optional[dict]:
    if not pidpath.exists():
        return None
    try:
        return json.loads(pidpath.read_text())
    except Exception:
        return None


def write_pidfile(pidpath: Path, info: dict):
    pidpath.write_text(json.dumps(info))


def remove_pidfile(pidpath: Path):
    try:
        pidpath.unlink()
    except Exception:
        pass


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, timeout: float) -> bool:
    waited = 0.0
    interval = 0.25
    while waited < timeout:
        if not is_pid_running(pid):
            return True
        time.sleep(interval)
        waited += interval
    return not is_pid_running(pid)


def start_session(
    mitm_exe: str,
    out_dir: Path,
    filename: str,
    run_dir: Path,
    mitm_host: str,
    mitm_port: int,
    extra_scripts: List[str],
    pidfile: Path,
    logfile: Path,
    standby: bool = False,
) -> int:
    """Start mitmdump. If `standby` is True, start without -w (no dump file)."""
    ensure_dirs(out_dir, run_dir, logfile.parent)

    dump_path = (out_dir / filename).resolve()
    log_path = logfile.resolve()

    # Check existing pidfile
    existing = read_pidfile(pidfile)
    if existing and "pid" in existing and is_pid_running(existing["pid"]):
        existing_mode = existing.get("mode", "record")
        if existing_mode == "standby" and not standby:
            pid_to_stop = int(existing["pid"])
            print(f"Found existing standby mitmdump (pid {pid_to_stop}). Stopping it to start recording...")
            try:
                os.killpg(pid_to_stop, signal.SIGINT)
            except Exception:
                try:
                    os.kill(pid_to_stop, signal.SIGINT)
                except Exception as e:
                    print(f"Failed to SIGINT standby pid {pid_to_stop}: {e}", file=sys.stderr)
            if not _wait_for_exit(pid_to_stop, DEFAULT_WAIT_AFTER_SIGINT):
                print(f"Standby pid {pid_to_stop} did not exit; sending SIGTERM.")
                try:
                    os.killpg(pid_to_stop, signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid_to_stop, signal.SIGTERM)
                    except Exception as e:
                        print(f"Failed to SIGTERM standby pid {pid_to_stop}: {e}", file=sys.stderr)
                        raise RuntimeError("Could not stop standby mitmdump cleanly.")
            remove_pidfile(pidfile)
            print("Standby stopped, proceeding to start recording.")
        else:
            raise RuntimeError(
                f"Existing mitmdump seems to be running with pid {existing['pid']} (mode={existing.get('mode')}). "
                "Stop it first or use --standby if you intended to start a standby proxy."
            )

    # sanitize extra_scripts: expanduser/env and keep only existing files
    cleaned_scripts: List[str] = []
    if isinstance(extra_scripts, str):
        extra_scripts = [extra_scripts]
    for s in (extra_scripts or []):
        if not s:
            continue
        s_expanded = os.path.expanduser(os.path.expandvars(str(s)))
        if len(s_expanded) <= 2:
            continue
        p = Path(s_expanded)
        if not p.exists():
            print(f"Warning: mitm script not found, skipping: {s_expanded}", file=sys.stderr)
            continue
        cleaned_scripts.append(str(p.resolve()))

    # build mitmdump command
    cmd = [mitm_exe]
    if not standby:
        cmd += ["-w", str(dump_path)]
    cmd += ["--listen-host", mitm_host, "--listen-port", str(mitm_port)]
    for s in cleaned_scripts:
        cmd += ["-s", s]

    # open log file
    lf = open(str(log_path), "ab", buffering=0)
    try:
        # start as its own process group (so we can signal the group)
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            preexec_fn=os.setsid,  # new process group on Unix
            cwd=str(out_dir),
        )
    except Exception:
        lf.close()
        raise

    # write pidfile/metadata
    info = {
        "pid": proc.pid,
        "start_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dump_path": str(dump_path) if not standby else "",
        "log_path": str(log_path),
        "cmd": cmd,
        "mitm_host": mitm_host,
        "mitm_port": mitm_port,
        "mode": "standby" if standby else "record",
    }
    write_pidfile(pidfile, info)
    print(f"Started mitmdump (pid {proc.pid}) mode={info['mode']}. Dump path: {info['dump_path'] or '(none)'}")
    print(f"Logs: {log_path}")
    return proc.pid


def stop_session(pidfile: Path, pid_timeout: int = DEFAULT_WAIT_AFTER_SIGINT) -> None:
    """Stop the running mitmdump process gracefully; write .ready sentinel next to dump."""
    info = read_pidfile(pidfile)
    if not info:
        print("No running session found (no pidfile).")
        return
    pid = int(info.get("pid", 0))
    dump_path = Path(info.get("dump_path")) if info.get("dump_path") else None
    log_path = Path(info.get("log_path")) if info.get("log_path") else None

    if not is_pid_running(pid):
        print(f"Process {pid} not running. Cleaning up pidfile.")
        remove_pidfile(pidfile)
        if dump_path and dump_path.exists():
            ready = dump_path.with_suffix(dump_path.suffix + ".ready")
            ready.write_text(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            print(f"Created ready sentinel: {ready}")
        return

    print(f"Stopping mitmdump pid {pid} (graceful SIGINT). See logs at {log_path} for progress.")
    try:
        os.killpg(pid, signal.SIGINT)
    except Exception:
        try:
            os.kill(pid, signal.SIGINT)
        except Exception as e:
            print(f"Failed to signal pid {pid}: {e}", file=sys.stderr)

    if not _wait_for_exit(pid, pid_timeout):
        print(f"Process {pid} did not exit after {pid_timeout}s; sending SIGTERM.")
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                print(f"Failed to SIGTERM pid {pid}: {e}", file=sys.stderr)

    if dump_path and dump_path.exists():
        ready = dump_path.with_suffix(dump_path.suffix + ".ready")
        ready.write_text(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        print(f"Created ready sentinel: {ready}")

    remove_pidfile(pidfile)


def status_session(pidfile: Path):
    info = read_pidfile(pidfile)
    if not info:
        print("No session pidfile found.")
        return
    pid = int(info.get("pid", 0))
    running = is_pid_running(pid)
    print("Session info:")
    print(json.dumps(info, indent=2))
    print(f"Running: {running}")
    if running:
        print(f"Process {pid} is alive (mode={info.get('mode')}).")
    else:
        print("Process not running; consider removing pidfile if stale.")


def main():
    p = argparse.ArgumentParser(description="Start/stop mitmdump that writes a flow dump.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # start subcommand
    s_start = sub.add_parser("start", help="Start mitmdump (write dump file). Use --standby to start proxy without writing a dump.")
    s_start.add_argument("--mitm-exe", default=DEFAULT_MITM, help="mitmdump executable (default: mitmdump)")
    s_start.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory for dump file")
    s_start.add_argument("--filename", default=DEFAULT_FILENAME, help="Dump filename (e.g. session.dump)")
    s_start.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR), help="Run directory for pid/log files")
    s_start.add_argument("--pidfile", default=str(DEFAULT_PIDFILE), help="PID/metadata file")
    s_start.add_argument("--logfile", default=str(DEFAULT_LOGFILE), help="Log file path")
    s_start.add_argument("--host", default=DEFAULT_HOST, help="mitm listen host (default 127.0.0.1)")
    s_start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"mitm listen port (default {DEFAULT_PORT})")
    s_start.add_argument("--script", "-s", action="append", default=[], help="Optional mitmproxy addon script to load (can repeat, use full path)")
    s_start.add_argument("--standby", action="store_true", help="Start proxy without writing a dump file.")
    s_start.set_defaults(cmd="start")

    # stop subcommand
    s_stop = sub.add_parser("stop", help="Stop mitmdump (writes .ready next to dump).")
    s_stop.add_argument("--pidfile", default=str(DEFAULT_PIDFILE), help="PID/metadata file")
    s_stop.add_argument("--timeout", type=int, default=DEFAULT_WAIT_AFTER_SIGINT, help="Seconds to wait after SIGINT before SIGTERM")
    s_stop.set_defaults(cmd="stop")

    # status subcommand
    s_stat = sub.add_parser("status", help="Show status of running session (if any).")
    s_stat.add_argument("--pidfile", default=str(DEFAULT_PIDFILE), help="PID/metadata file")
    s_stat.set_defaults(cmd="status")

    args = p.parse_args()

    if args.cmd == "start":
        out_dir = Path(os.path.expanduser(args.out_dir)).resolve()
        run_dir = Path(os.path.expanduser(args.run_dir)).resolve()
        pidfile = Path(os.path.expanduser(args.pidfile)).resolve()
        logfile = Path(os.path.expanduser(args.logfile)).resolve()
        ensure_dirs(out_dir, run_dir, logfile.parent)

        # Default dump filename to session_name.dump when not explicitly provided
        if not args.filename or args.filename == str(DEFAULT_FILENAME):
            try:
                session_name = _default_session_name()
            except Exception:
                session_name = "session"
            args.filename = session_name if session_name.lower().endswith(".dump") else f"{session_name}.dump"

        # sanitize scripts prior to passing into start_session
        cleaned_scripts: List[str] = []
        for s in (args.script or []):
            if not s:
                continue
            s_expanded = os.path.expanduser(os.path.expandvars(str(s)))
            if len(s_expanded) <= 2:
                continue
            pth = Path(s_expanded)
            if not pth.exists():
                print(f"Warning: mitm script not found, skipping: {s_expanded}", file=sys.stderr)
                continue
            cleaned_scripts.append(str(pth.resolve()))

        try:
            pid = start_session(
                mitm_exe=args.mitm_exe,
                out_dir=out_dir,
                filename=args.filename,
                run_dir=run_dir,
                mitm_host=args.host,
                mitm_port=args.port,
                extra_scripts=cleaned_scripts,
                pidfile=pidfile,
                logfile=logfile,
                standby=args.standby,
            )
            print(f"Session started with pid {pid}.")
            if args.standby:
                print("Started in standby mode (proxy running, not recording).")
            else:
                print("Recording to dump file. To stop: run `mitm_dump_control.py stop`")
        except Exception as e:
            print(f"Failed to start session: {e}", file=sys.stderr)
            sys.exit(2)

    elif args.cmd == "stop":
        pidfile = Path(os.path.expanduser(args.pidfile)).resolve()
        stop_session(pidfile, pid_timeout=args.timeout)

    elif args.cmd == "status":
        pidfile = Path(os.path.expanduser(args.pidfile)).resolve()
        status_session(pidfile)

    else:
        print("Invalid command.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
