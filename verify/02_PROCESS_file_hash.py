#!/usr/bin/env python3
import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path


def sha256_hex(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_index_list(directory: Path):
    """
    Find all numeric 4-digit prefixes (0001, 0002, ...)
    from files named like 'NNNN.png', 'NNNN.png.ots',
    'NNNN.json', 'NNNN.json.ots', and return a sorted list
    of ints excluding 0 (since 0000.* is special-cased).
    """
    pattern = re.compile(r"^(\d{4})\.(json|png)(?:\.ots)?$")
    indices = set()

    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        m = pattern.match(entry.name)
        if not m:
            continue
        idx = int(m.group(1))
        if idx != 0:  # 0000.* handled separately
            indices.add(idx)

    return sorted(indices)


def find_previous_json_hash(idx: int, json_hash_by_index: dict[int, str]) -> str | None:
    """
    For a given index N (from NNNN.png), walk backwards:
    N-1, N-2, ... 0 and return the first JSON hash found
    in json_hash_by_index. Return None if nothing found.
    """
    candidate = idx - 1
    while candidate >= 0:
        if candidate in json_hash_by_index:
            return json_hash_by_index[candidate]
        candidate -= 1
    return None


def create_bundle_csv(directory: Path, output_csv: Path):
    # 1. Make sure the directory exists
    if not directory.is_dir():
        raise SystemExit(f"Not a directory: {directory}")

    rows = []  # Each element: {"filename": ..., "file_sha256": ..., "display_sha256": ...}

    # Map numeric index -> file_sha256 of that index's JSON file
    # e.g. 0 -> hash of 0000.json, 1 -> hash of 0001.json, etc.
    json_hash_by_index: dict[int, str] = {}

    # 2. Process the first four fixed files in the required order
    fixed_files = [
        "start_time.json",
        "start_time.json.ots",
        "0000.json",
        "0000.json.ots",
    ]

    for name in fixed_files:
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"Required file missing: {path}")
        file_hash = sha256_hex(path)

        row = {
            "filename": name,
            "file_sha256": file_hash,
            "display_sha256": "",
        }
        rows.append(row)

        # If this is 0000.json, register its hash as index 0
        if name == "0000.json":
            json_hash_by_index[0] = file_hash

    # 3. Determine which numeric indices (NNNN) are present (excluding 0000)
    indices = build_index_list(directory)

    # 4. For each index, add the four files in the specified order,
    #    but skip any that are missing (tail-end / dropped files).
    for idx in indices:
        base = f"{idx:04d}"
        block_files = [
            f"{base}.png",
            f"{base}.png.ots",
            f"{base}.json",
            f"{base}.json.ots",
        ]

        for name in block_files:
            path = directory / name
            if not path.is_file():
                # Non-fatal: skip this missing file
                print(
                    f"Warning: expected file missing for index {base}: {path}",
                    file=sys.stderr,
                )
                continue

            file_hash = sha256_hex(path)

            row = {
                "filename": name,
                "file_sha256": file_hash,
                "display_sha256": "",
            }

            # If this is a JSON (not .json.ots), record its hash by index
            if name.endswith(".json") and not name.endswith(".json.ots"):
                json_hash_by_index[idx] = file_hash

            # For each *.png (not *.png.ots), we now:
            #   - Derive its numeric index (idx)
            #   - Find the closest previous JSON index that actually exists
            #   - Use that JSON's hash as display_sha256
            if name.endswith(".png") and not name.endswith(".png.ots"):
                prev_json_hash = find_previous_json_hash(idx, json_hash_by_index)
                if prev_json_hash is None:
                    # In theory this shouldn't happen because 0000.json exists,
                    # but we'll fail loudly if it does.
                    raise SystemExit(
                        f"Could not find any previous JSON hash for PNG {name} (index {idx})"
                    )
                row["display_sha256"] = prev_json_hash

            rows.append(row)

    # 5. Write out the CSV
    fieldnames = ["filename", "file_sha256", "display_sha256"]
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Wrote {len(rows)} rows to {output_csv}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create BUNDLE_file_sha256.csv listing files and SHA-256 hashes in a "
            "specific order, with display_sha256 on PNG rows pointing to the "
            "closest previous JSON hash."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing the files (default: current directory).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="BUNDLE_file_sha256.csv",
        help="Output CSV filename (default: BUNDLE_file_sha256.csv).",
    )

    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    output_csv = Path(args.output).resolve()

    create_bundle_csv(directory, output_csv)


if __name__ == "__main__":
    main()
