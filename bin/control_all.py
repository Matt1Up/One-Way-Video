#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control_all.py — unified controller with singleton locks and full command set.

Usage:
  python3 control_all.py interactive
  python3 control_all.py daemon
  python3 control_all.py immediate -- <command>
  python3 control_all.py --send "start ffox mitm"

Commands inside interactive REPL:
  help                - dynamic help listing available components
  list                - list available component names
  start <name> [...]  - start one or more components by key
  stop <name|all>     - stop components tracked by controller
  ps / status         - show controller-tracked processes
  start_session "<name>" <interval_sec>
  stop_session
  watch-start
  watch-clear         - one-shot clear of watcher
  exit | quit
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse
import os
import sys as _sys
import shlex
import shutil
import json
import time
import signal
import fcntl
import subprocess
from pathlib import Path

# ---------- Paths (portable via evidence_capture) ----------
from evidence_capture.paths import (
    BIN, RUN, RUN_LOGS as LOGS, RUN_LOCKS as LOCKDIR,
    ensure_runtime_dirs
)

STATE_JSON = RUN / "state.json"
LOGS.mkdir(parents=True, exist_ok=True)
LOCKDIR.mkdir(parents=True, exist_ok=True)
RUN.mkdir(parents=True, exist_ok=True)

# Preferred python interpreter (override with EVCAP_PY)
PY = os.environ.get("EVCAP_PY", _sys.executable)

# ---------- Controller state ----------
PROCS = {}  # name -> subprocess.Popen

# ---------- logging helpers ----------
def log_path(name: str, ext: str) -> str:
    return str(LOGS / f"controller-{name}{ext}")

def write_log_line(name: str, msg: str):
    path = LOGS / f"controller-{name}.log"
    with open(path, "ab") as fh:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"[{ts}] {msg}\n".encode("utf-8"))

# ---------- singleton guard ----------
class SingletonLock:
    """
    Cross-process lock for a component. Prevents starting duplicates across
    multiple controller invocations.
    """
    def __init__(self, name: str):
        self.path = LOCKDIR / f"{name}.lock"
        self.fd = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"pid={os.getpid()} time={time.time()}\n".encode("utf-8"))
            return True
        except BlockingIOError:
            return False

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

# ---------- state helpers ----------
def read_state() -> dict:
    try:
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def current_session_name() -> str | None:
    st = read_state()
    return st.get("session_name")

# ---------- process helpers ----------
def run_background(cmd: list, name: str, sudo: bool = False, one_shot: bool = False):
    """
    Start a background process (its own process group).
    - Enforces singleton via lock.
    - Logs stdout/err to per-component files.
    - If one_shot=True, don't stash it in PROCS and release the lock immediately.
    Returns the subprocess.Popen or None if skipped due to lock.
    """
    lock = SingletonLock(name)
    if not lock.acquire():
        msg = f"SKIP: '{name}' already running (singleton lock held)."
        write_log_line(name, msg)
        print(msg)
        return None

    if sudo:
        cmd = ["sudo"] + cmd

    stdout_f = open(log_path(name, ".out"), "ab")
    stderr_f = open(log_path(name, ".err"), "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=stdout_f,
        stderr=stderr_f,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        preexec_fn=os.setpgrp,  # new process group so we can killpg
        text=False,
    )
    header = (
        f"=== started {name} at {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"pid={proc.pid} cmd={' '.join(shlex.quote(x) for x in cmd)} ==="
    )
    write_log_line(name, header)
    print(header)

    if one_shot:
        # one-shot jobs release the lock immediately
        lock.release()
    else:
        setattr(proc, "_singleton_lock", lock)

    return proc

def stop_named(names):
    results = []
    for n in names:
        p = PROCS.get(n)
        if not p:
            results.append((n, "not-running", None))
            continue

        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
            results.append((n, "stopped-signal-sent", p.pid))
        else:
            results.append((n, "already-exited", p.pid))

        # release singleton lock if held
        try:
            lock = getattr(p, "_singleton_lock", None)
            if lock:
                lock.release()
        except Exception:
            pass

        PROCS.pop(n, None)
    return results

def start_named(names):
    started = []
    for n in names:
        if n in PROCS and PROCS[n] is not None and PROCS[n].poll() is None:
            started.append((n, "already-running", PROCS[n].pid))
            continue
        starter = PROC_CMDS.get(n)
        if not starter:
            started.append((n, "unknown", None))
            continue
        proc = starter()
        if proc is None:
            started.append((n, "skipped-locked", None))
            continue
        PROCS[n] = proc
        started.append((n, "started", proc.pid))
    return started

