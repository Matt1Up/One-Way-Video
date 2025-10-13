#!/usr/bin/env python3
# watch_numbered_json_to_array.py
#
# Watch a folder for files named ####.json (0000, 0001, ...),
# and maintain an output JSON ARRAY containing the full contents
# of those files, ordered NEWEST first (top), OLDEST last (bottom).
#
# - Polls every 0.5s by default
# - Clears output to [] when no ####.json files exist
# - Size-stability check (unchanged across two polls)
# - Atomic writes (temp + os.replace)
# - Preserves per-file key order from source JSON

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
# ----------------------------------------------------------------------

import argparse, json, os, re, sys, time
from pathlib import Path
from typing import List, Dict, Tuple
from collections import OrderedDict

# Repo-anchored defaults
from evidence_capture.paths import RUN, ensure_runtime_dirs
ensure_runtime_dirs()
DEFAULT_WATCH_DIR = RUN / "bundles"
DEFAULT_OUT_PATH  = RUN / "web-server" / "combined.json"

NUM_JSON_RE = re.compile(r"^\d{4}\.json$")

# When empty, keep valid JSON structure
EMPTY_PAYLOAD = "[]\n"

def is_numbered_json(name: str) -> bool:
    return bool(NUM_JSON_RE.match(name))

def file_time(p: Path) -> float:
    """Prefer birth time (if available), else modification time."""
    st = p.stat()
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_mtime
    return float(ts)

def directory_has_any_numbered_json(dir_path: Path, out_name: str) -> bool:
    for p in dir_path.iterdir():
        if p.is_dir(): continue
        if p.name == out_name: continue
        if is_numbered_json(p.name): return True
    return False

def gather_numbered_json(dir_path: Path, out_name: str) -> List[Path]:
    out = []
    for p in dir_path.iterdir():
        if p.is_dir(): continue
        if p.name == out_name: continue
        if is_numbered_json(p.name):
            out.append(p)
    return out

def atomic_write_json_array(out_path: Path, items: List[OrderedDict]):
    """
    Write the entire JSON array atomically (pretty printed, newline at EOF).
    Readers will see either the old file or the fully-written new file.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".tmp")

    if not items:
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.write(EMPTY_PAYLOAD)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, out_path)
        return

    payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as tf:
        tf.write(payload)
        tf.flush()
        os.fsync(tf.fileno())
    os.replace(tmp_path, out_path)

def main():
    ap = argparse.ArgumentParser(description="Maintain a JSON array of numbered JSON files (newest first).")
    ap.add_argument("--dir", default=str(DEFAULT_WATCH_DIR),
                    help=f"Directory to watch (default: {DEFAULT_WATCH_DIR})")
    ap.add_argument("--out", default=str(DEFAULT_OUT_PATH),
                    help=f"Output JSON filename or absolute path (default: {DEFAULT_OUT_PATH})")
    ap.add_argument("--interval", type=float, default=0.5, help="Poll interval seconds (default: 0.5)")
    args = ap.parse_args()

    watch_dir = Path(args.dir).expanduser().resolve()
    out_path  = Path(args.out).expanduser().resolve() if os.path.isabs(args.out) else (watch_dir / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Track size stability across polls
    last_sizes: Dict[str, int] = {}
    # Track snapshot to avoid redundant writes (name, mtime_ns, size)
    last_snapshot: Tuple[Tuple[str, int, int], ...] = tuple()

    print(f"[watching] {watch_dir} -> {out_path} (poll {args.interval}s)")
    try:
        while True:
            # If no numbered JSON files exist, clear to []
            if not directory_has_any_numbered_json(watch_dir, out_path.name):
                if last_snapshot != tuple():
                    atomic_write_json_array(out_path, [])
                    last_snapshot = tuple()
                last_sizes.clear()
                time.sleep(args.interval)
                continue

            candidates = gather_numbered_json(watch_dir, out_path.name)

            # Only process size-stable files
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

            # Sort newest -> oldest by file time (tiebreaker by name desc for deterministic order)
            stable.sort(key=lambda pp: (file_time(pp), pp.name), reverse=True)

            # Build array of objects, preserving key order from each source JSON
            items: List[OrderedDict] = []
            snapshot_builder: List[Tuple[str, int, int]] = []

            for p in stable:
                try:
                    st = p.stat()
                    snapshot_builder.append((p.name, int(st.st_mtime_ns), int(st.st_size)))
                    data = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
                    items.append(data)
                except Exception as e:
                    # If a file is momentarily unreadable/invalid, skip this round
                    print(f"[warn] skipping {p.name}: {e}", file=sys.stderr)

            snap = tuple(snapshot_builder)
            if snap != last_snapshot:
                atomic_write_json_array(out_path, items)
                last_snapshot = snap
                print(f"[ok] wrote {len(items)} objects")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[stopped]")

if __name__ == "__main__":
    main()
