#!/usr/bin/env python3
"""
Automated timecode processing pipeline.

Reads all source data from 01__source/ and produces the final JSON output
matching the structure in 03__data/.

Pipeline:
  1. Load source files
  2. Determine start time (t=0) from BUNDLES.json
  3. Build 5 intermediate CSVs (in memory)
  4. Apply timecode alignment (30fps)
  5. Compile into JSON output files

Usage:
  python3 process_timecodes.py [--source-dir 01__source] [--output-dir 03__data_auto]

Requires: pandas, numpy
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

LOCAL_TZ = "America/Chicago"
FPS = 30

# Columns that step1_timecode_align adds (these get dropped in step2, keeping only t_ms)
TIME_BLOCK_COLS = ["t_ms", "t_ns", "t_s", "frame_30", "tc_30fps",
                   "ts_local_fmt", "ts_iso_local", "ts_iso_utc"]

# step2 rename for bundles columns
BUNDLES_RENAME_MAP = {
    "add_to_timestamp_file_list": "add_to_timestamp_ots",
    "add_to_timestamp_file_list_receipt": "add_to_timestamp_receipt",
}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1a: BUILD INTERMEDIATE CSVs FROM SOURCE
# ═══════════════════════════════════════════════════════════════════════════════

def build_har_data(source_dir: Path) -> pd.DataFrame:
    """
    Combine the 3 HAR source CSVs into a single DataFrame matching the
    final har.events.json column structure.

    Sources: 09__HAR_case_details.csv, 10__HAR_mcro_form.csv, 11__HAR_case_search.csv
    """
    case_details = pd.read_csv(source_dir / "09__HAR_case_details.csv")
    mcro_form = pd.read_csv(source_dir / "10__HAR_mcro_form.csv")
    case_search = pd.read_csv(source_dir / "11__HAR_case_search.csv")

    out_cols = [
        "started_date_time", "entry_index", "method", "url", "http_status",
        "request_form_type", "request_case_search_id", "result_case_id",
        "result_defendant", "html_filename", "json_filename",
        "response_body_raw", "response_json_code", "response_json_message",
    ]
    rows = []

    # 1) MCRO form validation rows
    for _, row in mcro_form.iterrows():
        ei = row.get("entry_index")
        if pd.isna(ei):
            continue
        r = {c: None for c in out_cols}
        r["started_date_time"] = row.get("startedDateTime")
        r["entry_index"] = int(ei)
        r["method"] = row.get("method")
        r["url"] = row.get("url")
        r["http_status"] = _safe_int(row.get("http_status"))
        r["request_form_type"] = _first_non_empty(row, "request_FormType", "FormType")
        r["request_case_search_id"] = _first_non_empty(row, "request_CaseSearchNumber", "CaseSearchNumber")
        r["response_body_raw"] = row.get("response_body_raw")
        r["response_json_code"] = row.get("response_json_code")
        r["response_json_message"] = row.get("response_json_message")
        rows.append(r)

    # 2) Case search results
    for _, row in case_search.iterrows():
        ei = row.get("entry_index")
        if pd.isna(ei):
            continue
        r = {c: None for c in out_cols}
        r["started_date_time"] = row.get("startedDateTime")
        r["entry_index"] = int(ei)
        r["method"] = row.get("method")
        r["url"] = row.get("url")
        r["http_status"] = _safe_int(row.get("http_status"))
        r["request_form_type"] = _first_non_empty(row, "request_FormType", "FormType")
        r["request_case_search_id"] = _first_non_empty(row, "request_CaseSearchNumber", "CaseSearchNumber")
        r["result_case_id"] = row.get("result_case_number")
        r["result_defendant"] = row.get("result_defendant")
        r["html_filename"] = row.get("html_filename")
        rows.append(r)

    # 3) Case details pages
    for _, row in case_details.iterrows():
        ei = row.get("entry_index")
        if pd.isna(ei):
            continue
        r = {c: None for c in out_cols}
        r["started_date_time"] = row.get("startedDateTime")
        r["entry_index"] = int(ei)
        r["method"] = row.get("method")
        r["url"] = row.get("url")
        r["http_status"] = _safe_int(row.get("http_status"))
        r["request_case_search_id"] = row.get("case_number")
        r["html_filename"] = row.get("html_filename")
        # Derive json_filename from html_filename for CaseSearchDetails entries
        html_fn = row.get("html_filename")
        if isinstance(html_fn, str) and "CaseSearchDetails" in html_fn:
            r["json_filename"] = html_fn.replace(".html", ".json")
        rows.append(r)

    # Sort by (entry_index, startedDateTime) to match HAR order
    rows.sort(key=lambda r: (r["entry_index"] or 0, r["started_date_time"] or ""))

    df = pd.DataFrame(rows, columns=out_cols)
    # Convert empty strings from None handling
    df = df.where(pd.notna(df), None)
    return df


def build_downloads(source_dir: Path) -> pd.DataFrame:
    """
    Build downloads event pairs from 02__BUNDLE_downloads.csv and BUNDLES.json.

    Each download produces two rows:
      1. Detection row (downloaded_file set, vault_file null)
      2. Vault copy row (vault_file set, downloaded_file null)

    Note: Vault copy timestamps are derived from BUNDLES.json bundle timing
    since the original .pdf.meta.json files are not in the source directory.

    Returns an empty DataFrame (with the canonical columns) when the session
    has no downloads — i.e. 02__BUNDLE_downloads.csv is missing OR contains
    only a header row. Form-sub sessions typically have no downloads.
    """
    out_cols = ["download_sys_time", "download_index", "downloaded_file",
                "downloaded_file_metadata", "vault_file"]

    dl_csv_path = source_dir / "02__BUNDLE_downloads.csv"
    if not dl_csv_path.exists():
        return pd.DataFrame([], columns=out_cols)

    try:
        dl_csv = pd.read_csv(dl_csv_path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame([], columns=out_cols)

    if len(dl_csv) == 0:
        return pd.DataFrame([], columns=out_cols)

    with open(source_dir / "BUNDLES.json", "r") as f:
        bundles_json = json.load(f)

    # Build bundle lookup by id for freeze timestamps
    bundle_by_id = {}
    for b in bundles_json:
        bid = int(b["id"])
        bundle_by_id[bid] = b

    rows = []

    for _, row in dl_csv.iterrows():
        bundle_id = int(row["bundle_id"])
        dl_idx = int(row["download_idx"])
        dl_name = row["download_name"]
        dl_sys_time = row["download_sys_time_detected"]
        vault_pdf = row["vault_pdf"]

        # Row 1: download detection
        meta_name = f"{dl_name}.meta.json" if isinstance(dl_name, str) else None
        rows.append({
            "download_sys_time": dl_sys_time,
            "download_index": dl_idx,
            "downloaded_file": dl_name,
            "downloaded_file_metadata": meta_name,
            "vault_file": None,
        })

        # Row 2: vault copy
        # Estimate vault timestamp from bundle data:
        # The vault copy is made between detection and the bundle's freeze time.
        # We use the freeze_sys minus ~4 seconds as an approximation, capped
        # to be after detection.
        vault_ts = _estimate_vault_timestamp(bundle_by_id, bundle_id, dl_sys_time)

        rows.append({
            "download_sys_time": vault_ts,
            "download_index": dl_idx,
            "downloaded_file": None,
            "downloaded_file_metadata": None,
            "vault_file": vault_pdf,
        })

    df = pd.DataFrame(rows, columns=out_cols)
    return df


def _estimate_vault_timestamp(bundle_by_id: dict, bundle_id: int, detect_time_str: str) -> str:
    """
    Estimate the vault copy timestamp.

    The vault copy happens between detection and the bundle's freeze_sys.
    Based on observed data, vault copies occur ~5 seconds after detection.
    We estimate: detection_time + (freeze_time - detection_time) * 0.56
    """
    bundle = bundle_by_id.get(bundle_id, {})
    freeze_str = bundle.get("downloads", {}).get("files_recent_freeze_sys", "")

    if not freeze_str or not detect_time_str:
        return detect_time_str  # fallback to detection time

    try:
        detect_ts = _parse_cst_timestamp(detect_time_str)
        freeze_ts = _parse_cst_timestamp(freeze_str)

        if detect_ts is None or freeze_ts is None:
            return detect_time_str

        delta = (freeze_ts - detect_ts).total_seconds()
        vault_offset = delta * 0.56  # ~56% of the way to freeze
        vault_dt = detect_ts + pd.Timedelta(seconds=vault_offset)

        # Format back to CST string with nanosecond precision
        ns = vault_dt.nanosecond
        base = vault_dt.strftime("%Y-%m-%d %H:%M:%S")
        frac = f".{vault_dt.microsecond:06d}{ns:03d}"
        return f"{base}{frac} CST"
    except Exception:
        return detect_time_str


def _parse_cst_timestamp(ts_str: str) -> Optional[pd.Timestamp]:
    """Parse a 'YYYY-MM-DD HH:MM:SS.NNNNNNNNN CST' string to Timestamp."""
    ts_str = ts_str.strip()
    if ts_str.endswith(" CST") or ts_str.endswith(" CDT"):
        ts_str = ts_str.rsplit(" ", 1)[0]
    try:
        return pd.Timestamp(ts_str, tz=LOCAL_TZ)
    except Exception:
        try:
            return pd.Timestamp(ts_str).tz_localize(LOCAL_TZ)
        except Exception:
            return None


def build_network_stream(source_dir: Path) -> pd.DataFrame:
    """
    Build network stream events.

    Priority:
      1. network_stream.jsonl (full capture) — filters and samples like the
         original 05__filter_net_stream_json.py script
      2. 06__BUNDLE_net_streams.csv (per-bundle snapshots, deduplicated)
      3. Empty DataFrame if neither exists (graceful skip)
    """
    jsonl_path = source_dir / "network_stream.jsonl"
    csv_path = source_dir / "06__BUNDLE_net_streams.csv"

    if jsonl_path.exists():
        print("    (using network_stream.jsonl)")
        return _build_network_from_jsonl(jsonl_path)
    elif csv_path.exists():
        print("    (using per-bundle CSV fallback)")
        return _build_network_from_bundle_csv(csv_path)
    else:
        print("    (no network stream source found — skipping)")
        return pd.DataFrame(columns=[
            "event_ts", "event_idx", "event_timecode",
            "seconds_from_start", "proto", "src", "dst", "t"
        ])


def _build_network_from_jsonl(jsonl_path: Path, max_per_second: int = 18) -> pd.DataFrame:
    """
    Reproduce the filtering from 05__filter_net_stream_json.py:
    - Exclude noisy protocols (ARP, SSDP, IGMP, etc.)
    - Prioritize TLS/HTTP traffic
    - Cap at max_per_second events per wall-clock second
    """
    EXCLUDED_PROTOS = {"ARP", "SSDP", "IGMP", "IGMPv3", "MDNS", "MDNS-QU",
                       "TPLINK-SMARTHOME/JSON"}
    INTERESTING_PREFIXES = ("TLS",)
    INTERESTING_EXACT = {"HTTP", "HTTPS"}

    def _make_timecode(secs: float, fps: int = 30) -> str:
        if secs < 0:
            secs = 0.0
        ws = int(secs)
        frac = secs - ws
        frames = int(round(frac * fps))
        if frames >= fps:
            frames = 0
            ws += 1
        h, rem = divmod(ws, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"

    t0 = None
    current_sec = None
    bucket = []
    all_rows = []
    event_idx = 0

    def flush_bucket(bkt):
        interesting = []
        other = []
        for r in bkt:
            proto = r.get("proto", "")
            if proto.startswith(INTERESTING_PREFIXES) or proto in INTERESTING_EXACT:
                interesting.append(r)
            else:
                other.append(r)
        chosen = interesting[:max_per_second]
        remaining = max_per_second - len(chosen)
        if remaining > 0:
            chosen.extend(other[:remaining])
        chosen.sort(key=lambda r: r["t"])
        return chosen

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            proto = ev.get("proto", "")
            if proto in EXCLUDED_PROTOS:
                continue

            t = ev.get("t")
            if t is None:
                continue
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue

            if t0 is None:
                t0 = t

            sfs = t - t0
            sec_bucket = int(math.floor(sfs))

            if current_sec is None:
                current_sec = sec_bucket
            elif sec_bucket != current_sec:
                all_rows.extend(flush_bucket(bucket))
                bucket = []
                current_sec = sec_bucket

            event_idx += 1
            ts_iso = ev.get("ts")
            if not ts_iso:
                ts_iso = datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

            tc = _make_timecode(sfs, fps=30)
            bucket.append({
                "event_idx": event_idx,
                "event_ts": ts_iso,
                "event_timecode": tc,
                "seconds_from_start": sfs,
                "proto": proto,
                "src": ev.get("src", ""),
                "dst": ev.get("dst", ""),
                "t": t,
            })

    if bucket:
        all_rows.extend(flush_bucket(bucket))

    out_cols = ["event_ts", "event_idx", "event_timecode",
                "seconds_from_start", "proto", "src", "dst", "t"]
    if not all_rows:
        return pd.DataFrame(columns=out_cols)

    df = pd.DataFrame(all_rows)
    return df[out_cols].reset_index(drop=True)


def _build_network_from_bundle_csv(csv_path: Path) -> pd.DataFrame:
    """Fallback: deduplicate per-bundle network stream CSV."""
    df = pd.read_csv(csv_path)
    df = df.drop_duplicates(subset=["event_idx"], keep="first")
    df = df.sort_values("event_ts")
    out = df[["event_ts", "event_idx", "event_timecode",
              "seconds_from_start", "proto", "src", "dst", "t"]].copy()
    return out.reset_index(drop=True)


def build_bundles(source_dir: Path) -> pd.DataFrame:
    """
    Build the bundles event table from BUNDLES.json.

    Each bundle produces multiple rows:
    - Bundle 0 (start): prev.last_hash, sys_time_in, start.ots, sys_time_out (4 rows)
    - Bundle 1+: prev.last_hash, sys_time_in, image.sha256, image.ots, sys_time_out (5 rows)

    Two 'add_to_timestamp_file_list' columns are produced (OTS file and receipt).
    """
    with open(source_dir / "BUNDLES.json", "r") as f:
        bundles_json = json.load(f)

    # Use distinct column names; step2 rename map will produce the final names
    out_cols = [
        "sys_time", "bundle_id", "sys_time_key", "path_key", "value_key",
        "add_to_bundle_file_list", "add_to_img_file_list",
        "add_to_timestamp_file_list", "add_to_timestamp_file_list_receipt",
        "last_bundle_hash", "last_img_hash",
    ]

    all_rows = []

    for bundle_data in bundles_json:
        bundle_id = int(bundle_data["id"])
        bstr = f"{bundle_id:04d}"

        # Helper to safely extract nested JSON paths
        def _get(path: str):
            return _extract_key(bundle_data, path)

        # 1) prev.last_hash.sys_time
        prev_hash_val = _get("prev.last_hash.value") or None
        all_rows.append([
            _get("prev.last_hash.sys_time"),  # sys_time
            bundle_id,
            "prev.last_hash.sys_time",  # sys_time_key
            None,  # path_key
            "prev.last_hash.value",  # value_key
            None,  # add_to_bundle_file_list
            None,  # add_to_img_file_list
            None,  # add_to_timestamp_file_list (ots)
            None,  # add_to_timestamp_file_list (receipt)
            prev_hash_val or None,  # last_bundle_hash
            None,  # last_img_hash
        ])

        # 2) sys_time_in
        all_rows.append([
            _get("sys_time_in"),
            bundle_id,
            "sys_time_in",
            None,
            None,
            f"{bstr}.json",
            None,
            None,
            None,
            None,
            None,
        ])

        if bundle_id == 0:
            # Bundle 0 is the start bundle — has start.ots instead of image
            start_ots_sys = _get("start.ots.sys_time")
            if start_ots_sys:
                start_ots_path = os.path.basename(_get("start.ots.path") or "")
                start_ots_rt = os.path.basename(_get("start.ots.rt_json") or "")
                all_rows.append([
                    start_ots_sys,
                    bundle_id,
                    "start.ots.sys_time",
                    "start.ots.path",
                    "start.ots.rt_json",
                    None,
                    None,
                    start_ots_path or None,  # ots
                    start_ots_rt or None,  # receipt
                    None,
                    None,
                ])
        else:
            # 3) image.sha256.sys_time
            img_sha_sys = _get("image.sha256.sys_time")
            img_path = os.path.basename(_get("image.path") or "")
            img_sha_val = _get("image.sha256.value") or None

            if img_sha_sys:
                all_rows.append([
                    img_sha_sys,
                    bundle_id,
                    "image.sha256.sys_time",
                    "image.path",
                    "image.sha256.value",
                    None,
                    f"{bstr}.png",
                    None,
                    None,
                    None,
                    img_sha_val,
                ])

            # 4) image.ots.sys_time
            ots_sys = _get("image.ots.sys_time")
            ots_path = os.path.basename(_get("image.ots.path") or "")
            ots_rt = os.path.basename(_get("image.ots.rt_json") or "")

            if ots_sys:
                all_rows.append([
                    ots_sys,
                    bundle_id,
                    "image.ots.sys_time",
                    "image.ots.path",
                    "image.ots.rt_json",
                    None,
                    None,
                    f"{bstr}.png.ots",  # ots
                    ots_rt or None,  # receipt
                    None,
                    None,
                ])

        # 5) sys_time_out
        all_rows.append([
            _get("sys_time_out"),
            bundle_id,
            "sys_time_out",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ])

    # Build DataFrame with duplicate column names
    df = pd.DataFrame(all_rows, columns=out_cols)
    # Replace empty strings with None
    df = df.replace({"": None})
    return df


def build_http_events(source_dir: Path) -> pd.DataFrame:
    """
    Build HTTP events.

    Priority:
      1. .dump file → convert via mitmproxy (if available) or look for
         pre-converted *events.json alongside it
      2. 05__BUNDLE_http_events.csv (per-bundle snapshots, deduplicated)

    The .dump file is auto-detected as the only *.dump file in source_dir.
    """
    out_cols = [
        "ts", "seq", "flow_id", "client_addr", "client_port",
        "server_addr", "server_port", "sni", "tls_version", "tls_cipher",
        "host", "port", "path", "url", "method", "http_version",
        "status_code", "content_type", "resp_body_len", "req_body_len",
        "req_headers.Host", "req_headers.User-Agent", "req_headers.Accept",
        "req_headers.Accept-Language", "req_headers.Accept-Encoding",
        "req_headers.Content-Type", "resp_headers.content-type",
        "resp_headers.cache-control", "resp_headers.server", "summary",
    ]

    # 1. Try to find and use the .dump file
    dump_files = list(source_dir.glob("*.dump"))
    if dump_files:
        dump_path = dump_files[0]
        # Look for a pre-converted events JSON next to it
        events_json = _find_events_json_for_dump(dump_path, source_dir)
        if events_json:
            print(f"    (using pre-converted events JSON: {events_json.name})")
            return _build_http_from_events_json(events_json, out_cols)

        # Try mitmproxy conversion
        result = _try_convert_dump(dump_path, source_dir)
        if result is not None:
            print(f"    (converted .dump via mitmproxy)")
            return _build_http_from_events_json(result, out_cols)

        print(f"    (found {dump_path.name} but no events JSON and mitmproxy unavailable)")

    # 2. Fall back to per-bundle CSV
    csv_path = source_dir / "05__BUNDLE_http_events.csv"
    if csv_path.exists():
        print("    (using per-bundle CSV fallback)")
        return _build_http_from_bundle_csv(csv_path, out_cols)

    print("    (no HTTP events source found)")
    return pd.DataFrame(columns=out_cols)


def _find_events_json_for_dump(dump_path: Path, source_dir: Path) -> Optional[Path]:
    """Look for a pre-converted events JSON file in the source directory."""
    # Check for common naming patterns
    stem = dump_path.stem
    candidates = [
        source_dir / f"{stem}.events.json",
        source_dir / f"{stem}.json",
        source_dir / "http_events.json",
        source_dir / "5_http_events.json",
    ]
    # Also check for any *events.json that isn't BUNDLES-related
    for p in source_dir.glob("*events.json"):
        if p.name not in ("BUNDLES.json",) and "har" not in p.name.lower():
            candidates.append(p)

    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _try_convert_dump(dump_path: Path, source_dir: Path) -> Optional[Path]:
    """
    Try to convert a .dump file using mitmproxy.
    Returns path to the generated events JSON, or None if mitmproxy unavailable.
    """
    try:
        from mitmproxy.io import FlowReader  # noqa: F401
    except ImportError:
        return None

    # If mitmproxy is available, do the conversion inline
    try:
        out_path = source_dir / f"{dump_path.stem}.events.json"
        events = _convert_dump_to_events(dump_path)
        with open(out_path, "w") as f:
            json.dump(events, f)
        return out_path
    except Exception as e:
        print(f"    (mitmproxy conversion failed: {e})")
        return None


def _convert_dump_to_events(dump_path: Path) -> list:
    """Convert a mitmproxy .dump file to events list."""
    from mitmproxy.io import FlowReader
    from mitmproxy.http import HTTPFlow

    events = []
    seq = 0
    with open(dump_path, "rb") as f:
        reader = FlowReader(f)
        for flow in reader.stream():
            if not isinstance(flow, HTTPFlow):
                continue
            if not flow.request:
                continue
            seq += 1
            req = flow.request
            resp = flow.response

            ts = req.timestamp_start
            ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

            evt = {
                "seq": seq,
                "flow_id": flow.id,
                "ts": ts_iso,
                "client_addr": flow.client_conn.peername[0] if flow.client_conn and flow.client_conn.peername else None,
                "client_port": flow.client_conn.peername[1] if flow.client_conn and flow.client_conn.peername else None,
                "server_addr": flow.server_conn.peername[0] if flow.server_conn and flow.server_conn.peername else None,
                "server_port": flow.server_conn.peername[1] if flow.server_conn and flow.server_conn.peername else None,
                "sni": flow.server_conn.sni if flow.server_conn else None,
                "tls_version": None,
                "tls_cipher": None,
                "host": req.host,
                "port": req.port,
                "path": req.path,
                "url": req.url,
                "method": req.method,
                "http_version": req.http_version,
                "status_code": resp.status_code if resp else None,
                "content_type": resp.headers.get("content-type") if resp else None,
                "resp_body_len": len(resp.content) if resp and resp.content else None,
                "req_body_len": len(req.content) if req.content else None,
                "req_headers": dict(req.headers) if req.headers else {},
                "resp_headers": dict(resp.headers) if resp and resp.headers else {},
                "summary": None,
            }
            events.append(evt)

    return events


def _build_http_from_events_json(json_path: Path, out_cols: list) -> pd.DataFrame:
    """Build HTTP events DataFrame from a pre-converted events JSON file."""
    with open(json_path, "r") as f:
        events = json.load(f)

    rows = []
    for evt in events:
        if not isinstance(evt, dict):
            continue
        req_hdrs = evt.get("req_headers", {}) or {}
        resp_hdrs = evt.get("resp_headers", {}) or {}

        # Normalize header key lookup (case-insensitive)
        def _hdr(d, key):
            if not isinstance(d, dict):
                return None
            # Try exact match first, then case-insensitive
            if key in d:
                return d[key]
            for k, v in d.items():
                if k.lower() == key.lower():
                    return v
            return None

        r = {
            "ts": evt.get("ts"),
            "seq": evt.get("seq"),
            "flow_id": evt.get("flow_id"),
            "client_addr": evt.get("client_addr"),
            "client_port": evt.get("client_port"),
            "server_addr": evt.get("server_addr"),
            "server_port": evt.get("server_port"),
            "sni": evt.get("sni"),
            "tls_version": evt.get("tls_version"),
            "tls_cipher": evt.get("tls_cipher"),
            "host": evt.get("host"),
            "port": evt.get("port"),
            "path": evt.get("path"),
            "url": evt.get("url"),
            "method": evt.get("method"),
            "http_version": evt.get("http_version"),
            "status_code": evt.get("status_code"),
            "content_type": evt.get("content_type"),
            "resp_body_len": evt.get("resp_body_len"),
            "req_body_len": evt.get("req_body_len"),
            "req_headers.Host": _hdr(req_hdrs, "Host"),
            "req_headers.User-Agent": _hdr(req_hdrs, "User-Agent") or _hdr(req_hdrs, "user-agent"),
            "req_headers.Accept": _hdr(req_hdrs, "Accept") or _hdr(req_hdrs, "accept"),
            "req_headers.Accept-Language": _hdr(req_hdrs, "Accept-Language") or _hdr(req_hdrs, "accept-language"),
            "req_headers.Accept-Encoding": _hdr(req_hdrs, "Accept-Encoding") or _hdr(req_hdrs, "accept-encoding"),
            "req_headers.Content-Type": _hdr(req_hdrs, "Content-Type") or _hdr(req_hdrs, "content-type"),
            "resp_headers.content-type": _hdr(resp_hdrs, "content-type"),
            "resp_headers.cache-control": _hdr(resp_hdrs, "cache-control"),
            "resp_headers.server": _hdr(resp_hdrs, "server"),
            "summary": evt.get("summary"),
        }
        rows.append(r)

    return pd.DataFrame(rows, columns=out_cols)


def _build_http_from_bundle_csv(csv_path: Path, out_cols: list) -> pd.DataFrame:
    """Fallback: build HTTP events from per-bundle CSV, deduplicated."""
    df = pd.read_csv(csv_path)
    df = df.drop_duplicates(subset=["event_idx"], keep="first")
    df = df.sort_values("event_startedDateTime")
    df = df.reset_index(drop=True)

    rows = []
    for seq_num, (_, row) in enumerate(df.iterrows(), start=1):
        url_str = str(row.get("req_url", "") or "")
        parsed = urlparse(url_str) if url_str else None
        sni = row.get("sni") or ""
        method = row.get("req_method") or ""
        status = _safe_int_or_none(row.get("resp_status"))
        host = sni if sni else (parsed.hostname if parsed else None)
        port = _safe_int_or_none(row.get("server_port"))
        path_str = parsed.path if parsed else None

        tls = row.get("tls_version") or ""
        parts = []
        if method:
            parts.append(method)
        if host and path_str:
            parts.append(f"{host}{path_str}")
        if status:
            parts.append(f"\u2192 {status}")
        if tls:
            parts.append(f"[{tls}]")
        summary = " ".join(parts) if parts else None

        r = {c: None for c in out_cols}
        r.update({
            "ts": row.get("event_startedDateTime"),
            "seq": seq_num,
            "flow_id": row.get("flow_id"),
            "client_addr": row.get("client_ip"),
            "client_port": _safe_int_or_none(row.get("client_port")),
            "server_addr": row.get("server_ip"),
            "server_port": port,
            "sni": sni or None,
            "tls_version": tls or None,
            "tls_cipher": row.get("tls_cipher") or None,
            "host": host, "port": port, "path": path_str,
            "url": url_str or None,
            "method": method or None,
            "http_version": row.get("req_http_version") or None,
            "status_code": status,
            "req_headers.Host": host,
            "summary": summary,
        })
        rows.append(r)

    return pd.DataFrame(rows, columns=out_cols)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1b: TIMECODE ALIGNMENT (adapted from step1_timecode_align_v2.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_series_to_local(ts_series: pd.Series, local_tz: str, errors: str) -> pd.Series:
    """Parse mixed timestamp formats into tz-aware local datetime series."""
    s = ts_series.astype(str).str.strip()
    s = s.replace({"": np.nan, "None": np.nan, "nan": np.nan})

    out = pd.Series(pd.NaT, index=s.index, dtype=f"datetime64[ns, {local_tz}]")

    # 1) Local with trailing TZ abbrev: "... CST" or "... CDT"
    mask_abbrev = s.notna() & s.str.contains(r"\s(?:CST|CDT)$", regex=True)
    if mask_abbrev.any():
        base = s[mask_abbrev].str.replace(r"\s(?:CST|CDT)$", "", regex=True)
        parsed = pd.to_datetime(base, errors=errors)
        parsed = parsed.dt.tz_localize(local_tz, ambiguous="infer", nonexistent="shift_forward")
        out.loc[mask_abbrev] = parsed

    # 2) Explicit offset or Z
    mask_tzinfo = s.notna() & s.str.contains(r"(?:Z$|[+-]\d{2}:\d{2}$|[+-]\d{4}$)", regex=True)
    mask_utc_like = (~mask_abbrev) & mask_tzinfo
    if mask_utc_like.any():
        parsed_utc = pd.to_datetime(s[mask_utc_like], errors=errors, utc=True)
        out.loc[mask_utc_like] = parsed_utc.dt.tz_convert(local_tz)

    # 3) Local naive
    mask_local_naive = s.notna() & (~mask_abbrev) & (~mask_tzinfo)
    if mask_local_naive.any():
        parsed = pd.to_datetime(s[mask_local_naive], errors=errors)
        parsed = parsed.dt.tz_localize(local_tz, ambiguous="infer", nonexistent="shift_forward")
        out.loc[mask_local_naive] = parsed

    return out


def _frame_to_tc(frame, fps: int) -> str:
    if frame is None or (isinstance(frame, float) and math.isnan(frame)):
        return ""
    try:
        f = int(frame)
    except Exception:
        return ""
    sign = "-" if f < 0 else ""
    f = abs(f)
    total_sec, ff = divmod(f, fps)
    hh, rem = divmod(total_sec, 3600)
    mm, ss = divmod(rem, 60)
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def add_timecode_columns(df: pd.DataFrame, start_ts_local: pd.Timestamp) -> pd.DataFrame:
    """Add timecode alignment columns to a DataFrame. First column must be timestamp."""
    if df.shape[1] < 1:
        raise ValueError("DataFrame has no columns.")

    orig_ts_col = df.columns[0]
    ts_local = _parse_series_to_local(df[orig_ts_col], local_tz=LOCAL_TZ, errors="coerce")

    start_value = int(start_ts_local.value)
    ts_int = ts_local.astype("int64")
    is_valid = ts_local.notna()

    elapsed_ns = (ts_int - start_value).astype("Int64")
    elapsed_ns = elapsed_ns.where(is_valid, pd.NA)
    elapsed_ms = (elapsed_ns // 1_000_000).astype("Int64")

    elapsed_s = pd.Series(np.nan, index=df.index, dtype="float64")
    elapsed_s.loc[is_valid] = (elapsed_ns.loc[is_valid].astype("int64") / 1_000_000_000.0)

    frames = (elapsed_ns * FPS // 1_000_000_000).astype("Int64")
    frames = frames.where(is_valid, pd.NA)

    tc = frames.apply(lambda x: _frame_to_tc(x, FPS))

    # Format strings for metadata
    mask = ts_local.notna()
    ts_local_fmt = pd.Series([""] * len(ts_local), index=ts_local.index, dtype="string")
    ts_iso_local = pd.Series([""] * len(ts_local), index=ts_local.index, dtype="string")
    ts_iso_utc = pd.Series([""] * len(ts_local), index=ts_local.index, dtype="string")

    if mask.any():
        base = ts_local[mask].dt.strftime("%Y-%m-%d %H:%M:%S")
        tz_abbr = ts_local[mask].dt.strftime("%Z")
        ints = ts_local[mask].astype("int64")
        ns = (ints % 1_000_000_000).astype(np.int64)
        ns_str = pd.Series(ns, index=base.index).astype(str).str.zfill(9)
        ts_local_fmt.loc[mask] = (base + "." + ns_str + " " + tz_abbr).astype("string")

        base2 = ts_local[mask].dt.strftime("%Y-%m-%dT%H:%M:%S")
        off = ts_local[mask].dt.strftime("%z")
        off = off.str.slice(0, 3) + ":" + off.str.slice(3, 5)
        ts_iso_local.loc[mask] = (base2 + "." + ns_str + off).astype("string")

        ts_utc = ts_local[mask].dt.tz_convert("UTC")
        base3 = ts_utc.dt.strftime("%Y-%m-%dT%H:%M:%S")
        ints_utc = ts_utc.astype("int64")
        ns_utc = (ints_utc % 1_000_000_000).astype(np.int64)
        ns_str_utc = pd.Series(ns_utc, index=base3.index).astype(str).str.zfill(9)
        ts_iso_utc.loc[mask] = (base3 + "." + ns_str_utc + "Z").astype("string")

    new_cols = pd.DataFrame({
        "t_ms": elapsed_ms,
        "t_ns": elapsed_ns,
        "t_s": elapsed_s,
        f"frame_{FPS}": frames,
        f"tc_{FPS}fps": tc.astype("string"),
        "ts_local_fmt": ts_local_fmt,
        "ts_iso_local": ts_iso_local,
        "ts_iso_utc": ts_iso_utc,
    }, index=df.index)

    rest_cols = [c for c in df.columns if c != orig_ts_col]
    out = pd.concat([new_cols, df[rest_cols], df[[orig_ts_col]]], axis=1)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: COMPILE TO JSON (adapted from step2_compile_sources_v2.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _drop_time_block_keep_tms(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop timecode columns except t_ms, ensure t_ms is first.
    Also move the original timestamp column (last column) to position 2 (after t_ms).
    """
    if "t_ms" not in df.columns:
        raise ValueError("Expected column 't_ms'.")
    drop = [c for c in TIME_BLOCK_COLS if c in df.columns and c != "t_ms"]
    out = df.drop(columns=drop, errors="ignore")
    col_list = list(out.columns)

    # The original timestamp column was moved to last by add_timecode_columns
    # Move it back to position 2 (right after t_ms)
    if len(col_list) >= 2:
        orig_ts_col = col_list[-1]
        rest = [c for c in col_list if c != "t_ms" and c != orig_ts_col]
        col_list = ["t_ms", orig_ts_col] + rest

    return out[col_list]