def ps_table():
    rows = []
    for n, p in PROCS.items():
        if p is None:
            continue
        status = "running" if p.poll() is None else f"exit={p.returncode}"
        rows.append((n, p.pid, status))
    return rows

# ---------- component starters ----------
# Each function returns a Popen or None if skipped.

def start_ffox():
    """
    Start Firefox. You can override the binary/profile with env vars:
      EVCAP_FFOX=<path/to/firefox>
      EVCAP_FFOX_PROFILE=<path/to/profile>
    """
    home = Path.home()
    # Prefer a standalone (non-snap) Firefox so we can run alongside the user's
    # normal snap-firefox session. Snap firefox shares a single D-Bus name across
    # all profiles, so a second snap instance exits with "Firefox is already
    # running" no matter what flags we pass.
    standalone_candidates = [
        home / "opt" / "firefox" / "firefox",
        home / "opt" / "firefox-ephemeral" / "firefox",
    ]
    standalone = next((str(p) for p in standalone_candidates if p.is_file() and os.access(p, os.X_OK)), None)
    firefox_bin = os.environ.get(
        "EVCAP_FFOX",
        standalone or shutil.which("firefox") or str(standalone_candidates[0]),
    )
    profile = os.environ.get("EVCAP_FFOX_PROFILE", str(home / ".firefox-mitmproxy"))
    # Remove stale profile lock if present (left by crashes)
    for lock in ("lock", ".parentlock"):
        lp = Path(profile) / lock
        try:
            lp.unlink(missing_ok=True)
        except Exception:
            pass

    # MOZ_NO_REMOTE + --no-remote together ensure full isolation
    env = dict(os.environ)
    env["MOZ_NO_REMOTE"] = "1"
    cmd = [firefox_bin, "--profile", profile, "--no-remote", "--new-instance"]

    lock = SingletonLock("firefox")
    if not lock.acquire():
        msg = "SKIP: 'firefox' already running (singleton lock held)."
        write_log_line("firefox", msg)
        print(msg)
        return None

    stdout_f = open(log_path("firefox", ".out"), "ab")
    stderr_f = open(log_path("firefox", ".err"), "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=stdout_f,
        stderr=stderr_f,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        preexec_fn=os.setpgrp,
        env=env,
        text=False,
    )
    header = (
        f"=== started firefox at {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"pid={proc.pid} cmd={' '.join(shlex.quote(x) for x in cmd)} ==="
    )
    write_log_line("firefox", header)
    print(header)
    setattr(proc, "_singleton_lock", lock)
    return proc

def start_mitm():
    script = BIN / "mitm_dump_control.py"
    cmd = [PY, str(script), "start", "--standby", "--script", str(BIN / "httpstream_json.py")]
    return run_background(cmd, "mitm", one_shot=True)

def _wireshark_preflight() -> bool:
    """Verify this process can run dumpcap (which tshark needs).

    dumpcap is mode 0754 (owner=rwx, group=rwx, other=---), gated by the
    'wireshark' group. If the controller's shell didn't load that group at
    login (common after `usermod -a -G wireshark` without logout), every
    `start net` attempt silently dies because tshark cannot exec dumpcap.

    Returns True if OK to proceed. Returns False after printing an actionable
    error if the group isn't loaded.
    """
    import grp
    import pwd
    try:
        ws_gid = grp.getgrnam("wireshark").gr_gid
    except KeyError:
        return True  # no wireshark group on this system; assume open perms
    if os.geteuid() == 0 or ws_gid in os.getgroups():
        return True
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        in_etc_group = user in grp.getgrgid(ws_gid).gr_mem
    except Exception:
        in_etc_group = False
    lines = [
        "SKIP: 'netstream' cannot start — this shell is missing the 'wireshark' group.",
        "      tshark must exec /usr/bin/dumpcap (mode 0754, group=wireshark) to capture",
        "      packets, but this process does not have wireshark in its group list.",
        "",
    ]
    if in_etc_group:
        lines += [
            "      You ARE in /etc/group as a wireshark member — but your current login",
            "      session predates that change, so the group isn't loaded. Fix (pick one):",
            "",
            "        (permanent)  Log out of your desktop and log back in, OR",
            "        (this term)  Quit the controller, then in this terminal run:",
            "                       exec sg wireshark -",
            "                     and relaunch the controller from that shell.",
        ]
    else:
        lines += [
            "      You are NOT yet in /etc/group as a wireshark member. Add yourself:",
            "        sudo usermod -a -G wireshark $USER",
            "      then log out and back in (or use `exec sg wireshark -`).",
        ]
    msg = "\n".join(lines)
    write_log_line("netstream", msg)
    print(msg)
    return False

