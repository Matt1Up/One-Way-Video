#!/usr/bin/env python3
# combine_json_stripping_keys.py — portable combiner for numbered bundle JSONs

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, sys
from pathlib import Path
from typing import Iterable, List

# Repo-anchored paths
from evidence_capture.paths import RUN

# Default bundle directory (works from anywhere)
DEFAULT_IN_DIR = RUN / "bundles"

# Default “heavy” keys you may want to strip (kept empty by default for back-compat)
DEFAULT_STRIP_KEYS: List[str] = []  # e.g. ["streams", "http_events", "last_files", "downloads"]

def load_and_strip(p: Path, strip_keys: Iterable[str]):
    with p.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    # Strip only top-level keys
    for k in strip_keys:
        if k in obj:
            obj.pop(k, None)
    return obj

def iter_numbered_files(in_dir: Path, start: int, end: int, width: int) -> Iterable[Path]:
    for i in range(start, end + 1):
        yield in_dir / f"{i:0{width}d}.json"

def main():
    ap = argparse.ArgumentParser(
        description="Combine numbered bundle JSON files and (optionally) strip heavy top-level keys."
    )
    ap.add_argument("--in-dir", default=str(DEFAULT_IN_DIR),
                    help=f"Input directory (default: {DEFAULT_IN_DIR})")
    ap.add_argument("--start", type=int, default=1, help="First index (default: 1)")
    ap.add_argument("--end", type=int, default=29, help="Last index (default: 29)")
    ap.add_argument("--width", type=int, default=4,
                    help="Zero-pad width (default: 4 -> 0001.json)")
    ap.add_argument("--out", default="combined.json",
                    help="Output JSON filename (default: combined.json). If relative, goes under --in-dir.")
    ap.add_argument("--indent", type=int, default=2,
                    help="Indent for pretty output (default: 2; use 0 for compact)")
    ap.add_argument("--strip", action="append", default=None,
                    help="Top-level key to strip (repeatable). "
                         "Example: --strip streams --strip http_events")
    ap.add_argument("--from-list", type=str, default=None,
                    help="Optional text file listing JSON filenames (one per line) relative to --in-dir. "
                         "If provided, --start/--end/--width are ignored.")
    args = ap.parse_args()

    in_dir = Path(args.in_dir).expanduser().resolve()
    if not in_dir.exists():
        print(f"[error] input dir does not exist: {in_dir}", file=sys.stderr)
        sys.exit(2)

    # Determine which files to read
    if args.from_list:
        list_path = Path(args.from_list).expanduser()
        if not list_path.is_absolute():
            list_path = in_dir / list_path
        files = []
        try:
            for ln in list_path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                p = (in_dir / ln).resolve()
                files.append(p)
        except Exception as e:
            print(f"[error] failed reading --from-list: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        files = list(iter_numbered_files(in_dir, args.start, args.end, args.width))

    # Figure strip keys (default: none, to match your original script)
    strip_keys = args.strip if args.strip else list(DEFAULT_STRIP_KEYS)

    combined = []
    for path in files:
        if not path.exists():
            print(f"[warn] missing file: {path.name} — skipping", file=sys.stderr)
            continue
        try:
            combined.append(load_and_strip(path, strip_keys))
        except Exception as e:
            print(f"[error] failed {path.name}: {e}", file=sys.stderr)

    # Output path
    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = (in_dir / out_path).resolve()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if args.indent and args.indent > 0:
            json.dump(combined, f, ensure_ascii=False, indent=args.indent)
            f.write("\n")
        else:
            json.dump(combined, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[ok] wrote {out_path} with {len(combined)} records")

if __name__ == "__main__":
    main()
