#!/usr/bin/env python3
import subprocess
from pathlib import Path
import argparse
import sys

def run_ots(input_file: Path, output_dir: Path):
    input_file = input_file.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # OTS output filename: original name + ".ots"
    output_file = output_dir / f"{input_file.name}.ots"

    # Run ots stamp
    try:
        subprocess.run(
            ["ots", "stamp", str(input_file)],
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error: ots stamp failed ({e})", file=sys.stderr)
        sys.exit(1)

    # By default, ots writes `<file>.ots` next to the input file.
    # Move it into the output directory.
    stamped_file = input_file.with_suffix(input_file.suffix + ".ots")
    if not stamped_file.exists():
        print(f"Error: expected stamped file not found: {stamped_file}", file=sys.stderr)
        sys.exit(1)

    stamped_file.rename(output_file)
    print(f"Stamped file saved to {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Run ots stamp and place .ots file in output directory")
    parser.add_argument("input_file", help="Path to input file")
    parser.add_argument("output_dir", help="Directory where .ots file should be written")
    args = parser.parse_args()

    run_ots(Path(args.input_file), Path(args.output_dir))

if __name__ == "__main__":
    main()
