#!/usr/bin/env python3
# script_A.py — user-extensible hook, fired ONCE by capture_randomized_save_json.py
# immediately before the first frame capture of a session (see SCRIPT_A there).
# Default behaviour is deliberately minimal: append a UTC timestamp + "A triggered"
# to run/trigger_A.log so you can confirm the hook fired. Replace the body with
# whatever you need to happen at session start (e.g. start a secondary recorder).
# It is NOT dead code; do not delete it without also removing the SCRIPT_A call.

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
t = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
with LOG.open("a", encoding="utf-8") as f:
    f.write(f"{t}\tA triggered\n")
