#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path
import sys
import os

def sha256_of_file(p: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(bufsize), b''):
            h.update(chunk)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser(description="Compute SHA-256 of a file and optionally write/overwrite/rename.")
    ap.add_argument("--in", dest="inp", required=True, help="Path to input file to hash")
    ap.add_argument("--out-path", help="Directory to write <input_name>_hash.txt (contains only the SHA-256)")
    ap.add_argument("--out-overwrite", help="Path to an existing .txt file to overwrite with the SHA-256")
    ap.add_argument("--out-rename", action="store_true",
                    help="Rename the input file to include '__<sha256>' before its extension")
    ap.add_argument("--out", help="Custom output file path to write the SHA-256 into")
    args = ap.parse_args()

    inp_path = Path(args.inp).expanduser().resolve()
    if not inp_path.is_file():
        print(f"Error: input file not found: {inp_path}", file=sys.stderr)
        sys.exit(1)

    digest = sha256_of_file(inp_path)

    # 1) Write to <out-path>/<input_name>_hash.txt
    if args.out_path:
        out_dir = Path(args.out_path).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        hash_txt = out_dir / f"{inp_path.name}_hash.txt"
        hash_txt.write_text(digest + "\n", encoding="utf-8")
        print(f"Wrote SHA-256 to {hash_txt}")

    # 2) Overwrite a specific .txt file
    if args.out_overwrite:
        overwrite_path = Path(args.out_overwrite).expanduser().resolve()
        overwrite_path.parent.mkdir(parents=True, exist_ok=True)
        overwrite_path.write_text(digest + "\n", encoding="utf-8")
        print(f"Overwrote with SHA-256: {overwrite_path}")

    # 3) Arbitrary custom output file
    if args.out:
        out_file = Path(args.out).expanduser().resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(digest + "\n", encoding="utf-8")
        print(f"Wrote SHA-256 to {out_file}")

    # 4) Rename the input file (use .stem to handle multi-suffix names)
    if args.out_rename:
        base = inp_path.stem
        suffix = ''.join(inp_path.suffixes)  # keep multi-suffix like .tar.gz
        target_name = f"{base}__{digest}{suffix}"
        target_path = inp_path.with_name(target_name)

        if target_path.exists():
            print(f"Error: target rename already exists: {target_path}", file=sys.stderr)
            sys.exit(2)

        os.rename(str(inp_path), str(target_path))
        print(f"Renamed input file -> {target_path}")

    # Always print the SHA-256 to stdout
    print(digest)

if __name__ == "__main__":
    main()