def start_net():
    if not _wireshark_preflight():
        return None
    script = BIN / "netstream_json.py"
    return run_background([PY, str(script)], "netstream")

def start_hash():
    # EXACTLY like before: 1920x72, Liberation Mono Bold 36, refresh 0.5,
    # showing state.json:last_hash via the Qt overlay window.
    script = BIN / "overlay_json.py"
    cmd = [
        PY, str(script),
        "--placeholder", "", "--init-mode", "empty", "--refresh", "0.5",
        "--x", "1920", "--y", "1368",
        "--w", "1920", "--h", "72",
        "--font", "Liberation Mono", "--font-size", "36", "--font-weight", "bold",
        "--always-on-top",
    ]
    return run_background(cmd, "overlay_hash")

def start_vcam_overlay():
    """
    Create/ensure an extra v4l2loopback device and feed the hash overlay into it.
    OBS: Add → Video Capture Device (V4L2) → pick EC_Overlay ( /dev/video20 )
    """
    setup = "/usr/local/sbin/ec_overlay_vcam_setup.sh"
    run_background([setup], "vcam_overlay_setup", sudo=True, one_shot=True)

    script = BIN / "overlay_vcam_hash.py"
    cmd = [
        PY, str(script),
        "--device", "/dev/video20",
        "--w", "1920", "--h", "72",
        "--refresh", "0.5",
        "--font-file", "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "--font-size", "36",
        "--font-color", "white",
        "--bg", "black",
        "--key", "last_hash",
        "--placeholder", ""
    ]
    return run_background(cmd, "vcam_overlay")

def start_vcam():
    wrapper = "/usr/local/sbin/obs_vcam_setup.sh"
    run_background([wrapper], "vcam_setup", sudo=True, one_shot=True)
    time.sleep(0.5)
    script = BIN / "obs_studio_ctrl.py"
    subprocess.run([PY, str(script), "vcam-start"], check=False)

# download watcher functions
def start_watch(clear=False, session_name: str | None = None):
    script = BIN / "download_watcher_json.py"
    name = "download_watcher_clear" if clear else "download_watcher"
    args = [PY, str(script)]
    if session_name:
        args += ["--session-name", session_name]
    if clear:
        args += ["--clear"]
        return run_background(args, name, one_shot=True)
    return run_background(args, name)

def start_startsession(session_name: str, interval: str):
    # Stop any leftover mitm before starting the session
    # (start_json.py also does this, but belt-and-suspenders)
    try:
        subprocess.run(
            [PY, str(BIN / "mitm_dump_control.py"), "stop"],
            check=False, capture_output=True,
        )
    except Exception:
        pass

    script = BIN / "start_json.py"
    return run_background(
        [PY, str(script), "--session-name", session_name, "--interval", interval],
        "start_json",
        one_shot=True
    )

def start_stopsession():
    script = BIN / "stop_json.py"
    return run_background([PY, str(script)], "stop_json", one_shot=True)

# --- Local web servers (long-running, with proper args & no absolute user paths) ---
def start_ws_dl():
    """
    Alias: ws-dl
    Runs:  RUN/web-server/local_web_server_5.py
    Args:  --config RUN/web-server/viewer.config.json
           --root   RUN/web-server/
           --host   127.0.0.1
           --port   8050
    """
    ws_dir  = RUN / "web-server"
    script  = ws_dir / "local_web_server_5.py"
    cfg     = ws_dir / "viewer.config.json"
    cmd = [
        PY, str(script),
        "--config", str(cfg),
        "--root",   str(ws_dir),
        "--host",   "127.0.0.1",
        "--port",   "8050",
    ]
    return run_background(cmd, "ws-dl")

def start_ws_bf1():
    """
    Alias: ws-bf1
    Runs:  RUN/web-server/live_hash_loop.py
    Args:  --out RUN/web-server/live_hash_loop.txt
           --dir RUN/bundles
           --interval 0.5
    """
    ws_dir  = RUN / "web-server"
    script  = ws_dir / "live_hash_loop.py"
    outp    = ws_dir / "live_hash_loop.txt"
    bundles = RUN / "bundles"
    cmd = [
        PY, str(script),
        "--out", str(outp),
        "--dir", str(bundles),
        "--interval", "0.5",
    ]
    return run_background(cmd, "ws-bf1")

