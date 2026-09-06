#!/usr/bin/env python3
"""
build_http_streams.py — Supplemental converter.

Background
----------
process_timecodes.py converts 05__BUNDLE_http_events.csv into a chunked
JSON stream at 03__data_auto/http_events/. It does NOT convert
04__BUNDLE_http_streams.csv, which contains the lightweight
{bundle_id, event_idx, event_ts, url} record of HTTP requests captured
LIVE during the session.

In the Fraud-Reports recording, the bundle writer mistakenly pulled
streams.http_events from a pre-session capture file, so the http_events
chunks contain ~5 minutes of irrelevant browsing from ~8 hours before
the recording. The streams.http data (CSV 04) is correct and includes
every session HTTP request — including the 6 outbound form-submission
POSTs aligned to their proper bundles.

This script fills the gap by emitting an http_streams chunked stream
mirroring the format of the existing http_events stream. The playback
engine can ingest it without modification.

Future work: fold this into process_timecodes.py via a
build_http_streams() function so it runs automatically.

Run
---
    python3 postprocess/build_http_streams.py --session-dir /path/to/sessions/<name>
    (run after final_conversion.py; defaults to the current directory)
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── paths ───────────────────────────────────────────────────────────────────
_ap = argparse.ArgumentParser(
    description="Supplemental: emit an http_streams chunked stream from 04__BUNDLE_http_streams.csv."
)
_ap.add_argument("--session-dir", default=".",
                 help="Session directory containing post_processing_pipeline/ (default: current directory).")
_ARGS = _ap.parse_args()

PIPELINE_DIR  = Path(_ARGS.session_dir).expanduser().resolve() / "post_processing_pipeline"
SOURCE_CSV    = PIPELINE_DIR / "01__source" / "04__BUNDLE_http_streams.csv"
OUT_DIR       = PIPELINE_DIR / "03__data_auto" / "http_streams"
META_JSON     = PIPELINE_DIR / "03__data_auto" / "meta.json"
BUNDLES_JSON  = PIPELINE_DIR / "03__data_auto" / "bundles.events.json"

CHUNK_ROWS    = 2000
DATASET_NAME  = "http_streams"
PAYLOAD_COLS  = ["event_ts", "bundle_id", "index_raw", "event_idx", "url"]

# ── timestamp helpers ───────────────────────────────────────────────────────
TZ_OFFSETS = {"CDT": -5, "CST": -6, "EDT": -4, "EST": -5,
              "PDT": -7, "PST": -8, "MDT": -6, "MST": -7, "UTC": 0}


def parse_local_timestamp(s: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS.fffffffff CDT' → tz-aware UTC datetime.
    Accepts up to 9-digit fractional seconds (truncates to 6 for stdlib)."""
    body, tz = s.rsplit(" ", 1)
    if "." in body:
        date_part, frac = body.split(".", 1)
        body = f"{date_part}.{frac[:6]}"
    naive = datetime.strptime(body, "%Y-%m-%d %H:%M:%S.%f")
    offset_hours = TZ_OFFSETS.get(tz.upper())
    if offset_hours is None:
        raise ValueError(f"Unknown timezone in '{s}': {tz}")
    return (naive - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)


def parse_iso_utc(s: str) -> datetime:
    """Parse ISO-8601 UTC timestamp (handles Z and +00:00 suffixes)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def session_start_utc() -> datetime:
    """Read bundle 0's prev.last_hash.sys_time event from bundles.events.json."""
    with BUNDLES_JSON.open() as f:
        data = json.load(f)
    for ev in data["events"]:
        if (ev.get("bundle_id") == 0
                and ev.get("sys_time_key") == "prev.last_hash.sys_time"):
            return parse_local_timestamp(ev["sys_time"])
    raise RuntimeError(
        "bundle 0 'prev.last_hash.sys_time' event not found in bundles.events.json"
    )


