#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Embed files from a folder into a PDF as document attachments (Associated Files).
- Works with absolute or relative paths
- Optional recursion, include/exclude globs, size limits
- Stores a helpful description (filename, size, sha256) with each embedded file
- Writes a new PDF next to the original (or to --output)
- Never alters the original PDF

Examples:
  # mimic your original usage (from any directory)
  python3 embed_pdf_files.py -f "./Embed-Files" -p "./EXHIBIT EMG.pdf"

  # recursive with filters, verbose
  python3 embed_pdf_files.py -f ~/evidence-capture/downloads -p ~/Docs/exhibit.pdf \
      --recursive --include "*.txt" --include "*.json" --exclude "*.zip" -v

  # limit large files and write to a specific output path
  python3 embed_pdf_files.py -f ./payloads -p ./exhibit.pdf --max-bytes 10485760 --output ./exhibit_internal.pdf
"""

import argparse
import fnmatch
import hashlib
import sys
from pathlib import Path
from typing import List, Tuple

try:
    import fitz  # PyMuPDF
except Exception:
    sys.exit("PyMuPDF is required. Install with:  pip install --user pymupdf")

# ---------- helpers ----------

def sha256_hex(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def collect_files(root: Path, recursive: bool, includes: List[str], excludes: List[str], max_bytes: int) -> List[Path]:
    if not recursive:
        candidates = [p for p in root.iterdir() if p.is_file()]
    else:
        candidates = [p for p in root.rglob("*") if p.is_file()]

    def match_any(name: str, patterns: List[str]) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in patterns)

    files = []
    for p in candidates:
        name = p.name
        if includes and not match_any(name, includes):
            continue
        if excludes and match_any(name, excludes):
            continue
        if max_bytes > 0:
            try:
                if p.stat().st_size > max_bytes:
                    continue
            except Exception:
                continue
        files.append(p)
    # deterministic order
    files.sort(key=lambda x: str(x).lower())
    return files

def rel_or_base(path: Path, base: Path, use_relative: bool) -> str:
    if use_relative:
        try:
            return str(path.relative_to(base))
        except Exception:
            return path.name
    return path.name

def embfile_add_safe(doc, filename: str, data: bytes, desc: str, af_relationship: str = "Unspecified") -> None:
    """
    Wrap PyMuPDF API differences across versions.
    Newer PyMuPDF supports: doc.embfile_add(filename, data, ufilename=None, desc=None, afrelationship=None)
    """
    try:
        # Try with desc + AF relationship first
        doc.embfile_add(filename=filename, filedata=data, desc=desc, afrelationship=af_relationship)
    except TypeError:
        # Older versions: positional args only
        try:
            doc.embfile_add(filename, data, None, desc)
        except Exception:
            # Last resort: filename + data only
            doc.embfile_add(filename, data)

# ---------- main embedding ----------

def embed_folder_into_pdf(
    folder: Path,
    pdf_path: Path,
    output_path: Path,
    recursive: bool,
    includes: List[str],
    excludes: List[str],
    max_bytes: int,
    relative_names: bool,
    af_relationship: str,
    dry_run: bool,
    verbose: bool,
) -> Tuple[int, int]:
    """
    Returns: (embedded_count, skipped_count)
    """
    if verbose:
        print(f"[i] PDF:     {pdf_path}")
        print(f"[i] Folder:  {folder}")
        print(f"[i] Output:  {output_path}")
        print(f"[i] Mode:    recursive={recursive} relative_names={relative_names}")
        if includes: print(f"[i] Include: {includes}")
        if excludes: print(f"[i] Exclude: {excludes}")
        if max_bytes > 0: print(f"[i] Max bytes: {max_bytes}")

    files = collect_files(folder, recursive, includes, excludes, max_bytes)
    if verbose:
        print(f"[i] Found {len(files)} candidate file(s).")

    embedded, skipped = 0, 0

    # open pdf
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        raise SystemExit(f"❌ Failed to open PDF: {e}")

    try:
        # embed each file
        for p in files:
            # never embed the target PDF itself
            if p.resolve() == pdf_path.resolve():
                if verbose: print(f"[skip] {p.name} (target PDF itself)")
                skipped += 1
                continue

            try:
                data = p.read_bytes()
            except Exception as e:
                print(f"❌ Failed to read {p}: {e}")
                skipped += 1
                continue

            display_name = rel_or_base(p, folder, relative_names)
            try:
                file_hash = sha256_hex(p)
                size_bytes = len(data)
                desc = f"{display_name} | size={size_bytes} bytes | sha256={file_hash}"
            except Exception:
                desc = display_name

            if dry_run:
                print(f"🔗 (dry-run) would embed: {display_name}")
                embedded += 1
                continue

            try:
                embfile_add_safe(doc, filename=display_name, data=data, desc=desc, af_relationship=af_relationship)
                if verbose:
                    print(f"🔗 Embedded: {display_name}")
                embedded += 1
            except Exception as e:
                print(f"❌ Failed to embed {display_name}: {e}")
                skipped += 1

        if dry_run:
            if verbose: print("[i] dry-run: skipping save")
            return embedded, skipped

        # save output
        try:
            # garbage=4 cleans xref; deflate=True compresses streams where sensible
            doc.save(str(output_path), garbage=4, deflate=True)
            print(f"✅ Saved embedded PDF: {output_path.name}")
        except Exception as e:
            raise SystemExit(f"❌ Error saving {output_path.name}: {e}")

    finally:
        try:
            doc.close()
        except Exception:
            pass

    return embedded, skipped

# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Embed files from a folder into a PDF as attachments (Associated Files)."
    )
    p.add_argument("-f", "--folder", required=True,
                   help="Path to folder containing files to embed.")
    p.add_argument("-p", "--pdf", required=True,
                   help="Path to the target PDF to embed into (original is never modified).")
    p.add_argument("-o", "--output", default=None,
                   help="Output PDF path. Default: <pdf_stem>_internal.pdf next to the input PDF.")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Recurse into subfolders.")
    p.add_argument("--include", action="append", default=[],
                   help="Glob of files to include (can repeat). Example: --include '*.txt' --include '*.json'")
    p.add_argument("--exclude", action="append", default=[],
                   help="Glob of files to exclude (can repeat). Example: --exclude '*.zip'")
    p.add_argument("--max-bytes", type=int, default=0,
                   help="Skip files larger than this many bytes (0 = no limit).")
    p.add_argument("--relative-names", action="store_true",
                   help="Store embedded names relative to the folder root (otherwise just the basename).")
    p.add_argument("--af", default="Unspecified",
                   choices=["Unspecified","Source","Data","Alternative","Supplement","EncryptedPayload","FormData"],
                   help="Associated File relationship tag to set (PDF/A-3 usage).")
    p.add_argument("--dry-run", action="store_true",
                   help="Don’t modify anything; just print what would be embedded.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose logging.")
    return p.parse_args()

def main():
    args = parse_args()

    folder = Path(args.folder).expanduser().resolve()
    pdf_file = Path(args.pdf).expanduser().resolve()

    if not folder.is_dir():
        sys.exit(f"❌ Folder not found: {folder}")
    if not pdf_file.is_file():
        sys.exit(f"❌ PDF not found: {pdf_file}")

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        out_parent = output_path.parent
    else:
        out_parent = pdf_file.parent
        output_path = out_parent / f"{pdf_file.stem}_internal.pdf"

    out_parent.mkdir(parents=True, exist_ok=True)

    embedded, skipped = embed_folder_into_pdf(
        folder=folder,
        pdf_path=pdf_file,
        output_path=output_path,
        recursive=bool(args.recursive),
        includes=list(args.include or []),
        excludes=list(args.exclude or []),
        max_bytes=int(args.max_bytes or 0),
        relative_names=bool(args.relative_names),
        af_relationship=args.af,
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
    )

    if args.dry_run:
        print(f"[dry-run] candidates={embedded+skipped} would_embed={embedded} skipped={skipped}")
    else:
        print(f"[done] embedded={embedded} skipped={skipped} -> {output_path}")

if __name__ == "__main__":
    main()
