#!/usr/bin/env python3
"""
Final dump_to_redacted_har_sanitized.py (bodies EXCLUDED by default)

- Flags: --in, --out-dir, --include-bodies (new), --keep-bodies-hash (same as before)
- Outputs:
    <base>-redacted.har.gz (GZ-compressed HAR; bodies excluded by default)
    <base>.events.json (compact events array)
    <base>.meta.json (metadata with original dump sha256 and har.gz sha256)
- Keeps: NaN-safe numeric sanitization, TLS/cert fingerprints, timezone-aware timestamps
- If --include-bodies is used, bodies are added with JSON-aware + heuristic redaction.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, parse_qsl

# Try to import mitmproxy FlowReader
try:
    from mitmproxy.io import FlowReader
    from mitmproxy import version as mitm_version
except Exception:
    FlowReader = None
    mitm_version = None

# -------------------------
# Redaction tokens + regexes
# -------------------------
DEFAULT_SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-csrf-token", "x-xsrf-token", "x-api-key",
    "x-amz-security-token", "x-aws-security-token"
}

DEFAULT_SENSITIVE_QS = {
    "access_token", "token", "auth", "sig", "pathsig",
    "session", "sessionid", "session_id", "jwt",
    "api_key", "apikey", "key", "password"
}

# regexes
JWT_RE = re.compile(r'([A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+)')
BASE64_LONG_RE = re.compile(r'[A-Za-z0-9+/=]{80,}')
EMAIL_RE = re.compile(r'[A-Za-z0-9.\-_+%]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
CC_RE = re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b')
SSN_RE = re.compile(r'\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b')
PHONE_RE = re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')

# -------------------------
# Helpers
# -------------------------
def sha256_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_b64_prefix(b: bytes, n: int = 512) -> str:
    try:
        return base64.b64encode(b[:n]).decode("ascii", errors="ignore")
    except Exception:
        return ""

def try_getattr(obj: Any, *attrs):
    for a in attrs:
        try:
            if obj is None:
                continue
            v = getattr(obj, a)
            if v is not None:
                return v
        except Exception:
            continue
    return None

def pem_bytes_from_cert_obj(obj) -> Optional[bytes]:
    if obj is None:
        return None
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, str) and obj.startswith("-----BEGIN"):
        return obj.encode("utf-8")
    for cand in ("to_pem", "pem", "as_pem"):
        meth = getattr(obj, cand, None)
        if callable(meth):
            try:
                val = meth()
                if isinstance(val, bytes):
                    return val
                if isinstance(val, str):
                    return val.encode("utf-8")
            except Exception:
                pass
        elif isinstance(meth, (bytes, str)):
            return meth if isinstance(meth, bytes) else meth.encode("utf-8")
    if hasattr(obj, "public_bytes"):
        try:
            pb = obj.public_bytes()
            if isinstance(pb, bytes):
                return pb
        except Exception:
            pass
    return None

# -------------------------
# Body sanitation utilities
# -------------------------
def redact_heuristic_text(s: str) -> str:
    """Apply heuristic regex redaction to a text blob (JWTs, long base64, emails, CC, SSN, phones)."""
    if not s:
        return s
    s = JWT_RE.sub("[REDACTED_JWT]", s)
    s = BASE64_LONG_RE.sub("[REDACTED_BASE64]", s)
    s = EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    s = CC_RE.sub("[REDACTED_CC]", s)
    s = SSN_RE.sub("[REDACTED_SSN]", s)
    s = PHONE_RE.sub("[REDACTED_PHONE]", s)
    return s

def redact_json_obj(obj: Any, redact_keys: set) -> Any:
    """Recursively redact values in a JSON object for keys in redact_keys (case-insensitive contained)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lower = k.lower()
            if any(tok in lower for tok in redact_keys):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_json_obj(v, redact_keys)
        return out
    if isinstance(obj, list):
        return [redact_json_obj(i, redact_keys) for i in obj]
    if isinstance(obj, str):
        return redact_heuristic_text(obj)
    return obj