# ── main pipeline ───────────────────────────────────────────────────────────
def main() -> None:
    start = session_start_utc()
    print(f"[build_http_streams] session t=0: {start.isoformat()}")

    # Read CSV, deduplicate by event_idx (a single HTTP request can appear
    # in multiple consecutive bundles' streams.http snapshots).
    rows: list[dict] = []
    seen: set[str] = set()
    skipped_empty_ts = 0
    skipped_dupe = 0
    with SOURCE_CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            evidx = (r.get("event_idx") or "").strip()
            if not evidx:
                continue
            if evidx in seen:
                skipped_dupe += 1
                continue
            seen.add(evidx)
            ts_str = (r.get("event_ts") or "").strip()
            if not ts_str:
                skipped_empty_ts += 1
                continue
            try:
                t = parse_iso_utc(ts_str)
            except Exception as exc:
                print(f"  [warn] skipping unparseable event_ts {ts_str!r}: {exc}")
                continue
            t_ms = int(round((t - start).total_seconds() * 1000))
            rows.append({
                "t_ms":      t_ms,
                "event_ts":  ts_str,
                "bundle_id": int((r.get("bundle_id") or "0").lstrip("0") or "0"),
                "index_raw": int((r.get("index_raw") or "0")),
                "event_idx": int(evidx),
                "url":       r.get("url") or "",
            })

    print(f"[build_http_streams] CSV: {len(rows)} unique events "
          f"(dedup skipped {skipped_dupe}; empty_ts skipped {skipped_empty_ts})")

    # Stable sort by t_ms (events.json convention).
    rows.sort(key=lambda r: r["t_ms"])

    # Write chunks.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Remove any stale chunk files from a prior run.
    for old in OUT_DIR.glob("chunk_*.json"):
        old.unlink()

    chunks_meta: list[dict] = []
    for ci, start_i in enumerate(range(0, len(rows), CHUNK_ROWS)):
        end_i = min(start_i + CHUNK_ROWS, len(rows))
        slice_ = rows[start_i:end_i]
        t_ms_arr = [r["t_ms"] for r in slice_]
        rows_arr = [[r[c] for c in PAYLOAD_COLS] for r in slice_]

        chunk_file = f"chunk_{ci:04d}.json"
        (OUT_DIR / chunk_file).write_text(json.dumps({
            "dataset": DATASET_NAME,
            "cols":    PAYLOAD_COLS,
            "t_ms":    t_ms_arr,
            "rows":    rows_arr,
        }), encoding="utf-8")

        chunks_meta.append({
            "file":       chunk_file,
            "start_t_ms": t_ms_arr[0] if t_ms_arr else None,
            "end_t_ms":   t_ms_arr[-1] if t_ms_arr else None,
            "n":          len(slice_),
        })

    t_min = rows[0]["t_ms"] if rows else None
    t_max = rows[-1]["t_ms"] if rows else None

    (OUT_DIR / "index.json").write_text(json.dumps({
        "dataset":     DATASET_NAME,
        "cols":        PAYLOAD_COLS,
        "chunk_rows":  CHUNK_ROWS,
        "total_rows":  len(rows),
        "t_ms_min":    t_min,
        "t_ms_max":    t_max,
        "chunks":      chunks_meta,
    }), encoding="utf-8")

    print(f"[build_http_streams] wrote {len(chunks_meta)} chunk file(s) + index → {OUT_DIR}")
    print(f"    total_rows: {len(rows)}")
    if rows:
        print(f"    t_ms range: {t_min} → {t_max} ({(t_max or 0)/60000:.2f} min)")

    # Patch meta.json: add an http_streams entry under outputs.
    with META_JSON.open() as f:
        meta = json.load(f)
    meta.setdefault("outputs", {})[DATASET_NAME] = {
        "type":       "chunked_stream",
        "dir":        DATASET_NAME,
        "index":      f"{DATASET_NAME}/index.json",
        "cols":       PAYLOAD_COLS,
        "chunk_rows": CHUNK_ROWS,
        "total_rows": len(rows),
        "n_chunks":   len(chunks_meta),
        "t_ms_min":   t_min,
        "t_ms_max":   t_max,
    }
    # Also reflect the input file so meta is internally consistent.
    meta.setdefault("inputs", {})[DATASET_NAME] = SOURCE_CSV.name

    META_JSON.write_text(json.dumps(meta), encoding="utf-8")
    print(f"[build_http_streams] patched outputs.{DATASET_NAME} into meta.json")


if __name__ == "__main__":
    main()
