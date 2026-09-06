from __future__ import annotations
from pathlib import Path
import os
import sys as _sys

# Allow deployments to relocate the whole repo:
#   EVCAP_HOME=/opt/evidence-capture python3 bin/control_all.py
def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]  # package -> repo root

ROOT = Path(os.environ.get("EVCAP_HOME", _default_root()))

# Common top-level dirs (add more here as you standardize)
BIN       = ROOT / "bin"
RUN       = ROOT / "run"
RUN_LOCKS = RUN / "locks"
RUN_LOGS  = RUN / "logs"
WEB       = RUN / "web-server"
BUNDLES   = RUN / "bundles"

# Optional: other named roots you already have
CAPTURES  = ROOT / "captures"
DOWNLOADS = ROOT / "downloads"
IMAGES    = ROOT / "images"
OVERLAY   = ROOT / "overlay"

# Python interpreter (current venv unless overridden)
PY = Path(os.environ.get("EVCAP_PY", _sys.executable))

def ensure_runtime_dirs() -> None:
    """Create runtime dirs safely (idempotent)."""
    for p in (RUN, RUN_LOCKS, RUN_LOGS, WEB, BUNDLES):
        p.mkdir(parents=True, exist_ok=True)

def resolve(pathlike: str | Path) -> Path:
    """Treat non-absolute as relative to repo ROOT; return absolute Path."""
    p = Path(pathlike)
    return p if p.is_absolute() else (ROOT / p)

def rel(pathlike: str | Path) -> Path:
    """Return a repo-root-relative path (useful for logs / UI messages)."""
    return Path(pathlike).resolve().relative_to(ROOT)
