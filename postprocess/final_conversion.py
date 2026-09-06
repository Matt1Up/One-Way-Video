#!/usr/bin/env python3
"""
final_conversion.py — one-shot post-processing pipeline.

Run against a session directory (one that contains bundles/ and dumps/):

    python3 postprocess/final_conversion.py --session-dir /path/to/sessions/<name>
    # or, from inside the session directory:
    python3 /path/to/One-Way-Video/postprocess/final_conversion.py

Working files and final outputs go under
<session>/post_processing_pipeline/{01__source,03__data_auto}/ — the same
layout the playback engine's data/<slug>/ directory is built from.

Stages (each fails loudly if its inputs are missing):
  1. Sanity checks (bundles dir, dump file, start_time.json)
  2. Bundle pipeline — runs the six 0X_PROCESS_*.py scripts with cwd=bundles
  3. HAR conversion — generates the body-bearing HAR.gz from the .dump if it
     doesn't exist yet (uses $EVCAP_MITM_PY, else ~/.venvs/mitm/bin/python,
     so mitmproxy imports)
  4. Form extraction — runs HAR_extract_form_subs.py into 01__source/
  5. Stage 01__source/ — copy + rename bundle outputs, unzip network jsonl,
     copy .dump file
  6. Timecode processing — runs process_timecodes.py, output to 03__data_auto/
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# ───────────────────────────── path resolution ──────────────────────────────
# This file lives in <repo>/postprocess/. The verify stages live in <repo>/verify/.
# The session to process comes from --session-dir (default: current directory).

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "One-shot post-processing for a capture session: run the verify stages, "
            "convert the mitmproxy dump to HAR, extract form submissions, and build the "
            "timecoded JSON streams the playback engine reads."
        )
    )
    ap.add_argument(
        "--session-dir",
        default=".",
        help="Session directory containing bundles/ and dumps/ (default: current directory).",
    )
    return ap.parse_args()


_ARGS = _parse_args()

SCRIPT       = Path(__file__).resolve()
REPO_ROOT    = SCRIPT.parents[1]                       # <repo>/postprocess/ -> <repo>
SESSION_DIR  = Path(_ARGS.session_dir).expanduser().resolve()
SESSION_NAME = SESSION_DIR.name
PIPELINE_DIR = SESSION_DIR / "post_processing_pipeline"  # per-session working dir

BUNDLES_DIR    = SESSION_DIR / "bundles"
DUMPS_DIR      = SESSION_DIR / "dumps"
PROCESSED_DIR  = DUMPS_DIR / "processed"
SOURCE_DIR     = PIPELINE_DIR / "01__source"
OUTPUT_DIR     = PIPELINE_DIR / "03__data_auto"

BUNDLE_SCRIPTS_DIR = REPO_ROOT / "verify"
HAR_EXTRACT_SCRIPT = SCRIPT.parent / "00_MCRO_extract_html_from_har" / "HAR_extract_form_subs.py"
TIMECODE_SCRIPT    = SCRIPT.parent / "process_timecodes.py"

# HAR converter from the capture stage (bin/); it must run under the mitmproxy venv
HAR_CONVERTER      = REPO_ROOT / "bin" / "dump_to_redacted_har_sanitized.py"
MITM_PY_CANDIDATES = [
    Path.home() / ".venvs" / "mitm" / "bin" / "python",
    Path.home() / ".venvs" / "mitm" / "bin" / "python3",
]

# Rename map: bundles/<bundle name>  →  01__source/<final name>
# Files not in this map are not staged into 01__source/.
RENAME_MAP = {
    "combined.json":                   "BUNDLES.json",
    "BUNDLE_report_master.csv":        "00__report_master.csv",
    "BUNDLE_ts_master.csv":            "00__timestamp_master.csv",
    "BUNDLE_report_overview.csv":      "03__BUNDLE_overview.csv",
    "BUNDLE_streams_http.csv":         "04__BUNDLE_http_streams.csv",
    "BUNDLE_streams_http_events.csv":  "05__BUNDLE_http_events.csv",
    "BUNDLE_streams_net.csv":          "06__BUNDLE_net_streams.csv",
    "BUNDLE_ts_ots.csv":               "07__BUNDLE_ots_report.csv",
    "BUNDLE_ts_rt.csv":                "08__BUNDLE_rt_report.csv",
    "BUNDLE_file_sha256.csv":          "BUNDLE_file_sha256.csv",
    "BUNDLE_file_sha256_ocr.csv":      "BUNDLE_file_sha256_ocr.csv",
}

# ─────────────────────────────── helpers ────────────────────────────────────

def banner(stage: str, msg: str = "") -> None:
    bar = "═" * 76
    print(f"\n{bar}\n  {stage}  {msg}\n{bar}")


def step(msg: str) -> None:
    print(f"  • {msg}")


def fail(msg: str) -> None:
    print(f"\n  ✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], cwd: Path) -> None:
    pretty = " ".join(str(x) for x in cmd)
    print(f"  $ (cd {cwd.name}/) {pretty}")
    try:
        subprocess.run(cmd, cwd=str(cwd), check=True)
    except subprocess.CalledProcessError as e:
        fail(f"command failed (exit {e.returncode}): {pretty}")


def find_mitm_python() -> Path:
    override = os.environ.get("EVCAP_MITM_PY")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
    for c in MITM_PY_CANDIDATES:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    fail("mitmproxy venv python not found — set EVCAP_MITM_PY or create ~/.venvs/mitm (see requirements-mitm.txt)")


# ──────────────────────────── stage 1: checks ───────────────────────────────

def stage_sanity() -> Path:
    """Verify the session looks complete. Returns the path to the .dump file."""
    banner("STAGE 1", "sanity checks")

    if not SESSION_DIR.is_dir():
        fail(f"session directory missing: {SESSION_DIR}")
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    if not BUNDLES_DIR.is_dir():
        fail(f"bundles directory missing: {BUNDLES_DIR}")
    bundle_jsons = sorted(p for p in BUNDLES_DIR.glob("[0-9][0-9][0-9][0-9].json"))
    if not bundle_jsons:
        fail(f"no bundle .json files in {BUNDLES_DIR}")
    step(f"found {len(bundle_jsons)} bundle JSONs (first: {bundle_jsons[0].name}, last: {bundle_jsons[-1].name})")

    start_time = BUNDLES_DIR / "start_time.json"
    if not start_time.is_file():
        fail(f"start_time.json missing in bundles: {start_time}")
    step(f"start_time.json: present ({start_time.stat().st_size} bytes)")

    dump_files = sorted(DUMPS_DIR.glob("*.dump"))
    if not dump_files:
        fail(f"no .dump file in {DUMPS_DIR}")
    dump_path = dump_files[0]
    step(f"dump: {dump_path.name} ({dump_path.stat().st_size:,} bytes)")

    for script in (HAR_EXTRACT_SCRIPT, TIMECODE_SCRIPT):
        if not script.is_file():
            fail(f"pipeline script missing: {script}")

    return dump_path


# ───────────────────────── stage 2: bundle pipeline ─────────────────────────

# Size threshold below which an .ots.upgraded file is clearly still pending
# (no Bitcoin attestation appended yet). Anchored receipts run ~1500-1900 bytes;
# pending ones are 600-900 bytes. 1200 is a safe boundary.
OTS_PENDING_SIZE_THRESHOLD = 1200


def _refresh_pending_ots_upgrades() -> None:
    """Re-run `ots upgrade` on any cached .upgraded receipt still in pending state.

    Script 06 (verify_bundle_timestamps) only calls `ots upgrade` the FIRST
    time it sees an .ots receipt — once `ots_upgraded_bundle/<name>.upgraded`
    exists, the script skips the upgrade call on subsequent runs.

    That's fine if the calendar had already anchored the hash on the first run.
    But OpenTimestamps calendars batch-commit to Bitcoin every 1–3 hours, so
    the first run often happens too soon and caches a pending receipt forever.
    This step fixes that: before script 06 runs, find every cached receipt that
    still has no Bitcoin attestation (size < threshold) and re-attempt upgrade.
    """
    ots_dir = BUNDLES_DIR / "ots_upgraded_bundle"
    if not ots_dir.is_dir():
        return  # First run — nothing cached yet; script 06 will populate it.

    pending = [
        p for p in ots_dir.glob("*.upgraded")
        if p.stat().st_size < OTS_PENDING_SIZE_THRESHOLD
    ]
    if not pending:
        step(f"OTS cache: all {sum(1 for _ in ots_dir.glob('*.upgraded'))} receipts already anchored, no refresh needed")
        return

    ots_cli = shutil.which("ots")
    if not ots_cli:
        step(f"OTS cache: {len(pending)} pending receipt(s), but `ots` CLI not on PATH — skipping refresh")
        return

    step(f"OTS cache: {len(pending)} pending receipt(s) — re-attempting upgrade (≈1s each, ~{len(pending)}s total)")
    upgraded_now = 0
    for p in pending:
        try:
            r = subprocess.run([ots_cli, "upgrade", str(p)],
                               capture_output=True, text=True, check=False)
            if p.stat().st_size >= OTS_PENDING_SIZE_THRESHOLD:
                upgraded_now += 1
        except Exception as e:
            print(f"    ⚠ upgrade failed for {p.name}: {e}", file=sys.stderr)
    step(f"OTS cache: {upgraded_now} newly anchored, {len(pending) - upgraded_now} still pending")


def stage_bundles() -> None:
    """Run the six bundle-verify scripts with cwd=BUNDLES_DIR."""
    banner("STAGE 2", "bundle verify + report")

    # Pre-step: refresh any cached but still-pending OTS upgrades so script 06
    # picks up newly-anchored Bitcoin attestations on re-runs.
    _refresh_pending_ots_upgrades()

    scripts = [
        "01_PROCESS_combine_json.py",
        "02_PROCESS_file_hash.py",
        "03_PROCESS_png_ocr.py",
        "04_PROCESS_build_bundle_report.py",
        "05_PROCESS_build_streams_report.py",
        "06_PROCESS_verify_bundle_timestamps.py",
    ]
    for idx, name in enumerate(scripts, start=1):
        path = BUNDLE_SCRIPTS_DIR / name
        if not path.is_file():
            fail(f"bundle script missing: {path}")
        print(f"\n  [{idx}/{len(scripts)}] {name}")
        run([sys.executable, str(path)], cwd=BUNDLES_DIR)


# ──────────────────────── stage 3: HAR conversion ───────────────────────────

def stage_har(dump_path: Path) -> Path:
    """Find or generate the body-bearing HAR.gz. Returns its path."""
    banner("STAGE 3", "HAR conversion")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [
        PROCESSED_DIR / f"{dump_path.stem}-redacted.har.gz",
        PROCESSED_DIR / f"{SESSION_NAME}-redacted.har.gz",
    ]
    for c in candidates:
        if c.is_file():
            step(f"HAR exists: {c.relative_to(SESSION_DIR)}")
            return c

    step("no HAR found — generating from dump (with --include-bodies --no-body-sanitize)")
    if not HAR_CONVERTER.is_file():
        fail(f"HAR converter missing: {HAR_CONVERTER}")
    mitm_py = find_mitm_python()
    run([
        str(mitm_py), str(HAR_CONVERTER),
        "--in",  str(dump_path),
        "--out-dir", str(PROCESSED_DIR),
        "--keep-bodies-hash",
        "--include-bodies",
        "--no-body-sanitize",
    ], cwd=PROCESSED_DIR)

    har = PROCESSED_DIR / f"{dump_path.stem}-redacted.har.gz"
    if not har.is_file():
        fail(f"HAR not produced: expected {har}")
    return har


# ───────────────────────── stage 4: stage 01__source ────────────────────────

def stage_source(dump_path: Path, har_path: Path) -> None:
    """Wipe and refill 01__source/ with this run's artifacts."""
    banner("STAGE 4", "stage 01__source/")

    if SOURCE_DIR.exists():
        backup = PIPELINE_DIR / "01__source.bak"
        if backup.exists():
            shutil.rmtree(backup)
        SOURCE_DIR.rename(backup)
        step(f"existing 01__source/ → 01__source.bak/ (previous contents preserved)")
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    # 4a: bundle outputs (with rename)
    copied = 0
    for src_name, dst_name in RENAME_MAP.items():
        src = BUNDLES_DIR / src_name
        if not src.is_file():
            fail(f"expected bundle output missing: {src}")
        shutil.copy2(src, SOURCE_DIR / dst_name)
        copied += 1
    step(f"bundle outputs copied + renamed: {copied} files")

    # 4b: unzip network_stream.jsonl.zip
    nz = BUNDLES_DIR / "network_stream.jsonl.zip"
    if nz.is_file():
        with zipfile.ZipFile(nz) as zf:
            names = zf.namelist()
            if not names:
                fail(f"empty zip: {nz}")
            # Extract the single jsonl, regardless of internal name
            zf.extract(names[0], path=SOURCE_DIR)
            extracted = SOURCE_DIR / names[0]
            if extracted.name != "network_stream.jsonl":
                extracted.rename(SOURCE_DIR / "network_stream.jsonl")
        step(f"unzipped network_stream.jsonl ({(SOURCE_DIR / 'network_stream.jsonl').stat().st_size:,} bytes)")
    else:
        step("no network_stream.jsonl.zip — skipping (process_timecodes will fall back to CSV)")

    # 4c: copy .dump file into source dir (process_timecodes auto-detects via glob)
    shutil.copy2(dump_path, SOURCE_DIR / dump_path.name)
    step(f"dump copied: {dump_path.name}")

    # 4d: run HAR_extract_form_subs.py — writes 09/10/11 + form_html/ in place
    print()
    run([sys.executable, str(HAR_EXTRACT_SCRIPT),
         "--in",      str(har_path),
         "--out-dir", str(SOURCE_DIR)],
        cwd=PIPELINE_DIR)


