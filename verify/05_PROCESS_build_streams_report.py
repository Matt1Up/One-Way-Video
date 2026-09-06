#!/usr/bin/env python3
"""
dump_streams_report.py

Dump all streams.http, streams.http_events, and streams.net entries
into three large CSV files with bundle IDs and timecodes.

Usage (from directory with combined.json + start_time.json):

    python3 dump_streams_report.py \
        --combined combined.json \
        --start start_time.json \
        --fps 30 \
        --prefix BUNDLE_streams

Outputs:

    BUNDLE_streams_http.csv
    BUNDLE_streams_http_events.csv
    BUNDLE_streams_net.csv
"""

import argparse
import csv
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional


# ---------------- Time helpers ----------------

# Offsets for the timezone abbreviation the capture machine writes after its
# local timestamps ("... CDT"). Only unambiguous North-American/UTC abbreviations.
TZ_ABBREV_OFFSET_HOURS = {
    "UTC": 0, "GMT": 0,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
}


def parse_start_time_to_utc(ts: str) -> datetime:
    """
    Parse a start_time entry like:
        '2025-11-19 02:41:05.162276335 CST'
        '2026-05-13 22:41:55.393061139 CDT'
    using the trailing timezone abbreviation, and convert to UTC.
    (Earlier versions assumed a fixed CST offset, which put every timecode
    for a daylight-time session off by exactly one hour.)
    """
    if not ts:
        raise ValueError("Empty start_time string")

    parts = ts.split()
    tz_token = parts[-1].upper() if len(parts) >= 2 and parts[-1].isalpha() else None
    base = " ".join(parts[:-1]) if tz_token else ts

    # Trim/pad fractional seconds to microseconds
    if "." in base:
        date_part, frac = base.split(".", 1)
        frac_digits = "".join(ch for ch in frac if ch.isdigit())
        frac6 = (frac_digits + "000000")[:6]
        base6 = f"{date_part}.{frac6}"
        fmt = "%Y-%m-%d %H:%M:%S.%f"
    else:
        base6 = base
        fmt = "%Y-%m-%d %H:%M:%S"

    offset_hours = TZ_ABBREV_OFFSET_HOURS.get(tz_token or "UTC")
    if offset_hours is None:
        raise ValueError(f"Unknown timezone abbreviation {tz_token!r} in start_time entry {ts!r}")

    dt_local = datetime.strptime(base6, fmt)
    dt_aware = dt_local.replace(tzinfo=timezone(timedelta(hours=offset_hours)))
    return dt_aware.astimezone(timezone.utc)


