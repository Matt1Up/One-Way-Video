#!/usr/bin/env python3
"""
httpstream_json.py — mitmproxy addon that writes BOTH compact and detailed HTTP flow events
into a shared JSON state file.

Defaults updated for portability:
- Uses your repo's run/state.json and run/state.lock by default (via evidence_capture.paths)
- Still honors env overrides:
    STATE_JSON_FILE, STATE_LOCK_FILE, HTTP_FIFO_MAX, HTTP_EVENTS_MAX, HTTP_BODY_HASH_MAX_MB

Run:
  mitmproxy -s httpstream_json.py
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

from mitmproxy import ctx, http
import os, hashlib, math
from pathlib import Path
from datetime import datetime, timezone

# ---------- Repo-anchored defaults (overridable via env) ----------
from evidence_capture.paths import RUN
from evidence_capture.state import json_update_locked
from evidence_capture.timeutil import now_utc_iso

STATE_JSON = Path(os.path.expanduser(os.environ.get("STATE_JSON_FILE", str(RUN / "state.json"))))
STATE_LOCK = Path(os.path.expanduser(os.environ.get("STATE_LOCK_FILE", str(RUN / "state.lock"))))
MAX_COMPACT   = int(os.environ.get("HTTP_FIFO_MAX", "5"))
MAX_DETAILED  = int(os.environ.get("HTTP_EVENTS_MAX", "2000"))
BODY_HASH_MB  = float(os.environ.get("HTTP_BODY_HASH_MAX_MB", "0"))  # 0 = hash all bodies (provenance: no cap)

# When the request/response is a form-style submission, also embed the body
# text directly into the http_events record so it is anchored to the hash-
# locked bundle chain (not just its sha256). Gated by content-type and size
# so we never pour binary blobs or huge JSON dumps into state.json.
FORM_BODY_CONTENT_TYPES = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "application/json",          # most non-MCRO form engines (HubSpot, etc.)
    "application/csp-report",    # excluded below by host filter
)
FORM_BODY_MAX_BYTES = int(os.environ.get("HTTP_FORM_BODY_MAX_BYTES", "10485760"))  # 10 MB

STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)

# ---------- Time & hashing helpers ----------
def now_iso() -> str:
    return now_utc_iso()

def _sha256_or_none(raw: bytes | None):
    if raw is None:
        return None
    if BODY_HASH_MB > 0 and len(raw) > BODY_HASH_MB * 1024 * 1024:
        return None
    return hashlib.sha256(raw).hexdigest()

def _maybe_body_text(raw: bytes | None, content_type: str | None) -> str | None:
    """Return decoded body text iff content-type is form-like and size is sane."""
    if not raw:
        return None
    ct = (content_type or "").lower()
    if not any(s in ct for s in FORM_BODY_CONTENT_TYPES):
        return None
    if FORM_BODY_MAX_BYTES > 0 and len(raw) > FORM_BODY_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None

def _ts_from_float(f: float | None):
    if f is None or (isinstance(f, float) and (math.isnan(f) or math.isinf(f))):
        return None
    try:
        return datetime.fromtimestamp(float(f), tz=timezone.utc).isoformat()
    except Exception:
        return None

# ---------- state.json I/O (via shared module) ----------
def _append_compact(url: str):
    """streams.http: prepend compact row, cap, bump http_index."""
    def _update(st):
        streams = st.get("streams") or {}
        lst = streams.get("http") or []
        idx = int(streams.get("http_index", 0)) + 1
        item = {"url": url, "ts": now_iso(), "idx": idx}
        streams["http"] = [item] + lst[: max(0, MAX_COMPACT - 1)]
        streams["http_index"] = idx
        st["streams"] = streams
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)

def _append_detailed(ev: dict):
    """streams.http_events: prepend detailed flow record, cap, bump http_events_index."""
    def _update(st):
        streams = st.get("streams") or {}
        lst = streams.get("http_events") or []
        idx = int(streams.get("http_events_index", 0)) + 1
        ev["idx"] = idx
        streams["http_events"] = [ev] + lst[: max(0, MAX_DETAILED - 1)]
        streams["http_events_index"] = idx
        st["streams"] = streams
        return st
    json_update_locked(STATE_JSON, STATE_LOCK, _update)

# ---------- connection/TLS helpers ----------
def _addr_tuple(conn):
    # Prefer peername; fall back to address
    try:
        pn = getattr(conn, "peername", None)
        if isinstance(pn, (list, tuple)) and len(pn) >= 2:
            return [pn[0], int(pn[1])]
    except Exception:
        pass
    try:
        addr = getattr(conn, "address", None)
        if isinstance(addr, (list, tuple)) and len(addr) >= 2:
            return [addr[0], int(addr[1])]
    except Exception:
        pass
    return None

def _tls_info(conn):
    """Return {'version','cipher','cert_sha256':[...]} if TLS present, else None."""
    try:
        if not getattr(conn, "tls_established", False):
            ver = getattr(conn, "tls_version", None)
            ciph = getattr(conn, "cipher", None)
            if not (ver or ciph):
                return None
        ver   = getattr(conn, "tls_version", None)
        ciph  = getattr(conn, "cipher", None)
        chain_hashes = []
        cert_list = getattr(conn, "certificate_list", None)
        if cert_list:
            for c in cert_list:
                try:
                    der = c.to_der() if hasattr(c, "to_der") else None
                    if der is None and hasattr(c, "to_pem"):
                        der = c.to_pem().encode("utf-8")
                    if der:
                        chain_hashes.append(hashlib.sha256(der).hexdigest())
                except Exception:
                    continue
        else:
            cert = getattr(conn, "certificate", None)
            if cert:
                try:
                    der = cert.to_der() if hasattr(cert, "to_der") else None
                    if der is None and hasattr(cert, "to_pem"):
                        der = cert.to_pem().encode("utf-8")
                    if der:
                        chain_hashes.append(hashlib.sha256(der).hexdigest())
                except Exception:
                    pass
        if ver or ciph or chain_hashes:
            return {"version": ver, "cipher": ciph, "cert_sha256": (chain_hashes or None)}
    except Exception:
        pass
    return None

# ---------- event builder ----------
def build_flow_event(flow: http.HTTPFlow) -> dict:
    """
    Build a detailed per-flow record:
      - flow_id (UUID)
      - timings: startedDateTime, time_ms
      - client/server addr, SNI
      - TLS: version, cipher, cert chain digests
      - request: method, url, http_version, body sha256 (optional if large)
      - response: status, http_version, body sha256 (optional if large)
    """
    try:
        rid = getattr(flow, "id", None)

        req = flow.request
        resp = flow.response
        cc = flow.client_conn
        sc = flow.server_conn

        # timings
        t_start = getattr(req, "timestamp_start", None)
        t_end   = getattr(resp, "timestamp_end", None) if resp else None
        started = _ts_from_float(t_start)
        time_ms = 0
        try:
            if t_start is not None and t_end is not None:
                dt = max(0.0, float(t_end) - float(t_start))
                time_ms = int(dt * 1000)
        except Exception:
            pass

        # conn meta
        client = _addr_tuple(cc)
        server = _addr_tuple(sc)
        sni    = getattr(sc, "sni", None)
        tls    = _tls_info(sc)

        # request
        req_raw = getattr(req, "raw_content", None)
        req_ct = None
        try:
            if getattr(req, "headers", None):
                req_ct = req.headers.get("Content-Type")
        except Exception:
            req_ct = None
        req_obj = {
            "method": getattr(req, "method", None),
            "url":    getattr(req, "pretty_url", None) or getattr(req, "url", None),
            "http_version": getattr(req, "http_version", None),
            "body_sha256": _sha256_or_none(req_raw),
        }
        req_btxt = _maybe_body_text(req_raw, req_ct)
        if req_btxt is not None:
            req_obj["body_text"] = req_btxt
            req_obj["body_content_type"] = req_ct

        # response (may be absent on error)
        resp_obj = None
        if resp:
            r_raw = getattr(resp, "raw_content", None)
            resp_ct = None
            try:
                if getattr(resp, "headers", None):
                    resp_ct = resp.headers.get("Content-Type")
            except Exception:
                resp_ct = None
            resp_obj = {
                "status": getattr(resp, "status_code", None),
                "http_version": getattr(resp, "http_version", None),
                "body_sha256": _sha256_or_none(r_raw),
            }
            # Only embed response body text when the request itself was a form
            # submission — the goal is to capture the form's success/error reply,
            # not arbitrary JSON responses from page loads.
            if req_btxt is not None:
                resp_btxt = _maybe_body_text(r_raw, resp_ct)
                if resp_btxt is not None:
                    resp_obj["body_text"] = resp_btxt
                    resp_obj["body_content_type"] = resp_ct

        return {
            "flow_id": rid,
            "startedDateTime": started,
            "time_ms": time_ms,
            "client": client,
            "server": server,
            "sni": sni,
            "tls": tls,
            "req": req_obj,
            "resp": resp_obj,
        }
    except Exception as e:
        return {"error": f"build_flow_event failed: {e}", "ts": now_iso()}

# ---------- mitmproxy addon ----------
class HttpToStateJSON:
    def __init__(self, max_compact: int = MAX_COMPACT, max_detailed: int = MAX_DETAILED):
        self.max_compact = int(max_compact)
        self.max_detailed = int(max_detailed)
        ctx.log.info(
            f"httpstream_json: state={STATE_JSON} lock={STATE_LOCK} "
            f"compact={self.max_compact} detailed={self.max_detailed} body_hash_mb={BODY_HASH_MB}"
        )

    # Compact URL row as soon as we see the request (keeps HUD snappy)
    def request(self, flow: http.HTTPFlow) -> None:
        try:
            url = getattr(flow.request, "pretty_url", None) or flow.request.url
            _append_compact(url)
        except Exception as e:
            ctx.log.warn(f"httpstream_json: compact capture exception: {e}")

    # Detailed record when response arrives (includes flow UUID, TLS, body digests)
    def response(self, flow: http.HTTPFlow) -> None:
        try:
            ev = build_flow_event(flow)
            _append_detailed(ev)
        except Exception as e:
            ctx.log.warn(f"httpstream_json: detailed capture exception: {e}")

    # Also record failures with at least the request + error message
    def error(self, flow: http.HTTPFlow) -> None:
        try:
            ev = build_flow_event(flow)
            err = getattr(flow, "error", None)
            if err:
                ev["resp"] = None
                ev["error"] = getattr(err, "msg", str(err))
            _append_detailed(ev)
        except Exception as e:
            ctx.log.warn(f"httpstream_json: error capture exception: {e}")

addons = [HttpToStateJSON()]
