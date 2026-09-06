#!/usr/bin/env python3
"""
archive_session.py — Move all session artifacts into sessions/<name>/

Collects:
  - run/bundles/*           -> sessions/<name>/bundles/
  - run/dumps/<name>.dump*  -> sessions/<name>/dumps/
  - run/dumps/processed/*   -> sessions/<name>/dumps/processed/
  - downloads/*             -> sessions/<name>/downloads/
  - OBS video (newest file) -> sessions/<name>/video/
  - run/state.json          -> sessions/<name>/state.json (copy, not move)
  - run/logs/*              -> sessions/<name>/logs/

Safe to run multiple times — skips if session dir already has content.
"""

# --- portable import bootstrap ---
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
# ---------------------------------

import argparse
import glob
import os
import shutil
from pathlib import Path

from evidence_capture.paths import ROOT, RUN, ensure_runtime_dirs
from evidence_capture.state import state_read

# Default OBS output directory (override with EVCAP_OBS_RECORD_DIR)
DEFAULT_OBS_DIR = Path(os.environ.get(
    "EVCAP_OBS_RECORD_DIR",
    os.path.expanduser("~/Videos"),
))

SESSIONS_DIR = ROOT / "sessions"
BUNDLES = RUN / "bundles"
DUMPS = RUN / "dumps"
DOWNLOADS = ROOT / "downloads"
LOGS = RUN / "logs"


def find_newest_video(obs_dir: Path, since_minutes: int = 30) -> Path | None:
    """Find the most recently modified video file in the OBS output directory."""
    import time
    cutoff = time.time() - (since_minutes * 60)
    candidates = []
    for ext in ("*.mkv", "*.mp4", "*.flv", "*.avi", "*.ts"):
        for p in obs_dir.glob(ext):
            if p.stat().st_mtime > cutoff:
                candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def archive_session(session_name: str, obs_dir: Path, since_minutes: int = 30):
    session_dir = SESSIONS_DIR / session_name

    if session_dir.exists() and any(session_dir.iterdir()):
        print(f"[WARN] Session directory already has content: {session_dir}")
        print("       Skipping archive to avoid overwriting.")
        return

    session_dir.mkdir(parents=True, exist_ok=True)
    moved = []

    # 1) Bundles
    dst = session_dir / "bundles"
    if BUNDLES.exists() and any(BUNDLES.iterdir()):
        shutil.copytree(BUNDLES, dst, dirs_exist_ok=True)
        # Clear bundles after copy
        for p in BUNDLES.iterdir():
            if p.is_file():
                p.unlink()
        moved.append(f"bundles/ ({len(list(dst.iterdir()))} files)")

    # 2) Dumps (session-specific dump + processed HAR)
    dst = session_dir / "dumps"
    dst.mkdir(parents=True, exist_ok=True)
    dump_pattern = DUMPS / f"{session_name}*"
    for p in glob.glob(str(dump_pattern)):
        p = Path(p)
        if p.is_file():
            shutil.move(str(p), str(dst / p.name))
            moved.append(f"dumps/{p.name}")

    # Also move processed/ contents
    processed_src = DUMPS / "processed"
    if processed_src.exists() and any(processed_src.iterdir()):
        processed_dst = dst / "processed"
        processed_dst.mkdir(parents=True, exist_ok=True)
        for p in processed_src.iterdir():
            if p.is_file() and p.name != ".gitkeep":
                shutil.move(str(p), str(processed_dst / p.name))
                moved.append(f"dumps/processed/{p.name}")

    # 3) Downloads (everything in downloads/ except .gitkeep)
    if DOWNLOADS.exists():
        has_files = False
        for p in DOWNLOADS.rglob("*"):
            if p.is_file() and p.name != ".gitkeep":
                has_files = True
                break
        if has_files:
            dst = session_dir / "downloads"
            shutil.copytree(DOWNLOADS, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".gitkeep"))
            # Clear downloads
            for p in DOWNLOADS.rglob("*"):
                if p.is_file() and p.name != ".gitkeep":
                    p.unlink()
            # Remove empty subdirs
            for p in sorted(DOWNLOADS.rglob("*"), reverse=True):
                if p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
            moved.append("downloads/")

    # 4) OBS video (newest recording)
    video = find_newest_video(obs_dir, since_minutes=since_minutes)
    if video:
        dst = session_dir / "video"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video), str(dst / video.name))
        moved.append(f"video/{video.name}")
    else:
        print(f"[INFO] No recent video found in {obs_dir} (last {since_minutes} min)")

    # 5) State snapshot (copy, don't move — state.json is still needed)
    state_src = RUN / "state.json"
    if state_src.exists():
        shutil.copy2(state_src, session_dir / "state.json")
        moved.append("state.json")

    # 6) Logs (copy, don't move)
    if LOGS.exists() and any(LOGS.iterdir()):
        dst = session_dir / "logs"
        shutil.copytree(LOGS, dst, dirs_exist_ok=True)
        moved.append("logs/")

    print(f"\n[OK] Session archived to: {session_dir}")
    for item in moved:
        print(f"     {item}")
    print(f"     Total: {len(moved)} items")


def main():
    ensure_runtime_dirs()

    ap = argparse.ArgumentParser(description="Archive session artifacts into sessions/<name>/")
    ap.add_argument("--session-name", default=None,
                    help="Session name (default: read from state.json)")
    ap.add_argument("--obs-dir", default=str(DEFAULT_OBS_DIR),
                    help=f"OBS recording directory (default: {DEFAULT_OBS_DIR})")
    ap.add_argument("--video-window", type=int, default=30,
                    help="Look for OBS videos modified within this many minutes (default: 30)")
    args = ap.parse_args()

    sess = args.session_name
    if not sess:
        st = state_read()
        sess = st.get("session_name")
    if not sess:
        print("ERROR: No session name provided and none found in state.json", file=sys.stderr)
        sys.exit(1)

    obs_dir = Path(args.obs_dir).expanduser()
    archive_session(sess, obs_dir, since_minutes=args.video_window)


if __name__ == "__main__":
    main()
