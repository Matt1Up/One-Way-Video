#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
overlay_json.py - JSON-backed overlay window showing a key from state.json

Portable defaults:
  • Reads run/state.json (key: last_hash) within this repo
  • Uses run/state.lock for shared-read safety
  • Same UI/behavior as original overlay.py (colors, fonts, show-current toggle)
"""

# --- portable import bootstrap (find evidence_capture from anywhere) ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ----------------------------------------------------------------------

from pathlib import Path
import sys, argparse, time, json, re, os
from PyQt5 import QtWidgets, QtCore, QtGui

# Repo-anchored paths
from evidence_capture.paths import RUN, ensure_runtime_dirs
from evidence_capture.state import json_read_locked
ensure_runtime_dirs()

# ---------- color utils (unchanged from your overlay.py) ----------
def parse_rgba(s: str):
    s = s.strip()
    if s.lower().startswith("rgba"):
        inner = s[s.find("(")+1 : s.rfind(")")]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) != 4:
            raise ValueError("rgba() requires 4 components")
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        a_raw = parts[3]
        if "/" in a_raw:
            num, den = a_raw.split("/",1)
            a = float(num) / float(den)
        else:
            a = float(a_raw)
        if a > 1.0:
            a = max(0.0, min(1.0, a/255.0))
        else:
            a = max(0.0, min(1.0, a))
        return (r, g, b, a)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 6:
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            return (r,g,b,1.0)
        if len(h) == 8:
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            a = int(h[6:8],16)/255.0
            return (r,g,b,a)
    parts = re.split(r"[,\s]+", s)
    if len(parts) == 3:
        r,g,b = int(parts[0]), int(parts[1]), int(parts[2])
        return (r,g,b,1.0)
    raise ValueError("Unsupported color format: " + s)

def rgb_to_css_rgba(r,g,b,a):
    return f"rgba({r},{g},{b},{a:.3f})"

# ---------- robust JSON read with shared lock ----------
def read_state_json(json_path: Path, lock_path: Path, key: str) -> str:
    """Safely read *key* from state.json using a shared lock. Returns '' on error."""
    try:
        obj = json_read_locked(json_path, lock_path)
        val = obj.get(key, "")
        return str(val) if val is not None else ""
    except Exception:
        return ""

class PrevHashWindow(QtWidgets.QWidget):
    def __init__(self, state_json: Path, lock_file: Path, key: str,
                 bg_rgba, fg_rgba,
                 font_family, font_size, font_weight, padding, valign, refresh,
                 always_on_top=False, show_current=True, title="Hash Display", x=120, y=120, w=600, h=64,
                 placeholder_text="NO-HASH"):
        super().__init__()

        self.state_json = state_json
        self.lock_file = lock_file
        self.key = key

        # config
        self.refresh = max(0.05, float(refresh))
        self.show_current = bool(show_current)
        self.padding = int(padding)
        self.valign = valign
        self.placeholder_text = placeholder_text  # may be ""

        # state
        self.last_seen = ""
        self.prev_seen = ""
        try:
            txt = read_state_json(self.state_json, self.lock_file, self.key).strip()
            self.last_seen = txt
            self.prev_seen = txt if txt else self.placeholder_text
        except Exception:
            self.last_seen = ""
            self.prev_seen = self.placeholder_text

        # window flags (decorated window; OBS Window-Capture friendly)
        flags = (QtCore.Qt.Window | QtCore.Qt.WindowTitleHint | QtCore.Qt.WindowSystemMenuHint |
                 QtCore.Qt.WindowMinMaxButtonsHint)
        if always_on_top:
            flags |= QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle(title)
        self.setGeometry(x, y, w, h)
        self.setMinimumSize(120, 24)

        # layout
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0,0,0,0)
        self.setLayout(layout)

        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout()
        container_layout.setContentsMargins(self.padding, self.padding, self.padding, self.padding)
        container.setLayout(container_layout)

        self.label = QtWidgets.QLabel(self)
        self.label.setWordWrap(True)
        if self.valign == "top":
            self.label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        elif self.valign == "bottom":
            self.label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        else:
            self.label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        container_layout.addWidget(self.label)
        layout.addWidget(container)

        # style
        br, bg, bb, ba = bg_rgba
        fr, fg, fb, fa = fg_rgba
        bg_css = rgb_to_css_rgba(br, bg, bb, ba)
        fg_css = rgb_to_css_rgba(fr, fg, fb, fa)
        style = f"""
            QWidget {{
                background-color: {bg_css};
            }}
            QLabel {{
                color: {fg_css};
                background: transparent;
            }}
        """
        self.setStyleSheet(style)

        # font
        weight = QtGui.QFont.Normal if font_weight.lower() != "bold" else QtGui.QFont.Bold
        qfont = QtGui.QFont(font_family, int(font_size), weight)
        qfont.setStyleStrategy(QtGui.QFont.PreferAntialias)
        self.label.setFont(qfont)

        # initial update
        self.update_display()

        # filesystem watcher + polling (watch the JSON file)
        self.watcher = QtCore.QFileSystemWatcher(self)
        self._watch_path(self.state_json)
        self.watcher.fileChanged.connect(self._on_file_changed)
        self.watcher.directoryChanged.connect(self._on_dir_changed)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(int(self.refresh * 1000))
        self.timer.timeout.connect(self._poll_file)
        self.timer.start()

        self.show()

    def _watch_path(self, path: Path):
        try:
            p = str(path)
            if p not in self.watcher.files():
                parent = str(path.parent)
                if parent not in self.watcher.directories():
                    self.watcher.addPath(parent)
                if path.exists():
                    self.watcher.addPath(p)
        except Exception as e:
            print(f"[overlay_json] watcher add failed: {e}")

    def _on_file_changed(self, _path):
        QtCore.QTimer.singleShot(80, self._read_and_update)

    def _on_dir_changed(self, _path):
        QtCore.QTimer.singleShot(120, lambda: self._watch_path(self.state_json))
        QtCore.QTimer.singleShot(150, self._read_and_update)

    def _poll_file(self):
        try:
            txt = read_state_json(self.state_json, self.lock_file, self.key).strip()
        except Exception:
            txt = ""
        if txt != self.last_seen:
            self._handle_new_text(txt)

    def _read_and_update(self):
        try:
            txt = read_state_json(self.state_json, self.lock_file, self.key).strip()
        except Exception:
            txt = ""
        if txt != self.last_seen:
            self._handle_new_text(txt)
        else:
            self.update_display()

    def _handle_new_text(self, new_text: str):
        if self.last_seen:
            self.prev_seen = self.last_seen
        else:
            if not self.prev_seen:
                self.prev_seen = self.placeholder_text
        self.last_seen = new_text
        self._watch_path(self.state_json)
        self.update_display()

    def update_display(self):
        display = (self.last_seen if self.show_current else self.prev_seen) or self.placeholder_text
        self.label.setText(display)
        self.label.repaint()
        self.update()

def main():
    p = argparse.ArgumentParser(description="Window that displays a key from state.json (for OBS capture).")

    # Window geometry / style (same as your overlay.py)
    p.add_argument("--x", type=int, default=120, help="window X position")
    p.add_argument("--y", type=int, default=120, help="window Y position")
    p.add_argument("--w", type=int, default=1360, help="window width")
    p.add_argument("--h", type=int, default=72, help="window height")
    p.add_argument("--bg", type=str, default="#000000", help="background color (e.g. rgba(...) or #RRGGBB)")
    p.add_argument("--fg", type=str, default="#FFFFFF", help="foreground (text) color")
    p.add_argument("--font", dest="font_family", default="Liberation Mono", help="font family name or path")
    p.add_argument("--font-size", dest="font_size", type=int, default=24)
    p.add_argument("--font-weight", dest="font_weight", choices=["normal","bold"], default="bold")
    p.add_argument("--padding", type=int, default=6, help="inner padding in pixels")
    p.add_argument("--valign", choices=["top","center","bottom"], default="center", help="vertical alignment")
    p.add_argument("--refresh", type=float, default=0.9, help="poll interval in seconds (fallback)")
    p.add_argument("--always-on-top", action="store_true", help="keep window on top")
    p.add_argument("--title", type=str, default="Hash Display", help="window title")

    # JSON source (defaults now repo-relative)
    p.add_argument("--state-json", type=str, default=str(RUN / "state.json"),
                   help="path to state.json (default: repo-root/run/state.json)")
    p.add_argument("--lock-file", type=str, default=str(RUN / "state.lock"),
                   help="path to state.lock for shared reads (default: repo-root/run/state.lock)")
    p.add_argument("--key", type=str, default="last_hash",
                   help="state.json key to display (default: last_hash)")

    # Show current vs previous (same toggle behavior)
    try:
        from argparse import BooleanOptionalAction
        p.add_argument("--show-current", action=BooleanOptionalAction, default=True,
                       help="Show current value (default true). Use --no-show-current to show previous.")
    except Exception:
        p.add_argument("--show-current", dest="show_current", action="store_true", default=True,
                       help="Show current value (default).")
        p.add_argument("--no-show-current", dest="show_current", action="store_false",
                       help="Show previous value instead of current.")

    # Placeholders (kept for parity)
    p.add_argument("--placeholder", type=str, default="NO-HASH",
                   help='Text to show when empty (set to "" for blank).')
    p.add_argument("--init-mode", choices=["placeholder","empty","none"], default="placeholder",
                   help="Kept for parity; only influences initial display (doesn't mutate state.json).")

    args = p.parse_args()

    state_json = Path(args.state_json).expanduser()
    lock_file  = Path(args.lock_file).expanduser()
    state_json.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch(exist_ok=True)

    try:
        bg_rgba = parse_rgba(args.bg)
    except Exception as e:
        print("Bad --bg:", e, file=sys.stderr); sys.exit(2)
    try:
        fg_rgba = parse_rgba(args.fg)
    except Exception as e:
        print("Bad --fg:", e, file=sys.stderr); sys.exit(2)

    app = QtWidgets.QApplication([])

    win = PrevHashWindow(state_json, lock_file, args.key,
                         bg_rgba, fg_rgba,
                         args.font_family, args.font_size, args.font_weight,
                         args.padding, args.valign, args.refresh,
                         always_on_top=args.always_on_top,
                         show_current=args.show_current,
                         title=args.title,
                         x=args.x, y=args.y, w=args.w, h=args.h,
                         placeholder_text=args.placeholder)

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