def start_ws_bf2():
    """
    Aliases: wf-bf2 (primary), ws-bf2 (compat)
    Runs:    RUN/web-server/combined.py
    Args:    --out RUN/web-server/combined.json
             --dir RUN/bundles
             --interval 0.5
    """
    ws_dir  = RUN / "web-server"
    script  = ws_dir / "combined.py"
    outp    = ws_dir / "combined.json"
    bundles = RUN / "bundles"
    cmd = [
        PY, str(script),
        "--out", str(outp),
        "--dir", str(bundles),
        "--interval", "0.5",
    ]
    return run_background(cmd, "wf-bf2")

# Build the mapping of available components.
PROC_CMDS = {
    "ffox": start_ffox,
    "mitm": start_mitm,
    "net": start_net,
    "hash":        start_hash,          # short alias
    "overlay-hash": start_hash,         # explicit alias if you want it
    "vcam": start_vcam,
    "vcam-overlay": start_vcam_overlay, # NEW: second device feeding overlay INTO OBS
    "hash-vcam":    start_vcam_overlay, # optional alias
    "watch-start": lambda: start_watch(clear=False, session_name=current_session_name()),
    "watch-clear": lambda: start_watch(clear=True, session_name=current_session_name()),
    "start_json": lambda: start_startsession(current_session_name() or "session", "8"),
    "stop_json": start_stopsession,

    # Web servers:
    "ws-dl":  start_ws_dl,   # RUN/web-server/local_web_server_5.py
    "ws-bf1": start_ws_bf1,  # RUN/web-server/live_hash_loop.py
    "wf-bf2": start_ws_bf2,  # RUN/web-server/combined.py (primary alias requested)
    "ws-bf2": start_ws_bf2,  # compat alias
}

# ---------- command parser ----------

