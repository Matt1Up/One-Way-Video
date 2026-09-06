#!/usr/bin/env python3
"""
har_extract_mcro_cases.py

Given a HAR (or HAR.GZ) file that contains bodies, extract structured data for:

1) CaseSearchFormValidation (script):
   - Request payload: FormType, CaseSearchNumber
   - Response JSON: code, message
   - Date/time details from HAR

2) CaseSearchDetails (document):
   - Save full HTML per response, named using the Case Number
   - Extract Case Number from HTML
   - Log basic timing + filename

3) CaseSearchSearch (document):
   - Request payload: FormType, CaseSearchNumber
   - Save full HTML per response (unique filename)
   - Parse HTML to get all (Case Number, Defendant) pairs listed
   - Log them with timing + payload info

Outputs (in --out-dir):
    har_case_form_validation.csv
    har_case_details.csv
    har_case_search.csv
and two HTML subdirs:
    CaseSearchDetails_html/
    CaseSearchSearch_html/
"""

import argparse
import base64
import csv
import gzip
import json
import os
import re
import sys
from urllib.parse import urlsplit, parse_qsl

# ------------------------
# Helpers
# ------------------------

def load_har(path: str):
    """Load HAR JSON from .har or .har.gz."""
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def get_entries(har_json):
    return har_json.get("log", {}).get("entries", [])


def get_response_text(entry) -> str:
    """
    Get decoded response body text from a HAR entry, handling optional base64 encoding.
    Returns '' if missing or on error.
    """
    content = (entry.get("response") or {}).get("content") or {}
    text = content.get("text")
    if text is None:
        return ""
    encoding = content.get("encoding")
    if encoding == "base64":
        try:
            raw = base64.b64decode(text)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    # Assume already text
    return text


def extract_form_fields(post_data: dict) -> dict:
    """
    Extract form fields (FormType, CaseSearchNumber, etc.) from HAR postData.
    Handles:
      - postData.params (list of name/value)
      - postData.text as application/x-www-form-urlencoded
    """
    fields = {}
    if not post_data:
        return fields

    # 1) params list
    params = post_data.get("params")
    if isinstance(params, list):
        for p in params:
            name = p.get("name")
            value = p.get("value")
            if name is not None:
                fields[name] = value
        # If we got something, we can return
        if fields:
            return fields

    # 2) raw text (urlencoded)
    text = post_data.get("text")
    if isinstance(text, str) and text:
        try:
            for k, v in parse_qsl(text, keep_blank_values=True):
                fields[k] = v
        except Exception:
            pass

    return fields


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def make_unique_filename(out_dir: str, base_name: str) -> str:
    """
    Ensure a unique filename in out_dir.
    If base_name exists, append _2, _3, etc. before the extension.
    """
    full = os.path.join(out_dir, base_name)
    if not os.path.exists(full):
        return full

    root, ext = os.path.splitext(base_name)
    idx = 2
    while True:
        candidate = f"{root}_{idx}{ext}"
        full_candidate = os.path.join(out_dir, candidate)
        if not os.path.exists(full_candidate):
            return full_candidate
        idx += 1


def get_basic_timing(entry):
    """Return (startedDateTime, time_ms, timings_send_ms, timings_wait_ms, timings_receive_ms)"""
    started = entry.get("startedDateTime")
    time_ms = entry.get("time")
    timings = entry.get("timings") or {}
    send_ms = timings.get("send")
    wait_ms = timings.get("wait")
    recv_ms = timings.get("receive")
    return started, time_ms, send_ms, wait_ms, recv_ms


def url_path(url: str) -> str:
    try:
        return urlsplit(url).path
    except Exception:
        return ""


# Regex patterns based on provided HTML examples
# Case Number patterns (Details + Search pages)
CASE_NUMBER_PATTERN = re.compile(
    r"Case Number:\s*</span>\s*<span[^>]*>([^<]+)</span>",
    re.IGNORECASE
)

# Defendant pattern for CaseSearchSearch results
DEFENDANT_PATTERN = re.compile(
    r"<h4[^>]*>\s*Defendant\s*</h4>\s*<span[^>]*>([^<]+)</span>",
    re.IGNORECASE
)


# ------------------------
# Processing functions
# ------------------------