# ─────────────────────── stage 5: timecode processing ───────────────────────

def stage_timecode() -> None:
    """Run process_timecodes.py (01__source → 03__data_auto).

    process_timecodes overwrites the JSON outputs and the http_events/ and
    network_stream/ chunk dirs, but does not touch form_html/. Without us
    syncing it here, stale renders from a prior session would linger in
    03__data_auto/form_html/ and the new ones would only live in 01__source/.
    """
    banner("STAGE 5", "process_timecodes.py")
    run([sys.executable, str(TIMECODE_SCRIPT),
         "--source-dir", str(SOURCE_DIR),
         "--output-dir", str(OUTPUT_DIR)],
        cwd=PIPELINE_DIR)

    src_html = SOURCE_DIR / "form_html"
    dst_html = OUTPUT_DIR / "form_html"
    if src_html.is_dir():
        if dst_html.exists():
            shutil.rmtree(dst_html)
        shutil.copytree(src_html, dst_html)
        n = sum(1 for _ in dst_html.iterdir() if _.is_file())
        step(f"form_html/ synced to output ({n} files)")


# ──────────────────────────────── summary ───────────────────────────────────

def summary() -> None:
    banner("DONE", f"final outputs in {OUTPUT_DIR.relative_to(SESSION_DIR)}/")
    if not OUTPUT_DIR.is_dir():
        print("  (output directory not created)")
        return
    for p in sorted(OUTPUT_DIR.iterdir()):
        size = ""
        if p.is_file():
            size = f"  {p.stat().st_size:>10,} bytes"
        elif p.is_dir():
            n = sum(1 for _ in p.rglob("*") if _.is_file())
            size = f"  ({n} files)"
        print(f"  {p.name}{size}")


# ─────────────────────────────────── main ───────────────────────────────────

def main() -> None:
    print(f"final_conversion.py — session: {SESSION_NAME}")
    print(f"session dir:  {SESSION_DIR}")
    print(f"work dir:     {PIPELINE_DIR}")
    print(f"verify stages: {BUNDLE_SCRIPTS_DIR}")
    dump = stage_sanity()
    stage_bundles()
    har = stage_har(dump)
    stage_source(dump, har)
    stage_timecode()
    summary()


if __name__ == "__main__":
    main()
