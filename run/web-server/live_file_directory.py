#!/usr/bin/env python3
# watch_allowed_to_json.py  (portable defaults for your repo)
#
# Watch a folder and maintain a JSON file listing ONLY the allowed files,
# newest first (top). Writes atomically to avoid partial reads.
#
# Allowed names:
#   start_time.json, start_time.json.ots, start_time.json.ots__time-stamp.json
#   ####.png / ####.json (+ optional .ots / .ots__time-stamp.json)
#
# Usage:
#   python3 run/web-server/live_file_directory.py
#   # or with explicit overrides:
#   python3 run/web-server/live_file_directory.py --dir ./run/bundles --out ./run/web-server/live_file_directory.json

# --- portable import bootstrap (find repo root & RUN) ---
import sys as _sys, pathlib as _pathlib
_REPO_ROOT = _pathlib.Path(__file__).resolve().parents[2]   # repo/
_sys.path.insert(0, str(_REPO_ROOT))
from evidence_capture.paths import RUN
# --------------------------------------------------------

import argparse, json, os, re, time
from pathlib import Path
from typing import List, Dict, Tuple

# When empty, write [] (True) or an empty file "" (False)
CLEAR_TO_EMPTY_ARRAY = True

# Allowed exact names
EXACT = {
    "start_time.json",
    "start_time.json.ots",
    "start_time.json.ots__time-stamp.json",
}

# Allowed structured names:
#  ####.png / ####.json (+ optional .ots / .ots__time-stamp.json)
ALLOWED_RE = re.compile(
    r"""
    ^(?P<num>\d{4})\.
    (?P<base>png|json)
    (?:\.ots(?:__time-stamp\.json)?)?
    $
    """,
    re.X,
)

def is_allowed_name(name: str) -> bool:
    if name in EXACT:
        return True
    return bool(ALLOWED_RE.match(name))

def file_time(p: Path) -> float:
    """Prefer birth time if available; else mtime."""
    st = p.stat()
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_mtime
    return float(ts)

def gather_allowed(dir_path: Path, out_name: str) -> List[Path]:
    out_set = []
    for p in dir_path.iterdir():
        if p.is_dir():
            continue
        if p.name == out_name:
            continue
        if is_allowed_name(p.name):
            out_set.append(p)
    return out_set

def atomic_write_json(out_path: Path, items: List[str]):
    """Write the entire JSON array atomically (or empty file if configured)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".tmp")

    if len(items) == 0 and not CLEAR_TO_EMPTY_ARRAY:
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, out_path)
        return

    payload = "[]\n" if len(items) == 0 else json.dumps(items, ensure_ascii=False) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as tf:
        tf.write(payload)
        tf.flush()
        os.fsync(tf.fileno())
    os.replace(tmp_path, out_path)

def main():
    ap = argparse.ArgumentParser(description="Poll a directory and maintain an allowed-file JSON (newest first).")
    ap.add_argument("--dir", default=str(RUN / "bundles"), help="Directory to watch (default: repo/run/bundles)")
    ap.add_argument(
        "--out",
        default=str(RUN / "web-server" / "live_file_directory.json"),
        help="Output JSON filename or absolute path (default: repo/run/web-server/live_file_directory.json)",
    )
    ap.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")
    args = ap.parse_args()

    watch_dir = Path(args.dir).expanduser().resolve()
    out_path  = (Path(args.out).expanduser().resolve()
                 if os.path.isabs(args.out)
                 else (watch_dir / args.out).resolve())
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Track last sizes to ensure file stability across polls (avoid partial writes)
    last_sizes: Dict[str, int] = {}
    # Snapshot to avoid redundant rewrites when nothing changed
    last_snapshot: Tuple[str, ...] = tuple()

    print(f"[watching] {watch_dir} -> {out_path} (poll {args.interval}s)")
    try:
        while True:
            # Collect candidates that match the allow-list
            candidates = gather_allowed(watch_dir, out_path.name)

            # If none: clear output to [] (or empty file) and reset state
            if not candidates:
                if last_snapshot != tuple():  # only rewrite if changed
                    atomic_write_json(out_path, [])
                    last_snapshot = tuple()
                last_sizes.clear()
                time.sleep(args.interval)
                continue

            # Keep only size-stable files (unchanged across two polls)
            stable: List[Path] = []
            for p in candidates:
                try:
                    sz = p.stat().st_size
                except FileNotFoundError:
                    continue
                name = p.name
                if name not in last_sizes:
                    last_sizes[name] = sz
                    continue
                if last_sizes[name] != sz:
                    last_sizes[name] = sz
                    continue
                stable.append(p)

            if not stable:
                time.sleep(args.interval)
                continue

            # Sort by time DESC (newest first). Tiebreak by name DESC keeps a stable order.
            stable.sort(key=lambda p: (file_time(p), p.name), reverse=True)

            # Build array of filenames (strings) relative to the watched dir
            newest_first = [p.name for p in stable]

            snap = tuple(newest_first)
            if snap != last_snapshot:
                atomic_write_json(out_path, newest_first)
                last_snapshot = snap
                print(f"[ok] wrote {len(newest_first)} items")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[stopped]")

if __name__ == "__main__":
    main()
