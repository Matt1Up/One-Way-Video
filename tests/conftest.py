"""Shared test setup.

- Points EVCAP_HOME at a throwaway directory BEFORE evidence_capture is imported,
  so nothing a test does can touch a real run/ directory.
- Makes the repo root and verify/ importable.
- Provides load_script() for the numbered verify stages, whose filenames
  (e.g. 02_PROCESS_file_hash.py) are not valid module names.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "verify"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

os.environ.setdefault("EVCAP_HOME", tempfile.mkdtemp(prefix="evcap-test-home-"))

for p in (REPO, VERIFY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def load_script(filename: str, modname: str | None = None):
    """Import verify/<filename> as a module object."""
    path = VERIFY / filename
    name = modname or ("v_" + path.stem.replace("-", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