def handle_command(line: str) -> str:
    """
    Parse a single command line and execute it.
    Supports:
      - help, list
      - start <name>...
      - stop <name|all>
      - ps / status
      - start_session, stop_session
      - watch-start, watch-clear
      - exit | quit
    """
    args = shlex.split(line.strip())
    if not args:
        return ""
    cmd = args[0]

    if cmd in ("help", "?"):
        available = ", ".join(sorted(PROC_CMDS.keys()))
        return (
            "Commands:\n"
            "  start_session \"<name>\" <interval_sec>\n"
            "  stop_session\n"
            "  watch-start\n"
            "  watch-clear\n"
            "  start <name>       # names: " + available + "\n"
            "  stop <name|all>\n"
            "  ps / status        # show controller-tracked processes\n"
            "  list               # list available component names\n"
            "  help | ?\n"
            "  exit | quit\n"
        )

    if cmd == "list":
        return "available: " + ", ".join(sorted(PROC_CMDS.keys()))

    if cmd in ("ps", "status"):
        rows = ps_table()
        if not rows:
            return "(no tracked processes)"
        width = max(len(n) for n,_,_ in rows)
        lines = ["NAME".ljust(width) + "  PID      STATUS"]
        for n, pid, st in rows:
            lines.append(n.ljust(width) + f"  {pid:<8} {st}")
        return "\n".join(lines)

    if cmd == "start_session":
        if len(args) < 2:
            return 'usage: start_session "<name>" [interval_sec]  (default interval: 8)'
        session = args[1]
        interval = args[2] if len(args) >= 3 else "8"

        out = []

        # 1) vcam — safe to call even if already on
        out.append("[1/6] vcam")
        try:
            start_vcam()
        except Exception as e:
            out.append(f"  vcam warn: {e}")

        # 2) hash overlay — start if not already running
        if "overlay_hash" not in PROCS or PROCS["overlay_hash"].poll() is not None:
            out.append("[2/6] hash overlay")
            p = start_hash()
            if p:
                PROCS["overlay_hash"] = p
        else:
            out.append("[2/6] hash overlay (already running)")

        # 3) Firefox — start if not already running
        if "firefox" not in PROCS or PROCS["firefox"].poll() is not None:
            out.append("[3/6] firefox")
            p = start_ffox()
            if p:
                PROCS["firefox"] = p
        else:
            out.append("[3/6] firefox (already running)")

        # 4) netstream daemon — start if not already running
        #    Must be running BEFORE start_json.py sends START to the saver FIFO
        if "netstream" not in PROCS or PROCS["netstream"].poll() is not None:
            out.append("[4/6] netstream (tshark → network_stream.jsonl)")
            p = start_net()
            if p:
                PROCS["netstream"] = p
                time.sleep(0.5)  # let FIFO get created before start_json sends START
            else:
                out.append("  netstream skipped (singleton lock held — already running elsewhere)")
        else:
            out.append("[4/6] netstream (already running)")

        # 6) watch-clear + watch-start (fresh downloads folder)
        out.append("[5/6] watch-clear + watch-start")
        start_watch(clear=True, session_name=session)
        time.sleep(0.3)
        # Stop any existing watcher before starting a new one
        stop_named(["download_watcher"])
        p = start_watch(clear=False, session_name=session)
        if p:
            PROCS["download_watcher"] = p

        # 7) start_json.py (handles: kill old mitm, start mitm recording,
        #    OBS record, capture loop, seed bundle, hash chain bootstrap)
        out.append(f"[6/6] start_session \"{session}\" interval={interval}s")
        p = start_startsession(session, interval)
        if p is None:
            out.append("  start_json skipped (singleton lock held)")
        else:
            out.append(f"  start_json pid={p.pid}")

        return "\n".join(out)

    if cmd == "stop_session":
        out = []
        # Stop download watcher first (so it doesn't pick up artifacts during teardown)
        out.append("[1/4] stopping download watcher")
        stop_named(["download_watcher"])

        # Full teardown (capture, netstream saver, mitm, OBS, HAR conversion)
        out.append("[2/4] stop_session (teardown + HAR conversion)")
        p = start_stopsession()
        if p is None:
            out.append("  stop_session skipped (singleton lock held)")
        else:
            out.append(f"  stop_session pid={p.pid}")
            # Wait for stop_json to finish before archiving
            try:
                p.wait(timeout=120)
            except Exception:
                pass

        # Stop the netstream daemon (tshark) — the saver was already stopped by stop_json.py
        # which zipped the jsonl; now we stop the daemon itself
        out.append("[3/4] stopping netstream daemon")
        stop_named(["netstream"])

        # Archive everything into sessions/<name>/
        out.append("[4/4] archiving session")
        sess = current_session_name() or "session"
        try:
            archive_script = BIN / "archive_session.py"
            result = subprocess.run(
                [PY, str(archive_script), "--session-name", sess],
                capture_output=True, text=True, timeout=60,
            )
            if result.stdout:
                out.append(result.stdout.strip())
            if result.returncode != 0 and result.stderr:
                out.append(f"  archive warn: {result.stderr.strip()}")
        except Exception as e:
            out.append(f"  archive error: {e}")

        return "\n".join(out)

    if cmd == "watch-start":
        p = start_watch(clear=False, session_name=current_session_name())
        if p is None:
            return "watch-start skipped (singleton lock held)"
        PROCS["download_watcher"] = p
        return f"watch-start pid={p.pid}"

    if cmd == "watch-clear":
        start_watch(clear=True, session_name=current_session_name())
        return "watch-clear issued (one-shot)"

    if cmd == "start":
        if len(args) < 2:
            return "usage: start <name> [<name> ...]"
        results = start_named(args[1:])
        return "\n".join(f"{n}: {st} {'' if pid is None else f'(pid={pid})'}" for n, st, pid in results)

    if cmd == "stop":
        if len(args) < 2:
            return "usage: stop <name|all>"
        if args[1] == "all":
            # 'stop all' skips firefox and hash overlay — use 'stop firefox' explicitly
            names = [n for n in PROCS.keys() if n not in PERSISTENT_PROCS]
            skipped = [n for n in PROCS.keys() if n in PERSISTENT_PROCS and PROCS[n] and PROCS[n].poll() is None]
            if skipped:
                print(f"(keeping alive: {', '.join(skipped)} — use 'stop <name>' to force)")
        else:
            names = args[1:]
        results = stop_named(names)
        return "\n".join(f"{n}: {st} {'' if pid is None else f'(pid={pid})'}" for n, st, pid in results)

    if cmd in ("exit", "quit"):
        raise SystemExit(0)

    return f"unknown command: {cmd} (try 'help')"

# ---------- IPC for daemon (simple fifo control) ----------
FIFO = RUN / "controller.fifo"

