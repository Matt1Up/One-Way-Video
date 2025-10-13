#!/usr/bin/env python3
# get_start_time_json.py — write a burst of timestamps into a JSON array

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import time
import subprocess
from pathlib import Path
import argparse
import sys
import json

# Repo-anchored defaults
from evidence_capture.paths import RUN, ensure_runtime_dirs
ensure_runtime_dirs()

def get_timestamp() -> str:
    """
    Same format as your original (nanoseconds + TZ label), using `date '+%F %T.%N %Z'`.
    Keeping this shell call preserves exact formatting/precision semantics.
    """
    out = subprocess.run(["date", "+%F %T.%N %Z"], capture_output=True, text=True)
    return out.stdout.strip()

def main():
    default_out = RUN / "start_time.json"   # was ~/evidence-capture/run/bundles/start_time.json
    p = argparse.ArgumentParser(description="Write timestamps to a JSON file.")
    p.add_argument(
        "-o", "--save-path",
        default=str(default_out),
        help=f"Output JSON path (default: {default_out})"
    )
    p.add_argument(
        "-n", "--count",
        type=int, default=100,
        help="Number of timestamps to write (default: 100)"
    )
    p.add_argument(
        "-i", "--interval",
        type=float, default=0.01,
        help="Seconds between rows (default: 0.01)"
    )
    args = p.parse_args()

    out_path = Path(args.save_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for _ in range(args.count):
        rows.append(get_timestamp())
        time.sleep(args.interval)

    # Minified JSON for stable hashing (no spaces), newline at end
    out_path.write_text(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )
    print(f"Saved {args.count} timestamps (JSON array) to {out_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
