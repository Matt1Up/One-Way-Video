#!/usr/bin/env python3
"""
Range-enabled static file server (for HTML5 video seeking) — v3

- Serves index.html correctly (no forced directory listing)
- Supports byte-range requests (206 Partial Content)
- Suppresses noisy BrokenPipeError / ConnectionResetError that commonly happen during scrubbing
  (the browser cancels in-flight range requests while you drag the playhead)

Usage:

  python3 serve.py --bind 127.0.0.1 --port 8000

Or:
  python3 serve.py --dir /path/to/web_demo
"""
from __future__ import annotations

import argparse
import http.server
import os
import re
from pathlib import Path

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)?$")
_CHUNK = 64 * 1024  # 64 KiB


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self):
        # Advertise range support broadly; harmless for non-media responses.
        # (Browsers don't require this header, but it can help tooling/debugging.)
        try:
            self.send_header("Accept-Ranges", "bytes")
        except Exception:
            pass
        super().end_headers()

    def send_head(self):
        # For directories, let the base handler serve index.html if present.
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        # Range request
        m = _RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(416, "Invalid Range")
            return None

        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        size = fs.st_size

        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else size - 1

        if start >= size or end < start:
            f.close()
            self.send_error(416, "Range Not Satisfiable")
            return None

        end = min(end, size - 1)
        length = end - start + 1
        ctype = self.guess_type(path)

        self.send_response(206)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()

        # Stream the requested range. Browsers often cancel and re-request while scrubbing.
        try:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # Client went away (common during scrubbing). Not a real server problem.
                    break
                remaining -= len(chunk)
        finally:
            f.close()

        # We've already written the response body (or client disconnected).
        return None

    def do_GET(self):
        try:
            f = self.send_head()
            if f:
                try:
                    self.copyfile(f, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    f.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        try:
            f = self.send_head()
            if f:
                f.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Optional: silence "Broken pipe" tracebacks even if something slips through
    def log_error(self, format, *args):
        msg = (format % args) if args else format
        if "Broken pipe" in msg or "Connection reset" in msg:
            return
        super().log_error(format, *args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=".", help="Directory to serve (default: current working directory)")
    args = ap.parse_args(argv)

    directory = str(Path(args.dir).expanduser().resolve())
    handler = lambda *h_args, **h_kwargs: RangeRequestHandler(*h_args, directory=directory, **h_kwargs)

    http.server.test(
        HandlerClass=handler,
        ServerClass=http.server.ThreadingHTTPServer,
        port=args.port,
        bind=args.bind,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
