"""Timestamp utilities for the evidence-capture pipeline."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def ts_local_ns() -> str:
    """Nanosecond-precision local timestamp: '2025-01-15 14:30:00.123456789 CST'"""
    ns = time.time_ns()
    sec, rem = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(sec, tz=datetime.now().astimezone().tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{rem:09d} {dt.tzname() or ''}"


def now_utc_iso() -> str:
    """UTC ISO-8601 timestamp ending in Z: '2025-01-15T20:30:00.123456Z'"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
