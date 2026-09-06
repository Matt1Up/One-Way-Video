#!/usr/bin/env python3
import json
import argparse
from pathlib import Path

def find_numeric_json_files(directory: Path):
    """
    Return a list of (index, Path) for files named like '0000.json', '0001.json', etc.
    """
    files = []
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".json":
            stem = entry.stem
            if stem.isdigit():
                files.append((int(stem), entry))
    # Sort by numeric value of the stem
    files.sort(key=lambda x: x[0])
    return files

def combine_json_files(directory: Path, output_path: Path):
    files = find_numeric_json_files(directory)

    if not files:
        raise SystemExit(f"No numeric JSON files found in {directory}")

    combined = []

    for index, path in files:
        with path.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Error parsing JSON in {path}: {e}") from e
        combined.append(data)

    # Write combined list as a single, human-readable JSON array
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            combined,
            f,
            ensure_ascii=False,
            indent=2,      # pretty-print with 2-space indentation
            sort_keys=True # keep keys ordered for readability
        )

    print(f"Combined {len(files)} files into {output_path}")

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Combine sequential numeric JSON files "
            "(0000.json, 0001.json, ...) into one pretty-printed JSON array."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan for JSON files (default: current directory).",
    )
    parser.add_argument(
        "-o", "--output",
        default="combined.json",
        help="Output JSON file name (default: combined.json).",
    )

    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    output_path = Path(args.output).resolve()

    if not directory.is_dir():
        raise SystemExit(f"Not a directory: {directory}")

    combine_json_files(directory, output_path)

if __name__ == "__main__":
    main()

