#!/usr/bin/env python3
"""
HAR_extract_form_subs.py

Sibling to MCRO_extract_html_from_har.py.

Usage:

    python3 HAR_extract_form_subs.py \
      --in /path/to/<session>/dumps/processed/<session>-redacted.har.gz \
      --out-dir /path/to/<session>/post_processing_pipeline/01__source

Extracts website form submissions from a HAR (with intact bodies) and emits
phase-1 CSVs in the EXACT schema that process_timecodes.py expects, so the
downstream timecode pipeline ingests them without modification:

    09__HAR_case_details.csv      (header-only stub; not used for form-subs)
    10__HAR_mcro_form.csv         (header-only stub; not used for form-subs)
    11__HAR_case_search.csv       (one row per form submission)
    form_html/<idx>_<host>.formsub.html               (readable parsed body)

Column meaning when source is form-subs (not MCRO):
    request_FormType         -> form engine ("cf7", "wpforms", "ninja", ...)
    request_CaseSearchNumber -> site host  ("coalfire.com")
    result_case_number       -> form id/handle if parseable, else site host
    result_defendant         -> submitter name parsed from form fields
    html_filename            -> readable per-row HTML rendering of the submission

Usage:
    python3 HAR_extract_form_subs.py \\
        --in  sessions/<X>/dumps/processed-bodies/<X>-redacted.har.gz \\
        --out-dir sessions/<X>/conversion/01__source

The HTML files contain the verbatim body (so the bytes that hash-match the
HAR's body_sha256 are recoverable on disk too) plus a friendly key/value list.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import re
import sys
from email import message_from_bytes
from email.policy import default as email_default
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote_plus, urlsplit


# --------------------------------------------------------------------- HAR I/O
def load_har(path: str) -> dict:
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- noise filters
NOISE_HOST_RE = re.compile(
    r"(google|gstatic|googleapis|doubleclick|googletagmanager|googlesyndication|"
    r"google-analytics|googleadservices|youtube|ytimg|gvt[12]|sentry|datadog|"
    r"hotjar|fullstory|segment|mixpanel|amplitude|new-relic|nr-data|braze|"
    r"optimizely|appdynamics|launchdarkly|cloudflareinsights|bugsnag|rollbar|"
    r"facebook\.com|fbcdn|linkedin\.com|adsystem|driftapi|drift\.com|"
    r"clarity\.ms|telemetry\.mozilla|incoming\.telemetry|storylane|callrail|"
    r"hs-sites\.com)\.",
    re.I,
)
NOISE_PATH_RE = re.compile(
    r"(/log\b|/csp/|/csp_report|/_/|/gen_204|/collect\b|/beacon\b|"
    r"/v1/(rum|metrics|errors)|/jserror|/track|/api/log|/error_logging|"
    r"/recaptcha|/cdn-cgi/rum|/_hcms/perf|/tr/?$)",
    re.I,
)


# ---------------------------------------------------------- body parsing utils
def parse_multipart(body: str, mime: str) -> list[tuple[str, str]]:
    raw = f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + body.encode("utf-8", errors="replace")
    msg = message_from_bytes(raw, policy=email_default)
    pairs: list[tuple[str, str]] = []
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "") or ""
        m = re.search(r'name="([^"]*)"', cd)
        if not m:
            continue
        name = m.group(1)
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = part.get_payload()
            value = "" if isinstance(payload, list) else str(payload)
        else:
            try:
                value = payload.decode("utf-8")
            except Exception:
                value = f"<{len(payload)} bytes binary>"
        pairs.append((name, value))
    return pairs


def parse_urlencoded(body: str) -> list[tuple[str, str]]:
    return list(parse_qsl(body, keep_blank_values=True))


def flatten_json(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested JSON into ['a.b.c', 'value'] pairs for field-name search."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.extend(flatten_json(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            out.extend(flatten_json(v, key))
    else:
        out.append((prefix, "" if obj is None else str(obj)))
    return out


def parse_form_body(body: str, mime: str) -> tuple[list[tuple[str, str]], str]:
    """Return (key/value pairs, kind) where kind is 'multipart'|'urlencoded'|'json'|'raw'."""
    m = (mime or "").lower()
    if "multipart/form-data" in m:
        return parse_multipart(body, mime), "multipart"
    if "x-www-form-urlencoded" in m:
        pairs = parse_urlencoded(body)
        # Some engines (Ninja Forms) wrap a JSON blob inside a `formData` value.
        merged: list[tuple[str, str]] = []
        for k, v in pairs:
            if k == "formData":
                try:
                    obj = json.loads(v)
                    merged.extend(flatten_json(obj, "formData"))
                    continue
                except Exception:
                    pass
            merged.append((k, v))
        return merged, "urlencoded"
    if "json" in m:
        try:
            obj = json.loads(body)
            return flatten_json(obj), "json"
        except Exception:
            pass
    return [], "raw"


# ------------------------------------------------------- field-pattern guesses
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

NAME_KEY_RE         = re.compile(r"(?:^|[._\[\]/-])(name|full[_\s]?name|fullname)(?:$|[._\[\]/-])", re.I)
FIRST_NAME_KEY_RE   = re.compile(r"(first[_\s]?name|fname|firstname|first|givenname|given[_\s]?name|txt[_\s_]?first|input[_\s_]?first)", re.I)
LAST_NAME_KEY_RE    = re.compile(r"(last[_\s]?name|lname|lastname|last|familyname|surname|family[_\s]?name|txt[_\s_]?last|input[_\s_]?last)", re.I)
EMAIL_KEY_RE        = re.compile(r"(email|e[\-_]?mail|mail)", re.I)
PHONE_KEY_RE        = re.compile(r"(phone|tel|mobile|cell)", re.I)
MESSAGE_KEY_RE      = re.compile(r"(message|comments?|details|inquiry|description|body)", re.I)
COMPANY_KEY_RE      = re.compile(r"(company|organization|business|firm)", re.I)


def _looks_like_name(value: str) -> bool:
    if not value:
        return False
    v = value.strip()
    if "@" in v or len(v) > 80:
        return False
    # 2+ word characters, optional comma/space/period
    return bool(re.match(r"^[A-Za-z][A-Za-z' .,-]{1,79}$", v))


def extract_submitter_name(pairs: list[tuple[str, str]]) -> Optional[str]:
    """Best-effort: try first/last name pair, then full name field, then email local-part."""
    by_key_norm: dict[str, str] = {}
    for k, v in pairs:
        v = (v or "").strip()
        if v:
            by_key_norm.setdefault(k.lower(), v)

    first = last = full = None
    for k, v in pairs:
        kl = k.lower()
        if first is None and FIRST_NAME_KEY_RE.search(kl) and _looks_like_name(v):
            first = v.strip()
        if last is None and LAST_NAME_KEY_RE.search(kl) and _looks_like_name(v):
            last = v.strip()
        if full is None and NAME_KEY_RE.search(kl) and _looks_like_name(v):
            full = v.strip()

    if first and last:
        return f"{first} {last}"
    if full:
        return full
    if first:
        return first
    if last:
        return last

    # Gravity Forms etc. sometimes use input_1 = "Matt Guertin" (just the full name)
    for k, v in pairs:
        if re.fullmatch(r"input_\d+", k.lower()) and _looks_like_name(v) and " " in v.strip():
            return v.strip()

    # Value-pattern fallback. Skip config-like keys (date-picker months,
    # settings dictionaries, locale strings) so we don't latch onto
    # "January February" or similar UI config values.
    config_key_re = re.compile(
        r"(settings|options|labels?|messages?|months?|weekdays?|"
        r"placeholders?|defaults?|locale|notif|errors?|i18n)",
        re.I,
    )
    field_pairs = [(k, (v or "").strip()) for k, v in pairs if not config_key_re.search(k)]

    # Try consecutive-singles before bare 2-words to avoid latching onto job
    # titles like "Private Investigator".
    one_word_re = re.compile(r"^[A-Z][a-z][A-Za-z'-]{0,30}$")
    for i in range(len(field_pairs) - 1):
        v1, v2 = field_pairs[i][1], field_pairs[i + 1][1]
        if v1 and v2 and one_word_re.match(v1) and one_word_re.match(v2):
            return f"{v1} {v2}"

    # Then any single field whose value is a 2–3-word capitalized name
    # (catches Ninja Forms' `formData.fields.1.value` = "Matthew Guertin").
    full_name_re = re.compile(r"^[A-Z][a-z][A-Za-z'.-]*(?:\s+[A-Z][a-z][A-Za-z'.-]*){1,2}$")
    for k, v in field_pairs:
        if v and len(v) <= 80 and full_name_re.match(v):
            return v

    # Last resort: email local-part
    for k, v in pairs:
        if EMAIL_KEY_RE.search(k.lower()):
            m = EMAIL_RE.search(v or "")
            if m:
                return m.group(0).split("@")[0]
    return None


def detect_form_type(url: str, body: str, pairs: list[tuple[str, str]]) -> str:
    u, b = url.lower(), body or ""
    keys = " ".join(k for k, _ in pairs).lower()
    # Government complaint portals + JotForm (checked before WP/CMS engines so
    # specific hosts win over generic markers).
    if "ic3.gov" in u or "ic3complaintform" in keys:
        return "ic3"
    if "reportfraud.ftc.gov" in u:
        return "ftc_reportfraud"
    if "/fcsexternal/" in u or "bo_externalcomplaint" in keys:
        return "uspis"
    if "jotform.com/submit/" in u or re.search(r"\bq\d+_", keys):
        return "jotform"
    if "/contact-form-7/" in u or "_wpcf7" in keys:
        return "cf7"
    if "wp-admin/admin-ajax.php" in u and "wpforms" in keys:
        return "wpforms"
    if "wp-admin/admin-ajax.php" in u and ("nf_ajax_submit" in b or "formData" in keys):
        return "ninja"
    if "freeform-action" in keys or "freeform/submit" in keys:
        return "freeform"
    if "frm_action" in keys or "frm_submit_entry" in keys:
        return "formidable"
    if "com_requestforhelp" in b or "com_requestforhelp" in u:
        return "joomla"
    if "hscollectedforms.net" in u or "hubspotutk" in keys.lower() or "contactfields" in keys.lower():
        return "hubspot"
    if "gform_" in keys or re.search(r"input_\d+", keys):
        return "gravity"
    return "other"


def detect_form_id(url: str, pairs: list[tuple[str, str]], form_type: str) -> Optional[str]:
    # Engine-specific shortcuts.
    if form_type == "jotform":
        m = re.search(r"jotform\.com/submit/(\d+)", url)
        if m:
            return m.group(1)
    if form_type == "ic3":
        for k, v in pairs:
            if k.upper() in ("COMPLAINT_SESSION", "COMPLAINTID"):
                return (v or "").strip() or None
    if form_type == "cf7":
        m = re.search(r"/contact-forms/(\d+)/", url)
        if m:
            return m.group(1)
        for k, v in pairs:
            if k == "_wpcf7":
                return v.strip() or None
    if form_type == "ninja":
        for k, v in pairs:
            if k.endswith(".id") and v and v.isdigit():
                return v
    if form_type == "formidable":
        for k, v in pairs:
            if k == "form_id":
                return v.strip() or None
    if form_type == "freeform":
        for k, v in pairs:
            if k == "formHash":
                return v.strip() or None
    if form_type == "wpforms":
        for k, v in pairs:
            if re.fullmatch(r"wpforms\[id\]", k) or k == "wpforms[id]":
                return v.strip() or None
        m = re.search(r"wpforms\[id\]=(\d+)", " ".join(f"{k}={v}" for k, v in pairs))
        if m:
            return m.group(1)
    return None


# ------------------------------------------------------------ form-sub filter
def is_form_submission(entry: dict) -> bool:
    req = entry.get("request") or {}
    if (req.get("method") or "").upper() != "POST":
        return False
    pd = req.get("postData") or {}
    body = pd.get("text") or ""
    if not body:
        return False
    mime = (pd.get("mimeType") or "").lower()
    is_form_ct = (
        "multipart/form-data" in mime
        or "x-www-form-urlencoded" in mime
        or "application/json" in mime
        or mime.endswith("+json")
    )
    if not is_form_ct:
        return False
    url = req.get("url") or ""
    host = urlsplit(url).netloc
    if NOISE_HOST_RE.search(host) or NOISE_PATH_RE.search(url):
        return False
    # Bodies may be URL-encoded (`%40` for `@`); decode once for the email check.
    decoded = unquote_plus(body) if "%" in body else body
    if not EMAIL_RE.search(decoded):
        return False
    return True


# ---------------------------------------------------------------- HTML render
HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Form submission &#8212; {host_html} &#8212; entry {entry_index}</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;margin:1.5em;max-width:60rem;color:#222;}}
 h1{{font-size:1.15rem;}} h2{{font-size:0.95rem;margin-top:1.5em;border-bottom:1px solid #ccc;padding-bottom:.2em;}}
 dl{{display:grid;grid-template-columns:max-content 1fr;column-gap:1.2em;row-gap:.25em;}}
 dt{{font-weight:600;color:#444;font-family:ui-monospace,Menlo,monospace;}}
 dd{{margin:0;white-space:pre-wrap;word-break:break-word;}}
 pre{{background:#f4f4f4;padding:.6em;border-radius:.3em;overflow-x:auto;font-size:.8rem;}}
 .meta{{color:#666;font-size:.85rem;}} .meta b{{color:#222;}}
</style>
</head>
<body>
<h1>Form submission &mdash; <code>{host_html}</code></h1>
<p class="meta">
  <b>HAR entry index:</b> {entry_index} &nbsp; <b>Started:</b> {started_html} &nbsp;
  <b>Form type:</b> {form_type_html} &nbsp; <b>Form id:</b> {form_id_html}<br>
  <b>Submitter:</b> {submitter_html} &nbsp; <b>HTTP status:</b> {status_html}<br>
  <b>{method_html}</b> {url_html}
</p>
<h2>Form fields (parsed)</h2>
<dl>
{fields_html}
</dl>
<h2>Raw request body</h2>
<pre>{raw_html}</pre>
{response_html}
</body>
</html>
"""


