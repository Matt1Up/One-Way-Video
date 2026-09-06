#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sign PDF files with an invisible, document-wide digital signature.

Used by download_watcher_json.py to seal each captured download into an
"evidence vault" PDF; can also be run by hand on a file or a folder.

Behavior:
  • Invisible signature (no visible field), full-document, MDP=no changes, SHA-256
  • Incremental append (original bytes + signature), so the original is preserved
  • Single PDF (--pdf) or directory (--in-dir), optional --recursive
  • Include / exclude globs, output folder/suffix/overwrite, PEM or PKCS#12

Credentials are resolved in this order:
  1) CLI flags: --key-pem/--cert-pem or --p12/--p12-pass
  2) Environment: EVCAP_KEY_PEM / EVCAP_CERT_PEM / EVCAP_P12 / EVCAP_P12_PASS
  3) evidence_capture.config: SIGN_KEY_PEM / SIGN_CERT_PEM / SIGN_P12 / SIGN_P12_PASS
"""

import argparse
import fnmatch
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# Optional: pull defaults from evidence_capture.config if present
try:
    # --- portable import bootstrap (find evidence_capture from anywhere) ---
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    # ----------------------------------------------------------------------
    from evidence_capture import config as _evcfg  # may define SIGN_* defaults
except Exception:
    _evcfg = None

# Signing libs
from endesive.pdf import cms
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
try:
    from cryptography.hazmat.primitives.serialization import pkcs12
    HAVE_PKCS12 = True
except Exception:
    HAVE_PKCS12 = False

# ===== Last-resort fallbacks (empty; set env vars or evidence_capture.config) =====
_HARDCODE_KEY  = ""
_HARDCODE_CERT = ""
_HARDCODE_P12  = ""

# ===== Final defaults resolved with precedence: ENV → config.py → hard-coded =====
DEFAULT_KEY_PEM  = os.environ.get("EVCAP_KEY_PEM")  or (getattr(_evcfg, "SIGN_KEY_PEM",  None) or _HARDCODE_KEY)
DEFAULT_CERT_PEM = os.environ.get("EVCAP_CERT_PEM") or (getattr(_evcfg, "SIGN_CERT_PEM", None) or _HARDCODE_CERT)
DEFAULT_P12      = os.environ.get("EVCAP_P12")      or (getattr(_evcfg, "SIGN_P12",      None) or _HARDCODE_P12)
DEFAULT_P12_PASS = os.environ.get("EVCAP_P12_PASS") or getattr(_evcfg, "SIGN_P12_PASS", None)

def die(msg: str, code: int = 1):
    print(f"❌ {msg}")
    sys.exit(code)

def utc_pdf_date() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")

def collect_pdfs(in_dir: Path, recursive: bool, includes, excludes):
    candidates = [p for p in (in_dir.rglob("*.pdf") if recursive else in_dir.glob("*.pdf")) if p.is_file()]

    def match_any(name: str, patterns):
        return any(fnmatch.fnmatch(name, pat) for pat in patterns)

    out = []
    for p in candidates:
        name = p.name
        if includes and not match_any(name, includes):
            continue
        if excludes and match_any(name, excludes):
            continue
        out.append(p)

    out.sort(key=lambda p: str(p).lower())
    return out

def load_credentials(args):
    """
    Returns (key_obj, cert_obj, chain_list)
    Priority when CLI flags are NOT used:
      1) PEM pair (env/config/hardcoded)
      2) PKCS#12 (env/config/hardcoded)
         - If PKCS#12 fails due to bad password/data, fall back to PEM.
    """

    # 1) Explicit CLI choice wins
    if args.p12:
        return _load_p12(args.p12, args.p12_pass)
    if args.key_pem and args.cert_pem:
        return _load_pem(args.key_pem, args.cert_pem)

    # 2) Defaults (prefer PEM first)
    pem_key  = (DEFAULT_KEY_PEM or "").strip()
    pem_cert = (DEFAULT_CERT_PEM or "").strip()
    p12_path = (DEFAULT_P12 or "").strip()

    # Prefer PEM if both exist
    if pem_key and pem_cert and Path(pem_key).expanduser().is_file() and Path(pem_cert).expanduser().is_file():
        return _load_pem(pem_key, pem_cert)

    # Otherwise try PKCS#12 (and fall back to PEM on password/data errors)
    if p12_path and Path(p12_path).expanduser().is_file():
        try:
            return _load_p12(p12_path, DEFAULT_P12_PASS)
        except ValueError:
            # Bad/missing password or invalid p12 -> try PEM before giving up
            if pem_key and pem_cert and Path(pem_key).expanduser().is_file() and Path(pem_cert).expanduser().is_file():
                return _load_pem(pem_key, pem_cert)
            raise  # no PEM to fall back to; re-raise

    # Last resort
    return _load_pem(pem_key, pem_cert)

def _load_p12(p12_path_in, p12_pass_in):
    if not HAVE_PKCS12:
        die("cryptography lacks PKCS#12 support; upgrade it to use --p12 or DEFAULT_P12.")
    p12_path = Path(p12_path_in).expanduser()
    if not p12_path.is_file():
        die(f"PKCS#12 not found: {p12_path}")
    pw = (p12_pass_in if p12_pass_in is not None else DEFAULT_P12_PASS)
    pw_bytes = pw.encode("utf-8") if isinstance(pw, str) else (pw if pw is None else pw)
    data = p12_path.read_bytes()
    # Let ValueError propagate so load_credentials can fall back to PEM
    key, cert, chain = pkcs12.load_key_and_certificates(data, pw_bytes, backend=default_backend())
    if key is None or cert is None:
        die("PKCS#12 did not contain both a private key and a certificate.")
    return key, cert, list(chain or [])

def _load_pem(key_path_in, cert_path_in):
    key_path  = Path(key_path_in).expanduser()
    cert_path = Path(cert_path_in).expanduser()
    if not key_path.is_file():
        die(f"Key PEM not found: {key_path}")
    if not cert_path.is_file():
        die(f"Cert PEM not found: {cert_path}")
    key_obj = serialization.load_pem_private_key(key_path.read_bytes(), password=None, backend=default_backend())
    cert_obj = x509.load_pem_x509_certificate(cert_path.read_bytes(), backend=default_backend())
    return key_obj, cert_obj, []

def make_meta():
    """Invisible, full-document, MDP (no changes), SHA-256."""
    return {
        "sigpage":     0,
        "sigbutton":   False,
        "sigfield":    "Signature1",
        "mdp":         True,
        "contact":     os.environ.get("EVCAP_SIGN_CONTACT", ""),
        "location":    os.environ.get("EVCAP_SIGN_LOCATION", ""),
        "signingdate": utc_pdf_date(),
        "reason":      os.environ.get("EVCAP_SIGN_REASON", "Digitally signed"),
        "md":          "sha256",
    }

def sign_one_pdf(input_pdf: Path, out_pdf: Path, key_obj, cert_obj, chain_list, incremental: bool):
    pdf_bytes = input_pdf.read_bytes()
    meta = make_meta()
    signed_bytes = cms.sign(pdf_bytes, meta, key_obj, cert_obj, chain_list)
    if incremental:
        # Legacy behavior preserved: append signature bytes after original
        out_pdf.write_bytes(pdf_bytes + signed_bytes)
    else:
        out_pdf.write_bytes(signed_bytes)
    print(f"✅ Signed -> {out_pdf}")

def build_parser():
    p = argparse.ArgumentParser(
        description="Sign PDF(s) with an invisible, full-document, MDP-protected SHA-256 signature."
    )
    # Input selection
    grp_in = p.add_mutually_exclusive_group(required=True)
    grp_in.add_argument("--pdf", help="Path to a single PDF to sign.")
    grp_in.add_argument("--in-dir", help="Directory of PDFs to sign.")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders with --in-dir.")
    p.add_argument("--include", action="append", default=[], help="Glob of PDFs to include (repeatable).")
    p.add_argument("--exclude", action="append", default=[], help="Glob of PDFs to exclude (repeatable).")

    # Credentials
    grp_cred = p.add_mutually_exclusive_group(required=False)
    grp_cred.add_argument("--p12", help=f"PKCS#12 file (default chain: env/config/hardcoded)")
    grp_cred.add_argument("--key-pem", help=f"PEM private key path (default chain: env/config/hardcoded)")
    p.add_argument("--cert-pem", help=f"PEM certificate path (required with --key-pem)")
    p.add_argument("--p12-pass", default=None, help="PKCS#12 password (if needed).")

    # Output
    p.add_argument("--out-dir", default=None, help="Directory for signed PDFs. Default: next to each input.")
    p.add_argument("--suffix", default="_signed", help='Suffix before ".pdf" (default: _signed).')
    p.add_argument("--overwrite", action="store_true", help="Overwrite outputs if they already exist.")

    # Behavior
    p.add_argument("--no-incremental", action="store_true",
                   help="Write Endesive-returned signed bytes (default is incremental append).")
    return p

def main():
    args = build_parser().parse_args()

    # Validate PEM pairing if either provided explicitly
    if args.key_pem and not args.cert_pem:
        die("--key-pem requires --cert-pem (or use --p12).")
    if args.cert_pem and not args.key_pem:
        die("--cert-pem requires --key-pem (or use --p12).")

    # Resolve inputs
    if args.pdf:
        pdfs = [Path(args.pdf).expanduser().resolve()]
        if not pdfs[0].is_file():
            die(f"PDF not found: {pdfs[0]}")
    else:
        root = Path(args.in_dir).expanduser().resolve()
        if not root.is_dir():
            die(f"Input directory not found: {root}")
        pdfs = collect_pdfs(root, args.recursive, args.include, args.exclude)
        if not pdfs:
            die("No PDF files matched your selection.")

    # Output directory (optional)
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    # Load creds using the precedence chain
    key_obj, cert_obj, chain_list = load_credentials(args)

    # Incremental default ON (matches your original behavior)
    incremental = not args.no_incremental

    # Sign
    ok, skip = 0, 0
    for pdf in pdfs:
        out_path = (out_dir / (pdf.stem + args.suffix + ".pdf")) if out_dir else pdf.with_name(pdf.stem + args.suffix + ".pdf")
        if out_path.exists() and not args.overwrite:
            print(f"↷ Skip (exists): {out_path}  (use --overwrite to replace)")
            skip += 1
            continue
        try:
            sign_one_pdf(pdf, out_path, key_obj, cert_obj, chain_list, incremental)
            ok += 1
        except Exception as e:
            print(f"❌ Error signing {pdf.name}: {e}")
            skip += 1

    print(f"[done] signed={ok} skipped={skip}")

if __name__ == "__main__":
    main()
