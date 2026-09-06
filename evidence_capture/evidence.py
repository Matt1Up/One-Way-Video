"""Roughtime and OpenTimestamps bindings for the evidence-capture pipeline."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from .paths import BIN, RUN, PY
from .process import run_cmd, log as default_log

TIME_DIR = BIN / "time"
LAST_RT_TXT = RUN / "last_roughtime.txt"

# Roughtime defaults (Cloudflare)
RT_SERVER = "roughtime.cloudflare.com"
RT_PORT = "2003"
RT_PUBKEY = "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg="


def roughtime_bind(
    target: Path,
    json_out_dir: Path,
    *,
    server: str = RT_SERVER,
    port: str = RT_PORT,
    pubkey: str = RT_PUBKEY,
    last_rt_txt: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> Path | None:
    """Roughtime-bind a file.  Returns the newest JSON proof, or ``None``."""
    _log = log_fn or default_log
    _last_rt = last_rt_txt or LAST_RT_TXT
    json_out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            str(PY), str(TIME_DIR / "roughtime_client.py"), "query", "-v",
            "--server", server, "--port", port,
            "--pubkey-base64", pubkey,
            "--reveal-hash",
            "--bind-file", str(target),
            "--last-time", str(_last_rt),
            "--json-out", str(json_out_dir),
        ],
        check=True, capture=True,
        label=f"roughtime {target.name}", log_fn=_log,
    )
    newest = None
    for p in json_out_dir.glob("*.json"):
        if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
            newest = p
    return newest


def ots_stamp(
    target: Path,
    out_dir: Path,
    *,
    wait_if_exists: float = 8.0,
    log_fn: Callable[[str], None] | None = None,
) -> Path:
    """OTS-stamp a file.  Idempotent — reuses an existing ``.ots`` if found.

    Returns the path to the ``.ots`` file inside *out_dir*.
    """
    _log = log_fn or default_log
    out_dir.mkdir(parents=True, exist_ok=True)

    # Candidate locations where the .ots might appear
    candidates = [
        out_dir / f"{target.name}.ots",
        out_dir / (target.stem + ".ots"),
        target.parent / f"{target.name}.ots",
        target.parent / (target.stem + ".ots"),
    ]

    def _normalize(c: Path) -> Path:
        """Move an .ots into *out_dir* if it landed elsewhere."""
        if c.parent == out_dir:
            return c
        dest = out_dir / c.name
        if dest.exists():
            return dest
        try:
            c.replace(dest)
            _log(f"OTS normalized: {dest}")
            return dest
        except Exception:
            return c

    # 0) Reuse if already present
    for c in candidates:
        if c.exists():
            _log(f"OTS exists already: {c}")
            return _normalize(c)

    # 1) Try stamping
    try:
        run_cmd(
            [str(PY), str(BIN / "get_ots_stamp.py"), str(target), str(out_dir)],
            check=True, capture=True,
            label=f"ots {target.name}", log_fn=_log,
        )
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "") + "\n" + (e.stdout or "")
        if "File exists" in msg:
            _log("OTS: 'File exists' reported; trying discovery.")
        elif "need at least 2 attestations" in msg:
            _log("OTS: Pool attestation timeout; trying discovery window.")
        else:
            _log("OTS: non-benign error; will attempt discovery before failing.")

    # 2) Discovery loop (wait for file to appear)
    deadline = time.time() + max(0.0, wait_if_exists)
    while True:
        for c in candidates:
            if c.exists():
                _log(f"OTS discovered: {c}")
                return _normalize(c)
        if time.time() >= deadline:
            break
        time.sleep(0.25)

    raise RuntimeError(f"OTS not created for {target}")