def run_daemon():
    # Prepare FIFO for --send control
    if FIFO.exists():
        try:
            FIFO.unlink()
        except Exception:
            pass
    os.mkfifo(FIFO, 0o600)
    print(f"[daemon] listening on FIFO: {FIFO}")
    try:
        while True:
            with open(FIFO, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out = handle_command(line)
                        if out:
                            print(out)
                    except SystemExit:
                        print("[daemon] exit requested; ignoring in daemon mode.")
                    except Exception as e:
                        print(f"[daemon] error: {e}")
    finally:
        try:
            FIFO.unlink()
        except Exception:
            pass

def send_to_daemon(cmd: str):
    if not FIFO.exists():
        print(f"controller daemon FIFO not found: {FIFO}", file=sys.stderr)
        sys.exit(1)
    with open(FIFO, "w", encoding="utf-8") as f:
        f.write(cmd.strip() + "\n")

# ---------- reconnect to running processes ----------
def _detect_running():
    """Check singleton locks to find processes still running from a prior controller."""
    found = []
    for name in PROC_CMDS:
        lock_path = LOCKDIR / f"{name}.lock"
        if not lock_path.exists():
            continue
        # Try to acquire the lock — if we can't, something is holding it (i.e., running)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Got the lock — nothing is running, release it
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                # Lock is held — process is still running
                found.append(name)
            finally:
                os.close(fd)
        except Exception:
            pass
    return found


# ---------- main ----------
def main():
    ensure_runtime_dirs()  # <- create run/, logs/, locks/ if missing

    ap = argparse.ArgumentParser(description="Evidence-capture controller with singleton locks.")
    sub = ap.add_subparsers(dest="mode")

    sub.add_parser("interactive", help="Run interactive REPL (recommended).")
    sub.add_parser("daemon", help="Run as a background daemon (control via --send).")

    ap.add_argument("--send", help="Send a single command string to a running 'daemon' instance and exit.")

    p_immediate = sub.add_parser("immediate", help="Run a single command and exit.")
    p_immediate.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (after --).")

    args = ap.parse_args()

    if args.send:
        send_to_daemon(args.send)
        return

    if args.mode == "daemon":
        run_daemon()
        return

    if args.mode == "immediate":
        if not args.cmd:
            print("usage: control_all.py immediate -- <command>", file=sys.stderr)
            sys.exit(2)
        line = " ".join(args.cmd)
        try:
            out = handle_command(line)
            if out:
                print(out)
        except SystemExit as e:
            sys.exit(e.code if isinstance(e.code, int) else 0)
        return

    # default: interactive
    print("controller interactive mode. Type 'help' for commands.")

    # Detect processes still running from a prior controller session
    still_running = _detect_running()
    if still_running:
        print(f"\n  Detected running from prior session: {', '.join(still_running)}")
        print("  (These are tracked by singleton locks — use 'stop <name>' or 'stop all' to manage)\n")
    try:
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line.strip():
                continue
            try:
                out = handle_command(line)
                if out:
                    print(out)
            except SystemExit:
                break
            except Exception as e:
                print(f"error: {e}")
    finally:
        _cleanup_on_exit()


# Processes that persist across sessions and controller restarts
PERSISTENT_PROCS = {"firefox", "overlay_hash"}


def _cleanup_on_exit():
    """Handle controller exit — offer to stop tracked processes (except persistent ones)."""
    running = [(n, p) for n, p in PROCS.items()
               if p and p.poll() is None and n not in PERSISTENT_PROCS]
    persistent_up = [n for n, p in PROCS.items()
                     if p and p.poll() is None and n in PERSISTENT_PROCS]

    if persistent_up:
        print(f"\n[exit] Keeping alive: {', '.join(persistent_up)}")

    if not running:
        print("[exit] No other processes to stop. Goodbye.")
        return

    names = [n for n, _ in running]
    print(f"[exit] Still running: {', '.join(names)}")
    try:
        reply = input("  Stop these before exiting? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = "n"
        print()

    if reply in ("", "y", "yes"):
        print("[exit] Stopping...")
        results = stop_named(names)
        for n, st, pid in results:
            pid_str = f" (pid={pid})" if pid else ""
            print(f"  {n}: {st}{pid_str}")
        print("[exit] Done. Goodbye.")
    else:
        print("[exit] Leaving processes running.")
        print("       Re-run the controller to manage them, or use 'stop all' next time.")

if __name__ == "__main__":
    main()
