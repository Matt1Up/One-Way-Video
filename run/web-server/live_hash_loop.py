#!/usr/bin/env python3
# monitor_and_prepend.py
#
# Poll a directory and prepend converted rows to a live text file.
# - 0000.json -> converted with the 0000 logic (index_raw + 1, aligned)
# - 0001+.json -> converted with the 0001+ logic (aligned; drops image.sha rows)
# - 0001+.png -> prepend a single row: ["####.png"]   (ignore 0000.png)
# - When the directory becomes empty, clear the output file
# - Atomic write + rename to avoid partial reads by JS/HTTP readers
#
# Usage (portable defaults now set to your repo):
#   python3 run/web-server/live_hash_loop.py
#   # or with explicit overrides:
#   python3 run/web-server/live_hash_loop.py --dir ./run/bundles --out ./run/web-server/live_hash_loop.txt --interval 0.5

# --- portable import bootstrap (find repo root & RUN) ---
import sys as _sys, pathlib as _pathlib
_REPO_ROOT = _pathlib.Path(__file__).resolve().parents[2]   # repo/
_sys.path.insert(0, str(_REPO_ROOT))
from evidence_capture.paths import RUN
# --------------------------------------------------------

import argparse, json, os, re, sys, time
from pathlib import Path
from typing import Dict, List

NUM_RE = re.compile(r"^(\d{4})\.(json|png)$")

# ---------------------------------------------------------------------------
# Minimal helpers
# ---------------------------------------------------------------------------

def directory_effectively_empty(dir_path: Path, out_path: Path) -> bool:
    """True if the directory has no files except the output file itself."""
    for p in dir_path.iterdir():
        if p.is_dir():
            continue
        if p.name == out_path.name:
            continue
        return False
    return True

def atomic_prepend(out_path: Path, block: str):
    """
    Prepend 'block' to 'out_path' atomically:
      - read prior content (if any)
      - write block+prior to a .tmp file in the same dir
      - fsync and os.replace(.tmp -> live)
    Readers will see either the old file or the complete new file.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".tmp")

    try:
        prior = out_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        prior = ""

    with open(tmp_path, "w", encoding="utf-8") as tf:
        tf.write(block)
        tf.write(prior)
        tf.flush()
        os.fsync(tf.fileno())
    os.replace(tmp_path, out_path)

def atomic_clear(out_path: Path):
    """Atomically truncate the output file to empty."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / (out_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as tf:
        tf.flush()
        os.fsync(tf.fileno())
    os.replace(tmp_path, out_path)

# ---------------------------------------------------------------------------
# Converters (inlined – match your approved outputs/spacing/alignment)
# ---------------------------------------------------------------------------

def _get(d, *path, required=False):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            if required:
                raise KeyError("Missing required key: " + ".".join(path))
            return None
        cur = cur[k]
    return cur

