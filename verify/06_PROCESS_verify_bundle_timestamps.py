#!/usr/bin/env python3
"""
bundle_timestamp_report.py

Scan a bundles directory for timestamp evidence and emit CSV reports that
tie together:

  - Each data file that has an OTS receipt (filename + ".ots")
  - The original OTS receipt file (never modified)
  - An upgraded copy of the OTS receipt (stored in a separate directory)
  - The Bitcoin anchoring information for that receipt
  - The Roughtime bundle that timestamps the OTS file
    (filename + ".ots__time-stamp.json")

Outputs (by default):

  - bundle_ts_master.csv  – full detail, one row per data file
  - bundle_ts_ots.csv     – OTS / Bitcoin anchoring view
  - bundle_ts_rt.csv      – Roughtime-for-OTS view

Requirements:
  - `verify_roughtime_bundle.py` in the same directory (importable as a module)
  - `ots` CLI installed and on PATH
  - `requests` Python package
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

# Import the existing Roughtime verifier helper
import verify_roughtime_bundle as vrt

# ── constants ──────────────────────────────────────────────────────────────────
ESPLORA = "https://blockstream.info/api"
OTS_CLI = "ots"
CT_TZNAME = "America/Chicago"
CT = ZoneInfo(CT_TZNAME) if ZoneInfo else timezone.utc  # fallback to UTC name

# Parse lines from `ots info`
RE_BBHA = re.compile(r"BitcoinBlockHeaderAttestation\((\d{5,7})\)")
RE_BLOCK = re.compile(r"[Bb]itcoin\s+block\s+(\d{5,7})\b")
RE_FILE_HASH = re.compile(r"File sha256 hash:\s*([0-9a-f]{64})", re.IGNORECASE)

# Master CSV fields (one row per data file that has an OTS receipt)
MASTER_FIELDS = [
    "bundle_id",
    "base_filename",
    "file_kind",
    "data_file_exists",
    "data_file_path",
    "data_file_size_bytes",
    "data_file_mtime_ct",
    "data_file_sha256",
    "ots_original_filename",
    "ots_original_path",
    "ots_original_sha256",
    "ots_upgraded_filename",
    "ots_upgraded_path",
    "ots_upgraded_sha256",
    "ots_receipt_status",
    "ots_info_file_sha256",
    "ots_info_file_sha256_matches_data",
    "ots_block_height",
    "ots_block_hash",
    "ots_earliest_onchain_mtp_ct",
    "ots_block_header_time_ct",
    "ots_latest_onchain_upper_ct",
    "rt_ots_bundle_filename",
    "rt_ots_bundle_path",
    "rt_ots_midp_utc",
    "rt_ots_mint_utc",
    "rt_ots_maxt_utc",
    "rt_ots_radius_us",
    "rt_ots_bind_sha256_hex",
    "rt_ots_bind_matches_ots_sha256",
    "rt_ots_bind_source",
    "rt_ots_nonce_match",
    "rt_ots_merkle_ok",
    "rt_ots_cert_sig_ok",
    "rt_ots_srep_sig_ok",
    "rt_ots_window_ok",
    "rt_ots_all_ok",
]

# Concise OTS/BTC view
OTS_FIELDS = [
    "bundle_id",
    "base_filename",
    "file_kind",
    "data_file_sha256",
    "data_file_mtime_ct",
    "ots_original_filename",
    "ots_original_sha256",
    "ots_upgraded_filename",
    "ots_upgraded_sha256",
    "ots_receipt_status",
    "ots_block_height",
    "ots_block_hash",
    "ots_earliest_onchain_mtp_ct",
    "ots_block_header_time_ct",
    "ots_latest_onchain_upper_ct",
]

# Concise Roughtime view (for OTS receipts)
RT_OTS_FIELDS = [
    "bundle_id",
    "base_filename",
    "file_kind",
    "ots_original_filename",
    "ots_original_sha256",
    "rt_ots_bundle_filename",
    "rt_ots_midp_utc",
    "rt_ots_radius_us",
    "rt_ots_mint_utc",
    "rt_ots_maxt_utc",
    "rt_ots_bind_sha256_hex",
    "rt_ots_bind_matches_ots_sha256",
    "rt_ots_nonce_match",
    "rt_ots_merkle_ok",
    "rt_ots_cert_sig_ok",
    "rt_ots_srep_sig_ok",
    "rt_ots_window_ok",
    "rt_ots_all_ok",
]


# ── utility: time formatting, hashing, ots helpers ────────────────────────────
def fmt_ct(ts: int | float) -> str:
    """Format epoch seconds in America/Chicago (or UTC fallback)."""
    return datetime.fromtimestamp(ts, CT).strftime("%Y-%m-%d %H:%M:%S %Z")


def sha256_file(path: Path, bufsize: int = 1024 * 1024) -> str:
    """SHA-256 of a file as lowercase hex."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def run_ots_info(path: Path) -> str:
    """Return `ots info` stdout+stderr as text (never raises)."""
    try:
        proc = subprocess.run(
            [OTS_CLI, "info", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        print("ERROR: `ots` CLI not found (install opentimestamps-client).", file=sys.stderr)
        return ""
    except Exception:
        return ""


def extract_height(txt: str):
    """Extract BTC block height from `ots info` output (if anchored)."""
    m = RE_BBHA.search(txt)
    if m:
        return int(m.group(1))
    m = RE_BLOCK.search(txt)
    if m:
        return int(m.group(1))
    return None


def extract_file_hash(txt: str):
    """Extract the 'File sha256 hash:' line from `ots info`."""
    m = RE_FILE_HASH.search(txt)
    return m.group(1) if m else ""


def esplora_block_hash(height: int) -> str:
    """Look up the block hash for a given height via Esplora."""
    r = requests.get(f"{ESPLORA}/block-height/{height}", timeout=15)
    r.raise_for_status()
    h = r.text.strip()
    if len(h) < 64:
        raise RuntimeError(f"Bad block hash for height {height}: {h!r}")
    return h


def esplora_block_info(h: str) -> dict:
    """Return Esplora block JSON with at least timestamp/mediantime/height."""
    r = requests.get(f"{ESPLORA}/block/{h}", timeout=15)
    r.raise_for_status()
    j = r.json()
    for k in ("timestamp", "mediantime", "height"):
        if k not in j:
            raise RuntimeError("Esplora response missing fields")
    return j


# ── Roughtime analysis wrapper (based on verify_roughtime_bundle + auth script) ───
def analyze_roughtime_bundle(bundle_path: Path, bind_sha256_hex: str | None = None) -> dict:
    """
    Structured wrapper around verify_roughtime_bundle:

      - Uses vrt.RTMessage, vrt.Ed25519Verifier, vrt.verify_inclusion, etc.
      - Returns a dict of booleans/values instead of printing.
    """
    import json, base64, os as _os

    bundle_path = Path(bundle_path)
    with bundle_path.open("r", encoding="utf-8") as f:
        bundle = json.load(f)

    proof = bundle["proof"]
    lt_b64 = bundle["longterm_pubkey_b64"]
    lt_pub = base64.b64decode(lt_b64)

    # Determine SHA-256 to bind
    src = None
    bind_hex = None
    if bind_sha256_hex:
        bind_hex = bind_sha256_hex.lower()
        src = "explicit_arg"
    else:
        artifact = bundle.get("artifact") or {}
        if "bind_sha256_hex" in artifact:
            bind_hex = artifact["bind_sha256_hex"].lower()
            src = "bundle.artifact.bind_sha256_hex"
        elif "bind_file_path" in artifact and _os.path.isfile(artifact["bind_file_path"]):
            bind_hex = vrt.sha256_file_hex(artifact["bind_file_path"])
            src = f"SHA-256({artifact['bind_file_path']})"
        else:
            bind_hex = None
            src = None

    if bind_hex:
        computed_nonce = vrt.derive_nonce_from_sha256_hex(bind_hex)
    else:
        computed_nonce = None

    # Parse bytes from bundle
    cert_b = bytes.fromhex(proof["cert_bytes"])
    cert_sig = bytes.fromhex(proof["cert_sig"])
    srep_b = bytes.fromhex(proof["srep_bytes"])
    srep_sig = bytes.fromhex(proof["srep_sig"])
    online_pub = bytes.fromhex(proof["online_pubkey"])
    nonce_from_bundle = bytes.fromhex(proof["nonce"])
    root = bytes.fromhex(proof["root"])
    path = bytes.fromhex(proof["path"])
    indx = proof["indx"]
    midp = proof["midpoint_us"]
    mint = proof["mint_us"]
    maxt = proof["maxt_us"]
    radius = proof.get("radius_us")

    # Verify signatures
    cert = vrt.RTMessage.parse(cert_b)
    dele = cert.map[vrt.DELE]
    v_lt = vrt.Ed25519Verifier(lt_pub)
    v_online = vrt.Ed25519Verifier(online_pub)

    ok_cert = v_lt.verify(cert_sig, vrt.CTX_DELE + dele)
    ok_srep = v_online.verify(srep_sig, vrt.CTX_SREP + srep_b)

    # Merkle inclusion
    if computed_nonce is not None:
        ok_merkle = vrt.verify_inclusion(computed_nonce, indx, path, root)
        nonce_match = (nonce_from_bundle == computed_nonce)
    else:
        ok_merkle = vrt.verify_inclusion(nonce_from_bundle, indx, path, root)
        nonce_match = None

    ok_window = (mint <= midp <= maxt)
    all_ok = ok_cert and ok_srep and ok_window and ok_merkle and (nonce_match in (True, None))

    return {
        "server": bundle.get("server"),
        "port": bundle.get("port"),
        "lt_pubkey_b64": lt_b64,
        "midp_us": midp,
        "midp_iso": vrt.human_time(midp),
        "mint_us": mint,
        "maxt_us": maxt,
        "mint_iso": vrt.human_time(mint),
        "maxt_iso": vrt.human_time(maxt),
        "radius_us": radius,
        "bind_sha256_hex": bind_hex,
        "bind_source": src,
        "computed_nonce_hex": computed_nonce.hex() if computed_nonce is not None else "",
        "bundle_nonce_hex": nonce_from_bundle.hex(),
        "nonce_match": nonce_match,
        "ok_cert_sig": ok_cert,
        "ok_srep_sig": ok_srep,
        "ok_merkle": ok_merkle,
        "ok_window": ok_window,
        "all_ok": all_ok,
    }


# ── core per-file processing ──────────────────────────────────────────────────
def classify_file(basename: str) -> tuple[str, str]:
    """
    Return (bundle_id, file_kind) from a base filename.
      - bundle_id: '0000', '0001', ... or '' if not numeric
      - file_kind: 'start_time', 'bundle_json', 'bundle_png', or generic ext.
    """
    name = os.path.basename(basename)
    if name.startswith("start_time"):
        return ("", "start_time")
    m = re.match(r"^(\d{4})\.(json|png)$", name)
    if m:
        bid, ext = m.group(1), m.group(2)
        if ext == "json":
            kind = "bundle_json"
        else:
            kind = "bundle_png"
        return (bid, kind)
    # fallback: no numeric id
    return ("", Path(name).suffix.lstrip(".") or "other")


def process_one(
    basename: str,
    base_dir: Path,
    ots_up_dir: Path,
    block_cache: dict,
    sleep_secs: float = 0.0,
) -> dict:
    """
    Process a single data file (identified by its base filename):

      - compute SHA-256 for data file
      - verify / upgrade OTS (copy only; original .ots left untouched)
      - analyze Roughtime bundle for the OTS receipt

    Return a dict keyed by MASTER_FIELDS.
    """
    row: dict[str, str] = {}
    basename = basename.strip()
    if not basename:
        return {}

    bundle_id, file_kind = classify_file(basename)
    row["bundle_id"] = bundle_id
    row["base_filename"] = basename
    row["file_kind"] = file_kind

    # --- main data file ---
    data_path = base_dir / basename
    if not data_path.is_file():
        # If the data file is missing, silently skip this one (omit from report)
        print(f"⚠️  Data file missing for {basename}, skipping.", file=sys.stderr)
        return {}

    row["data_file_exists"] = "yes"
    row["data_file_path"] = str(data_path)
    st = data_path.stat()
    row["data_file_size_bytes"] = str(st.st_size)
    row["data_file_mtime_ct"] = fmt_ct(st.st_mtime)
    data_sha256 = sha256_file(data_path)
    row["data_file_sha256"] = data_sha256

    # --- OTS: original + upgraded copy ---
    ots_orig_path = base_dir / (basename + ".ots")
    if ots_orig_path.is_file():
        row["ots_original_filename"] = ots_orig_path.name
        row["ots_original_path"] = str(ots_orig_path)
        ots_orig_sha256 = sha256_file(ots_orig_path)
        row["ots_original_sha256"] = ots_orig_sha256

        ots_up_dir.mkdir(parents=True, exist_ok=True)
        ots_up_path = ots_up_dir / (basename + ".ots.upgraded")

        # Copy original .ots if upgraded copy does not yet exist
        if not ots_up_path.exists():
            shutil.copy2(ots_orig_path, ots_up_path)
            # Upgrade the copy in-place (original .ots is never touched)
            try:
                subprocess.run([OTS_CLI, "upgrade", str(ots_up_path)], check=False)
            except Exception as e:
                print(f"⚠️  OTS upgrade failed for {ots_up_path.name}: {e}", file=sys.stderr)

        row["ots_upgraded_filename"] = ots_up_path.name
        row["ots_upgraded_path"] = str(ots_up_path)

        if ots_up_path.exists():
            row["ots_upgraded_sha256"] = sha256_file(ots_up_path)

            info_txt = run_ots_info(ots_up_path)
            file_hash = extract_file_hash(info_txt)
            row["ots_info_file_sha256"] = file_hash
            if file_hash:
                row["ots_info_file_sha256_matches_data"] = (
                    "yes" if file_hash.lower() == data_sha256.lower() else "no"
                )
            else:
                row["ots_info_file_sha256_matches_data"] = ""

            height = extract_height(info_txt)
            row["ots_block_height"] = str(height) if height else ""
            status = "anchored" if height else "pending"
            row["ots_receipt_status"] = status

            block_hash = ""
            mtp_ct = ntime_ct = upper_ct = ""
            if height:
                try:
                    if height not in block_cache:
                        bh = esplora_block_hash(height)
                        bj = esplora_block_info(bh)
                        block_cache[height] = {
                            "hash": bh,
                            "timestamp": int(bj["timestamp"]),
                            "mediantime": int(bj["mediantime"]),
                        }
                        if sleep_secs:
                            time.sleep(sleep_secs)
                    bi = block_cache[height]
                    block_hash = bi["hash"]
                    ntime = bi["timestamp"]
                    mtp = bi["mediantime"]
                    upper = ntime + 2 * 3600
                    mtp_ct = fmt_ct(mtp)
                    ntime_ct = fmt_ct(ntime)
                    upper_ct = fmt_ct(upper)
                except Exception as e:
                    print(f"⚠️  Esplora lookup failed for height {height}: {e}", file=sys.stderr)
            row["ots_block_hash"] = block_hash
            row["ots_earliest_onchain_mtp_ct"] = mtp_ct
            row["ots_block_header_time_ct"] = ntime_ct
            row["ots_latest_onchain_upper_ct"] = upper_ct
        else:
            row["ots_upgraded_sha256"] = ""
            row["ots_info_file_sha256"] = ""
            row["ots_info_file_sha256_matches_data"] = ""
            row["ots_block_height"] = ""
            row["ots_receipt_status"] = "error"
            row["ots_block_hash"] = ""
            row["ots_earliest_onchain_mtp_ct"] = ""
            row["ots_block_header_time_ct"] = ""
            row["ots_latest_onchain_upper_ct"] = ""
    else:
        # No OTS receipt present; we don't include this row in the report
        print(f"⚠️  OTS receipt missing for {basename}, skipping.", file=sys.stderr)
        return {}

    # --- Roughtime for the OTS receipt file ---
    rt_path = base_dir / (basename + ".ots__time-stamp.json")
    if rt_path.is_file():
        rt = analyze_roughtime_bundle(rt_path)
        row["rt_ots_bundle_filename"] = rt_path.name
        row["rt_ots_bundle_path"] = str(rt_path)
        row["rt_ots_midp_utc"] = rt["midp_iso"]
        row["rt_ots_mint_utc"] = rt["mint_iso"]
        row["rt_ots_maxt_utc"] = rt["maxt_iso"]
        row["rt_ots_radius_us"] = str(rt.get("radius_us") or "")
        bind_hex = rt.get("bind_sha256_hex") or ""
        row["rt_ots_bind_sha256_hex"] = bind_hex
        ots_orig_sha256 = row.get("ots_original_sha256") or ""
        if bind_hex and ots_orig_sha256:
            row["rt_ots_bind_matches_ots_sha256"] = (
                "yes" if bind_hex.lower() == ots_orig_sha256.lower() else "no"
            )
        else:
            row["rt_ots_bind_matches_ots_sha256"] = ""
        row["rt_ots_bind_source"] = rt.get("bind_source") or ""
        nm = rt["nonce_match"]
        row["rt_ots_nonce_match"] = "" if nm is None else ("yes" if nm else "no")
        row["rt_ots_merkle_ok"] = "yes" if rt["ok_merkle"] else "no"
        row["rt_ots_cert_sig_ok"] = "yes" if rt["ok_cert_sig"] else "no"
        row["rt_ots_srep_sig_ok"] = "yes" if rt["ok_srep_sig"] else "no"
        row["rt_ots_window_ok"] = "yes" if rt["ok_window"] else "no"
        row["rt_ots_all_ok"] = "yes" if rt["all_ok"] else "no"
    else:
        # No Roughtime bundle: leave RT fields empty but still emit the OTS info.
        row["rt_ots_bundle_filename"] = ""
        row["rt_ots_bundle_path"] = ""
        row["rt_ots_midp_utc"] = ""
        row["rt_ots_mint_utc"] = ""
        row["rt_ots_maxt_utc"] = ""
        row["rt_ots_radius_us"] = ""
        row["rt_ots_bind_sha256_hex"] = ""
        row["rt_ots_bind_matches_ots_sha256"] = ""
        row["rt_ots_bind_source"] = ""
        row["rt_ots_nonce_match"] = ""
        row["rt_ots_merkle_ok"] = ""
        row["rt_ots_cert_sig_ok"] = ""
        row["rt_ots_srep_sig_ok"] = ""
        row["rt_ots_window_ok"] = ""
        row["rt_ots_all_ok"] = ""

    return row


# ── CSV helpers ────────────────────────────────────────────────────────────────
def write_table(path: Path, fieldnames: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            if not r:
                continue
            w.writerow({k: r.get(k, "") for k in fieldnames})


# ── discovery helper ──────────────────────────────────────────────────────────
def discover_basenames(base_dir: Path) -> list[str]:
    """
    Discover all base filenames that have an OTS receipt in base_dir.

    For each 'X.ots' file we return the base 'X' (e.g. '0001.json' or
    'start_time.json'). Results are sorted alphabetically.
    """
    basenames = set()
    for ots_path in base_dir.glob("*.ots"):
        name = ots_path.name
        if not name.endswith(".ots"):
            continue
        base = name[:-4]
        basenames.add(base)
    return sorted(basenames)


# ── main CLI ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description=(
            "Generate combined OTS + Roughtime reports for all files in a bundles "
            "directory that have .ots receipts."
        )
    )
    ap.add_argument(
        "--base-dir",
        default=".",
        help="Directory containing the data files, .ots receipts, and Roughtime bundles (default: current directory).",
    )
    ap.add_argument(
        "--ots-upgraded-dir",
        default="ots_upgraded_bundle",
        help="Directory to store upgraded OTS receipts (copies). Default: ./ots_upgraded_bundle",
    )
    ap.add_argument(
        "--out-master",
        default="BUNDLE_ts_master.csv",
        help="Master CSV with all details (default: BUNDLE_ts_master.csv).",
    )
    ap.add_argument(
        "--out-ots",
        default="BUNDLE_ts_ots.csv",
        help="Concise OTS/BTC view (default: BUNDLE_ts_ots.csv).",
    )
    ap.add_argument(
        "--out-rt",
        default="BUNDLE_ts_rt.csv",
        help="Concise Roughtime view for OTS receipts (default: BUNDLE_ts_rt.csv).",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Sleep seconds between Esplora block-info API calls (optional).",
    )
    args = ap.parse_args()

    base_dir = Path(args.base_dir).resolve()
    if not base_dir.is_dir():
        sys.exit(f"Base directory not found: {base_dir}")

    ots_up_dir = Path(args.ots_upgraded_dir)
    if not ots_up_dir.is_absolute():
        ots_up_dir = base_dir / ots_up_dir

    basenames = discover_basenames(base_dir)
    if not basenames:
        sys.exit("No .ots receipts found in base directory.")

    rows = []
    block_cache: dict[int, dict] = {}

    for name in basenames:
        print(f"[*] Processing {name} ...")
        row = process_one(name, base_dir, ots_up_dir, block_cache, sleep_secs=args.sleep)
        if row:
            rows.append(row)

    if not rows:
        sys.exit("No valid data/OTS pairs processed. Nothing to write.")

    # Write CSV outputs from a single master row set
    write_table(Path(args.out_master), MASTER_FIELDS, rows)
    write_table(Path(args.out_ots), OTS_FIELDS, rows)
    write_table(Path(args.out_rt), RT_OTS_FIELDS, rows)

    print(f"\n✅ Wrote {len(rows)} rows to:")
    print(f"   {args.out_master}")
    print(f"   {args.out_ots}")
    print(f"   {args.out_rt}")


if __name__ == "__main__":
    main()
