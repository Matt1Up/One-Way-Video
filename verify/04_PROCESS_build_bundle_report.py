#!/usr/bin/env python3
"""
build_bundle_report.py

Use:
    python3 build_bundle_report.py \
        --csv BUNDLE_file_sha256_ocr.csv \
        --combined combined.json \
        --start start_time.json \
        --fps 30

Defaults assume the three files live in the current directory and
frame rate is 30 fps.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from typing import List, Dict, Any, Tuple


# ---------- Time parsing / timecode helpers ----------

def parse_sys_time(ts: str) -> datetime | None:
    """
    Parse strings like:
        '2025-11-19 02:41:21.588749613 CST'
    into naive datetime (timezone dropped).

    Handles 6+ fractional digits by trimming to microseconds.
    """
    if not ts or not isinstance(ts, str):
        return None

    # Drop final timezone token (e.g. 'CST')
    parts = ts.split()
    if len(parts) >= 2:
        base = " ".join(parts[:-1])
    else:
        base = ts

    # Separate fractional seconds and trim/pad to 6 digits
    if "." in base:
        date_part, frac = base.split(".", 1)
        frac_digits = "".join(ch for ch in frac if ch.isdigit())
        if not frac_digits:
            frac6 = "000000"
        else:
            frac6 = (frac_digits + "000000")[:6]
        base6 = f"{date_part}.{frac6}"
        fmt = "%Y-%m-%d %H:%M:%S.%f"
    else:
        base6 = base
        fmt = "%Y-%m-%d %H:%M:%S"

    try:
        return datetime.strptime(base6, fmt)
    except Exception as e:
        print(f"[WARN] Failed to parse time '{ts}' -> '{base6}': {e}", file=sys.stderr)
        return None


def seconds_to_timecode(seconds: float, fps: float = 30.0) -> str:
    """
    Simple non-drop-frame SMPTE-style timecode HH:MM:SS:FF.
    Frames are computed with the given fps (default 30).
    """
    if seconds is None:
        return ""

    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)

    fps_int = int(round(fps))
    total_frames = int(round(seconds * fps_int))
    frames = total_frames % fps_int
    total_seconds = total_frames // fps_int

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


# ---------- Core report builder ----------

def build_reports(
    combined: List[Dict[str, Any]],
    start_times: List[str],
    bundle_csv_rows: List[Dict[str, str]],
    fps: float = 30.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Build:
      * master_rows  – one row per bundle id
      * download_rows – one row per file in downloads.files_recent
    """

    # Reference zero for timecode: first timestamp in start_time.json
    t0 = parse_sys_time(start_times[0]) if start_times else None

    # Maps from bundle id ('0001', ...) to JSON + hashes
    bundle_by_id: Dict[str, Dict[str, Any]] = {b["id"]: b for b in combined}

    json_hash_by_id: Dict[str, str] = {}
    png_info_by_id: Dict[str, Dict[str, str]] = {}
    start_file_hash = ""

    for row in bundle_csv_rows:
        fname = row["filename"]

        # Capture start_time.json hash for 0000's "start" block
        if fname == "start_time.json":
            start_file_hash = row["file_sha256"]

        m = re.match(r"^(\d{4})\.(json|png)$", fname)
        if not m:
            continue
        bid, ext = m.group(1), m.group(2)

        if ext == "json":
            json_hash_by_id[bid] = row["file_sha256"]
        else:  # png
            png_info_by_id[bid] = {
                "file_sha256": row["file_sha256"],
                "display_sha256": row.get("display_sha256", ""),
                "ocr_sha256": row.get("ocr_sha256", ""),
                "is_match_flag": row.get("is_match_flag", ""),
            }

    all_ids = sorted(
        set(bundle_by_id.keys())
        | set(json_hash_by_id.keys())
        | set(png_info_by_id.keys())
    )

    master_rows: List[Dict[str, Any]] = []
    download_rows: List[Dict[str, Any]] = []

    # For checking which previous JSON a prev.last_hash belongs to
    id_by_json_hash = {h: bid for bid, h in json_hash_by_id.items()}

    for bid in all_ids:
        bundle = bundle_by_id.get(bid)
        png_info = png_info_by_id.get(bid, {})
        json_hash = json_hash_by_id.get(bid, "")

        row: Dict[str, Any] = {
            "bundle_id": bid,
            "index_raw": bundle.get("index_raw", "") if bundle else "",
            "json_present": "True" if bundle is not None else "False",
            "png_present": "True" if png_info else "False",

            # file hashes from BUNDLE_file_sha256_ocr.csv
            "json_file_sha256": json_hash,
            "image_file_sha256": png_info.get("file_sha256", ""),

            # image hash as stored in bundle JSON
            "image_sha256_json": "",
            "image_hash_match": "",

            # chain hash as captured under the PNG overlay
            "display_sha256_csv": png_info.get("display_sha256", ""),
            "ocr_sha256": png_info.get("ocr_sha256", ""),
            "ocr_match_flag": png_info.get("is_match_flag", ""),

            # chain state recorded in JSON
            "prev_last_hash_json": "",
            "prev_last_hash_matches_json_prev": "",
            "prev_last_hash_prev_id": "",
            "prev_last_hash_equals_display_sha256": "",
            "prev_last_img_hash_json": "",
            "prev_last_img_hash_equals_image": "",

            # start_time.json linkage (mainly for bundle 0000)
            "start_time_file_sha256": "",
            "start_sha256_json": "",
            "start_sha256_match": "",

            # timing
            "sys_time_in": "",
            "sys_time_out": "",
            "sys_duration_seconds": "",
            "timecode_in": "",
            "timecode_out": "",
            "timecode_duration": "",

            # downloads summary
            "downloads_count": 0,
            "downloads_names": "",
            "downloads_sha256s": "",
            "downloads_first_sys_time": "",
            "downloads_first_timecode": "",
        }

        if bundle is not None:
            # --- Image hash checks ---
            img_sha_json = bundle.get("image", {}).get("sha256", {}).get("value", "")
            row["image_sha256_json"] = img_sha_json
            if img_sha_json and row["image_file_sha256"]:
                row["image_hash_match"] = (
                    "True" if img_sha_json == row["image_file_sha256"] else "False"
                )

            # --- Chain state ('prev' block) ---
            prev = bundle.get("prev", {})
            prev_last_hash = prev.get("last_hash", {}).get("value", "")
            prev_last_img_hash = prev.get("last_img_hash", {}).get("value", "")

            row["prev_last_hash_json"] = prev_last_hash
            row["prev_last_img_hash_json"] = prev_last_img_hash

            # Which earlier JSON file does prev.last_hash belong to?
            if prev_last_hash:
                prev_id = id_by_json_hash.get(prev_last_hash, "")
                if prev_id:
                    row["prev_last_hash_matches_json_prev"] = "True"
                    row["prev_last_hash_prev_id"] = prev_id
                else:
                    row["prev_last_hash_matches_json_prev"] = "False"

            # prev.last_hash vs live overlay hash shown under PNG
            if prev_last_hash and row["display_sha256_csv"]:
                row["prev_last_hash_equals_display_sha256"] = (
                    "True"
                    if prev_last_hash == row["display_sha256_csv"]
                    else "False"
                )

            # prev.last_img_hash vs actual PNG file hash
            if prev_last_img_hash and row["image_file_sha256"]:
                row["prev_last_img_hash_equals_image"] = (
                    "True"
                    if prev_last_img_hash == row["image_file_sha256"]
                    else "False"
                )

            # --- start_time.json linkage (bundle 0000) ---
            start_sha_json = bundle.get("start", {}).get("sha256", {}).get("value", "")
            row["start_sha256_json"] = start_sha_json
            if start_sha_json or start_file_hash:
                row["start_time_file_sha256"] = start_file_hash or ""
                if start_sha_json and start_file_hash:
                    row["start_sha256_match"] = (
                        "True" if start_sha_json == start_file_hash else "False"
                    )

            # --- sys_time and timecodes ---
            sys_in = bundle.get("sys_time_in", "")
            sys_out = bundle.get("sys_time_out", "")
            row["sys_time_in"] = sys_in
            row["sys_time_out"] = sys_out

            t_in = parse_sys_time(sys_in) if sys_in else None
            t_out = parse_sys_time(sys_out) if sys_out else None

            if t_in and t_out:
                dur = (t_out - t_in).total_seconds()
                row["sys_duration_seconds"] = f"{dur:.6f}"
                row["timecode_duration"] = seconds_to_timecode(dur, fps)

            if t0 and t_in:
                offset_in = (t_in - t0).total_seconds()
                row["timecode_in"] = seconds_to_timecode(offset_in, fps)

            if t0 and t_out:
                offset_out = (t_out - t0).total_seconds()
                row["timecode_out"] = seconds_to_timecode(offset_out, fps)

            # --- Downloads inside this bundle ---
            downloads = bundle.get("downloads", {}).get("files_recent", [])
            row["downloads_count"] = len(downloads)

            if downloads:
                names = [d.get("name", "") for d in downloads]
                shas = [d.get("sha256", "") for d in downloads]
                row["downloads_names"] = " | ".join(names)
                row["downloads_sha256s"] = " | ".join(shas)

                first = downloads[0]
                dt_detect = parse_sys_time(first.get("sys_time_detected", ""))
                if dt_detect:
                    row["downloads_first_sys_time"] = first.get(
                        "sys_time_detected", ""
                    )
                    if t0:
                        offset = (dt_detect - t0).total_seconds()
                        row["downloads_first_timecode"] = seconds_to_timecode(
                            offset, fps
                        )

                # Per-download detailed rows
                for d in downloads:
                    dt_det = parse_sys_time(d.get("sys_time_detected", ""))
                    if t0 and dt_det:
                        offset = (dt_det - t0).total_seconds()
                        tc = seconds_to_timecode(offset, fps)
                    else:
                        tc = ""

                    download_rows.append(
                        {
                            "bundle_id": bid,
                            "index_raw": bundle.get("index_raw", ""),
                            "download_idx": d.get("idx", ""),
                            "download_name": d.get("name", ""),
                            "download_sha256": d.get("sha256", ""),
                            "download_size_bytes": d.get("size_bytes", ""),
                            "download_sys_time_detected": d.get(
                                "sys_time_detected", ""
                            ),
                            "download_timecode": tc,
                            "vault_pdf": d.get("vault_pdf", ""),
                            "vault_sha256": d.get("vault_sha256", ""),
                        }
                    )

        master_rows.append(row)

    return master_rows, download_rows


