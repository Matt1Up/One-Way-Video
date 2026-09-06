#!/usr/bin/env python3
"""
00_RUN_all_bundle_processing.py

Sequentially run all bundle processing scripts:

    01_PROCESS_combine_json.py
    02_PROCESS_file_hash.py
    03_PROCESS_png_ocr.py
    04_PROCESS_build_bundle_report.py
    05_PROCESS_build_streams_report.py
    06_PROCESS_verify_bundle_timestamps.py

Each script is run with the same Python interpreter, with the *bundles
directory* as its working directory (the stages default to "." for their
inputs and write their reports there). If any script fails (non-zero exit),
the chain stops.

Usage:
    python3 verify/00_RUN_all_bundle_processing.py --bundles-dir /path/to/session/bundles
    # or, from inside a bundles directory:
    python3 /path/to/One-Way-Video/verify/00_RUN_all_bundle_processing.py
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(
        description="Run the six bundle verification stages in order against a bundles directory."
    )
    ap.add_argument(
        "--bundles-dir",
        default=".",
        help=(
            "Directory holding a session's bundle files (start_time.json, NNNN.json, NNNN.png, "
            "*.ots, *.ots__time-stamp.json). Reports are written there. Default: current directory."
        ),
    )
    args = ap.parse_args()
    bundles_dir = Path(args.bundles_dir).expanduser().resolve()
    if not bundles_dir.is_dir():
        sys.exit(f"[ERROR] bundles directory not found: {bundles_dir}")

    # Directory where this script (and the stage scripts) live
    script_dir = Path(__file__).resolve().parent

    # List of scripts to run in order
    scripts = [
        "01_PROCESS_combine_json.py",
        "02_PROCESS_file_hash.py",
        "03_PROCESS_png_ocr.py",
        "04_PROCESS_build_bundle_report.py",
        "05_PROCESS_build_streams_report.py",
        "06_PROCESS_verify_bundle_timestamps.py",
    ]

    print(f"[*] Stage scripts:   {script_dir}")
    print(f"[*] Bundles directory: {bundles_dir}")
    print(f"[*] Using Python:    {sys.executable}\n")

    for idx, script_name in enumerate(scripts, start=1):
        script_path = script_dir / script_name

        if not script_path.is_file():
            print(f"[ERROR] Script not found: {script_path}")
            sys.exit(1)

        print(f"[{idx}/{len(scripts)}] Starting: {script_name}")
        try:
            # Run the script with the same interpreter, inside the bundles directory
            subprocess.run(
                [sys.executable, str(script_path)],
                cwd=bundles_dir,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print()
            print(f"[FATAL] Script failed: {script_name}")
            print(f"        Exit code: {e.returncode}")
            sys.exit(e.returncode)

        print(f"[{idx}/{len(scripts)}] Finished: {script_name}\n")

    print("[OK] All bundle processing scripts completed successfully.")


if __name__ == "__main__":
    main()
