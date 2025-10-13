#!/usr/bin/env python3
# script_A.py — portable trigger logger

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path

from evidence_capture.paths import RUN, ensure_runtime_dirs

ensure_runtime_dirs()
LOG = RUN / "trigger_A.log"

LOG.parent.mkdir(parents=True, exist_ok=True)
t = datetime.utcnow().isoformat() + "Z"
with LOG.open("a", encoding="utf-8") as f:
    f.write(f"{t}\tA triggered\n")
