#!/usr/bin/env python3
"""
check_deps.py — verify that all required dependencies are available.

Run:  python3 bin/check_deps.py
"""

import shutil
import sys
import os

OK = "\033[32mOK\033[0m"
MISS = "\033[31mMISSING\033[0m"
WARN = "\033[33mWARN\033[0m"


def check_python_pkg(name, import_name=None):
    mod = import_name or name
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def check_system_cmd(name):
    return shutil.which(name) is not None


def main():
    print("=" * 60)
    print("Evidence-Capture Dependency Checker")
    print("=" * 60)

    errors = 0

    # --- Python packages ---
    print("\n--- Python packages (pip install -r requirements.txt) ---")
    pkgs = [
        ("Pillow", "PIL"),
        ("PyMuPDF", "fitz"),
        ("cryptography", "cryptography"),
        ("endesive", "endesive"),
        ("obsws-python", "obsws_python"),
        ("PyQt5", "PyQt5"),
        ("mss", "mss"),
        ("opentimestamps-client", "opentimestamps"),
        # verify/ and postprocess/
        ("pytesseract", "pytesseract"),
        ("requests", "requests"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
    ]
    for display, imp in pkgs:
        found = check_python_pkg(display, imp)
        status = OK if found else MISS
        if not found:
            errors += 1
        print(f"  {display:<30s} {status}")

    # --- mitmproxy (separate venv) ---
    print("\n--- mitmproxy (separate venv, see requirements-mitm.txt) ---")
    mitm_py = os.environ.get("EVCAP_MITM_PY", "")
    if mitm_py and os.path.isfile(os.path.expanduser(mitm_py)):
        print(f"  EVCAP_MITM_PY                  {OK}  ({mitm_py})")
    else:
        print(f"  EVCAP_MITM_PY                  {WARN}  (not set or not found; HAR conversion will fail)")
        print("    Set: export EVCAP_MITM_PY=~/.venvs/mitm/bin/python")

    # --- System tools ---
    print("\n--- System tools (apt install) ---")
    tools = [
        ("ffmpeg", "sudo apt install ffmpeg"),
        ("tshark", "sudo apt install tshark"),
        ("websocat", "cargo install websocat  OR  snap install websocat"),
        ("exiftool", "sudo apt install libimage-exiftool-perl"),
        ("pdfsig", "sudo apt install poppler-utils"),
        ("ots", "pip install opentimestamps-client"),
        ("tesseract", "sudo apt install tesseract-ocr   (frame OCR in verify/)"),
    ]
    for name, install in tools:
        found = check_system_cmd(name)
        status = OK if found else MISS
        if not found:
            errors += 1
        line = f"  {name:<30s} {status}"
        if not found:
            line += f"  ({install})"
        print(line)

    # --- Applications ---
    print("\n--- Applications ---")
    apps = [
        ("firefox", "Firefox browser"),
        ("obs", "OBS Studio"),
    ]
    for name, desc in apps:
        found = check_system_cmd(name)
        status = OK if found else WARN
        print(f"  {name:<30s} {status}  ({desc})")

    # --- Fonts ---
    print("\n--- Fonts ---")
    font_path = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
    if os.path.isfile(font_path):
        print(f"  Liberation Mono Bold           {OK}")
    else:
        print(f"  Liberation Mono Bold           {MISS}  (sudo apt install fonts-liberation)")
        errors += 1

    # --- Signing keys ---
    print("\n--- Signing credentials ---")
    key_pem = os.environ.get("EVCAP_KEY_PEM", "")
    cert_pem = os.environ.get("EVCAP_CERT_PEM", "")
    p12 = os.environ.get("EVCAP_P12", "")
    if key_pem and os.path.isfile(os.path.expanduser(key_pem)):
        if cert_pem and os.path.isfile(os.path.expanduser(cert_pem)):
            print(f"  EVCAP_KEY_PEM + EVCAP_CERT_PEM {OK}  ({key_pem})")
        else:
            print(f"  EVCAP_KEY_PEM                  {WARN}  (set, but EVCAP_CERT_PEM is missing; PDF signing needs both)")
    elif p12 and os.path.isfile(os.path.expanduser(p12)):
        print(f"  EVCAP_P12                      {OK}  ({p12})")
    else:
        print(f"  EVCAP_KEY_PEM / EVCAP_P12      {WARN}  (not set; PDF signing will fail)")
        print("    Set: export EVCAP_KEY_PEM=/path/to/key.pem")
        print("         export EVCAP_CERT_PEM=/path/to/cert.pem")

    # --- Summary ---
    print("\n" + "=" * 60)
    if errors == 0:
        print(f"All checks passed. {OK}")
    else:
        print(f"{errors} issue(s) found. {MISS}")
        print("Run: pip install -r requirements.txt")
    print("=" * 60)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
