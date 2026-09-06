#!/usr/bin/env python3
"""
build_downloads_from_bundles.py — Supplemental converter.

Background
----------
process_timecodes.py:build_downloads() reads 02__BUNDLE_downloads.csv to
produce downloads.events.json. In the Fraud-Reports session that CSV is
not generated (the form-submission recording emits download info directly
into each bundle's `downloads.files_recent` rather than into a top-level
CSV), so the standard pipeline produces zero download events even though
6 PDFs were actually saved during the session.

This script pulls download data straight from bundles/combined.json
(authoritative source) and emits the standard two-row-per-download
events JSON format that the playback engine expects:
  row 1: detection (downloaded_file set, vault_file null)
  row 2: vault copy (vault_file set, downloaded_file null)

Run after final_conversion.py, from the session directory (or pass it):
    python3 postprocess/build_downloads_from_bundles.py --session-dir /path/to/sessions/<name>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── paths ───────────────────────────────────────────────────────────────────
_ap = argparse.ArgumentParser(
    description="Supplemental: build downloads.events.json from bundles/combined.json."
)
_ap.add_argument("--session-dir", default=".",
                 help="Session directory containing bundles/ and post_processing_pipeline/ (default: current directory).")
_ARGS = _ap.parse_args()

SESSION_DIR  = Path(_ARGS.session_dir).expanduser().resolve()
PIPELINE_DIR = SESSION_DIR / "post_processing_pipeline"
COMBINED     = SESSION_DIR / "bundles" / "combined.json"
OUT_FILE     = PIPELINE_DIR / "03__data_auto" / "downloads.events.json"
META_JSON    = PIPELINE_DIR / "03__data_auto" / "meta.json"

OUT_COLS = ["t_ms", "download_sys_time", "download_index",
            "downloaded_file", "downloaded_file_metadata", "vault_file"]

TZ_OFFSETS = {"CDT": -5, "CST": -6, "EDT": -4, "EST": -5,
              "PDT": -7, "PST": -8, "MDT": -6, "MST": -7, "UTC": 0}


def parse_local(s: str) -> datetime:
    body, tz = s.rsplit(" ", 1)
    if "." in body:
        d, f = body.split(".", 1)
        body = f"{d}.{f[:6]}"
    naive = datetime.strptime(body, "%Y-%m-%d %H:%M:%S.%f")
    return (naive - timedelta(hours=TZ_OFFSETS[tz.upper()])).replace(tzinfo=timezone.utc)


def main() -> None:
    with COMBINED.open() as f:
        bundles = json.load(f)

    start = parse_local(bundles[0]["prev"]["last_hash"]["sys_time"])
    print(f"[downloads] session t=0: {start.isoformat()}")

    events: list[dict] = []
    seen_idx: set[int] = set()

    for b in bundles:
        dls = b.get("downloads", {}) or {}
        files = dls.get("files_recent") or []
        if not files:
            continue
        freeze_str = dls.get("files_recent_freeze_sys") or b.get("sys_time_out")
        for entry in files:
            idx = int(entry["idx"])
            if idx in seen_idx:
                continue
            seen_idx.add(idx)

            name        = entry.get("name") or ""
            vault_pdf   = entry.get("vault_pdf") or ""
            detect_str  = entry.get("sys_time_detected")
            if not (name and vault_pdf and detect_str):
                continue

            detect_t  = parse_local(detect_str)
            detect_ms = int(round((detect_t - start).total_seconds() * 1000))

            vault_t  = parse_local(freeze_str)
            vault_ms = int(round((vault_t - start).total_seconds() * 1000))

            # Row 1: detection
            events.append({
                "t_ms":                      detect_ms,
                "download_sys_time":         detect_str,
                "download_index":            idx,
                "downloaded_file":           name,
                "downloaded_file_metadata":  f"{name}.meta.json",
                "vault_file":                None,
            })
            # Row 2: vault copy
            events.append({
                "t_ms":                      vault_ms,
                "download_sys_time":         freeze_str,
                "download_index":            idx,
                "downloaded_file":           None,
                "downloaded_file_metadata":  None,
                "vault_file":                vault_pdf,
            })

    events.sort(key=lambda e: e["t_ms"])

    out_obj = {"cols": OUT_COLS, "events": events}
    OUT_FILE.write_text(json.dumps(out_obj), encoding="utf-8")
    print(f"[downloads] wrote {len(events)} events ({len(events)//2} downloads) → {OUT_FILE}")
    if events:
        print(f"  t_ms range: {events[0]['t_ms']} → {events[-1]['t_ms']}")
        for e in events:
            tag = "detection" if e["downloaded_file"] else "vault    "
            f = e["downloaded_file"] or e["vault_file"]
            print(f"    t_ms={e['t_ms']:>10}  idx={e['download_index']}  {tag}  {f}")

    # Patch meta.json
    with META_JSON.open() as f:
        meta = json.load(f)
    meta.setdefault("outputs", {})["downloads"] = {
        "type":      "events",
        "file":      "downloads.events.json",
        "rows":      len(events),
        "cols":      OUT_COLS,
        "t_ms_min":  events[0]["t_ms"] if events else None,
        "t_ms_max":  events[-1]["t_ms"] if events else None,
    }
    META_JSON.write_text(json.dumps(meta), encoding="utf-8")
    print("[downloads] patched meta.json outputs.downloads")


if __name__ == "__main__":
    main()
