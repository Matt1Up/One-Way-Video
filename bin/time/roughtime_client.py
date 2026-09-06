#!/usr/bin/env python3
import argparse, base64, binascii, hashlib, json, os, socket, struct, sys, time
from datetime import datetime, timezone

# --- portable import bootstrap: make repo imports available everywhere ---
import pathlib as _pathlib
_REPO_ROOT = _pathlib.Path(__file__).resolve().parents[2]  # bin/time/ -> bin/ -> repo/
sys.path.insert(0, str(_REPO_ROOT))
from evidence_capture.paths import RUN  # repo-anchored run/

"""
Roughtime client with file-binding, convenience outputs, and path hooks.

Default paths updated for portability:
  --last-time     defaults to  repo/run/last_roughtime.txt
  --last-imghash  defaults to  repo/run/last_img_hash.txt
  --json-out      defaults to  repo/run/bundles/

Otherwise behavior and flags are unchanged.

Example:
python3 time/roughtime_client.py query -v \
  --server roughtime.cloudflare.com --port 2003 \
  --pubkey-base64 0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg= \
  --reveal-hash \
  --bind-file ./run/bundles/0001.png
"""

# ---------- Ed25519 backend ----------
class Ed25519Verifier:
    def __init__(self, pubkey_bytes: bytes):
        self.pub = pubkey_bytes
        self.backend = None
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
            self._InvalidSignature = InvalidSignature
            self.backend = "cryptography"
            self.key = Ed25519PublicKey.from_public_bytes(self.pub)
        except Exception:
            try:
                import nacl.signing, nacl.exceptions
                self.backend = "pynacl"
                self._nacl_bad = nacl.exceptions.BadSignatureError
                self.key = nacl.signing.VerifyKey(self.pub)
            except Exception as e2:
                raise RuntimeError(
                    "No Ed25519 backend found. Install 'cryptography' or 'pynacl'."
                ) from e2

    def verify(self, signature: bytes, message: bytes) -> bool:
        if self.backend == "cryptography":
            try:
                self.key.verify(signature, message)
                return True
            except self._InvalidSignature:
                return False
        else:  # PyNaCl
            try:
                self.key.verify(message, signature)
                return True
            except self._nacl_bad:
                return False

# ---------- Roughtime tags ----------
def tag4(s: bytes) -> int: return struct.unpack("<I", s)[0]
NONC = tag4(b'NONC'); PADF = tag4(b'PAD\xff'); SREP = tag4(b'SREP'); SIG0 = tag4(b'SIG\x00')
CERT = tag4(b'CERT'); INDX = tag4(b'INDX'); PATH = tag4(b'PATH')
ROOT = tag4(b'ROOT'); MIDP = tag4(b'MIDP'); RADI = tag4(b'RADI')
DELE = tag4(b'DELE'); MINT = tag4(b'MINT'); MAXT = tag4(b'MAXT'); PUBK = tag4(b'PUBK')

# Roughtime v1 signature contexts
CTX_SREP = b"RoughTime v1 response signature\0"
CTX_DELE = b"RoughTime v1 delegation signature--\0"