def render_html(entry_index, started, host, form_type, form_id, submitter,
                method, url, status, mime, pairs, raw_body, resp_body, resp_ct) -> str:
    if pairs:
        rows = "".join(
            f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>\n"
            for k, v in pairs
        )
    else:
        rows = "<dt>(no fields parsed)</dt><dd>see raw body</dd>"
    resp_block = ""
    if resp_body:
        resp_block = (
            f"<h2>Response body ({html.escape(resp_ct or '')})</h2>"
            f"<pre>{html.escape(resp_body[:4000])}</pre>"
        )
    return HTML_TEMPLATE.format(
        entry_index=entry_index,
        started_html=html.escape(str(started or "")),
        host_html=html.escape(host or ""),
        form_type_html=html.escape(form_type or ""),
        form_id_html=html.escape(form_id or ""),
        submitter_html=html.escape(submitter or ""),
        status_html=html.escape(str(status)),
        method_html=html.escape(method or "POST"),
        url_html=html.escape(url or ""),
        fields_html=rows,
        raw_html=html.escape(raw_body or ""),
        response_html=resp_block,
    )


# ----------------------------------------------------------------- CSV writers
HAR_CASE_SEARCH_COLS = [
    "entry_index", "result_index", "startedDateTime",
    "time_ms", "timings_send_ms", "timings_wait_ms", "timings_receive_ms",
    "method", "url", "http_status",
    "request_FormType", "request_CaseSearchNumber",
    "result_case_number", "result_defendant",
    "html_filename", "html_relpath",
]
HAR_CASE_DETAILS_COLS = [
    "entry_index", "startedDateTime",
    "time_ms", "timings_send_ms", "timings_wait_ms", "timings_receive_ms",
    "method", "url", "http_status",
    "case_number",
    "html_filename", "html_relpath",
]
HAR_MCRO_FORM_COLS = [
    "entry_index", "startedDateTime",
    "time_ms", "timings_send_ms", "timings_wait_ms", "timings_receive_ms",
    "method", "url", "http_status",
    "FormType", "CaseSearchNumber",
    "response_body_raw", "response_json_code", "response_json_message",
]


