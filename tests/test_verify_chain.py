"""The bundle chain checks in verify/ stages 02, 03 and 04, exercised on a
synthetic session built in a temp dir, then on a deliberately tampered copy.
A regression here means a broken chain could be reported as intact.
"""
import csv
import hashlib
import json
from pathlib import Path

from PIL import Image

from conftest import load_script

v02 = load_script("02_PROCESS_file_hash.py")
v03 = load_script("03_PROCESS_png_ocr.py")
v04 = load_script("04_PROCESS_build_bundle_report.py")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _ts(i: int) -> str:
    return f"2026-05-13 22:42:{10 + i:02d}.000000000 CDT"


def make_session(root: Path, n: int = 3) -> None:
    """start_time.json, 0000.json (start block), then 0001..000n with frames.

    Mirrors what the capture loop writes:
      bundle N: prev.last_hash = SHA-256(file N-1.json)
                image.sha256 = prev.last_img_hash = SHA-256(file N.png)
    """
    start = [_ts(0), _ts(0)]
    (root / "start_time.json").write_text(json.dumps(start))
    (root / "start_time.json.ots").write_bytes(b"ots-placeholder")
    b0 = {
        "id": "0000", "index_raw": "0", "sys_time_in": _ts(0), "sys_time_out": _ts(0),
        "prev": {"last_hash": {"value": ""}, "last_img_hash": {"value": ""}},
        "start": {"path": "start_time.json", "sha256": {"value": _sha(root / "start_time.json")}},
        "streams": {"http": [], "net": []}, "downloads": {"files_recent": []},
    }
    (root / "0000.json").write_text(json.dumps(b0))
    (root / "0000.json.ots").write_bytes(b"ots-placeholder")
    prev_json = root / "0000.json"
    for i in range(1, n + 1):
        png = root / f"{i:04d}.png"
        Image.new("RGB", (64, 24), (i * 40 % 255, 0, 0)).save(png)
        img_hash = _sha(png)
        b = {
            "id": f"{i:04d}", "index_raw": str(i), "sys_time_in": _ts(i), "sys_time_out": _ts(i),
            "prev": {"last_hash": {"value": _sha(prev_json)}, "last_img_hash": {"value": img_hash}},
            "image": {"path": str(png), "sha256": {"value": img_hash}},
            "streams": {"http": [], "net": []},
            "downloads": {"files_recent": [] if i != 2 else [
                {"idx": 1, "name": "doc.pdf", "sha256": "ab" * 32, "size_bytes": 10,
                 "sys_time_detected": _ts(i), "vault_pdf": "0001__doc.pdf", "vault_sha256": "cd" * 32}]},
        }
        (root / f"{i:04d}.json").write_text(json.dumps(b))
        prev_json = root / f"{i:04d}.json"


def run_02_and_04(root: Path):
    csv_path = root / "BUNDLE_file_sha256.csv"
    v02.create_bundle_csv(root, csv_path)
    rows = list(csv.DictReader(csv_path.open(newline="")))
    combined = [json.loads((root / f"{i:04d}.json").read_text()) for i in range(0, 4)]
    start_times = json.loads((root / "start_time.json").read_text())
    master, downloads = v04.build_reports(combined, start_times, rows, fps=30.0)
    return {r["bundle_id"]: r for r in master}, downloads, rows


def test_clean_chain_passes_every_check(tmp_path):
    make_session(tmp_path)
    master, downloads, rows = run_02_and_04(tmp_path)
    assert set(master) == {"0000", "0001", "0002", "0003"}
    for bid in ("0001", "0002", "0003"):
        r = master[bid]
        assert r["image_hash_match"] == "True", bid
        assert r["prev_last_hash_matches_json_prev"] == "True", bid
        assert r["prev_last_hash_prev_id"] == f"{int(bid) - 1:04d}", bid
        assert r["prev_last_hash_equals_display_sha256"] == "True", bid
        assert r["prev_last_img_hash_equals_image"] == "True", bid
    assert master["0000"]["start_sha256_match"] == "True"
    # display_sha256 on a PNG row is the hash of the nearest preceding bundle file
    by_name = {r["filename"]: r for r in rows}
    assert by_name["0002.png"]["display_sha256"] == by_name["0001.json"]["file_sha256"]
    # downloads land in the per-download report with a timecode
    assert len(downloads) == 1 and downloads[0]["bundle_id"] == "0002" and downloads[0]["download_timecode"]
    # timecodes are relative to start_time.json[0] at 30 fps: bundle 2 is +2 s
    assert master["0002"]["timecode_in"] == "00:00:02:00"


def test_tampered_bundle_breaks_the_next_link_only(tmp_path):
    make_session(tmp_path)
    p = tmp_path / "0002.json"
    d = json.loads(p.read_text()); d["streams"]["http"] = [{"injected": True}]
    p.write_text(json.dumps(d))
    master, _, _ = run_02_and_04(tmp_path)
    # 0003 recorded the ORIGINAL hash of 0002.json, which no longer exists on disk
    assert master["0003"]["prev_last_hash_matches_json_prev"] == "False"
    assert master["0003"]["prev_last_hash_prev_id"] == ""
    assert master["0003"]["prev_last_hash_equals_display_sha256"] == "False"
    # 0002's own link back to 0001 is untouched
    assert master["0002"]["prev_last_hash_matches_json_prev"] == "True"


def test_swapped_frame_is_detected(tmp_path):
    make_session(tmp_path)
    Image.new("RGB", (64, 24), (0, 0, 255)).save(tmp_path / "0001.png")
    master, _, _ = run_02_and_04(tmp_path)
    assert master["0001"]["image_hash_match"] == "False"
    assert master["0001"]["prev_last_img_hash_equals_image"] == "False"
    assert master["0002"]["image_hash_match"] == "True"


def test_02_refuses_to_run_without_the_start_block(tmp_path):
    make_session(tmp_path)
    (tmp_path / "start_time.json.ots").unlink()
    import pytest
    with pytest.raises(SystemExit):
        v02.create_bundle_csv(tmp_path, tmp_path / "out.csv")


def test_find_previous_json_hash_walks_back_over_gaps():
    h = {0: "h0", 1: "h1", 4: "h4"}
    assert v02.find_previous_json_hash(2, h) == "h1"
    assert v02.find_previous_json_hash(4, h) == "h1"
    assert v02.find_previous_json_hash(5, h) == "h4"
    assert v02.find_previous_json_hash(0, h) is None


def test_ocr_six_char_window_semantics():
    expected = "9562b3ef0319212e6d36297a54ebfcd8fe039fe9c0ddcda5a4d1682468250855"
    assert v03.six_char_match(expected, expected)
    # a real Tesseract read from the example session: two zeros dropped, still matches
    assert v03.six_char_match(expected, "9562b3ef0319212e6d36297a54ebfcd8fed39fe9cddcda5a4d1682468250855")
    assert not v03.six_char_match(expected, "0" * 64)
    assert not v03.six_char_match(expected, "")
    assert not v03.six_char_match("abcde", "abcde")  # shorter than the window
    assert v03.six_char_match(expected.upper(), expected)  # case-insensitive


def test_04_time_helpers():
    assert v04.seconds_to_timecode(12.5, 30) == "00:00:12:15"
    assert v04.seconds_to_timecode(3661.0, 30) == "01:01:01:00"
    dt = v04.parse_sys_time("2026-05-13 22:41:58.791349502 CDT")
    assert (dt.year, dt.hour, dt.microsecond) == (2026, 22, 791349)
    assert v04.parse_sys_time("") is None
