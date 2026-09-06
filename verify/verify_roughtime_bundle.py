#!/usr/bin/env python3
import argparse, base64, binascii, hashlib, json, os, struct, sys
from datetime import datetime, timezone

# === Tiny Ed25519 wrapper (cryptography or PyNaCl) ===
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
                self._Bad = nacl.exceptions.BadSignatureError
                self.key = nacl.signing.VerifyKey(self.pub)
            except Exception as e2:
                raise RuntimeError("Install 'cryptography' or 'pynacl' to verify Ed25519 signatures.") from e2

    def verify(self, sig: bytes, msg: bytes)->bool:
        if self.backend == "cryptography":
            try:
                self.key.verify(sig, msg)
                return True
            except self._InvalidSignature:
                return False
        else:
            try:
                self.key.verify(msg, sig)
                return True
            except self._Bad:
                return False

# === Roughtime parsing helpers (minimal) ===
def tag4(s: bytes) -> int: return struct.unpack("<I", s)[0]
NONC = tag4(b'NONC'); PADF = tag4(b'PAD\xff'); SREP = tag4(b'SREP'); SIG0 = tag4(b'SIG\x00')
CERT = tag4(b'CERT'); INDX = tag4(b'INDX'); PATH = tag4(b'PATH')
ROOT = tag4(b'ROOT'); MIDP = tag4(b'MIDP'); RADI = tag4(b'RADI')
DELE = tag4(b'DELE'); MINT = tag4(b'MINT'); MAXT = tag4(b'MAXT'); PUBK = tag4(b'PUBK')

CTX_SREP = b"RoughTime v1 response signature\0"
CTX_DELE = b"RoughTime v1 delegation signature--\0"

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

# Merkle helpers
def sha512(b: bytes)->bytes: return hashlib.sha512(b).digest()
def hash_leaf(leaf: bytes)->bytes: return sha512(b"\x00"+leaf)
def hash_node(L: bytes, R: bytes)->bytes: return sha512(b"\x01"+L+R)

def verify_inclusion(nonce: bytes, idx: int, path: bytes, root: bytes)->bool:
    if len(root) != 64 or len(path) % 64 != 0: return False
    h = hash_leaf(nonce)
    p = path
    while len(p) > 0:
        sib = p[:64]; p = p[64:]
        h = hash_node(h, sib) if (idx & 1) == 0 else hash_node(sib, h)
        idx >>= 1
    return h == root

def human_time(midp_us: int) -> str:
    return datetime.fromtimestamp(midp_us/1e6, tz=timezone.utc).isoformat()

def sha256_file_hex(path: str, bufsize=1<<20)->str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b: break
            h.update(b)
    return h.hexdigest()

def derive_nonce_from_sha256_hex(hexstr: str) -> bytes:
    try:
        raw = binascii.unhexlify(hexstr)
    except binascii.Error as e:
        raise SystemExit(f"Bad --bind-hash / artifact.bind_sha256_hex: {e}")
    return hashlib.sha512(b"RTNONC" + raw).digest()