def slug(host: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (host or "").lower()).strip("-")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract form submissions from a HAR (with bodies) into "
                    "phase-1 CSVs that match MCRO's column shape."
    )
    ap.add_argument("--in", dest="infile", required=True,
                    help="Input HAR or HAR.gz with intact bodies")
    ap.add_argument("--out-dir", dest="outdir", required=True,
                    help="Output directory (typically <session>/conversion/01__source)")
    ap.add_argument("--html-subdir", default="form_html",
                    help="Subdirectory for per-submission HTML files")
    args = ap.parse_args(argv)

    har = load_har(args.infile)
    entries = har.get("log", {}).get("entries", [])
    print(f"loaded HAR: {args.infile}  ({len(entries)} entries)")

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_dir = out_dir / args.html_subdir
    html_dir.mkdir(exist_ok=True)

    rows_search: list[dict] = []
    seen = 0

    for idx, e in enumerate(entries):
        if not is_form_submission(e):
            continue
        seen += 1

        req = e["request"]; pd = req.get("postData") or {}
        url, mime, body = req["url"], (pd.get("mimeType") or ""), (pd.get("text") or "")
        host = urlsplit(url).netloc
        method = req.get("method", "POST")
        status = (e.get("response") or {}).get("status") or 0

        pairs, _kind = parse_form_body(body, mime)
        form_type = detect_form_type(url, body, pairs)
        form_id = detect_form_id(url, pairs, form_type)
        submitter = extract_submitter_name(pairs)

        # Response body (if textual) – copied for transparency only.
        resp = e.get("response") or {}
        resp_content = resp.get("content") or {}
        resp_text = resp_content.get("text") if isinstance(resp_content.get("text"), str) else None
        resp_ct = resp_content.get("mimeType")

        html_filename = f"{idx}_{slug(host)}.formsub.html"
        html_relpath = f"{args.html_subdir}/{html_filename}"
        page = render_html(
            entry_index=idx,
            started=e.get("startedDateTime"),
            host=host,
            form_type=form_type,
            form_id=form_id or "",
            submitter=submitter or "",
            method=method, url=url, status=status,
            mime=mime, pairs=pairs, raw_body=body,
            resp_body=resp_text or "", resp_ct=resp_ct or "",
        )
        (html_dir / html_filename).write_text(page, encoding="utf-8")

        timings = e.get("timings") or {}
        rows_search.append({
            "entry_index": idx,
            "result_index": 1,
            "startedDateTime": e.get("startedDateTime"),
            "time_ms": int(e.get("time") or 0),
            "timings_send_ms": int(timings.get("send") or 0),
            "timings_wait_ms": int(timings.get("wait") or 0),
            "timings_receive_ms": int(timings.get("receive") or 0),
            "method": method,
            "url": url,
            "http_status": status,
            "request_FormType": form_type,
            "request_CaseSearchNumber": host,
            "result_case_number": form_id or host,
            "result_defendant": submitter or "",
            "html_filename": html_filename,
            "html_relpath": html_relpath,
        })

    print(f"matched form submissions: {seen}")

    # 11__HAR_case_search.csv  (where form-sub rows live)
    p11 = out_dir / "11__HAR_case_search.csv"
    with p11.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HAR_CASE_SEARCH_COLS)
        w.writeheader()
        for r in rows_search:
            w.writerow({c: r.get(c, "") for c in HAR_CASE_SEARCH_COLS})

    # 09__HAR_case_details.csv  (header-only stub)
    p09 = out_dir / "09__HAR_case_details.csv"
    with p09.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=HAR_CASE_DETAILS_COLS).writeheader()

    # 10__HAR_mcro_form.csv  (header-only stub)
    p10 = out_dir / "10__HAR_mcro_form.csv"
    with p10.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=HAR_MCRO_FORM_COLS).writeheader()

    print(f"wrote: {p09}")
    print(f"wrote: {p10}")
    print(f"wrote: {p11}  ({len(rows_search)} rows)")
    print(f"wrote: {html_dir}/  ({seen} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