# ---------- CSV writing helpers ----------

def write_csv(path: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


# ---------- CLI ----------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build hash-chain verification & timing reports from "
            "BUNDLE_file_sha256_ocr.csv, combined.json, and start_time.json."
        )
    )
    parser.add_argument(
        "--csv",
        default="BUNDLE_file_sha256_ocr.csv",
        help="Path to BUNDLE_file_sha256_ocr.csv",
    )
    parser.add_argument(
        "--combined",
        default="combined.json",
        help="Path to combined.json (stack of bundle JSONs)",
    )
    parser.add_argument(
        "--start",
        default="start_time.json",
        help="Path to start_time.json (list of system times)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Frame rate used for timecode calculations (default: 30)",
    )
    parser.add_argument(
        "--prefix",
        default="BUNDLE_report",
        help="Prefix for output CSVs (default: BUNDLE_report)",
    )

    args = parser.parse_args()

    # Load inputs
    with open(args.combined, "r") as f:
        combined = json.load(f)

    with open(args.start, "r") as f:
        start_times = json.load(f)

    bundle_csv_rows: List[Dict[str, str]] = []
    with open(args.csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bundle_csv_rows.append(row)

    # Build reports
    master_rows, download_rows = build_reports(
        combined=combined,
        start_times=start_times,
        bundle_csv_rows=bundle_csv_rows,
        fps=args.fps,
    )

    if not master_rows:
        print("[ERROR] No bundle rows produced – check your inputs.", file=sys.stderr)
        sys.exit(1)

    # Master table – full detail
    master_fields = list(master_rows[0].keys())
    master_path = f"{args.prefix}_master.csv"
    write_csv(master_path, master_rows, master_fields)

    # Overview slice – more compact, good for PDF/print
    overview_fields = [
        "bundle_id",
        "index_raw",
        "json_present",
        "png_present",
        "json_file_sha256",
        "prev_last_hash_json",
        "prev_last_hash_prev_id",
        "prev_last_hash_matches_json_prev",
        "prev_last_hash_equals_display_sha256",
        "image_file_sha256",
        "image_sha256_json",
        "image_hash_match",
        "display_sha256_csv",
        "ocr_match_flag",
        "sys_time_in",
        "sys_time_out",
        "sys_duration_seconds",
        "timecode_in",
        "timecode_out",
        "timecode_duration",
        "downloads_count",
        "downloads_names",
    ]
    # Keep only columns that actually exist
    overview_fields = [c for c in overview_fields if c in master_fields]

    overview_path = f"{args.prefix}_overview.csv"
    write_csv(overview_path, master_rows, overview_fields)

    # Downloads slice – one row per file download
    if download_rows:
        download_fields = list(download_rows[0].keys())
        downloads_path = f"{args.prefix}_downloads.csv"
        write_csv(downloads_path, download_rows, download_fields)
    else:
        downloads_path = None

    print(f"[OK] Wrote master bundle report to     {master_path}")
    print(f"[OK] Wrote overview bundle report to   {overview_path}")
    if downloads_path:
        print(f"[OK] Wrote downloads detail report to {downloads_path}")
    print(f"[INFO] Bundles processed: {len(master_rows)}")
    print(f"[INFO] Downloads found:   {len(download_rows)}")


if __name__ == "__main__":
    main()