def _find_start_block(obj):
    """Recursively find a dict with start-style keys: path + sha256{value,sys_time}."""
    if isinstance(obj, dict):
        if "path" in obj and "sha256" in obj and isinstance(obj["sha256"], dict):
            sha = obj["sha256"]
            if "value" in sha and "sys_time" in sha:
                return obj
        for v in obj.values():
            found = _find_start_block(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_start_block(item)
            if found is not None:
                return found
    return None

def convert_json_0000_text(src: Dict) -> str:
    """Return text block for 0000.json (index_raw +1), aligned and spaced."""
    rows = []
    def add(id_, label, value, trail): rows.append({"id": id_, "label": label, "val": value, "trail": trail})

    idx_str = str(_get(src, "index_raw", required=True))
    try:
        idx_plus_one = str(int(idx_str) + 1)
    except Exception:
        raise ValueError(f"index_raw is not an integer: {idx_str!r}")

    sys_in  = _get(src, "sys_time_in", required=True)
    sys_out = _get(src, "sys_time_out", required=True)

    start_block = src.get("start")
    if not isinstance(start_block, dict):
        start_block = _find_start_block(src)
    if not isinstance(start_block, dict):
        raise KeyError("Could not locate a 'start' object with path+sha256")
    start_path = start_block.get("path")
    sha        = start_block.get("sha256")
    if not (isinstance(start_path, str) and isinstance(sha, dict)):
        raise KeyError("start.path and/or start.sha256 missing or malformed")
    sha_val  = sha.get("value")
    sha_time = sha.get("sys_time")
    if not (isinstance(sha_val, str) and isinstance(sha_time, str)):
        raise KeyError("start.sha256.value and/or start.sha256.sys_time missing or malformed")

    # Stage 1 baseline
    add("index_raw",   '{"index_raw": ',                      idx_plus_one, ',\n\n')
    add("sys_time_in", '"sys_time_in": ',                     sys_in,       ',\n\n')
    add("start.path",  '"start": { "path": ',                 start_path,   ',\n')
    add("start.sha.value",
                       '          "sha256": { "value": ',     sha_val,      ',\n')
    add("start.sha.sys_time",
                       '                      "sys_time": ',  sha_time,     ' }},\n\n')
    add("sys_time_out",'"sys_time_out": ',                    sys_out,      ' }\n')

    # Stage 2 reorder + align + spacing
    by_id = {r["id"]: r for r in rows}
    order = ["sys_time_out", "start.path", "start.sha.value", "start.sha.sys_time", "index_raw", "sys_time_in"]
    ordered = [by_id[i] for i in order if i in by_id]

    for r in ordered:
        if r["id"] == "index_raw" and not r["label"].startswith('{'):
            r["label"] = '{' + r["label"]
    if ordered and ordered[0]["id"] == "sys_time_out":
        ordered[0]["trail"] = ' }\n\n'

    max_lab = max(len(r["label"]) for r in ordered)
    out_lines = []
    for r in ordered:
        pre = " " * (max_lab - len(r["label"]))
        out_lines.append(f'{pre}{r["label"]}"{r["val"]}"{r["trail"]}')

    text = "".join(out_lines)
    return text.rstrip("\n") + "\n"

def convert_json_after_text(src: Dict) -> str:
    """Return text block for 0001+.json, aligned & spaced; drops image.sha rows."""
    rows = []
    def add(id_, label, value, trail): rows.append({"id": id_, "label": label, "val": value, "trail": trail})

    index_raw   = str(_get(src, "index_raw", required=True))
    sys_in      = _get(src, "sys_time_in", required=True)
    ph_val      = _get(src, "prev", "last_hash", "value", required=True)
    ph_sys      = _get(src, "prev", "last_hash", "sys_time", required=True)
    pimg_val    = _get(src, "prev", "last_img_hash", "value", required=True)
    pimg_sys    = _get(src, "prev", "last_img_hash", "sys_time", required=True)
    pts_val     = _get(src, "prev", "last_timestamp", "value", required=True)
    pts_sys     = _get(src, "prev", "last_timestamp", "sys_time", required=True)
    img_path    = _get(src, "image", "path", required=True)
    img_sha_v   = _get(src, "image", "sha256", "value", required=False)
    img_sha_t   = _get(src, "image", "sha256", "sys_time", required=False)
    sys_out     = _get(src, "sys_time_out", required=True)

    # Stage 1 baseline
    add("index_raw",                '{"index_raw": ',                                   index_raw, ',\n\n')
    add("sys_time_in",              '"sys_time_in": ',                                   sys_in,    ',\n\n')
    add("prev.last_hash.value",     '"prev": { "last_hash": { "value": ',                ph_val,    ',\n')
    add("prev.last_hash.sys_time",  '"sys_time": ',                                      ph_sys,    ' }},\n\n')
    add("prev.last_img_hash.value", '      "last_img_hash": { "value": ',                pimg_val,  ',\n')
    add("prev.last_img_hash.sys_time", '"sys_time": ',                                   pimg_sys,  ' }},\n\n')
    add("prev.last_timestamp.value",'     "last_timestamp": { "value": ',                pts_val,   ',\n')
    add("prev.last_timestamp.sys_time", '"sys_time": ',                                  pts_sys,   ' }},\n\n')
    add("image.path",               '"image": { "path": ',                               img_path,  ',\n')
    if img_sha_v is not None:
        add("image.sha256.value",   '             "sha256": { "value": ',                img_sha_v, ',\n')
    if img_sha_t is not None:
        add("image.sha256.sys_time",'                      "sys_time": ',                img_sha_t, ' }},\n\n')
    if img_sha_v is None or img_sha_t is None:
        rows[-1]["trail"] = ' },\n\n'
    add("sys_time_out",             '"sys_time_out": ',                                  sys_out,   ' }\n')

    # Stage 2: drop sha rows, reorder, align, spacing tweaks
    keep_ids = {
        "index_raw","sys_time_in",
        "prev.last_hash.value","prev.last_hash.sys_time",
        "prev.last_img_hash.value","prev.last_img_hash.sys_time",
        "prev.last_timestamp.value","prev.last_timestamp.sys_time",
        "image.path","sys_time_out",
    }
    rows = [r for r in rows if r["id"] in keep_ids]

    by_id = {r["id"]: r for r in rows}
    order = [
        "sys_time_out",
        "prev.last_timestamp.value",
        "prev.last_timestamp.sys_time",
        "image.path",
        "prev.last_img_hash.value",
        "prev.last_img_hash.sys_time",
        "index_raw",
        "sys_time_in",
        "prev.last_hash.value",
        "prev.last_hash.sys_time",
    ]
    ordered = [by_id[i] for i in order if i in by_id]

    for r in ordered:
        if r["id"] == "index_raw" and not r["label"].startswith('{'):
            r["label"] = '{' + r["label"]
    # collapse the blank line after image.path (single newline to butt up against last_img_hash)
    for r in ordered:
        if r["id"] == "image.path":
            r["trail"] = ' },\n'
    # add blank line after the first row
    if ordered and ordered[0]["id"] == "sys_time_out":
        ordered[0]["trail"] = ' }\n\n'

    max_lab = max(len(r["label"]) for r in ordered)
    out_lines = []
    for r in ordered:
        pre = " " * (max_lab - len(r["label"]))
        out_lines.append(f'{pre}{r["label"]}"{r["val"]}"{r["trail"]}')

    text = "".join(out_lines)
    return text.rstrip("\n") + "\n"

# ---------------------------------------------------------------------------
# Main watcher
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Watch a folder and prepend converted rows to a live text file.")
    ap.add_argument("--dir", default=str(RUN / "bundles"), help="Directory to watch (default: repo/run/bundles)")
    ap.add_argument("--out", default=str(RUN / "web-server" / "live_hash_loop.txt"),
                    help="Output text file path (default: repo/run/web-server/live_hash_loop.txt)")
    ap.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")
    args = ap.parse_args()

    watch_dir = Path(args.dir).expanduser().resolve()
    out_path  = Path(args.out).expanduser().resolve() if os.path.isabs(args.out) else (watch_dir / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    processed: set[str] = set()
    last_sizes: dict[str, int] = {}

    print(f"[watching] {watch_dir} -> {out_path} (poll {args.interval}s)")
    try:
        while True:
            # If directory is empty (except the output), clear and reset
            if directory_effectively_empty(watch_dir, out_path):
                if out_path.exists():
                    atomic_clear(out_path)
                processed.clear()
                last_sizes.clear()
                time.sleep(args.interval)
                continue

            # Gather candidate files
            candidates: List[Path] = []
            for p in watch_dir.iterdir():
                if p.is_dir() or p.name == out_path.name:
                    continue
                m = NUM_RE.match(p.name)
                if not m:
                    continue
                digits, ext = m.group(1), m.group(2)
                if ext == "png" and digits == "0000":
                    continue  # ignore 0000.png
                candidates.append(p)

            # Sort so higher indices get prepended last (and thus end up on top)
            candidates.sort(key=lambda pp: (pp.suffix, int(pp.stem)))

            # Process only size-stable newbies to avoid half-written files
            for p in candidates:
                name = p.name
                if name in processed:
                    continue
                try:
                    sz = p.stat().st_size
                except FileNotFoundError:
                    continue
                if name not in last_sizes:
                    last_sizes[name] = sz
                    continue  # wait one cycle to confirm stability
                if last_sizes[name] != sz:
                    last_sizes[name] = sz
                    continue  # still changing; wait

                # Stable -> process
                digits, ext = NUM_RE.match(name).groups()
                try:
                    if ext == "json":
                        data = json.loads(p.read_text(encoding="utf-8"))
                        block = convert_json_0000_text(data) if digits == "0000" else convert_json_after_text(data)
                        atomic_prepend(out_path, block)
                    else:  # png
                        line = f'["{name}"]\n'
                        atomic_prepend(out_path, line)
                    processed.add(name)
                    print(f"[ok] {name}")
                except Exception as e:
                    print(f"[warn] failed {name}: {e}", file=sys.stderr)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[stopped]")

if __name__ == "__main__":
    main()
