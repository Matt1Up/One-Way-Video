#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
download_watcher_json.py — JSON-native download watcher & vault builder with super-verbose logging.

Portability updates:
- No hard-coded ~/evidence-capture paths; everything anchors to the repo root via evidence_capture.paths
- Uses your current Python interpreter (sys.executable) for all helper scripts; override with EVCAP_PY if needed
- Keeps behavior, logging, and file layout identical to the original

Fixes in this version (retained from your script):
- Never processes .zip files in the watched folder
- Embed ZIP created in processed/file-meta/ (outside the watched root)
- Keeps canonical state.json keys: file_count(+_sys_time), files_recent(+_sys_time)
- --clear is one-shot (resets and exits)
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

import argparse, json, os, sys as _sys, time, re, shutil, zipfile, subprocess, tempfile, hashlib, fcntl, signal
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Tuple, Optional, List

try:
    import fitz  # PyMuPDF
except Exception:
    sys.exit("PyMuPDF is required. Install with:  pip install --user pymupdf")

# ---------- Paths (portable via evidence_capture) ----------
from evidence_capture.paths import (
    ROOT, BIN, RUN, ensure_runtime_dirs
)

# Top-level repo dirs you already have
TIME_DIR    = ROOT / "time"
DOWNLOADS   = ROOT / "downloads"

# Derived paths (same structure as your original)
PROOFS_DIR  = DOWNLOADS / ".proofs"
PROCESSED   = DOWNLOADS / "processed"
META_DIR    = PROCESSED / "file-meta"

STATE_JSON  = RUN / "state.json"
STATE_LOCK  = RUN / "state.lock"
LEDGER_JSON = RUN / "downloaded_files.json"
LEDGER_LOCK = RUN / "downloaded_files.lock"

LOG_DIR     = RUN / "logs"
LOG_PATH    = LOG_DIR / "download_watcher.log"

# Use your active interpreter unless EVCAP_PY is set
PY = Path(os.environ.get("EVCAP_PY", _sys.executable))

DEBUG = False
CREATED_VAULTS: set[str] = set()  # vaults created in THIS run

# Roughtime config (unchanged)
RT_SERVER = "roughtime.cloudflare.com"
RT_PORT   = "2003"
RT_PUBKEY = "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg="

# Tuning (unchanged)
POLL_SECS        = 0.5
STABILIZE_SECS   = 1.0
MAX_FILE_BYTES   = 50 * 1024 * 1024 * 1024
RECENT_MAX       = 5
OTS_DISCOVERY_WAIT_SECS = 8.0

# ---------- Logging ----------
def ts_local_ns() -> str:
    ns = time.time_ns()
    sec, rem = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(sec, tz=datetime.now().astimezone().tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{rem:09d} {dt.tzname() or ''}"

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _ensure_logdir():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def log(msg: str):
    """Log to stdout AND logfile."""
    line = f"[{ts_local_ns()}] {msg}"
    print(line, flush=True)
    _ensure_logdir()
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log_exception(prefix: str, ex: Exception):
    log(f"{prefix}: {type(ex).__name__}: {ex}")

def run_cmd(args, check=True, capture=True, cwd=None, label: str = ""):
    """Run a subprocess with full logging of args/stdout/stderr/exit."""
    log(f"RUN{(' ['+label+']') if label else ''}: {' '.join(map(str, args))}")
    p = subprocess.run(
        args, check=False, cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture:
        if p.stdout:
            log(f"STDOUT{(' ['+label+']') if label else ''}: {p.stdout.strip()}")
        if p.stderr:
            log(f"STDERR{(' ['+label+']') if label else ''}: {p.stderr.strip()}")
    log(f"EXIT{(' ['+label+']') if label else ''}: {p.returncode}")
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, args, p.stdout, p.stderr)
    return p.returncode, (p.stdout or ""), (p.stderr or "")

# ---------- Locked JSON I/O ----------
class Flock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fd = None
    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *args):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd); self.fd = None

def _json_load_locked(lock_path: Path, json_path: Path) -> dict:
    if not json_path.exists():
        return {}
    try:
        with json_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        log_exception(f"JSON load failed for {json_path}", ex)
        return {}