# ---------- Message codec ----------
class RTMessage:
    def __init__(self, mapping=None): self.map = mapping or {}

    @staticmethod
    def parse(buf: bytes) -> "RTMessage":
        if len(buf) < 4: raise ValueError("buffer too short")
        num = struct.unpack_from("<I", buf, 0)[0]
        header_len = 4 + 4*max(0, num-1) + 4*num
        if len(buf) < header_len: raise ValueError("truncated header")

        offs = [0]
        for i in range(num-1):
            off = struct.unpack_from("<I", buf, 4+4*i)[0]
            if off % 4: raise ValueError("offset not multiple of 4")
            offs.append(off)

        base = 4 + 4*max(0, num-1)
        tags = [struct.unpack_from("<I", buf, base+4*i)[0] for i in range(num)]
        if any(tags[i] >= tags[i+1] for i in range(len(tags)-1)):
            raise ValueError("tags not strictly ascending")

        block = buf[header_len:]
        lens = []
        for i in range(num):
            start = offs[i]
            end = offs[i+1] if i+1 < num else len(block)
            if end < start: raise ValueError("invalid offsets")
            lens.append(end-start)

        mapping = {}
        for t, start, ln in zip(tags, offs, lens):
            if ln % 4: raise ValueError("value length not multiple of 4")
            mapping[t] = block[start:start+ln]
        return RTMessage(mapping)

    def encode(self) -> bytes:
        items = sorted(self.map.items(), key=lambda kv: kv[0])
        tags = [t for t,_ in items]; vals = [v for _,v in items]
        for v in vals:
            if len(v) % 4: raise ValueError("values must be multiple of 4 bytes")
        num = len(tags)
        offs = []; acc = 0
        for i in range(num-1):
            acc += len(vals[i]); offs.append(acc)
        header = struct.pack("<I", num)
        if num >= 2: header += b"".join(struct.pack("<I", o) for o in offs)
        header += b"".join(struct.pack("<I", t) for t in tags)
        return header + b"".join(vals)

# ---------- hashing / merkle ----------
def sha256_file_hex(path: str, bufsize: int = 1<<20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(bufsize), b""):
            h.update(chunk)
    return h.hexdigest()

def sha512(b: bytes)->bytes: return hashlib.sha512(b).digest()
def hash_leaf(leaf: bytes)->bytes: return sha512(b"\x00"+leaf)
def hash_node(L: bytes, R: bytes)->bytes: return sha512(b"\x01"+L+R)

def verify_inclusion(nonce: bytes, idx: int, path: bytes, root: bytes)->bool:
    if len(root) != 64 or len(path) % 64 != 0:
        return False
    h = hash_leaf(nonce)
    p = path
    while len(p) > 0:
        sib = p[:64]; p = p[64:]
        h = hash_node(h, sib) if (idx & 1) == 0 else hash_node(sib, h)
        idx >>= 1
    return h == root

def human_time(midp_us: int) -> str:
    return datetime.fromtimestamp(midp_us/1e6, tz=timezone.utc).isoformat()

