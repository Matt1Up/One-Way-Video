#!/usr/bin/env python3
import argparse, json, os, re, csv, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------- path helpers ----------
def expand_env_user(p: str) -> str:
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))

def resolve_relative_to(base_dir: str, p: str) -> str:
    """
    Resolve p relative to base_dir *iff* p is not absolute and does not start with ~ or $.
    Then expand env/user and absolutize.
    """
    if not p:
        return p
    # If the string begins with $ or ~ we want env/user expansion first
    if p.startswith("~") or p.startswith("$"):
        return expand_env_user(p)
    # If already absolute, keep it
    if os.path.isabs(p):
        return os.path.abspath(p)
    # Otherwise, make it relative to base_dir
    return os.path.abspath(os.path.join(base_dir, p))

def strip_vault(node):
    if isinstance(node, dict):
        return {k: strip_vault(v) for k, v in node.items() if k != "vault"}
    if isinstance(node, list):
        return [strip_vault(v) for v in node]
    return node

# ---------- live text parsing ----------
IMG_LINE = re.compile(r'^\s*\[\s*"?(?P<name>[^"\]]+\.(?:png|jpg|jpeg|gif|webp))"?\s*\]\s*$', re.IGNORECASE)

def parse_live_text(txt):
    items, buf = [], []

    def flush_buf():
        if buf:
            block = "\n".join(buf).strip("\n")
            if block:
                items.append({"type": "text", "text": block})
            buf.clear()

    for line in txt.splitlines():
        m = IMG_LINE.match(line)
        if m:
            flush_buf()
            items.append({"type": "image", "src": m.group("name")})
        else:
            buf.append(line)
    flush_buf()
    return items

# ---------- filters loader (tsv/csv/json/plain) ----------
def load_filters_file(path):
    path = path and os.path.abspath(path)
    if not path or not os.path.exists(path):
        return []
    _, ext = os.path.splitext(path.lower())

    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return [{"type":"literal","find":k,"replace":v} for k,v in raw.items()]
        out = []
        for row in (raw or []):
            t = str(row.get("type","literal")).lower()
            if "regex" in row and isinstance(row["regex"], bool):
                t = "regex" if row["regex"] else "literal"
            out.append({"type": "regex" if t=="regex" else "literal",
                        "find": str(row.get("find","")),
                        "replace": str(row.get("replace",""))})
        return out

    if ext in (".csv", ".tsv"):
        delim = "," if ext == ".csv" else "\t"
        out = []
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter=delim)
            for row in reader:
                t = (row.get("type") or "").lower()
                if not t and "regex" in row:
                    t = "regex" if str(row["regex"]).strip().lower() in ("1","true","yes","y") else "literal"
                out.append({"type": "regex" if t=="regex" else "literal",
                            "find": row.get("find",""),
                            "replace": row.get("replace","")})
        return out

    # fallback: plain text lines: FIND \t REPLACE
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 1:
                out.append({"type":"literal","find":parts[0],"replace":""})
            else:
                out.append({"type":"literal","find":parts[0],"replace":parts[1]})
    return out

