import re
from datetime import datetime, timezone

from evidence_capture import timeutil

# '2025-01-15 14:30:00.123456789 CST' — 9 fractional digits, then a tz abbreviation (may be empty)
LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{9} \S*$")


def test_ts_local_ns_format_and_nanosecond_field():
    s = timeutil.ts_local_ns()
    assert LOCAL_RE.match(s), s
    frac = s.split(".")[1].split(" ")[0]
    assert len(frac) == 9 and frac.isdigit()


def test_ts_local_ns_is_monotonic_non_decreasing():
    a = timeutil.ts_local_ns()
    b = timeutil.ts_local_ns()
    # Same format, lexicographic order == chronological order within one zone
    assert a.rsplit(" ", 1)[0] <= b.rsplit(" ", 1)[0]


def test_now_utc_iso_round_trips_and_is_utc():
    s = timeutil.now_utc_iso()
    assert s.endswith("Z")
    dt = datetime.fromisoformat(s[:-1] + "+00:00")
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 5