def _stable_sort_by_tms(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values("t_ms", kind="mergesort", ascending=True,
                          na_position="last").reset_index(drop=True)


def _coerce_nans_to_none(df: pd.DataFrame) -> pd.DataFrame:
    return df.where(pd.notna(df), None)


class _NumpyEncoder(json.JSONEncoder):
    """Handle numpy/pandas types during JSON serialization."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            if np.isnan(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, pd.Timestamp):
            return str(obj)
        try:
            if pd.isna(obj):
                return None
        except (ValueError, TypeError):
            pass
        return super().default(obj)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), cls=_NumpyEncoder)


def compile_events(df: pd.DataFrame, out_path: Path,
                   rename_map: Optional[dict] = None) -> dict:
    """Compile a timecoded DataFrame into an events JSON file."""
    df = _drop_time_block_keep_tms(df)
    if rename_map:
        df = df.rename(columns=rename_map)

    df = _stable_sort_by_tms(df)
    df = _coerce_nans_to_none(df)

    final_cols = list(df.columns)

    events = []
    arr = df.values
    for i in range(len(arr)):
        row_dict = {}
        for j, c in enumerate(final_cols):
            val = arr[i, j]
            if val is None:
                pass
            elif isinstance(val, (np.integer,)):
                val = int(val)
            elif isinstance(val, (np.floating,)):
                val = None if np.isnan(val) else float(val)
            elif isinstance(val, float) and math.isnan(val):
                val = None
            row_dict[c] = val
        events.append(row_dict)

    t_ms_list = [e["t_ms"] for e in events if e.get("t_ms") is not None]
    t_min = int(min(t_ms_list)) if t_ms_list else None
    t_max = int(max(t_ms_list)) if t_ms_list else None

    _write_json(out_path, {"cols": final_cols, "events": events})

    return {
        "type": "events",
        "file": str(out_path.name),
        "rows": len(events),
        "cols": final_cols,
        "t_ms_min": t_min,
        "t_ms_max": t_max,
    }


def compile_chunked(df: pd.DataFrame, out_dir: Path, *,
                    chunk_rows: int, dataset_name: str,
                    rename_map: Optional[dict] = None) -> dict:
    """Compile a timecoded DataFrame into chunked JSON files."""
    df = _drop_time_block_keep_tms(df)
    if rename_map:
        actual_map = {k: v for k, v in rename_map.items() if k in df.columns}
        df = df.rename(columns=actual_map)

    df = _stable_sort_by_tms(df)
    df = _coerce_nans_to_none(df)

    payload_cols = [c for c in df.columns if c != "t_ms"]
    total_rows = len(df)
    t_min = int(df["t_ms"].min()) if total_rows and df["t_ms"].notna().any() else None
    t_max = int(df["t_ms"].max()) if total_rows and df["t_ms"].notna().any() else None

    out_dir.mkdir(parents=True, exist_ok=True)

    t_values = df["t_ms"].astype("int64", errors="ignore").tolist()
    rows_matrix = df[payload_cols].values.tolist()

    chunks_meta = []
    chunk_idx = 0

    for start in range(0, total_rows, chunk_rows):
        end = min(start + chunk_rows, total_rows)
        chunk_t = t_values[start:end]
        chunk_rows_data = rows_matrix[start:end]

        chunk_file = f"chunk_{chunk_idx:04d}.json"
        chunk_path = out_dir / chunk_file

        _write_json(chunk_path, {
            "dataset": dataset_name,
            "cols": payload_cols,
            "t_ms": chunk_t,
            "rows": chunk_rows_data,
        })

        chunks_meta.append({
            "file": chunk_file,
            "start_t_ms": int(chunk_t[0]) if chunk_t else None,
            "end_t_ms": int(chunk_t[-1]) if chunk_t else None,
            "n": end - start,
        })
        chunk_idx += 1

    index_obj = {
        "dataset": dataset_name,
        "cols": payload_cols,
        "chunk_rows": chunk_rows,
        "total_rows": total_rows,
        "t_ms_min": t_min,
        "t_ms_max": t_max,
        "chunks": chunks_meta,
    }
    _write_json(out_dir / "index.json", index_obj)

    return {
        "type": "chunked_stream",
        "dir": dataset_name,
        "index": f"{dataset_name}/index.json",
        "cols": payload_cols,
        "chunk_rows": chunk_rows,
        "total_rows": total_rows,
        "n_chunks": len(chunks_meta),
        "t_ms_min": t_min,
        "t_ms_max": t_max,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_key(data: dict, dotted_path: str):
    """Follow a dotted path into nested dicts. Returns '' if missing."""
    cur = data
    for part in dotted_path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, "")
        else:
            return ""
    return cur if cur != "" else ""


def _safe_int(val) -> Optional[int]:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except Exception:
            return None


def _safe_int_or_none(val):
    result = _safe_int(val)
    return result


def _first_non_empty(row, *keys):
    for k in keys:
        val = row.get(k)
        if val is not None and not (isinstance(val, float) and math.isnan(val)) and str(val).strip():
            return val
    return None


def get_start_time(source_dir: Path) -> pd.Timestamp:
    """
    Determine the master start time (t=0) from BUNDLES.json.
    This is the earliest timestamp: bundle 0000's prev.last_hash.sys_time.
    """
    with open(source_dir / "BUNDLES.json", "r") as f:
        bundles = json.load(f)

    bundle_0 = bundles[0]
    start_str = _extract_key(bundle_0, "prev.last_hash.sys_time")

    if not start_str:
        raise ValueError("Cannot determine start time from BUNDLES.json bundle 0")

    # Parse the CST timestamp
    ts = _parse_cst_timestamp(start_str)
    if ts is None:
        raise ValueError(f"Cannot parse start time: {start_str}")

    return ts


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_pipeline(source_dir: Path, output_dir: Path, *, skip_network: bool = False) -> None:
    print(f"[pipeline] Source:  {source_dir}")
    print(f"[pipeline] Output:  {output_dir}")
    print()

    # --- 1. Determine start time ---
    start_ts = get_start_time(source_dir)
    print(f"[pipeline] Start time (t=0): {start_ts}")
    print()

    # --- 2. Build intermediate DataFrames ---
    print("[step1] Building intermediate DataFrames from source...")

    print("  [har] Combining HAR CSVs...")
    har_df = build_har_data(source_dir)
    print(f"  [har] {len(har_df)} rows")

    print("  [downloads] Building download event pairs...")
    downloads_df = build_downloads(source_dir)
    print(f"  [downloads] {len(downloads_df)} rows")

    network_df = None
    if skip_network:
        print("  [network] Skipped (--skip-network)")
    else:
        print("  [network] Building network stream...")
        network_df = build_network_stream(source_dir)
        if len(network_df) == 0:
            print("  [network] No events — will skip network output")
            network_df = None
        else:
            print(f"  [network] {len(network_df)} events")

    print("  [bundles] Parsing BUNDLES.json...")
    bundles_df = build_bundles(source_dir)
    print(f"  [bundles] {len(bundles_df)} rows")

    print("  [http] Deduplicating HTTP events...")
    http_df = build_http_events(source_dir)
    print(f"  [http] {len(http_df)} unique events")
    print()

    # --- 3. Apply timecode alignment ---
    print("[step2] Applying timecode alignment (30fps)...")

    har_tc = add_timecode_columns(har_df, start_ts)
    downloads_tc = add_timecode_columns(downloads_df, start_ts)
    network_tc = add_timecode_columns(network_df, start_ts) if network_df is not None else None
    bundles_tc = add_timecode_columns(bundles_df, start_ts)
    http_tc = add_timecode_columns(http_df, start_ts)

    active = sum(1 for x in [har_tc, downloads_tc, network_tc, bundles_tc, http_tc] if x is not None)
    print(f"  Timecodes applied to {active} datasets.")
    print()

    # --- 4. Compile to JSON output ---
    print("[step3] Compiling JSON output...")
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs = {}

    outputs["har"] = compile_events(har_tc, output_dir / "har.events.json")
    print(f"  [har] {outputs['har']['rows']} events -> har.events.json")

    outputs["downloads"] = compile_events(downloads_tc, output_dir / "downloads.events.json")
    print(f"  [downloads] {outputs['downloads']['rows']} events -> downloads.events.json")

    outputs["bundles"] = compile_events(
        bundles_tc, output_dir / "bundles.events.json",
        rename_map=BUNDLES_RENAME_MAP
    )
    print(f"  [bundles] {outputs['bundles']['rows']} events -> bundles.events.json")

    if network_tc is not None:
        outputs["network_stream"] = compile_chunked(
            network_tc, output_dir / "network_stream",
            chunk_rows=5000, dataset_name="network_stream"
        )
        print(f"  [network] {outputs['network_stream']['total_rows']} events -> network_stream/ ({outputs['network_stream']['n_chunks']} chunks)")
    else:
        print("  [network] Skipped")

    outputs["http_events"] = compile_chunked(
        http_tc, output_dir / "http_events",
        chunk_rows=2000, dataset_name="http_events"
    )
    print(f"  [http] {outputs['http_events']['total_rows']} events -> http_events/ ({outputs['http_events']['n_chunks']} chunks)")

    # --- 5. Write meta.json ---
    meta = {
        "version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "fps": FPS,
        "start_time": "",
        "timezone": LOCAL_TZ,
        "inputs": {
            "har": "1_har_data.csv",
            "downloads": "2_downloads.csv",
            "network_stream": "3_network_stream.csv",
            "bundles": "4_bundles.csv",
            "http_events": "5_http_events.csv",
        },
        "outputs": outputs,
    }
    _write_json(output_dir / "meta.json", meta)
    print(f"  [meta] -> meta.json")

    print()
    print("[pipeline] Done.")
    print()
    print("Notes:")
    print("  - HAR data: fully reproduced from source HAR CSVs")
    print("  - Bundles:  fully reproduced from BUNDLES.json")
    print(f"  - Downloads: {len(downloads_df)} rows; vault timestamps estimated")
    print(f"    (exact vault timestamps require .pdf.meta.json files not in source)")
    if network_df is not None:
        print(f"  - Network:  {len(network_df)} events")
    else:
        print("  - Network:  skipped")
    print(f"  - HTTP:     {len(http_df)} events")


def main():
    ap = argparse.ArgumentParser(
        description="Automated timecode processing: 01__source -> 03__data"
    )
    ap.add_argument("--source-dir", default="01__source",
                    help="Source directory (default: 01__source)")
    ap.add_argument("--output-dir", default="03__data_auto",
                    help="Output directory (default: 03__data_auto)")
    ap.add_argument("--skip-network", action="store_true",
                    help="Skip network stream processing entirely")
    args = ap.parse_args()

    source_dir = Path(args.source_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not source_dir.is_dir():
        print(f"Error: source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    run_pipeline(source_dir, output_dir, skip_network=args.skip_network)


if __name__ == "__main__":
    main()