def main():
    p = argparse.ArgumentParser(description="Verify a Roughtime JSON bundle and show the NONC math.")
    p.add_argument("bundle", help="Path to roughtime_proof_*.json")
    p.add_argument("--bind-hash", help="SHA-256 hex to bind (overrides bundle.artifact values)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    with open(args.bundle, "r") as f:
        bundle = json.load(f)

    proof = bundle["proof"]
    lt_b64 = bundle["longterm_pubkey_b64"]
    lt_pub = base64.b64decode(lt_b64)

    # Determine SHA-256 to bind
    bind_hex = None
    src = None
    if args.bind_hash:
        bind_hex = args.bind_hash.lower()
        src = "--bind-hash arg"
    else:
        artifact = bundle.get("artifact") or {}
        if "bind_sha256_hex" in artifact:
            bind_hex = artifact["bind_sha256_hex"].lower()
            src = "bundle.artifact.bind_sha256_hex"
        elif "bind_file_path" in artifact and os.path.isfile(artifact["bind_file_path"]):
            bind_hex = sha256_file_hex(artifact["bind_file_path"])
            src = f"SHA-256({artifact['bind_file_path']})"
        else:
            print("NOTE: No bind-hash available in bundle. Provide --bind-hash <sha256hex> to check NONC binding.")
    if bind_hex:
        computed_nonce = derive_nonce_from_sha256_hex(bind_hex)
    else:
        computed_nonce = None

    # Parse bytes from bundle
    cert_b = bytes.fromhex(proof["cert_bytes"])
    cert_sig = bytes.fromhex(proof["cert_sig"])
    srep_b = bytes.fromhex(proof["srep_bytes"])
    srep_sig = bytes.fromhex(proof["srep_sig"])
    online_pub = bytes.fromhex(proof["online_pubkey"])
    nonce_from_bundle = bytes.fromhex(proof["nonce"])
    root = bytes.fromhex(proof["root"])
    path = bytes.fromhex(proof["path"])
    indx = proof["indx"]
    midp = proof["midpoint_us"]; mint = proof["mint_us"]; maxt = proof["maxt_us"]

    # Verify signatures with contexts
    cert = RTMessage.parse(cert_b); dele = cert.map[DELE]
    v_lt = Ed25519Verifier(lt_pub)
    v_online = Ed25519Verifier(online_pub)

    ok_cert = v_lt.verify(cert_sig, CTX_DELE + dele)
    ok_srep = v_online.verify(srep_sig, CTX_SREP + srep_b)

    # Merkle inclusion (requires a nonce)
    if computed_nonce is not None:
        ok_merkle = verify_inclusion(computed_nonce, indx, path, root)
        nonce_match = (computed_nonce == nonce_from_bundle)
    else:
        ok_merkle = verify_inclusion(nonce_from_bundle, indx, path, root)
        nonce_match = None  # unknown; we didn't derive it

    ok_window = (mint <= midp <= maxt)

    # === Print report ===
    print("\n=== Roughtime Bundle Verification ===")
    print(f"Server: {bundle.get('server')}:{bundle.get('port')}   LT key (b64): {lt_b64}")
    print(f"MIDP (UTC): {human_time(midp)}")
    print(f"Delegation window: [{mint}, {maxt}] (UTC μs since epoch)")
    print(f"Signatures: CERT={ok_cert}, SREP={ok_srep}")
    print(f"Delegation window check: {ok_window}")

    if computed_nonce is not None:
        print("\nNONCE binding: using", src)
        print("Formula:  NONC = SHA-512( b'RTNONC' + bytes.fromhex(SHA256) )")
        print(f"SHA256 = {bind_hex}")
        print(f"NONC   = {computed_nonce.hex()}")
        print(f"Bundle NONC matches computed NONC: {nonce_match}")
        print("Merkle inclusion (computed NONC in ROOT via PATH):", ok_merkle)

        # One-liner helpers (quote-safe, no funky escapes)
        print("\n# One-liner: from known SHA-256 hex → NONC")
        print("python - <<'PY'")
        print("import hashlib, binascii")
        print(f"h='{bind_hex}'")
        print("print(hashlib.sha512(b'RTNONC'+binascii.unhexlify(h)).hexdigest())")
        print("PY")
        if bundle.get("artifact", {}).get("bind_file_path"):
            fp = bundle["artifact"]["bind_file_path"]
            print("\n# One-liner: from file → NONC (reads whole file)")
            cmd = ("python -c \"import hashlib,binascii; "
                   f"p=r'{fp}'; "
                   "d=hashlib.sha256(open(p,'rb').read()).digest(); "
                   "print(hashlib.sha512(b'RTNONC'+d).hexdigest())\"")
            print(cmd)
    else:
        print("\nNONCE binding: no SHA-256 available to re-derive NONC.")
        print("Merkle inclusion was checked using the bundle's nonce value only:", ok_merkle)

    all_ok = ok_cert and ok_srep and ok_window and ok_merkle and (nonce_match in (True, None))
    print("\nRESULT:", "OK ✅" if all_ok else "FAILED ❌")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