def parse_iso_to_utc(ts: str) -> Optional[datetime]:
    """
    Parse ISO8601 strings like '2025-11-19T08:41:07.944806+00:00'
    and return a UTC-aware datetime.
    """
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):          # Python < 3.11 fromisoformat() rejects a 'Z' suffix
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def seconds_to_timecode(seconds: Optional[float], fps: float = 30.0) -> str:
    """
    Simple non-drop-frame timecode HH:MM:SS:FF at given fps.
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


# ---------------- Stream row builders ----------------

def build_http_rows(
    combined: List[Dict[str, Any]],
    t0_utc: datetime,
    fps: float,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for bundle in combined:
        bid = bundle.get("id", "")
        index_raw = bundle.get("index_raw", "")

        for item in bundle.get("streams", {}).get("http", []):
            ts = item.get("ts", "")
            dt = parse_iso_to_utc(ts)
            secs = (dt - t0_utc).total_seconds() if dt else None

            row = {
                "bundle_id": bid,
                "index_raw": index_raw,
                "event_idx": item.get("idx", ""),
                "event_ts": ts,
                "event_timecode": seconds_to_timecode(secs, fps) if secs is not None else "",
                "seconds_from_start": f"{secs:.6f}" if secs is not None else "",
                "url": item.get("url", ""),
            }
            rows.append(row)

    # Chronological sort
    rows.sort(
        key=lambda r: float(r["seconds_from_start"]) if r["seconds_from_start"] else float("inf")
    )
    return rows


def build_http_events_rows(
    combined: List[Dict[str, Any]],
    t0_utc: datetime,
    fps: float,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for bundle in combined:
        bid = bundle.get("id", "")
        index_raw = bundle.get("index_raw", "")

        for item in bundle.get("streams", {}).get("http_events", []):
            ts = item.get("startedDateTime", "")
            dt = parse_iso_to_utc(ts)
            secs = (dt - t0_utc).total_seconds() if dt else None

            client = item.get("client") or ["", ""]
            server = item.get("server") or ["", ""]
            if len(client) < 2:
                client = client + [""] * (2 - len(client))
            if len(server) < 2:
                server = server + [""] * (2 - len(server))

            req = item.get("req") or {}
            resp = item.get("resp") or {}
            tls = item.get("tls") or {}

            row = {
                "bundle_id": bid,
                "index_raw": index_raw,
                "event_idx": item.get("idx", ""),
                "event_startedDateTime": ts,
                "event_timecode": seconds_to_timecode(secs, fps) if secs is not None else "",
                "seconds_from_start": f"{secs:.6f}" if secs is not None else "",
                "time_ms": item.get("time_ms", ""),
                "sni": item.get("sni", ""),
                "flow_id": item.get("flow_id", ""),
                "client_ip": client[0],
                "client_port": client[1],
                "server_ip": server[0],
                "server_port": server[1],
                "req_method": req.get("method", ""),
                "req_url": req.get("url", ""),
                "req_http_version": req.get("http_version", ""),
                "req_body_sha256": req.get("body_sha256", ""),
                "resp_status": resp.get("status", ""),
                "resp_http_version": resp.get("http_version", ""),
                "resp_body_sha256": resp.get("body_sha256", ""),
                "tls_version": tls.get("version", ""),
                "tls_cipher": tls.get("cipher", ""),
                "tls_cert_sha256": tls.get("cert_sha256", "") or "",
            }
            rows.append(row)

    rows.sort(
        key=lambda r: float(r["seconds_from_start"]) if r["seconds_from_start"] else float("inf")
    )
    return rows


def build_net_rows(
    combined: List[Dict[str, Any]],
    t0_utc: datetime,
    fps: float,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for bundle in combined:
        bid = bundle.get("id", "")
        index_raw = bundle.get("index_raw", "")

        for item in bundle.get("streams", {}).get("net", []):
            ts = item.get("ts", "")
            dt = parse_iso_to_utc(ts)
            secs = (dt - t0_utc).total_seconds() if dt else None

            row = {
                "bundle_id": bid,
                "index_raw": index_raw,
                "event_idx": item.get("idx", ""),
                "event_ts": ts,
                "event_timecode": seconds_to_timecode(secs, fps) if secs is not None else "",
                "seconds_from_start": f"{secs:.6f}" if secs is not None else "",
                "proto": item.get("proto", ""),
                "src": item.get("src", ""),
                "dst": item.get("dst", ""),
                "t": item.get("t", ""),
            }
            rows.append(row)

    rows.sort(
        key=lambda r: float(r["seconds_from_start"]) if r["seconds_from_start"] else float("inf")
    )
    return rows


# ---------------- CSV helper ----------------

def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        # Still create an empty file with no rows if you want; for now just bail.
        return

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


# ---------------- CLI / main ----------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump streams.http, streams.http_events, and streams.net into CSV files."
    )
    parser.add_argument(
        "--combined",
        default="combined.json",
        help="Path to combined.json (list of bundle JSON objects).",
    )
    parser.add_argument(
        "--start",
        default="start_time.json",
        help="Path to start_time.json (list of system start times).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Frame rate used for timecode calculations (default: 30).",
    )
    parser.add_argument(
        "--prefix",
        default="BUNDLE_streams",
        help="Prefix for output CSV filenames (default: BUNDLE_streams).",
    )

    args = parser.parse_args()

    # Load data
    with open(args.combined, "r", encoding="utf-8") as f:
        combined = json.load(f)

    with open(args.start, "r", encoding="utf-8") as f:
        start_times = json.load(f)

    if not start_times:
        raise SystemExit("start_time.json is empty – cannot compute reference time.")

    # Common reference time (UTC) from very first start_time entry
    t0_utc = parse_start_time_to_utc(start_times[0])

    # Build per-stream rows
    http_rows = build_http_rows(combined, t0_utc, args.fps)
    http_events_rows = build_http_events_rows(combined, t0_utc, args.fps)
    net_rows = build_net_rows(combined, t0_utc, args.fps)

    # Write CSV outputs
    http_path = f"{args.prefix}_http.csv"
    http_events_path = f"{args.prefix}_http_events.csv"
    net_path = f"{args.prefix}_net.csv"

    write_csv(http_path, http_rows)
    write_csv(http_events_path, http_events_rows)
    write_csv(net_path, net_rows)

    print(f"[OK] Wrote HTTP stream dump to         {http_path}  (rows: {len(http_rows)})")
    print(f"[OK] Wrote HTTP-events stream dump to  {http_events_path}  (rows: {len(http_events_rows)})")
    print(f"[OK] Wrote NET stream dump to          {net_path}  (rows: {len(net_rows)})")


if __name__ == "__main__":
    main()
