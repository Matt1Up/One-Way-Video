from __future__ import annotations
from pathlib import Path
import json
from typing import Any, Dict
from .paths import resolve

# Keys in your profiles that represent file paths (string) or list-of-paths
_PATH_KEYS_SINGLE = ("json_file", "live_file", "live_filters")
_PATH_KEYS_LIST   = ("image_dirs",)

def _normalize_profile_paths(profile: Dict[str, Any]) -> Dict[str, Any]:
    prof = dict(profile)  # shallow copy
    for k in _PATH_KEYS_SINGLE:
        if k in prof and isinstance(prof[k], str):
            prof[k] = str(resolve(prof[k]))
    for k in _PATH_KEYS_LIST:
        if k in prof and isinstance(prof[k], list):
            prof[k] = [str(resolve(x)) for x in prof[k]]
    return prof

def load_config(config_rel_path: str = "run/web-server/viewer.config.json") -> Dict[str, Any]:
    """
    Load a JSON config file (default points at your existing viewer.config.json),
    and normalize any relative paths to absolute paths anchored at the repo root.
    """
    cfg_path = resolve(config_rel_path)
    data = json.loads(Path(cfg_path).read_text())
    if isinstance(data, dict) and "profiles" in data and isinstance(data["profiles"], list):
        data["profiles"] = [_normalize_profile_paths(p) for p in data["profiles"]]
    return data


# ──────────────────────────── PDF signing credentials ────────────────────────
# Defaults consumed by bin/sign_pdfs.py for invisible PDF signatures on
# evidence-vault PDFs. The precedence chain in sign_pdfs.py is:
#   1) Env vars  (EVCAP_KEY_PEM / EVCAP_CERT_PEM / EVCAP_P12 / EVCAP_P12_PASS)
#   2) These constants
#   3) Hard-coded fallback (empty)
# PEM is preferred over PKCS#12 when both are present and the key is unencrypted.
# Leave these as None and set the env vars instead (see .env.example), or point
# them at your own key material, e.g. "/path/to/evidence-key.pem".
#
# PREFERRED: set EVCAP_KEY_PEM / EVCAP_CERT_PEM (or EVCAP_P12 / EVCAP_P12_PASS) in
# your git-ignored .env. This file IS tracked by git: do not commit real key paths
# here. Values below are last-resort local defaults only.
SIGN_KEY_PEM  = None
SIGN_CERT_PEM = None
SIGN_P12      = None
SIGN_P12_PASS = None  # set if the .p12 above is password-protected