def _json_write_locked(lock_path: Path, json_path: Path, obj: dict) -> None:
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                      dir=str(json_path.parent), delete=False)
    try:
        json.dump(obj or {}, tmp, ensure_ascii=False, separators=(",", ":"))
        tmp.write("\n")
        tmp.flush(); os.fsync(tmp.fileno())
        name = tmp.name
    finally:
        tmp.close()
    os.replace(name, str(json_path))

def state_read() -> dict:
    with Flock(STATE_LOCK):
        return _json_load_locked(STATE_LOCK, STATE_JSON)

def state_write(obj: dict) -> None:
    with Flock(STATE_LOCK):
        _json_write_locked(STATE_LOCK, STATE_JSON, obj)
    log("STATE WRITE: updated state.json")

def state_set_with_time(key: str, value, sys_time: Optional[str] = None):
    """Atomic key update: reads, sets key + <key>_sys_time, writes under lock."""
    if sys_time is None:
        sys_time = ts_local_ns()
    with Flock(STATE_LOCK):
        st = _json_load_locked(STATE_LOCK, STATE_JSON)
        st[key] = value
        st[f"{key}_sys_time"] = sys_time
        _json_write_locked(STATE_LOCK, STATE_JSON, st)
    log(f"STATE SET: {key}={('[...]' if isinstance(value, (list, dict)) else value)} @ {sys_time}")
    return value, sys_time

def state_inc_file_count(sys_time: Optional[str] = None) -> int:
    """Atomic increment of file_count under lock; returns the NEW value."""
    if sys_time is None:
        sys_time = ts_local_ns()
    with Flock(STATE_LOCK):
        st = _json_load_locked(STATE_LOCK, STATE_JSON)
        cur = int(st.get("file_count", 0) or 0)
        new = cur + 1
        st["file_count"] = new
        st["file_count_sys_time"] = sys_time
        _json_write_locked(STATE_LOCK, STATE_JSON, st)
    log(f"STATE INC: file_count {cur} -> {new} @ {sys_time}")
    return new

def ledger_read() -> dict:
    with Flock(LEDGER_LOCK):
        return _json_load_locked(LEDGER_LOCK, LEDGER_JSON)

def ledger_write(obj: dict) -> None:
    with Flock(LEDGER_LOCK):
        _json_write_locked(LEDGER_LOCK, LEDGER_JSON, obj)
    log(f"LEDGER WRITE: {LEDGER_JSON}")