def process_case_search_form_validation(entries, out_dir: str):
    """
    Process CaseSearchFormValidation calls.
    Output CSV: har_case_form_validation.csv
    """
    rows = []

    for idx, entry in enumerate(entries, start=1):
        req = entry.get("request") or {}
        url = req.get("url", "") or ""
        path = url_path(url)
        if path != "/CaseSearch/CaseSearchFormValidation":
            continue

        method = req.get("method", "")
        post_data = req.get("postData") or {}
        fields = extract_form_fields(post_data)
        form_type = fields.get("FormType")
        case_search_number = fields.get("CaseSearchNumber")

        resp = entry.get("response") or {}
        status = resp.get("status")
        body_text = get_response_text(entry)
        resp_code = None
        resp_message = None
        # Try to parse JSON like {"code":"success","message":""}
        if body_text:
            try:
                jd = json.loads(body_text)
                if isinstance(jd, dict):
                    resp_code = jd.get("code")
                    resp_message = jd.get("message")
            except Exception:
                # leave as None
                pass

        started, time_ms, send_ms, wait_ms, recv_ms = get_basic_timing(entry)

        rows.append({
            "entry_index": idx,
            "startedDateTime": started,
            "time_ms": time_ms,
            "timings_send_ms": send_ms,
            "timings_wait_ms": wait_ms,
            "timings_receive_ms": recv_ms,
            "method": method,
            "url": url,
            "http_status": status,
            "FormType": form_type,
            "CaseSearchNumber": case_search_number,
            "response_body_raw": body_text,
            "response_json_code": resp_code,
            "response_json_message": resp_message,
        })

    if not rows:
        print("[INFO] No CaseSearchFormValidation entries found.")
        return

    out_path = os.path.join(out_dir, "har_case_form_validation.csv")
    fieldnames = [
        "entry_index",
        "startedDateTime",
        "time_ms",
        "timings_send_ms",
        "timings_wait_ms",
        "timings_receive_ms",
        "method",
        "url",
        "http_status",
        "FormType",
        "CaseSearchNumber",
        "response_body_raw",
        "response_json_code",
        "response_json_message",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"[OK] Wrote CaseSearchFormValidation CSV: {out_path}")


def process_case_search_details(entries, out_dir: str):
    """
    Process CaseSearchDetails pages.
    Save HTML per response, named by Case Number.
    Output CSV: har_case_details.csv
    """
    details_html_dir = os.path.join(out_dir, "CaseSearchDetails_html")
    ensure_dir(details_html_dir)

    rows = []

    for idx, entry in enumerate(entries, start=1):
        req = entry.get("request") or {}
        url = req.get("url", "") or ""
        path = url_path(url)
        if path != "/CaseSearch/CaseSearchDetails":
            continue

        method = req.get("method", "")
        resp = entry.get("response") or {}
        status = resp.get("status")

        body_html = get_response_text(entry)
        if not body_html:
            continue

        # Extract Case Number from HTML
        m = CASE_NUMBER_PATTERN.search(body_html)
        case_number = m.group(1).strip() if m else None

        # Build filename using Case Number
        if case_number:
            base_name = f"{case_number}.CaseSearchDetails.html"
        else:
            base_name = f"CaseSearchDetails_entry_{idx}.html"

        html_path = make_unique_filename(details_html_dir, base_name)
        with open(html_path, "w", encoding="utf-8") as hf:
            hf.write(body_html)

        started, time_ms, send_ms, wait_ms, recv_ms = get_basic_timing(entry)

        rows.append({
            "entry_index": idx,
            "startedDateTime": started,
            "time_ms": time_ms,
            "timings_send_ms": send_ms,
            "timings_wait_ms": wait_ms,
            "timings_receive_ms": recv_ms,
            "method": method,
            "url": url,
            "http_status": status,
            "case_number": case_number,
            "html_filename": os.path.basename(html_path),
            "html_relpath": os.path.relpath(html_path, out_dir),
        })

    if not rows:
        print("[INFO] No CaseSearchDetails entries found.")
        return

    out_path = os.path.join(out_dir, "har_case_details.csv")
    fieldnames = [
        "entry_index",
        "startedDateTime",
        "time_ms",
        "timings_send_ms",
        "timings_wait_ms",
        "timings_receive_ms",
        "method",
        "url",
        "http_status",
        "case_number",
        "html_filename",
        "html_relpath",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"[OK] Wrote CaseSearchDetails CSV: {out_path}")