def sanitize_body(content_bytes: Optional[bytes], content_type: str, redact_keys: set, no_body_sanitize: bool = False) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Return: (text_or_b64, encoding_flag_or_none, length)
    - If content_type implies text or JSON, return sanitized UTF-8 text.
    - If binary, return base64-encoded string and encoding='base64'.
    - Also returns the raw byte length.

    When no_body_sanitize=True, text bodies are returned verbatim (no PHONE/
    EMAIL/BASE64 substitution, no JSON-key redaction). Used for evidence
    capture, where the submitted form values ARE the artifact. Binary bodies
    are still base64-encoded the same way (that's an encoding, not redaction),
    and header/query-string redactors run independently.
    """
    if content_bytes is None:
        return None, None, None
    length = len(content_bytes)
    # Guess text if content-type contains 'json' or 'text' or 'xml' or 'javascript'
    ct = (content_type or "").lower()
    is_json = "json" in ct or ct.endswith("+json")
    is_text = ct.startswith("text/") or "xml" in ct or "javascript" in ct or "html" in ct
    # Try decode as UTF-8
    text = None
    try:
        text = content_bytes.decode("utf-8")
    except Exception:
        try:
            text = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = None

    if is_json and text is not None:
        try:
            obj = json.loads(text)
            if no_body_sanitize:
                out_text = json.dumps(obj, ensure_ascii=False)
            else:
                redacted_obj = redact_json_obj(obj, redact_keys)
                out_text = json.dumps(redacted_obj, ensure_ascii=False)
                out_text = redact_heuristic_text(out_text)
            return out_text, None, length
        except Exception:
            pass

    if (is_text or text is not None) and text is not None:
        out_text = text if no_body_sanitize else redact_heuristic_text(text)
        return out_text, None, length

    try:
        b64 = base64.b64encode(content_bytes).decode("ascii")
        if len(b64) > 200000:
            prefix = b64[:200000] + "...[TRUNCATED_BASE64]..."
            return prefix, "base64", length
        return b64, "base64", length
    except Exception:
        return None, None, length

# -------------------------
# Numeric sanitization (prevent NaN)
# -------------------------
def coerce_int_safe(v: Any, default: int) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return int(v)
    if isinstance(v, str):
        try:
            fv = float(v)
            if fv != fv or fv in (float("inf"), float("-inf")):
                return default
            return int(fv)
        except Exception:
            return default
    return default

def sanitize_har_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for e in entries:
        e["time"] = coerce_int_safe(e.get("time", 0), 0)
        timings = e.get("timings", {}) or {}
        timings["send"] = coerce_int_safe(timings.get("send", 0), 0)
        timings["wait"] = coerce_int_safe(timings.get("wait", e["time"]), 0)
        timings["receive"] = coerce_int_safe(timings.get("receive", 0), 0)
        e["timings"] = timings
        req = e.get("request", {}) or {}
        req["headersSize"] = coerce_int_safe(req.get("headersSize", -1), -1)
        req["bodySize"] = coerce_int_safe(req.get("bodySize", -1), -1)
        e["request"] = req
        resp = e.get("response", {}) or {}
        resp["headersSize"] = coerce_int_safe(resp.get("headersSize", -1), -1)
        resp["bodySize"] = coerce_int_safe(resp.get("bodySize", -1), -1)
        content = resp.get("content", {}) or {}
        content["size"] = coerce_int_safe(content.get("size", 0), 0)
        resp["content"] = content
        e["response"] = resp
    return entries

# -------------------------
# Flow -> HAR entry + event
# -------------------------
def make_har_entry_and_event(flow: Any, keep_bodies_hash: bool = False, include_bodies: bool = False, no_body_sanitize: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    fid = try_getattr(flow, "id", "flow_id") or ""

    started = try_getattr(flow, "timestamp_start", "start_time")
    ended = try_getattr(flow, "timestamp_end", "end_time")
    started_iso = None
    if started:
        try:
            started_iso = datetime.fromtimestamp(float(started), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            started_iso = None
    time_ms = 0
    try:
        if started and ended:
            time_ms = int((float(ended) - float(started)) * 1000.0)
    except Exception:
        time_ms = 0

    cc = try_getattr(flow, "client_conn", "client_connection", "client")
    sc = try_getattr(flow, "server_conn", "server_connection", "server")
    client_addr = try_getattr(cc, "peername", "address", "sockname") if cc else None
    server_addr = try_getattr(sc, "peername", "address", "sockname") if sc else None

    req = try_getattr(flow, "request") or {}
    req_method = try_getattr(req, "method", "get_method") or ""
    req_url = try_getattr(req, "url", "pretty_url", "path") or ""
    http_version = try_getattr(req, "http_version", "httpversion") or "HTTP/1.1"
    raw_req_headers = try_getattr(req, "headers")
    req_headers_list = []
    if raw_req_headers:
        try:
            if hasattr(raw_req_headers, "items"):
                try:
                    for k, v in raw_req_headers.items(multi=True):
                        req_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
                except TypeError:
                    for k, v in raw_req_headers.items():
                        req_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
            elif isinstance(raw_req_headers, dict):
                for k, v in raw_req_headers.items():
                    req_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
            else:
                for k, v in raw_req_headers:
                    req_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
        except Exception:
            pass

    # querystring redaction & rebuild URL for display
    def redact_qs_value(k: str, v: str) -> str:
        if k.lower() in DEFAULT_SENSITIVE_QS:
            return "[REDACTED]"
        return redact_heuristic_text(v)

    query_arr = []
    try:
        parts = urlsplit(req_url)
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            query_arr.append({"name": k, "value": redact_qs_value(k, v)})
        if parts.query:
            red_pairs = [f"{k}={redact_qs_value(k,v)}" for k,v in parse_qsl(parts.query, keep_blank_values=True)]
            req_url = parts._replace(query="&".join(red_pairs)).geturl()
    except Exception:
        pass

    # Request body (only include text if include_bodies=True)
    req_body_sha = None
    req_body_len = -1
    req_body_prefix = None
    postData = None
    try:
        raw_req_content = try_getattr(req, "content", "raw_content", "text", None)
        if raw_req_content is not None:
            raw_bytes = raw_req_content.encode("utf-8") if isinstance(raw_req_content, str) else raw_req_content
            ctype = ""
            if hasattr(req, "headers") and getattr(req, "headers", None):
                try:
                    ctype = req.headers.get("Content-Type") if hasattr(req.headers, "get") else ""
                except Exception:
                    ctype = ""
            if include_bodies:
                sanitized_text, encoding_flag, length = sanitize_body(raw_bytes, ctype or "", DEFAULT_SENSITIVE_QS, no_body_sanitize=no_body_sanitize)
                req_body_len = length if length is not None else -1
                pd = {"mimeType": ctype or "", "text": sanitized_text}
                if encoding_flag:
                    pd["encoding"] = encoding_flag
                postData = pd
            else:
                # still compute size for bodySize
                req_body_len = len(raw_bytes)
            if keep_bodies_hash and raw_bytes is not None:
                req_body_sha = sha256_of_bytes(raw_bytes)
                req_body_prefix = safe_b64_prefix(raw_bytes, 512)
    except Exception:
        postData = None

    # Response
    resp = try_getattr(flow, "response") or {}
    resp_status = try_getattr(resp, "status_code", "status") or 0
    resp_reason = try_getattr(resp, "reason") or ""
    resp_version = try_getattr(resp, "http_version", "httpversion") or "HTTP/1.1"
    raw_resp_headers = try_getattr(resp, "headers")
    resp_headers_list = []
    if raw_resp_headers:
        try:
            if hasattr(raw_resp_headers, "items"):
                try:
                    for k, v in raw_resp_headers.items(multi=True):
                        resp_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
                except TypeError:
                    for k, v in raw_resp_headers.items():
                        resp_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
            elif isinstance(raw_resp_headers, dict):
                for k, v in raw_resp_headers.items():
                    resp_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
            else:
                for k, v in raw_resp_headers:
                    resp_headers_list.append({"name": k, "value": redact_heuristic_text(v) if k.lower() not in DEFAULT_SENSITIVE_HEADERS else "[REDACTED]"})
        except Exception:
            pass

    resp_body_sha = None
    resp_body_len = -1
    resp_body_prefix = None
    content_obj = {"size": 0, "mimeType": ""}
    try:
        raw_resp_content = try_getattr(resp, "content", "raw_content", "text", None)
        if raw_resp_content is not None:
            raw_bytes = raw_resp_content.encode("utf-8") if isinstance(raw_resp_content, str) else raw_resp_content
            ctype = ""
            if hasattr(resp, "headers") and getattr(resp, "headers", None):
                try:
                    ctype = resp.headers.get("Content-Type") if hasattr(resp.headers, "get") else ""
                except Exception:
                    ctype = ""
            if include_bodies:
                sanitized_text, encoding_flag, length = sanitize_body(raw_bytes, ctype or "", DEFAULT_SENSITIVE_QS, no_body_sanitize=no_body_sanitize)
                resp_body_len = length if length is not None else -1
                content_obj = {"size": resp_body_len or 0, "mimeType": ctype or "", "text": sanitized_text}
                if encoding_flag:
                    content_obj["encoding"] = encoding_flag
            else:
                resp_body_len = len(raw_bytes)
                content_obj = {"size": resp_body_len or 0, "mimeType": ctype or ""}
            if keep_bodies_hash and raw_bytes is not None:
                resp_body_sha = sha256_of_bytes(raw_bytes)
                resp_body_prefix = safe_b64_prefix(raw_bytes, 512)
    except Exception:
        content_obj = {"size": 0, "mimeType": ""}

    # TLS enrichment
    tls_info = {}
    try:
        if sc:
            tls_info["tls_established"] = bool(try_getattr(sc, "tls_established", "ssl_established", False))
            sni = try_getattr(sc, "sni", "servername", "server_name")
            if sni:
                tls_info["sni"] = sni
            cipher = try_getattr(sc, "cipher", "tls_cipher", "cipher_suite", "tls_cipher_name")
            if cipher:
                tls_info["cipher"] = getattr(cipher, "name", str(cipher))
            version = try_getattr(sc, "tls_version", "ssl_version", "version")
            if version:
                tls_info["tls_version"] = str(version)
            cert_sha_list = []
            chain = try_getattr(sc, "certificate_list", "cert_chain", "certificate_chain", "chain", "certificates")
            if chain:
                try:
                    for c in chain:
                        pem = pem_bytes_from_cert_obj(c)
                        if pem:
                            cert_sha_list.append(sha256_of_bytes(pem))
                except Exception:
                    try:
                        pem = pem_bytes_from_cert_obj(chain)
                        if pem:
                            cert_sha_list.append(sha256_of_bytes(pem))
                    except Exception:
                        pass
            else:
                leaf = try_getattr(sc, "peer_cert", "server_cert", "server_certificate", None)
                if leaf:
                    pem = pem_bytes_from_cert_obj(leaf)
                    if pem:
                        cert_sha_list.append(sha256_of_bytes(pem))
            if cert_sha_list:
                tls_info["cert_sha256"] = cert_sha_list
    except Exception:
        tls_info = {}

    # Build HAR entry
    entry: Dict[str, Any] = {
        "connection": fid,
        "startedDateTime": started_iso,
        "time": time_ms,
        "request": {
            "method": req_method or "",
            "url": req_url,
            "httpVersion": str(http_version or "HTTP/1.1"),
            "cookies": [],
            "headers": req_headers_list,
            "queryString": query_arr,
            "headersSize": -1,
            "bodySize": req_body_len,
            "postData": postData,            # only present if include_bodies=True
            "body_sha256": req_body_sha,     # present if --keep-bodies-hash
            "body_prefix_b64": req_body_prefix
        },
        "response": {
            "status": int(resp_status) if resp_status else 0,
            "statusText": str(resp_reason or ""),
            "httpVersion": str(resp_version or "HTTP/1.1"),
            "cookies": [],
            "headers": resp_headers_list,
            "content": content_obj,          # content.text only present if include_bodies=True
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": resp_body_len
        },
        "cache": {},
        "timings": {
            "send": 0,
            "wait": time_ms,
            "receive": 0
        },
        "serverIPAddress": server_addr[0] if isinstance(server_addr, (list, tuple)) and server_addr else None,
        "client": {"address": client_addr},
        "server": {"address": server_addr}
    }
    if tls_info:
        entry["tls"] = tls_info

    # Event
    event: Dict[str, Any] = {
        "flow_id": fid,
        "startedDateTime": started_iso,
        "time_ms": time_ms,
        "client": client_addr,
        "server": server_addr,
        "sni": tls_info.get("sni") if tls_info else None,
        "tls": {
            "version": tls_info.get("tls_version") if tls_info else None,
            "cipher": tls_info.get("cipher") if tls_info else None,
            "cert_sha256": tls_info.get("cert_sha256") if tls_info else None
        } if tls_info else None,
        "req": {"method": req_method or "", "url": req_url, "body_sha256": req_body_sha},
        "resp": {"status": int(resp_status) if resp_status else 0, "body_sha256": resp_body_sha}
    }

    return entry, event

# -------------------------
# Conversion driver
# -------------------------
def convert_dump_to_redacted_har(dump_path: str, out_dir: str, keep_bodies_hash: bool, include_bodies: bool, no_body_sanitize: bool = False) -> Tuple[str, str, str]:
    os.makedirs(out_dir, exist_ok=True)
    dump_path_abs = os.path.abspath(dump_path)
    base = os.path.splitext(os.path.basename(dump_path_abs))[0]
    redacted_name = f"{base}-redacted.har.gz"
    events_name = f"{base}.events.json"
    meta_name = f"{base}.meta.json"

    # sha256 of original dump
    print(f"Computing SHA256 of original dump: {dump_path_abs}")
    original_sha = sha256_of_file(dump_path_abs)

    entries: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    count = 0

    print("Reading flows from dump...")
    with open(dump_path_abs, "rb") as fh:
        fr = FlowReader(fh)
        for flow in fr.stream():
            try:
                ent, ev = make_har_entry_and_event(flow, keep_bodies_hash=keep_bodies_hash, include_bodies=include_bodies, no_body_sanitize=no_body_sanitize)
                entries.append(ent)
                events.append(ev)
                count += 1
            except Exception as e:
                print(f"WARN: failed to convert flow: {e}")

    print(f"Built {count} entries; sanitizing numeric fields...")
    entries = sanitize_har_entries(entries)

    har = {"log": {"version": "1.2",
                   "creator": {"name": "mitmproxy-redactor-sanitized", "version": getattr(mitm_version, "short", str(mitm_version) if mitm_version else "unknown")},
                   "pages": [],
                   "entries": entries
                   }}

    redacted_path = os.path.join(out_dir, redacted_name)
    print(f"Writing HAR.gz to {redacted_path} ...")
    with gzip.open(redacted_path, "wt", encoding="utf-8") as gz:
        json.dump(har, gz, ensure_ascii=False)

    # compute sha256 of har.gz
    redacted_sha = sha256_of_file(redacted_path)

    # write events JSON
    events_path = os.path.join(out_dir, events_name)
    with open(events_path, "w", encoding="utf-8") as evf:
        json.dump(events, evf, ensure_ascii=False, indent=2)

    # write metadata with filenames only
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_dump": os.path.basename(dump_path_abs),
        "source_dump_sha256": original_sha,
        "redacted_har_gz": os.path.basename(redacted_path),
        "redacted_har_gz_sha256": redacted_sha,
        "events_filename": os.path.basename(events_path),
        "flow_count": count,
        "flags": {"keep_bodies_hash": keep_bodies_hash, "include_bodies": include_bodies, "no_body_sanitize": no_body_sanitize},
        "tool": {"name": "mitmproxy-redactor-sanitized", "version": getattr(mitm_version, "short", str(mitm_version) if mitm_version else "unknown")}
    }
    meta_path = os.path.join(out_dir, meta_name)
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(meta, mf, ensure_ascii=False, indent=2)

    print("Conversion complete.")
    return redacted_path, events_path, meta_path

# -------------------------
# CLI
# -------------------------
def main(argv):
    if FlowReader is None:
        print("ERROR: mitmproxy package not importable in this environment. Run where mitmproxy is installed.", file=sys.stderr)
        sys.exit(2)

    ap = argparse.ArgumentParser(
        description="Convert mitmproxy dump -> redacted HAR.gz + events.json + meta.json (bodies EXCLUDED by default; use --include-bodies to add sanitized bodies)"
    )
    ap.add_argument("--in", dest="infile", required=True, help="Path to mitmproxy dump file")
    ap.add_argument("--out-dir", dest="outdir", required=True, help="Output directory")
    ap.add_argument("--keep-bodies-hash", dest="keep_bodies_hash", action="store_true",
                    help="Compute SHA256 of request/response bodies and include them")
    ap.add_argument("--include-bodies", dest="include_bodies", action="store_true",
                    help="Include sanitized request/response bodies in the HAR")
    ap.add_argument("--no-body-sanitize", dest="no_body_sanitize", action="store_true",
                    help="Skip PHONE/EMAIL/JWT/BASE64 substitution AND JSON-key redaction on request/response bodies. "
                         "Headers and query strings are still redacted. Use this for evidence capture where the "
                         "submitted form values themselves are the artifact. No-op without --include-bodies.")
    args = ap.parse_args(argv)

    convert_dump_to_redacted_har(args.infile, args.outdir, args.keep_bodies_hash, args.include_bodies, no_body_sanitize=args.no_body_sanitize)

if __name__ == "__main__":
    main(sys.argv[1:])
