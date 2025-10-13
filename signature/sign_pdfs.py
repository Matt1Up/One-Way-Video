#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PDF signer for your workflow.

Defaults (match your old script):
  • Invisible signature (no visible field)
  • Full-document coverage
  • MDP policy = no changes allowed
  • SHA-256
  • Incremental append (original bytes + signature), preserving your old output style

Features:
  • Single PDF (--pdf) or directory (--in-dir), optional --recursive
  • Include / exclude glob filters
  • Output folder + suffix control, safe overwrite
  • Credentials via PEM (default paths) or PKCS#12 (.p12)

Examples:

# EXACTLY like your old behavior on one file (defaults already invisible+incremental+MDP)
python3 sign_pdfs.py \
  --pdf "/path/to/EXHIBIT EMG.pdf"

# Batch sign a folder recursively, write outputs to a folder
python3 sign_pdfs.py \
  --in-dir "/some/folder" --recursive \
  --out-dir "/some/output" --overwrite

# Use PKCS#12 instead of PEM
python3 sign_pdfs.py \
  --pdf "/path/to/file.pdf" \
  --p12 "/matt_cert.p12" \
  --p12-pass "yourpassword"

# Custom PEM paths (override defaults)
python3 sign_pdfs.py \
  --pdf "/path/to/file.pdf" \
  --key-pem "/custom/key.pem" \
  --cert-pem "/custom/cert.pem"
"""

import argparse
import fnmatch
import sys
from pathlib import Path
from datetime import datetime, timezone

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


# ===== Defaults tied to your machine (can be overridden via CLI) =====
DEFAULT_KEY_PEM  = "/matt_key.pem"
DEFAULT_CERT_PEM = "/matt_cert.pem"
DEFAULT_P12      = "/matt_cert.p12"



def die(msg: str, code: int = 1):
    print(f"❌ {msg}")
    sys.exit(code)

def utc_pdf_date() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")

def collect_pdfs(in_dir: Path, recursive: bool, includes, excludes):
    if recursive:
        candidates = [p for p in in_dir.rglob("*.pdf") if p.is_file()]
    else:
        candidates = [p for p in in_dir.glob("*.pdf") if p.is_file()]

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
    """
    # Priority: explicit PKCS#12, else explicit PEM, else default PEM, else default PKCS#12
    if args.p12:
        if not HAVE_PKCS12:
            die("cryptography package lacks PKCS#12 support; upgrade it to use --p12.")
        p12_path = Path(args.p12).expanduser()
        if not p12_path.is_file():
            die(f"PKCS#12 not found: {p12_path}")
        pw = args.p12_pass.encode("utf-8") if args.p12_pass is not None else None
        data = p12_path.read_bytes()
        key, cert, chain = pkcs12.load_key_and_certificates(data, pw, backend=default_backend())
        if key is None or cert is None:
            die("PKCS#12 did not contain both a private key and a certificate.")
        return key, cert, list(chain or [])

    # PEM mode
    key_path  = Path(args.key_pem or DEFAULT_KEY_PEM).expanduser()
    cert_path = Path(args.cert_pem or DEFAULT_CERT_PEM).expanduser()
    if not key_path.is_file():
        die(f"Key PEM not found: {key_path}")
    if not cert_path.is_file():
        die(f"Cert PEM not found: {cert_path}")

    key_obj = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None, backend=default_backend()
    )
    cert_obj = x509.load_pem_x509_certificate(
        cert_path.read_bytes(), backend=default_backend()
    )
    return key_obj, cert_obj, []

def make_meta():
    """
    Invisible, full-document, MDP (no changes), SHA-256 — your standard profile.
    """
    return {
        "sigpage":     0,                 # field anchor page (irrelevant when invisible)
        "sigbutton":   False,             # invisible signature (no visible widget)
        "sigfield":    "Signature1",      # consistent field name
        "mdp":         True,              # DocMDP policy: no changes allowed
        "contact":     "MattGuertin@protonmail.com",
        "location":    "Minneapolis, MN",
        "signingdate": utc_pdf_date(),
        "reason":      "Digitally signed by Matthew Guertin",
        "md":          "sha256",
    }

def sign_one_pdf(input_pdf: Path, out_pdf: Path, key_obj, cert_obj, chain_list, incremental: bool):
    pdf_bytes = input_pdf.read_bytes()
    meta = make_meta()
    signed_bytes = cms.sign(pdf_bytes, meta, key_obj, cert_obj, chain_list)

    if incremental:
        # Your legacy behavior: original bytes + appended signature
        out_pdf.write_bytes(pdf_bytes + signed_bytes)
    else:
        # Alternative: endesive returns a complete signed PDF
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
    grp_cred.add_argument("--p12", help=f"PKCS#12 file (default: {DEFAULT_P12})")
    grp_cred.add_argument("--key-pem", help=f"PEM private key path (default: {DEFAULT_KEY_PEM})")
    p.add_argument("--cert-pem", help=f"PEM certificate path (default: {DEFAULT_CERT_PEM})")
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

    # Load creds
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
