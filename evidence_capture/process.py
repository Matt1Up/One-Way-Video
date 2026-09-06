"""Subprocess, file-wait, and logging utilities for the evidence-capture pipeline."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from .timeutil import ts_local_ns


# ── logging ──────────────────────────────────────────────────────────

def log(msg: str) -> None:
    """Print a timestamped log line to stdout."""
    print(f"[{ts_local_ns()}] {msg}", flush=True)


def make_logger(
    *,
    log_file: Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    enabled: bool = True,
) -> Callable[[str], None]:
    """Return a ``log(msg)`` function that prints to stdout and optionally to a file.

    If *log_file* is given the function also appends to that file,
    rotating it once when it exceeds *max_bytes*.
    """

    def _log(msg: str) -> None:
        if not enabled:
            return
        line = f"[{ts_local_ns()}] {msg}"
        print(line, flush=True)
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                if log_file.exists() and log_file.stat().st_size > max_bytes:
                    log_file.replace(log_file.with_suffix(".log.1"))
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    return _log


# ── subprocess helper ────────────────────────────────────────────────

def run_cmd(
    argv,
    *,
    check: bool = True,
    capture: bool = True,
    cwd=None,
    label: str | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with full logging of args / stdout / stderr / exit code.

    Returns the ``CompletedProcess``.  Raises ``CalledProcessError`` when
    *check* is True and the return code is non-zero.
    """
    _log = log_fn or log
    lab = f" [{label}]" if label else ""
    _log(f"RUN{lab}: {' '.join(str(a) for a in argv)}")
    p = subprocess.run(
        [str(a) for a in argv],
        check=False,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture:
        if p.stdout:
            _log(f"STDOUT{lab}: {p.stdout.strip()}")
        if p.stderr:
            _log(f"STDERR{lab}: {p.stderr.strip()}")
    _log(f"EXIT{lab}: {p.returncode}")
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, argv, p.stdout, p.stderr)
    return p


# ── file-wait helper ─────────────────────────────────────────────────

def wait_for_file(
    path: Path | str,
    timeout: float = 240,
    poll: float = 0.25,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Block until *path* exists and is non-empty, or *timeout* expires."""
    _log = log_fn or log
    path = Path(path)
    _log(f"WAIT for file: {path} (timeout {timeout}s)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.exists() and path.stat().st_size > 0:
                _log(f"FOUND file: {path} ({path.stat().st_size} bytes)")
                return True
        except FileNotFoundError:
            pass
        time.sleep(poll)
    _log(f"TIMEOUT waiting for: {path}")
    return False
