import hashlib
import os

from evidence_capture import hashing

SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_bytes_known_vectors():
    assert hashing.sha256_bytes(b"") == SHA256_EMPTY
    assert hashing.sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_file_matches_hashlib_and_is_chunk_size_independent(tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 17)  # bigger than the 1 MiB read buffer, not a multiple of it
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert hashing.sha256_file(f) == expected
    assert hashing.sha256_file(str(f), bufsize=7) == expected
    assert hashing.sha256_file(f) == hashing.sha256_bytes(data)


def test_sha256_file_detects_single_byte_change(tmp_path):
    f = tmp_path / "x.json"
    f.write_bytes(b'{"id": "0001"}')
    before = hashing.sha256_file(f)
    f.write_bytes(b'{"id": "0002"}')
    assert hashing.sha256_file(f) != before