# ---------- exiftool / pdfsig ----------
def sha256_stream(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def exiftool_json(path: Path) -> Optional[List[dict]]:
    rc, out, err = run_cmd(["exiftool", "-json", "-a", "-G", str(path)],
                           check=False, capture=True, label=f"exiftool {path.name}")
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        cleaned = []
        for rec in data if isinstance(data, list) else [data]:
            if not isinstance(rec, dict): continue
            rec = dict(rec)
            for k in ["SourceFile","Directory","FilePermissions"]:
                rec.pop(k, None)
            cleaned.append(rec)
        return cleaned
    except Exception as ex:
        log_exception("exiftool JSON parse error", ex)
        return None

_SIG_LINE = re.compile(r"^\s*-\s*(?P<k>[^:]+):\s*(?P<v>.*)$")
_RANGE = re.compile(r"\[(\d+)\s*-\s*(\d+)\]")

def pdfsig_structured(path: Path) -> Optional[dict]:
    if path.suffix.lower() != ".pdf":
        return None
    rc, out, err = run_cmd(["pdfsig", str(path)],
                           check=False, capture=True, label=f"pdfsig {path.name}")
    if rc != 0 or not out.strip():
        return None
    lines = [ln.rstrip("\n") for ln in out.splitlines()]
    sigs, cur = [], None
    for ln in lines:
        if ln.strip().startswith("Digital Signature Info of:"):
            continue  # avoid absolute path header
        if ln.strip().startswith("Signature #"):
            if cur: sigs.append(cur)
            try:
                idx = int(ln.strip().split("#",1)[1].split(":",1)[0])
            except Exception:
                idx = len(sigs)+1
            cur = {"index": idx}
            continue
        m = _SIG_LINE.match(ln)
        if m and cur is not None:
            k = m.group("k").strip()
            v = m.group("v").strip()
            if k.startswith("Signer Certificate Common Name"):
                cur["signer_cn"] = v
            elif k.startswith("Signer full Distinguished Name"):
                cur["signer_dn"] = v
            elif k.startswith("Signing Time"):
                cur["signing_time"] = v
            elif k.startswith("Signing Hash Algorithm"):
                cur["hash_algo"] = v
            elif k.startswith("Signature Type"):
                cur["type"] = v
            elif k.startswith("Signed Ranges"):
                ranges = _RANGE.findall(v)
                cur["signed_ranges"] = [[int(a), int(b)] for a,b in ranges]
            elif k.startswith("Total document signed"):
                cur["total_document_signed"] = True
            elif k.startswith("Not total document signed"):
                cur["total_document_signed"] = False
            elif k.startswith("Signature Validation"):
                cur["signature_validation"] = v
            elif k.startswith("Certificate Validation"):
                cur["certificate_validation"] = v
    if cur: sigs.append(cur)
    return {"file": path.name, "signatures": sigs}

# ---------- Roughtime / OTS ----------
def roughtime_bind(target: Path, json_out_dir: Path) -> Optional[Path]:
    log(f"ROUGHTIME bind start: target={target} out_dir={json_out_dir}")
    json_out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd([
        str(PY), str(TIME_DIR / "roughtime_client.py"), "query", "-v",
        "--server", RT_SERVER, "--port", RT_PORT,
        "--pubkey-base64", RT_PUBKEY,
        "--reveal-hash",
        "--bind-file", str(target),
        "--last-time", str(RUN / "last_roughtime.txt"),
        "--json-out", str(json_out_dir),
    ], check=True, capture=True, label=f"roughtime {target.name}")
    newest = None
    for p in json_out_dir.glob("*.json"):
        if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
            newest = p
    log(f"ROUGHTIME bind done: newest={newest}")
    return newest

def ots_stamp(target: Path, out_dir: Path, wait_if_exists: float = OTS_DISCOVERY_WAIT_SECS) -> Path:
    """Idempotent OTS: reuse/normalize existing, tolerate 'File exists' + wait briefly for late file."""
    log(f"OTS stamp start: target={target} out_dir={out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    cand_same  = target.parent / f"{target.name}.ots"
    cand_same2 = target.parent / (target.stem + ".ots")
    cand_out   = out_dir / f"{target.name}.ots"
    cand_out2  = out_dir / (target.stem + ".ots")

    # 0) Reuse if present anywhere
    for c in (cand_out, cand_out2, cand_same, cand_same2):
        if c.exists():
            log(f"OTS exists already: {c}")
            if c.parent != out_dir:
                try:
                    dest = out_dir / c.name
                    if dest.exists():
                        log(f"OTS normalize skipped (dest exists): {dest}")
                        return dest
                    c.replace(dest)
                    log(f"OTS normalized: {dest}")
                    return dest
                except Exception as ex:
                    log_exception("OTS normalize failed; using original", ex)
                    return c
            return c

    # 1) Try stamping
    try:
        run_cmd([str(PY), str(BIN / "get_ots_stamp.py"), str(target), str(out_dir)],
                check=True, capture=True, label=f"ots {target.name}")
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "") + "\n" + (e.stdout or "")
        if "File exists" in msg:
            log("OTS: 'File exists' reported; trying discovery.")
        elif "need at least 2 attestations" in msg:
            log("OTS: Pool attestation timeout; trying discovery window.")
        else:
            log("OTS: non-benign error; will still attempt discovery before failing.")

    # 2) Discover/normalize; wait a bit in case the file appears late
    deadline = time.time() + max(0.0, wait_if_exists)
    while True:
        for c in (cand_out, cand_out2, cand_same, cand_same2):
            if c.exists():
                log(f"OTS discovered: {c}")
                if c.parent != out_dir:
                    try:
                        dest = out_dir / c.name
                        if dest.exists():
                            log(f"OTS normalize skipped (dest exists): {dest}")
                            return dest
                        c.replace(dest)
                        log(f"OTS normalized (late): {dest}")
                        return dest
                    except Exception as ex:
                        log_exception("OTS normalize (late) failed; using original", ex)
                        return c
                return c
        if time.time() >= deadline:
            break
        time.sleep(0.25)

    raise RuntimeError(f"OTS not created for {target}")

# ---------- One-page info PDF ----------
def make_info_pdf(out_pdf: Path, info: dict) -> None:
    W, H = (612, 792)
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    y, x = 48, 48
    def add(text, size=12):
        nonlocal y
        page.insert_text((x, y), text, fontname="helv", fontsize=size)
        y += size + 6
    def safe(v):
        return "" if v is None else str(v)
    add("Download Evidence Wrapper", 18); add(now_utc_iso()); add("")
    add("Summary", 14)
    for k in ["session_name","file_index","original_name","original_size","original_sha256","zip_name","vault_pdf","vault_sha256"]:
        v = info.get(k)
        if v is not None:
            add(f"{k}: {safe(v)}")
    add(""); add("Embedded contents", 14)
    for item in info.get("embedded", []):
        add(f"- {safe(item)}")
    doc.save(str(out_pdf), garbage=4, deflate=True)
    doc.close()
    log(f"INFO PDF written: {out_pdf}")

# ---------- Ignore rules ----------
TEMP_SUFFIXES = {".crdownload", ".part", ".partial", ".download", ".tmp", ".temp"}

def is_our_artifact(p: Path) -> bool:
    n = p.name
    if n.startswith("."):
        if DEBUG: log(f"[debug] ignore hidden/temp file: {n}")
        return True
    if n.endswith("_internal.pdf"):
        if DEBUG: log(f"[debug] ignore temporary internal PDF: {n}")
        return True
    if "__time-stamp.json" in n:
        if DEBUG: log(f"[debug] ignore roughtime JSON: {n}")
        return True
    if n.lower().endswith(".zip"):
        if DEBUG: log(f"[debug] ignore .zip (transient embed or foreign zip): {n}")
        return True
    if n.endswith(".ots"):
        if DEBUG: log(f"[debug] ignore OTS: {n}")
        return True
    if n.endswith(".meta.json"):
        if DEBUG: log(f"[debug] ignore meta JSON: {n}")
        return True
    suf = p.suffix.lower()
    if suf in TEMP_SUFFIXES:
        if DEBUG: log(f"[debug] ignore partial download: {n}")
        return True
    if n in CREATED_VAULTS:
        if DEBUG: log(f"[debug] ignore our freshly created vault PDF: {n}")
        return True
    return False

# ---------- Stability snapshot ----------
class DirSnapshot:
    def __init__(self):
        self.stat: Dict[Path, Tuple[int, float]] = {}
    @staticmethod
    def list_files(root: Path):
        for p in root.iterdir():
            if p.is_file():
                yield p
    def capture(self, root: Path):
        cur = {}
        for p in self.list_files(root):
            try:
                st = p.stat()
                cur[p] = (st.st_size, st.st_mtime)
            except FileNotFoundError:
                pass
        self.stat = cur

def stable_files(prev: DirSnapshot, cur: DirSnapshot, min_age: float) -> Dict[Path, Tuple[int, float]]:
    res = {}
    now = time.time()
    for p, (sz, mt) in cur.stat.items():
        if sz > MAX_FILE_BYTES:
            continue
        if p in prev.stat and prev.stat[p] == (sz, mt) and (now - mt) >= min_age:
            res[p] = (sz, mt)
    return res

# ---------- Main per-file processor ----------
def process_one_file(fpath: Path, session_name: str) -> None:
    parent = fpath.parent
    base   = fpath.name
    size   = fpath.stat().st_size
    appear_ts = ts_local_ns()
    log(f"PROCESS: start {base} ({size} bytes) at {appear_ts}")

    # Atomic counter increment (fixes duplicate indices)
    file_count = state_inc_file_count(appear_ts)

    # 1) Roughtime bind the file -> .proofs
    file_rt_json = roughtime_bind(fpath, PROOFS_DIR)

    # 2) OTS of file -> .proofs (idempotent)
    file_ots = ots_stamp(fpath, PROOFS_DIR, wait_if_exists=OTS_DISCOVERY_WAIT_SECS)

    # 3) Roughtime bind of .ots -> .proofs
    ots_rt_json = roughtime_bind(file_ots, PROOFS_DIR)

    # 4) Metadata of original
    sha_hex  = sha256_stream(fpath); log(f"SHA256(file): {sha_hex}")
    exif_raw = exiftool_json(fpath)
    pdfsig_orig = pdfsig_structured(fpath)

    # 5) Per-file JSON (individual) — write next to the original (we'll move later)
    per_file_json = parent / f"{base}.meta.json"
    file_meta = {
        "session_name": session_name,
        "file_index": file_count,
        "name": base,
        "size_bytes": size,
        "sha256": sha_hex,
        "sys_time_detected": appear_ts,
        "proofs": {
            "file_roughtime_json": file_rt_json.name if file_rt_json else None,
            "file_ots": file_ots.name,
            "ots_roughtime_json": ots_rt_json.name if ots_rt_json else None,
        },
        "pdfsig": pdfsig_orig,
        "exiftool": exif_raw,
    }
    per_file_json.write_text(json.dumps(file_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"WROTE per-file JSON: {per_file_json}")

    # 6) ZIP artifacts — create in META_DIR (NOT in watched folder)
    META_DIR.mkdir(parents=True, exist_ok=True)
    embed_zip = META_DIR / f"{base}.zip"
    with zipfile.ZipFile(embed_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(fpath, arcname=base)
        zf.write(per_file_json, arcname=per_file_json.name)
        if file_rt_json and file_rt_json.exists():
            zf.write(file_rt_json, arcname=file_rt_json.name)
        zf.write(file_ots, arcname=file_ots.name)
        if ots_rt_json and ots_rt_json.exists():
            zf.write(ots_rt_json, arcname=ots_rt_json.name)
    log(f"EMBED ZIP created: {embed_zip}")

    # 7) One-page info PDF → vault name
    vault_pdf = parent / f"{file_count:04d}__{base if base.lower().endswith('.pdf') else base + '.pdf'}"
    CREATED_VAULTS.add(vault_pdf.name)
    info = {
        "session_name": session_name,
        "file_index": file_count,
        "original_name": base,
        "original_size": size,
        "original_sha256": sha_hex,
        "zip_name": embed_zip.name,
        "embedded": [embed_zip.name, per_file_json.name, file_ots.name] + \
                    ([file_rt_json.name] if file_rt_json else []) + \
                    ([ots_rt_json.name] if ots_rt_json else []),
        "vault_pdf": str(vault_pdf.name),
        "vault_sha256": None,
    }
    make_info_pdf(vault_pdf, info)

    # 8) Embed ZIP (writes _internal.pdf then we rename over)
    internal_out = vault_pdf.with_name(vault_pdf.stem + "_internal.pdf")
    run_cmd([
        str(PY), str(BIN / "embed_pdf_files.py"),
        "-f", str(META_DIR),                   # << use META_DIR where the ZIP lives
        "-p", str(vault_pdf),
        "-o", str(internal_out),
        "--include", embed_zip.name,
    ], check=True, capture=True, label=f"embed {vault_pdf.name}")
    shutil.move(str(internal_out), str(vault_pdf))
    log(f"EMBED done → {vault_pdf}")

    # 9) Digitally sign (full-document; incremental append)
    run_cmd([
        str(PY), str(BIN / "sign_pdfs.py"),
        "--pdf", str(vault_pdf),
        "--suffix", "",
        "--overwrite"
    ], check=True, capture=True, label=f"sign {vault_pdf.name}")

    vault_sha = sha256_stream(vault_pdf); log(f"SHA256(vault): {vault_sha}")
    info["vault_sha256"] = vault_sha

    # 10) Move vault to processed/ BEFORE vault OTS to avoid path races
    PROCESSED.mkdir(parents=True, exist_ok=True)
    vault_pdf_proc = PROCESSED / vault_pdf.name
    try:
        shutil.move(str(vault_pdf), str(vault_pdf_proc))
        log(f"MOVED vault to processed/: {vault_pdf_proc}")
    except Exception as ex:
        log_exception("MOVE vault failed (maybe already moved); using processed path anyway", ex)
        vault_pdf_proc = PROCESSED / vault_pdf.name

    # 11) OTS + Roughtime for final PDF (processed path) -> .proofs
    vault_ots    = ots_stamp(vault_pdf_proc, PROOFS_DIR, wait_if_exists=OTS_DISCOVERY_WAIT_SECS)
    vault_ots_rt = roughtime_bind(vault_ots, PROOFS_DIR)

    # 12) Vault metadata (exiftool + pdfsig) — processed path
    exif_vault = exiftool_json(vault_pdf_proc)
    pdfsig_vault = pdfsig_structured(vault_pdf_proc)

    # 13) Update per-file JSON with vault_meta
    file_meta["vault_meta"] = {
        "pdf": vault_pdf_proc.name,
        "sha256": vault_sha,
        "pdf_ots": vault_ots.name,
        "pdf_ots_roughtime_json": vault_ots_rt.name if vault_ots_rt else None,
        "sys_time_signed": ts_local_ns(),
        "exiftool": exif_vault,
        "pdfsig": pdfsig_vault,
    }
    per_file_json.write_text(json.dumps(file_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"UPDATED per-file JSON with vault meta: {per_file_json}")

    # 14) Append to master ledger
    ledger = ledger_read() or {}
    st_session = state_read()
    session_name_final = st_session.get("session_name") or session_name or "session"
    ledger.setdefault("session_name", session_name_final)
    ledger.setdefault("created_at", now_utc_iso())
    items = ledger.get("items") or []
    items.append({
        "session_name": session_name_final,
        "file_index": file_count,
        "name": base,
        "size_bytes": size,
        "sha256": sha_hex,
        "sys_time_detected": appear_ts,
        "proofs": {
            "file_roughtime_json": file_rt_json.name if file_rt_json else None,
            "file_ots": file_ots.name,
            "ots_roughtime_json": ots_rt_json.name if ots_rt_json else None,
        },
        "pdfsig": pdfsig_orig,
        "exiftool": exif_raw,
        "vault": {
            "pdf": vault_pdf_proc.name,
            "sha256": vault_sha,
            "pdf_ots": vault_ots.name,
            "pdf_ots_roughtime_json": vault_ots_rt.name if vault_ots_rt else None,
            "sys_time_signed": file_meta["vault_meta"]["sys_time_signed"],
            "exiftool": exif_vault,
            "pdfsig": pdfsig_vault,
        }
    })
    ledger["items"] = items
    ledger_write(ledger)

    # 15) Create tiny meta ZIP (everything except original + vault)
    META_DIR.mkdir(parents=True, exist_ok=True)
    meta_zip = META_DIR / (vault_pdf_proc.name + "_meta.zip")
    with zipfile.ZipFile(meta_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in [per_file_json, file_ots, file_rt_json, ots_rt_json, vault_ots, vault_ots_rt]:
            if p and Path(p).exists():
                zf.write(Path(p), arcname=Path(p).name)
    log(f"META ZIP created: {meta_zip}")

    # 16) Move leftover artifacts out of watched folder
    def _safe_move(src: Path, dst_dir: Path):
        if not src: return
        if not Path(src).exists(): return
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst_dir / Path(src).name))
            log(f"MOVED {src} -> {dst_dir / Path(src).name}")
        except Exception as ex:
            log_exception(f"MOVE failed for {src}", ex)

    _safe_move(fpath, PROCESSED)
    _safe_move(per_file_json, META_DIR)
    _safe_move(file_ots, META_DIR)
    _safe_move(file_rt_json, META_DIR)
    _safe_move(ots_rt_json, META_DIR)
    _safe_move(vault_ots, META_DIR)
    _safe_move(vault_ots_rt, META_DIR)

    # 17) Update state.json: rolling files_recent (KEY-ONLY write to avoid clobbering file_count)
    st_recent = state_read()
    lst = list(st_recent.get("files_recent") or [])
    lst = [e for e in lst if isinstance(e, dict)]
    lst.insert(0, {
        "idx": file_count,
        "name": base,
        "sha256": sha_hex,
        "size_bytes": size,
        "sys_time_detected": appear_ts,
        "vault_pdf": vault_pdf_proc.name,
        "vault_sha256": vault_sha,
    })
    lst = lst[:RECENT_MAX]
    state_set_with_time("files_recent", lst)

    log(f"PROCESS: done {base} -> vault={vault_pdf_proc.name}")

# ---------- Clear / Reset (ONE-SHOT) ----------
def clear_downloads(downloads: Path):
    log(f"CLEAR start for {downloads}")
    for p in downloads.glob("*"):
        try:
            if p.is_file():
                p.unlink(); log(f"CLR removed file: {p}")
            elif p.is_dir():
                shutil.rmtree(p); log(f"CLR removed dir:  {p}")
        except Exception as ex:
            log_exception(f"CLR remove failed for {p}", ex)
    # reset ledger + file_count, and CLEAR files_recent
    ledger_write({"session_name": state_read().get("session_name", ""), "created_at": now_utc_iso(), "items": []})
    with Flock(STATE_LOCK):
        st = _json_load_locked(STATE_LOCK, STATE_JSON)
        st["file_count"] = 0
        st["file_count_sys_time"] = ts_local_ns()
        st["files_recent"] = []
        st["files_recent_sys_time"] = ts_local_ns()
        _json_write_locked(STATE_LOCK, STATE_JSON, st)
    log("CLEAR complete; ledger cleared; file_count=0; files_recent cleared.")

# ---------- Main ----------
def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser(description="Monitor downloads dir, build forensic vault PDFs (JSON-native).")
    ap.add_argument("--downloads-dir", default=str(DOWNLOADS))
    ap.add_argument("--poll-interval", type=float, default=POLL_SECS)
    ap.add_argument("--stabilize-seconds", type=float, default=STABILIZE_SECS)
    ap.add_argument("--session-name", default=None)
    ap.add_argument("--oneshot", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--debug", action="store_true", help="extra verbose filtering logs")
    args = ap.parse_args()

    global DEBUG
    DEBUG = bool(args.debug)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("===== DOWNLOAD WATCHER START =====")

    downloads = Path(args.downloads_dir).expanduser()
    downloads.mkdir(parents=True, exist_ok=True)
    PROOFS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear:
        clear_downloads(downloads)
        log("EXIT after --clear (one-shot).")
        return

    # Resolve session name; store if provided
    st = state_read()
    session_name = args.session_name or st.get("session_name") or "session"
    if args.session_name:
        state_set_with_time("session_name", session_name, ts_local_ns())

    log(f"Watching: {downloads}")
    log(f"Session:  {session_name}")
    log(f"Proofs:   {PROOFS_DIR}")
    log(f"Processed:{PROCESSED}")
    log(f"Meta dir: {META_DIR}")
    log(f"Log file: {LOG_PATH}")
    log(f"Debug:    {DEBUG}")

    # Graceful shutdown
    shutting_down = False
    def _sig(sig, frame):
        nonlocal shutting_down
        shutting_down = True
        log(f"Signal received: {sig}. Shutting down...")
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    prev = DirSnapshot(); cur = DirSnapshot()
    prev.capture(downloads)
    processed_cache: Dict[Path, float] = {}

    while not shutting_down:
        time.sleep(max(0.1, float(args.poll_interval)))
        cur.capture(downloads)
        stables = stable_files(prev, cur, float(args.stabilize_seconds))
        if DEBUG:
            log(f"[debug] stable count: {len(stables)}")

        for p, (sz, mt) in stables.items():
            if DEBUG:
                log(f"[debug] stable candidate: {p.name} size={sz} mt={mt}")
            if is_our_artifact(p):
                if DEBUG: log(f"[debug] skip artifact: {p.name}")
                continue
            if processed_cache.get(p) == mt:
                if DEBUG: log(f"[debug] already processed (unchanged): {p.name}")
                continue

            try:
                process_one_file(p, session_name)
                processed_cache[p] = mt
            except subprocess.CalledProcessError as e:
                log(f"ERROR: cmd failed ({e.returncode}): {' '.join(map(str, e.cmd))}")
                if e.stdout: log(f"  -> STDOUT: {e.stdout.strip()}")
                if e.stderr: log(f"  -> STDERR: {e.stderr.strip()}")
            except Exception as ex:
                log_exception(f"ERROR processing {p.name}", ex)

        prev, cur = cur, prev
        if args.oneshot:
            log("ONESHOT complete; exiting.")
            break

    log("===== DOWNLOAD WATCHER STOP =====")

if __name__ == "__main__":
    main()
