"""Stage 05 time parsing (the two bugs fixed in v2.0), the Roughtime verifier
primitives, and a real Cloudflare Roughtime receipt from the example session.
"""
import hashlib
import json
import shutil
from datetime import timezone

import pytest

from conftest import FIXTURES, load_script

v05 = load_script("05_PROCESS_build_streams_report.py")
vrt = load_script("verify_roughtime_bundle.py", "verify_roughtime_bundle")
v06 = load_script("06_PROCESS_verify_bundle_timestamps.py")

OTS = FIXTURES / "0001.json.ots"
RECEIPT = FIXTURES / "0001.json.ots__time-stamp.json"
CLOUDFLARE_LT_KEY_B64 = "0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg="


# ── stage 05: local-time and ISO parsing ─────────────────────────────

def test_start_time_honours_cdt_vs_cst():
    cdt = v05.parse_start_time_to_utc("2026-05-13 22:41:55.393061139 CDT")
    cst = v05.parse_start_time_to_utc("2026-05-13 22:41:55.393061139 CST")
    assert cdt.tzinfo == timezone.utc and cdt.hour == 3 and cdt.day == 14
    assert (cst - cdt).total_seconds() == 3600  # the old fixed-CST bug was exactly this hour
    assert cdt.microsecond == 393061  # nanoseconds truncated, not rounded away


def test_start_time_rejects_unknown_zone():
    with pytest.raises(ValueError):
        v05.parse_start_time_to_utc("2026-05-13 22:41:55.393061139 XYZ")


def test_iso_parse_accepts_z_suffix_on_py310():
    a = v05.parse_iso_to_utc("2026-05-14T03:42:44.945968Z")
    b = v05.parse_iso_to_utc("2026-05-14T03:42:44.945968+00:00")
    assert a == b and a is not None
    assert v05.parse_iso_to_utc("") is None
    assert v05.parse_iso_to_utc("not a date") is None


# ── Roughtime primitives ─────────────────────────────────────────────

def test_nonce_derivation_formula():
    h = hashlib.sha256(b"file bytes").hexdigest()
    expect = hashlib.sha512(b"RTNONC" + bytes.fromhex(h)).digest()
    assert vrt.derive_nonce_from_sha256_hex(h) == expect
    with pytest.raises(SystemExit):
        vrt.derive_nonce_from_sha256_hex("zz")


def test_merkle_inclusion_two_leaves():
    left, right = b"L" * 64, b"R" * 64
    hl, hr = vrt.hash_leaf(left), vrt.hash_leaf(right)
    root = vrt.hash_node(hl, hr)
    assert vrt.verify_inclusion(left, 0, hr, root)     # index 0 => we are the left child
    assert vrt.verify_inclusion(right, 1, hl, root)    # index 1 => right child
    assert not vrt.verify_inclusion(left, 1, hr, root)  # wrong side
    assert not vrt.verify_inclusion(left, 0, hr, b"\0" * 64)
    assert vrt.verify_inclusion(left, 0, b"", vrt.hash_leaf(left))  # single-leaf tree: empty path


def test_rtmessage_parse_minimal_two_tag_message():
    import struct
    # num_tags=2, offsets=[4] (second value starts at byte 4), tags ascending: NONC < PATH
    tags = sorted([vrt.NONC, vrt.PATH])
    header = struct.pack("<I", 2) + struct.pack("<I", 4) + struct.pack("<II", *tags)
    body = b"AAAA" + b"BBBBBBBB"
    m = vrt.RTMessage.parse(header + body)
    assert m.map[tags[0]] == b"AAAA" and m.map[tags[1]] == b"BBBBBBBB"
    with pytest.raises(ValueError):
        vrt.RTMessage.parse(header + b"AAA")  # truncated


# ── a real receipt from roughtime.cloudflare.com ─────────────────────

def test_fixture_receipt_verifies_end_to_end():
    r = v06.analyze_roughtime_bundle(RECEIPT)
    assert r["server"] == "roughtime.cloudflare.com"
    assert r["lt_pubkey_b64"] == CLOUDFLARE_LT_KEY_B64
    assert r["ok_cert_sig"] and r["ok_srep_sig"] and r["ok_merkle"] and r["ok_window"]
    assert r["nonce_match"] is True and r["all_ok"] is True
    # the receipt is bound to THIS .ots file
    assert r["bind_sha256_hex"] == hashlib.sha256(OTS.read_bytes()).hexdigest()
    assert r["radius_us"] == 1_000_000
    assert r["midp_iso"].startswith("2026-05-14T03:42:17")


def test_fixture_receipt_bound_to_a_different_file_fails(tmp_path):
    other = hashlib.sha256(b"some other file").hexdigest()
    r = v06.analyze_roughtime_bundle(RECEIPT, bind_sha256_hex=other)
    assert r["ok_cert_sig"] and r["ok_srep_sig"]      # signatures still fine …
    assert r["nonce_match"] is False and r["all_ok"] is False  # … but not about this file


def test_forged_signature_is_rejected(tmp_path):
    d = json.loads(RECEIPT.read_text())
    sig = bytearray(bytes.fromhex(d["proof"]["srep_sig"])); sig[0] ^= 0x01
    d["proof"]["srep_sig"] = bytes(sig).hex()
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(d))
    r = v06.analyze_roughtime_bundle(bad)
    assert r["ok_srep_sig"] is False and r["all_ok"] is False
    assert r["ok_cert_sig"] is True  # untouched delegation still verifies


def test_forged_time_outside_delegation_window_is_rejected(tmp_path):
    d = json.loads(RECEIPT.read_text())
    d["proof"]["midpoint_us"] = d["proof"]["maxt_us"] + 1
    bad = tmp_path / "late.json"; bad.write_text(json.dumps(d))
    r = v06.analyze_roughtime_bundle(bad)
    assert r["ok_window"] is False and r["all_ok"] is False


# ── stage 06 helpers ─────────────────────────────────────────────────

def test_ots_info_parsing_helpers():
    txt = "File sha256 hash: dbbbee6525a48e395363db32983ca9c2159369d6da52da064ffaec2da3ce80de\n" \
          "verify BitcoinBlockHeaderAttestation(949313)\n"
    assert v06.extract_height(txt) == 949313
    assert v06.extract_file_hash(txt) == "dbbbee6525a48e395363db32983ca9c2159369d6da52da064ffaec2da3ce80de"
    assert v06.extract_height("PendingAttestation('https://alice.btc.calendar.opentimestamps.org')") is None


def test_classify_file():
    assert v06.classify_file("0001.json") == ("0001", "bundle_json")
    assert v06.classify_file("0042.png") == ("0042", "bundle_png")
    assert v06.classify_file("start_time.json") == ("", "start_time")
    assert v06.classify_file("0001__doc.pdf") == ("", "pdf")


@pytest.mark.skipif(shutil.which("ots") is None, reason="ots CLI not installed")
def test_fixture_receipt_commits_to_the_bundle_file_hash():
    """`ots info` on the .ots fixture names the SHA-256 of 0001.json from the example session."""
    out = v06.run_ots_info(OTS)
    assert v06.extract_file_hash(out) == "dbbbee6525a48e395363db32983ca9c2159369d6da52da064ffaec2da3ce80de"