# ---------- HTTP handler ----------
class NoCacheHandler(SimpleHTTPRequestHandler):
    web_root = "."
    repo_root = "."       # project root (two levels up from run/web-server)
    config = {"profiles": []}
    default_profile = None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    # --- profile helpers ---
    @classmethod
    def profiles_list(cls):
        return [p.get("name") for p in cls.config.get("profiles", [])]

    @classmethod
    def get_profile(cls, name):
        if name:
            for p in cls.config.get("profiles", []):
                if p.get("name") == name:
                    return p
        return cls.default_profile or (cls.config.get("profiles") or [None])[0]

    # Paths in profiles are already resolved at load time.
    @classmethod
    def resolve_json_path(cls, prof):
        return prof and prof.get("json_file")

    @classmethod
    def resolve_live_path(cls, prof):
        return prof and prof.get("live_file")

    @classmethod
    def resolve_filters_path(cls, prof):
        return prof and prof.get("live_filters")

    @classmethod
    def candidate_image_paths(cls, prof, requested):
        # Absolute request: try as-is
        if requested and os.path.isabs(requested):
            yield requested
        dirs = prof.get("image_dirs") or []
        base = os.path.basename(requested) if requested else requested
        for d in dirs:
            if requested:
                yield os.path.join(d, requested)
                yield os.path.join(d, base)
        # Also try within static root
        if requested:
            yield os.path.join(cls.web_root, requested)
            yield os.path.join(cls.web_root, base)

    def _read_text(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # --- routing ---
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        profile_name = qs.get("profile", [None])[0]
        prof = self.get_profile(profile_name)

        # Profiles list (original behavior)
        if parsed.path == "/api/profiles":
            names = self.profiles_list()
            payload = {"profiles": names, "default": (self.default_profile or {}).get("name")}
            b = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return

        # Extra endpoint to fetch full config (harmless)
        if parsed.path == "/api/config":
            payload = json.dumps({"profiles": self.config.get("profiles", []),
                                  "active": prof.get("name") if prof else None}, ensure_ascii=False)
            b = payload.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return

        # Filters (optional, used by UI for live view masking)
        if parsed.path == "/api/filters":
            try:
                path = self.resolve_filters_path(prof)
                filters = load_filters_file(path) if path else []
                b = json.dumps({"filters": filters}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            except Exception as e:
                self.send_error(500, f"Error reading filters: {e}"); return

        # JSON data (original name /api/json) + compat alias /api/data
        if parsed.path in ("/api/json", "/api/data"):
            override = qs.get("file", [None])[0]
            path = expand_env_user(override) if override else self.resolve_json_path(prof)
            if not path:
                self.send_error(404, f"json_file not configured for profile: {prof.get('name') if prof else ''}")
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                safe = strip_vault(data)
                b = json.dumps(safe, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            except FileNotFoundError:
                self.send_error(404, f"JSON not found: {path}"); return
            except Exception as e:
                self.send_error(500, f"Error reading JSON: {e}"); return

        # Live text stream
        if parsed.path == "/api/live":
            override = qs.get("file", [None])[0]
            path = expand_env_user(override) if override else self.resolve_live_path(prof)
            if not path:
                self.send_error(404, f"live_file not configured for profile: {prof.get('name') if prof else ''}")
                return
            try:
                txt = self._read_text(path)
                items = parse_live_text(txt)
                cooked, pname = [], (prof.get("name") if prof else "")
                for it in items:
                    if it["type"] == "image":
                        raw = it["src"]
                        u = "/img?profile=" + urllib.parse.quote(pname, safe="") + "&name=" + urllib.parse.quote(raw, safe="")
                        cooked.append({"type": "image", "url": u, "name": os.path.basename(raw)})
                    else:
                        cooked.append(it)
                b = json.dumps({"items": cooked}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            except FileNotFoundError:
                self.send_error(404, f"Live text not found: {path}"); return
            except Exception as e:
                self.send_error(500, f"Error reading live text: {e}"); return

        # Image resolver
        if parsed.path == "/img":
            pname = qs.get("profile", [None])[0]; prof2 = self.get_profile(pname)
            req_name = qs.get("name", [None])[0]
            if not prof2 or not req_name:
                self.send_error(400, "missing profile or name"); return
            if not str(req_name).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                self.send_error(400, "unsupported image type"); return
            try:
                chosen = None
                for cand in self.candidate_image_paths(prof2, req_name):
                    if cand and os.path.exists(cand):
                        chosen = cand; break
                if not chosen:
                    self.send_error(404, f"image not found: {req_name}"); return
                with open(chosen, "rb") as f: data = f.read()
                lo = chosen.lower()
                ctype = "image/png"
                if lo.endswith(".jpg") or lo.endswith(".jpeg"): ctype = "image/jpeg"
                elif lo.endswith(".gif"): ctype = "image/gif"
                elif lo.endswith(".webp"): ctype = "image/webp"
                self.send_response(200); self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            except Exception as e:
                self.send_error(404, f"image error: {e}"); return

        # Root -> serve index
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

# ---------- config loader ----------
def load_config(config_path: str, repo_root: str):
    """
    Load profiles and resolve all non-absolute paths RELATIVE TO repo_root.
    """
    cfg_file = expand_env_user(config_path)
    if os.path.exists(cfg_file):
        with open(cfg_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        profs = raw.get("profiles", [])
        for p in profs:
            p["json_file"]    = resolve_relative_to(repo_root, p.get("json_file"))
            p["live_file"]    = resolve_relative_to(repo_root, p.get("live_file"))
            p["live_filters"] = resolve_relative_to(repo_root, p.get("live_filters"))
            p["image_dirs"]   = [resolve_relative_to(repo_root, d) for d in (p.get("image_dirs") or [])]
        return {"profiles": profs}
    # Fallback default (rare)
    return {"profiles": [{
        "name": "Default",
        "json_file": os.path.join(repo_root, "run", "downloaded_files.json"),
        "live_file": os.path.join(repo_root, "run", "web-server", "live_hash_loop.txt"),
        "image_dirs": [os.path.join(repo_root, "run", "bundles")]
    }]}

def compute_repo_root_from_web_root(web_root: str) -> str:
    # web_root = <repo>/run/web-server  -> repo_root = dirname(dirname(web_root))
    web_root = os.path.abspath(web_root)
    run_dir = os.path.dirname(web_root)
    repo_root = os.path.dirname(run_dir)
    return repo_root

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Local web server (portable paths, profile-aware)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--root", default=".", help="Static web root (usually run/web-server)")
    ap.add_argument("--config", default="viewer.config.json", help="Profiles config JSON (in web root)")
    args = ap.parse_args()

    # Set roots
    NoCacheHandler.web_root = os.path.abspath(args.root)
    NoCacheHandler.repo_root = compute_repo_root_from_web_root(NoCacheHandler.web_root)

    # Serve static files from web_root
    os.chdir(NoCacheHandler.web_root)

    # Load config & resolve paths relative to repo root (portable)
    NoCacheHandler.config = load_config(args.config, NoCacheHandler.repo_root)
    NoCacheHandler.default_profile = (NoCacheHandler.config.get("profiles") or [None])[0]

    # Log for sanity
    print(f"[web] web_root = {NoCacheHandler.web_root}")
    print(f"[web] repo_root = {NoCacheHandler.repo_root}")
    print(f"[web] profiles = {NoCacheHandler.profiles_list()}")

    with ThreadingHTTPServer((args.host, args.port), NoCacheHandler) as httpd:
        print(f"Serving {NoCacheHandler.web_root} on http://{args.host}:{args.port}")
        httpd.serve_forever()

if __name__ == "__main__":
    main()