def human_time_trim_seconds(midp_us: int) -> str:
    dt = datetime.fromtimestamp(midp_us/1e6, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat()

# ---------- request / response ----------
def build_request(nonce: bytes, min_size=1024)->bytes:
    if len(nonce) != 64: raise ValueError("NONC must be 64 bytes")
    req = RTMessage({NONC: nonce, PADF: b""}).encode()
    if len(req) < min_size:
        req += b"\x00" * (min_size - len(req))
    return req

def process_response(resp: bytes, nonce: bytes, lt_pub: bytes, verbose=False, verify=True):
    top = RTMessage.parse(resp)
    if verbose:
        print(f"[+] top-level tags: {[hex(t) for t in top.map.keys()]}")

    required = (SREP, SIG0, CERT, INDX, PATH)
    if not all(t in top.map for t in required):
        have = [hex(t) for t in top.map.keys()]
        raise ValueError(f"Top-level response missing required tags. Have: {have}")

    srep_b = top.map[SREP]
    sig_top = top.map[SIG0]
    cert_b  = top.map[CERT]
    indx_b  = top.map[INDX]
    path_b  = top.map[PATH]

    indx = struct.unpack("<I", indx_b)[0]

    cert = RTMessage.parse(cert_b)
    dele = cert.map.get(DELE); sig_cert = cert.map.get(SIG0)
    if not (dele and sig_cert): raise ValueError("CERT missing DELE or SIG\\x00")

    dmsg = RTMessage.parse(dele)
    pubk = dmsg.map.get(PUBK); mint = dmsg.map.get(MINT); maxt = dmsg.map.get(MAXT)
    if not (pubk and mint and maxt): raise ValueError("DELE missing PUBK/MINT/MAXT")

    online_pub = pubk
    mint_us = struct.unpack("<Q", mint)[0]
    maxt_us = struct.unpack("<Q", maxt)[0]

    srep = RTMessage.parse(srep_b)
    root = srep.map.get(ROOT); midp_b = srep.map.get(MIDP); radi_b = srep.map.get(RADI)
    if not (root and midp_b and radi_b): raise ValueError("SREP missing ROOT/MIDP/RADI")

    midp_us = struct.unpack("<Q", midp_b)[0]
    radi_us = struct.unpack("<I", radi_b)[0]

    if verbose:
        print(f"[+] MIDP(us)={midp_us} ({human_time(midp_us)}), RADI(us)={radi_us}")
        print(f"[+] INDX={indx}, PATH_len={len(path_b)}, ROOT[0:8]={root[:8].hex()}...")
        print(f"[+] Delegation window: [{mint_us}, {maxt_us}], online_pub[0:8]={online_pub[:8].hex()}...")

    if not verify:
        return midp_us, radi_us, {
            "srep_bytes": srep_b.hex(), "cert_bytes": cert_b.hex(),
            "indx": indx, "path": path_b.hex(), "root": root.hex(),
            "midpoint_us": midp_us, "radius_us": radi_us,
            "mint_us": mint_us, "maxt_us": maxt_us, "online_pubkey": online_pub.hex()
        }

    if len(sig_cert) != 64: raise ValueError("invalid CERT signature length")
    if verbose: print("[*] Verifying CERT (LT -> DELE) with context ...")
    if not Ed25519Verifier(lt_pub).verify(sig_cert, CTX_DELE + dele):
        raise ValueError("CERT signature invalid")

    if not sig_top or len(sig_top) != 64: raise ValueError("missing/invalid SREP signature")
    if verbose: print("[*] Verifying SREP signature (online key) with context ...")
    if not Ed25519Verifier(online_pub).verify(sig_top, CTX_SREP + srep_b):
        raise ValueError("SREP signature invalid")

    if verbose: print("[*] Verifying Merkle inclusion of our NONC ...")
    if not verify_inclusion(nonce, indx, path_b, root):
        raise ValueError("Merkle inclusion failed (nonce not in tree)")

    if verbose: print("[*] Checking MIDP within delegation window ...")
    if not (mint_us <= midp_us <= maxt_us):
        raise ValueError("MIDP outside [MINT, MAXT]")

    proof = {
        "midpoint_us": midp_us, "radius_us": radi_us,
        "mint_us": mint_us, "maxt_us": maxt_us,
        "online_pubkey": online_pub.hex(), "longterm_pubkey": lt_pub.hex(),
        "indx": indx, "nonce": nonce.hex(), "root": root.hex(), "path": path_b.hex(),
        "srep_bytes": srep_b.hex(), "srep_sig": sig_top.hex(),
        "cert_bytes": cert_b.hex(), "cert_sig": sig_cert.hex(),
    }
    return midp_us, radi_us, proof

def save_bundle(server, port, lt_b64, proof: dict, raw: bytes, out=None,
                reveal_hash=False, bound_sha256_hex=None, bound_file=None):
    bundle = {
        "version": 1, "server": server, "port": port,
        "longterm_pubkey_b64": lt_b64,
        "received_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proof": proof, "raw_response_hex": raw.hex(),
    }
    artifact = {}
    if bound_file:
        artifact["bind_file_basename"] = os.path.basename(bound_file)
    if reveal_hash and bound_sha256_hex:
        artifact["bind_sha256_hex"] = bound_sha256_hex
    if artifact:
        bundle["artifact"] = artifact

    out = out or f"roughtime_proof_{int(time.time())}.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f: json.dump(bundle, f, indent=2)
    return out

# ---------- UDP query ----------
def query(server, port, lt_b64, timeout=6.0, retries=5, bind_hash=None, verbose=False, verify=True):
    lt_pub = base64.b64decode(lt_b64)
    if len(lt_pub) != 32: raise SystemExit("Long-term public key must be 32 bytes (base64)")

    if bind_hash:
        try:
            file_hash = binascii.unhexlify(bind_hash)
        except binascii.Error:
            raise SystemExit("--bind-hash must be hex sha256")
        nonce = hashlib.sha512(b"RTNONC" + file_hash).digest()
        if verbose: print(f"[+] NONC derived from hash {bind_hash[:16]}...")
    else:
        nonce = os.urandom(64)
        if verbose: print("[+] NONC = 64 random bytes")

    req = build_request(nonce, 1024)
    addr = (server, port)
    last_err = None

    for i in range(retries + 1):
        try:
            if verbose: print(f"[*] UDP send -> {server}:{port} (attempt {i+1}/{retries+1})")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(req, addr)
                resp, _ = s.recvfrom(4096)
                midp_us, radi_us, proof = process_response(resp, nonce, lt_pub, verbose=verbose, verify=verify)
                return midp_us, radi_us, proof, resp
        except Exception as e:
            last_err = e
            if verbose: print(f"[!] attempt {i+1} failed: {e}")
            time.sleep(0.3 + 0.2*i)

    raise SystemExit(f"Failed to get/verify Roughtime response: {last_err}")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Roughtime client (Cloudflare/Google) with proof + file-binding.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_q = sub.add_parser("query", help="Query and save a proof bundle.")
    ap_q.add_argument("--server", default="roughtime.cloudflare.com")
    ap_q.add_argument("--port", type=int, default=2003)
    ap_q.add_argument("--pubkey-base64", default="0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=")
    ap_q.add_argument("--timeout", type=float, default=6.0)
    ap_q.add_argument("--retries", type=int, default=5)
    ap_q.add_argument("--out", default=None)
    ap_q.add_argument("--bind-hash", default=None, help="Bind NONC to this sha256 hex (e.g., file hash).")
    ap_q.add_argument("--bind-file", default=None, help="Bind NONC to the SHA-256 of this file.")
    ap_q.add_argument("--reveal-hash", action="store_true", help="Include file's sha256 in JSON bundle (outside signed bytes).")
    # --- Portable defaults here ---
    ap_q.add_argument("--last-time", default=str(RUN / "last_roughtime.txt"),
                      help="Path to write trimmed MIDP time (UTC). Default: repo/run/last_roughtime.txt")
    ap_q.add_argument("--last-imghash", default=str(RUN / "last_img_hash.txt"),
                      help="Path to write SHA-256 of --bind-file before querying. Default: repo/run/last_img_hash.txt")
    ap_q.add_argument("--json-out", default=str(RUN / "bundles"),
                      help="Directory to save the JSON bundle. Default: repo/run/bundles/")
    ap_q.add_argument("--verbose", "-v", action="store_true")
    ap_q.add_argument("--no-verify", action="store_true", help="Skip crypto checks; still parse and print.")

    ap_v = sub.add_parser("verify", help="Verify a saved proof bundle.")
    ap_v.add_argument("bundle")
    ap_v.add_argument("--verbose", "-v", action="store_true")

    args = ap.parse_args()

    if args.cmd == "query":
        # --- Preprocessing: handle --bind-file, write --last-imghash ---
        bound_sha256_hex = None
        bound_file = None
        out_path = None

        if args.bind_file:
            bound_file = os.path.expanduser(args.bind_file)
            if not os.path.isfile(bound_file):
                raise SystemExit(f"--bind-file not found: {bound_file}")
            if args.verbose:
                print(f"[*] Computing SHA-256 of file: {bound_file}")
            bound_sha256_hex = sha256_file_hex(bound_file)
            if args.verbose:
                print(f"[+] SHA-256({os.path.basename(bound_file)}) = {bound_sha256_hex}")
            if args.last_imghash:
                p = os.path.expanduser(args.last_imghash)
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                with open(p, "w") as fh:
                    fh.write(bound_sha256_hex + "\n")
                if args.verbose:
                    print(f"[+] Wrote last image hash -> {p}")
            args.bind_hash = bound_sha256_hex

            if args.json_out:
                out_dir = os.path.expanduser(args.json_out)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, os.path.basename(bound_file) + "__time-stamp.json")
            elif args.out:
                out_path = os.path.expanduser(args.out)

        else:
            if args.json_out:
                out_dir = os.path.expanduser(args.json_out)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"roughtime_proof_{int(time.time())}.json")
            elif args.out:
                out_path = os.path.expanduser(args.out)

        # --- Query ---
        midp_us, radi_us, proof, raw = query(
            args.server, args.port, args.pubkey_base64,
            timeout=args.timeout, retries=args.retries,
            bind_hash=args.bind_hash, verbose=args.verbose,
            verify=(not args.no_verify)
        )

        # --- Postprocessing: write last-time (trim seconds) ---
        trimmed = human_time_trim_seconds(midp_us)
        if args.last_time:
            p = os.path.expanduser(args.last_time)
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w") as ft:
                ft.write(trimmed + "\n")
            if args.verbose:
                print(f"[+] Wrote MIDP (trimmed) -> {p}: {trimmed}")

        # --- Save bundle ---
        if not args.no_verify:
            outp = save_bundle(
                args.server, args.port, args.pubkey_base64, proof, raw,
                out=out_path, reveal_hash=args.reveal_hash,
                bound_sha256_hex=bound_sha256_hex, bound_file=bound_file
            )
            print(f"\nRoughtime (server={args.server}:{args.port})")
            print(f"  MIDP (UTC): {human_time(midp_us)}")
            print(f"  RADI (μs):  {radi_us}  (~{radi_us/1e6:.6f} s)")
            print(f"Saved proof bundle: {outp}")
            print(f"Verify later with:\n  python {os.path.basename(__file__)} verify {outp}")
        else:
            print("(no-verify mode: not saving proof bundle)")

    else:
        with open(args.bundle, "r") as f:
            bundle = json.load(f)
        lt = base64.b64decode(bundle["longterm_pubkey_b64"])
        pr = bundle["proof"]

        cert_b = bytes.fromhex(pr["cert_bytes"]); cert = RTMessage.parse(cert_b)
        dele = cert.map[DELE]; cert_sig = bytes.fromhex(pr["cert_sig"])
        if not Ed25519Verifier(lt).verify(cert_sig, CTX_DELE + dele):
            raise SystemExit("Bundle: CERT invalid")

        dmsg = RTMessage.parse(dele)
        online = dmsg.map[PUBK]
        srep_b = bytes.fromhex(pr["srep_bytes"]); srep_sig = bytes.fromhex(pr["srep_sig"])
        if not Ed25519Verifier(online).verify(srep_sig, CTX_SREP + srep_b):
            raise SystemExit("Bundle: SREP invalid")

        RTMessage.parse(srep_b)  # structure check only; raises on a malformed SREP
        if not verify_inclusion(bytes.fromhex(pr["nonce"]), pr["indx"],
                                bytes.fromhex(pr["path"]), bytes.fromhex(pr["root"])):
            raise SystemExit("Bundle: Merkle inclusion failed")

        midp = pr["midpoint_us"]
        if not (pr["mint_us"] <= midp <= pr["maxt_us"]):
            raise SystemExit("Bundle: MIDP outside delegation window")

        if args.verbose:
            print("[v] bundle verified: CERT, SREP, INCLUSION, WINDOW all good")
        print("OK: proof bundle verifies.")
        print(f"  MIDP (UTC): {human_time(midp)}")
        print(f"  RADI (μs):  {pr['radius_us']} (~{pr['radius_us']/1e6:.6f} s)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
