# Verifying a session

This page is the short version. The full description of every check, every
report column, and what a pass does and does not prove is
[`verify/README.md`](../verify/README.md). A worked example with real numbers
is [`examples/README.md`](../examples/README.md).

## If you have been handed a session

You need the session's `bundles/` directory. Nothing else from the capture
machine is required.

```bash
git clone https://github.com/Matt1Up/One-Way-Video.git
cd One-Way-Video
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[verify]"
sudo apt install tesseract-ocr

python3 verify/00_RUN_all_bundle_processing.py --bundles-dir /path/to/bundles
```

About five minutes for a 280-bundle session. Then open, in this order:

1. `bundles/BUNDLE_report_overview.csv` — one row per bundle. Every check
   column should read `True` from bundle `0001` on. A `False` anywhere in
   `prev_last_hash_matches_json_prev` means a bundle file was altered,
   inserted, removed or reordered after the fact.
2. `bundles/BUNDLE_file_sha256_ocr.csv` — `is_match_flag` on the `.png` rows.
   `True` means the hash the frame was showing on screen is the hash of the
   previous bundle file. Look at `bundles/ocr_png/` to see what the OCR saw.
3. `bundles/BUNDLE_ts_ots.csv` — `ots_receipt_status` should be `anchored`
   and `ots_block_height` filled. `pending` means the OpenTimestamps calendar
   has not committed yet; wait a few hours and re-run.
4. `bundles/BUNDLE_ts_rt.csv` — `rt_ots_all_ok` should be `yes` on every row,
   and every row's long-term key should be Cloudflare's published key,
   `0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=`.

## Checking without this code

Every check can be repeated with standard tools:

```bash
sha256sum 0001.json                                   # compare with 0002.json → prev.last_hash.value
ots info 0001.json.ots                                # committed hash + attesting block
ots verify 0001.json.ots                              # full check against your own Bitcoin Core node
python3 - <<'PY'                                      # Roughtime nonce formula
import hashlib; d=hashlib.sha256(open('0001.json.ots','rb').read()).digest()
print(hashlib.sha512(b'RTNONC'+d).hexdigest())         # == proof.nonce in 0001.json.ots__time-stamp.json
PY
```

## What a pass means, in one paragraph

The files are exactly the ones whose hashes are recorded in each other and in
the receipts; they form a single ordered chain; the frames were showing that
chain on screen while the recording ran; and every file existed no later than
the Bitcoin block and Roughtime midpoint that attest it. It does not establish
that the material was not created *earlier* than the timestamps say, that the
remote websites served genuine content (TLS is terminated on the capture
machine), or anything about the MP4 itself, which is tied to the chain only
visually. Those limits, and a roadmap item that would add a lower bound on
time, are spelled out in `verify/README.md`.
