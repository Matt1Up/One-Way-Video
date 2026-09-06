"""state.py: the cross-process lock and the atomic read-modify-write it protects.

The regression these guard against: before json_update_locked existed, several
capture workers did read -> modify -> write on state.json without a lock and
silently lost each other's updates.
"""
import fcntl
import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

from evidence_capture import state


def _increment_worker(args):
    json_path, lock_path, n = args
    for _ in range(n):
        state.json_update_locked(Path(json_path), Path(lock_path), _bump)
    return n


def _bump(st):
    st["count"] = int(st.get("count", 0)) + 1
    return st


def test_json_update_locked_survives_concurrent_writers(tmp_path):
    j, lock = tmp_path / "state.json", tmp_path / "state.lock"
    procs, per = 6, 40
    ctx = mp.get_context("fork") if sys.platform != "win32" else mp.get_context()
    with ctx.Pool(procs) as pool:
        pool.map(_increment_worker, [(str(j), str(lock), per)] * procs)
    assert json.loads(j.read_text())["count"] == procs * per
    # atomic replace leaves no temp files behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.lock"]


def test_json_update_locked_returns_written_dict_and_starts_from_empty(tmp_path):
    j, lock = tmp_path / "s.json", tmp_path / "s.lock"
    out = state.json_update_locked(j, lock, lambda st: {**st, "a": 1})
    assert out == {"a": 1}
    assert state.json_read_locked(j, lock) == {"a": 1}
    out2 = state.json_update_locked(j, lock, lambda st: {**st, "b": 2})
    assert out2 == {"a": 1, "b": 2}


def test_corrupt_json_reads_as_empty_dict_not_crash(tmp_path):
    j, lock = tmp_path / "s.json", tmp_path / "s.lock"
    j.write_text("{not json")
    assert state.json_read_locked(j, lock) == {}
    # and an update repairs it rather than propagating garbage
    assert state.json_update_locked(j, lock, lambda st: {**st, "ok": True}) == {"ok": True}


def test_flock_is_exclusive_across_processes(tmp_path):
    lock = tmp_path / "x.lock"

    def try_nonblocking(path, q):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            q.put("acquired")
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            q.put("blocked")
        finally:
            os.close(fd)

    ctx = mp.get_context("fork")
    q = ctx.Queue()
    with state.Flock(lock):
        p = ctx.Process(target=try_nonblocking, args=(str(lock), q))
        p.start(); p.join(10)
        assert q.get(timeout=5) == "blocked"
    p = ctx.Process(target=try_nonblocking, args=(str(lock), q))
    p.start(); p.join(10)
    assert q.get(timeout=5) == "acquired"


def test_state_inc_file_count_uses_isolated_home():
    # conftest pointed EVCAP_HOME at a temp dir before import; make sure of it.
    assert str(state.STATE_JSON).startswith(os.environ["EVCAP_HOME"])
    state.state_write({})
    assert state.state_inc_file_count("2026-01-01 00:00:00.000000000 UTC") == 1
    assert state.state_inc_file_count("2026-01-01 00:00:01.000000000 UTC") == 2
    st = state.state_read()
    assert st["file_count"] == 2 and st["file_count_sys_time"].startswith("2026-01-01 00:00:01")


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl")
def test_flock_context_manager_closes_fd(tmp_path):
    lock = tmp_path / "y.lock"
    fl = state.Flock(lock)
    with fl:
        assert fl.fd is not None
    assert fl.fd is None
