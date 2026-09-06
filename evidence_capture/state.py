"""
Centralised JSON-state management for the evidence-capture pipeline.

Provides:
 - Flock           — cross-process file lock (context manager)
 - json_read_locked / json_write_locked / json_update_locked
                   — generic locked JSON I/O (works for state.json, ledger, etc.)
 - state_*         — convenience wrappers for the canonical run/state.json
 - snapshot_*      — point-in-time reads of streams, files, etc.
 - log_last_hash_change — append to the last_hash audit log
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

from .paths import RUN
from .timeutil import ts_local_ns

# ── canonical paths ──────────────────────────────────────────────────
STATE_JSON = RUN / "state.json"
STATE_LOCK = RUN / "state.lock"
LOG_DIR = RUN / "logs"
LAST_HASH_LOG = LOG_DIR / "last_hash.log"


# ── file locking ─────────────────────────────────────────────────────

class Flock:
    """Cross-process advisory lock using fcntl.flock (context manager).

    Usage::

        with Flock(lock_path):               # exclusive (default)
            ...
        with Flock(lock_path, exclusive=False):  # shared
            ...
    """

    def __init__(self, lock_path: Path | str, *, exclusive: bool = True):
        self.lock_path = Path(lock_path)
        self.exclusive = exclusive
        self.fd: int | None = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        fcntl.flock(self.fd, mode)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


# ── generic locked JSON I/O ──────────────────────────────────────────

def _load_json(json_path: Path) -> dict:
    """Read JSON without locking (caller must already hold a lock)."""
    if not json_path.exists():
        return {}
    try:
        with json_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(json_path: Path, obj: dict) -> None:
    """Atomic write of JSON (caller must hold an exclusive lock)."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(json_path.parent), delete=False,
    )
    try:
        json.dump(obj or {}, tmp, ensure_ascii=False, separators=(",", ":"))
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        name = tmp.name
    finally:
        tmp.close()
    os.replace(name, str(json_path))


def json_read_locked(json_path: Path, lock_path: Path) -> dict:
    """Read a JSON file under a **shared** lock."""
    with Flock(lock_path, exclusive=False):
        return _load_json(json_path)


def json_write_locked(json_path: Path, lock_path: Path, obj: dict) -> None:
    """Atomically write a JSON file under an **exclusive** lock."""
    with Flock(lock_path, exclusive=True):
        _save_json(json_path, obj)


def json_update_locked(json_path: Path, lock_path: Path, fn) -> dict:
    """Atomic read-modify-write under an exclusive lock.

    *fn* receives the current dict and must return the new dict to write.
    Returns the dict that was written.
    """
    with Flock(lock_path, exclusive=True):
        st = _load_json(json_path)
        result = fn(st)
        _save_json(json_path, result)
        return result


# ── state.json convenience wrappers ──────────────────────────────────

def state_read() -> dict:
    """Read the canonical state.json."""
    return json_read_locked(STATE_JSON, STATE_LOCK)


def state_write(obj: dict) -> None:
    """Overwrite the canonical state.json."""
    json_write_locked(STATE_JSON, STATE_LOCK, obj)


def state_get(key, default=None):
    """Read a single key from state.json."""
    return state_read().get(key, default)


def state_get_with_time(key):
    """Return (value, sys_time) for a key that has a companion _sys_time."""
    st = state_read()
    return st.get(key), st.get(f"{key}_sys_time")


def state_set(key, value):
    """Atomic read-modify-write: set one key."""
    def _update(st):
        st[key] = value
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)
    return value


def state_set_with_time(key, value, sys_time: str | None = None):
    """Atomic read-modify-write: set key + key_sys_time."""
    if sys_time is None:
        sys_time = ts_local_ns()

    def _update(st):
        st[key] = value
        st[f"{key}_sys_time"] = sys_time
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)
    return value, sys_time


def state_update(updates: dict) -> None:
    """Atomic read-modify-write: merge multiple keys at once."""
    def _update(st):
        st.update(updates)
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)


def state_clear_streams_and_files_recent() -> None:
    """Zero out streams (http, net), http_events, and files_recent."""
    now = ts_local_ns()

    def _update(st):
        streams = st.get("streams") or {}
        streams["http"] = []
        streams["net"] = []
        st["streams"] = streams
        st["http_events"] = []
        st["files_recent"] = []
        st["streams_sys_time"] = now
        st["files_recent_sys_time"] = now
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)


def state_inc_file_count(sys_time: str | None = None) -> int:
    """Atomic increment of file_count; returns the **new** value."""
    if sys_time is None:
        sys_time = ts_local_ns()
    result = [0]

    def _update(st):
        cur = int(st.get("file_count", 0) or 0)
        new = cur + 1
        st["file_count"] = new
        st["file_count_sys_time"] = sys_time
        result[0] = new
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)
    return result[0]


# ── snapshots (point-in-time reads) ──────────────────────────────────

def snapshot_stream(state_dict: dict, key: str, limit: int = 5) -> dict:
    """Extract the latest *limit* items from ``streams.<key>``."""
    now = ts_local_ns()
    streams = state_dict.get("streams") or {}
    items = (streams.get(key) or [])[:limit]
    return {"sys_time_freeze": now, "items": items}


def snapshot_tail_list(state_dict: dict, key: str, limit: int = 5) -> dict:
    """Tail the last *limit* entries of a top-level list."""
    now = ts_local_ns()
    items = state_dict.get(key) or []
    tail = items[-limit:] if isinstance(items, list) else []
    return {"sys_time_freeze": now, "items": tail}


def snapshot_last_files() -> dict:
    """Read ``run/last_files.txt`` under a shared lock → structured dict."""
    rows: list[dict] = []
    freeze = ts_local_ns()
    lf_path = RUN / "last_files.txt"
    lf_lock = RUN / "last_files.lock"
    if lf_path.exists():
        try:
            with Flock(lf_lock, exclusive=False):
                with lf_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        ln = ln.rstrip("\r\n")
                        if not ln:
                            continue
                        parts = ln.split("\t")
                        if len(parts) == 3:
                            rows.append({"name": parts[0], "time": parts[1], "sha256": parts[2]})
                        else:
                            rows.append({"raw": ln})
        except Exception:
            pass
    return {"sys_time_freeze": freeze, "rows": rows}


# ── last_hash audit log ──────────────────────────────────────────────

def log_last_hash_change(
    context: str,
    src: str,
    src_path: Path,
    id_hint: str | None,
    before: str,
    after: str,
    *,
    log_fn=None,
) -> None:
    """Append to the last_hash audit log (NDJSON)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": ts_local_ns(),
            "context": context,
            "source": src,
            "id": id_hint,
            "in_path": str(src_path),
            "before": before,
            "after": after,
        }
        with LAST_HASH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if log_fn:
            log_fn(f"[last_hash.log] {entry}")
    except Exception as e:
        if log_fn:
            log_fn(f"[WARN] failed to write last_hash.log: {e}")