def process_case_search_search(entries, out_dir: str):
    """
    Process CaseSearchSearch pages.
    - Extract request FormType, CaseSearchNumber.
    - Save full response HTML with a unique name.
    - Extract all (Case Number, Defendant) pairs from the HTML.
    Output CSV: har_case_search.csv
    """
    search_html_dir = os.path.join(out_dir, "CaseSearchSearch_html")
    ensure_dir(search_html_dir)

    rows = []

    for idx, entry in enumerate(entries, start=1):
        req = entry.get("request") or {}
        url = req.get("url", "") or ""
        path = url_path(url)
        if path != "/CaseSearch/CaseSearchSearch":
            continue

        method = req.get("method", "")
        post_data = req.get("postData") or {}
        fields = extract_form_fields(post_data)
        form_type = fields.get("FormType")
        case_search_number = fields.get("CaseSearchNumber")

        resp = entry.get("response") or {}
        status = resp.get("status")

        body_html = get_response_text(entry)
        if not body_html:
            continue

        # Save HTML with a unique name (based on CaseSearchNumber if available)
        if case_search_number:
            base_name = f"{case_search_number}.CaseSearchSearch.html"
        else:
            base_name = f"CaseSearchSearch_entry_{idx}.html"

        html_path = make_unique_filename(search_html_dir, base_name)
        with open(html_path, "w", encoding="utf-8") as hf:
            hf.write(body_html)

        # Extract all case numbers & defendants
        all_case_nums = [m.group(1).strip() for m in CASE_NUMBER_PATTERN.finditer(body_html)]
        defendants = [m.group(1).strip() for m in DEFENDANT_PATTERN.finditer(body_html)]

        # Align them: typically top search criteria + N cards, so take last N case numbers
        if defendants and len(all_case_nums) >= len(defendants):
            result_case_nums = all_case_nums[-len(defendants):]
        else:
            # Best effort fallback
            result_case_nums = all_case_nums[:len(defendants)]

        started, time_ms, send_ms, wait_ms, recv_ms = get_basic_timing(entry)

        # If no defendants found, still log one row for the search itself
        if not defendants:
            rows.append({
                "entry_index": idx,
                "result_index": None,
                "startedDateTime": started,
                "time_ms": time_ms,
                "timings_send_ms": send_ms,
                "timings_wait_ms": wait_ms,
                "timings_receive_ms": recv_ms,
                "method": method,
                "url": url,
                "http_status": status,
                "request_FormType": form_type,
                "request_CaseSearchNumber": case_search_number,
                "result_case_number": None,
                "result_defendant": None,
                "html_filename": os.path.basename(html_path),
                "html_relpath": os.path.relpath(html_path, out_dir),
            })
            continue

        # Otherwise, log one row per (case_number, defendant)
        for j, (cn, dn) in enumerate(zip(result_case_nums, defendants), start=1):
            rows.append({
                "entry_index": idx,
                "result_index": j,
                "startedDateTime": started,
                "time_ms": time_ms,
                "timings_send_ms": send_ms,
                "timings_wait_ms": wait_ms,
                "timings_receive_ms": recv_ms,
                "method": method,
                "url": url,
                "http_status": status,
                "request_FormType": form_type,
                "request_CaseSearchNumber": case_search_number,
                "result_case_number": cn,
                "result_defendant": dn,
                "html_filename": os.path.basename(html_path),
                "html_relpath": os.path.relpath(html_path, out_dir),
            })

    if not rows:
        print("[INFO] No CaseSearchSearch entries found.")
        return

    out_path = os.path.join(out_dir, "har_case_search.csv")
    fieldnames = [
        "entry_index",
        "result_index",
        "startedDateTime",
        "time_ms",
        "timings_send_ms",
        "timings_wait_ms",
        "timings_receive_ms",
        "method",
        "url",
        "http_status",
        "request_FormType",
        "request_CaseSearchNumber",
        "result_case_number",
        "result_defendant",
        "html_filename",
        "html_relpath",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"[OK] Wrote CaseSearchSearch CSV: {out_path}")


# ------------------------
# Main
# ------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract MCRO CaseSearch* data from HAR into CSV + HTML files."
    )
    ap.add_argument("--har", required=True, help="Path to HAR or HAR.GZ file")
    ap.add_argument("--out-dir", required=True, help="Output directory for CSV + HTML")
    args = ap.parse_args(argv)

    ensure_dir(args.out_dir)

    print(f"[*] Loading HAR from: {args.har}")
    har = load_har(args.har)
    entries = get_entries(har)
    print(f"[*] HAR entries: {len(entries)}")

    process_case_search_form_validation(entries, args.out_dir)
    process_case_search_details(entries, args.out_dir)
    process_case_search_search(entries, args.out_dir)

    print("[DONE] HAR extraction complete.")


if __name__ == "__main__":
    main()
