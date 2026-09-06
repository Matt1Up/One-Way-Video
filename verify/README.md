# verify/ — Stage 2: independent verification of a capture session

This directory holds the batch pipeline that re-checks everything a capture
session produced. It is the part of One-Way-Video meant to be run by someone
who does **not** trust the person who made the recording. It needs only the
session's `bundles/` directory, Python, Tesseract, the `ots` command, and (for
one stage) network access.

The most distinctive check is stage 03: it crops the bottom strip of every
captured frame, OCRs the hash that the on-screen overlay was displaying, and
compares it with the hash chain. That is what ties the *pictures* to the *data*.

---

## What a session's `bundles/` directory contains

| file | what it is |
|---|---|
| `start_time.json` | A list of 100 local clock readings taken at session start. Its SHA-256 is recorded in bundle `0000`. |
| `NNNN.json` | Bundle *N*: a JSON snapshot written every few seconds (see fields below). |
| `NNNN.png` | The frame captured from the OBS virtual camera when bundle *N* was assembled (700×393 in the example session). Its bottom 18 px show the overlay, i.e. the hash of bundle *N−1*. |
| `NNNN.json.ots`, `NNNN.png.ots`, `start_time.json.ots` | OpenTimestamps receipts committing each file's SHA-256 to the Bitcoin blockchain. |
| `*.ots__time-stamp.json` | A Roughtime receipt for the `.ots` file it is named after. Its nonce is derived from the SHA-256 of that `.ots` file, so a Roughtime server signed a time for it. |
| `network_stream.jsonl.zip` | Optional full packet-level log from tshark. |

Fields in a bundle (`0001.json`):

```
id, index_raw, sys_time_in, sys_time_out      local wall-clock bookkeeping
prev.last_hash.value                          SHA-256 of the previous bundle file (0000.json)
prev.last_img_hash.value                      SHA-256 of this bundle's frame (0001.png)
prev.last_timestamp.value                     midpoint time from the previous Roughtime reply
image.path, image.sha256.value, image.ots     this bundle's frame, its hash, its receipts
streams.http / streams.http_events / streams.net   network activity seen since the last bundle
last_files, downloads.files_recent            files that appeared in the download folder,
                                              with their SHA-256 and the signed vault PDF
```

Bundle `0000` additionally carries `start.sha256.value`, the hash of `start_time.json`.

### The chain

```
0000.json ──hash──▶ shown on screen ──captured in──▶ 0001.png ──hash──▶ 0001.json ──hash──▶ shown ──▶ 0002.png ──▶ 0002.json ──▶ …
                                                       ▲                     │
                                                       └── prev.last_img_hash ┘
```

Each bundle commits to the previous bundle file (`prev.last_hash`) and to its
own frame (`image.sha256`). Each frame shows the previous bundle's hash on
screen. Every bundle file and every frame gets an OpenTimestamps receipt, and
every receipt gets a Roughtime receipt.

---

## Running it

```bash
# one-time
pip install -r requirements.txt          # pytesseract, requests, Pillow, opentimestamps-client
sudo apt install tesseract-ocr           # the OCR engine itself

# run all six stages against a bundles directory
python3 verify/00_RUN_all_bundle_processing.py --bundles-dir /path/to/sessions/<name>/bundles
```

Reports are written into the bundles directory. Stages 01-05 work offline.
Stage 06 talks to the OpenTimestamps calendar servers (to upgrade receipts) and
to `blockstream.info` (to look up block times). It never modifies an original
`.ots` file: it copies each one into `ots_upgraded_bundle/` and upgrades the copy.

Each stage is an ordinary script with `--help`; you can run any one of them by
hand from inside the bundles directory.

---

## What each stage checks

| # | script | what it does | output |
|---|---|---|---|
| 01 | `01_PROCESS_combine_json.py` | Concatenates `NNNN.json` into one array. No check; a convenience for the later stages. | `combined.json` |
| 02 | `02_PROCESS_file_hash.py` | SHA-256 of every file in order. For each `NNNN.png` it records `display_sha256`: the hash of the nearest preceding bundle file, i.e. what the overlay should have been showing when the frame was taken. | `BUNDLE_file_sha256.csv` |
| 03 | `03_PROCESS_png_ocr.py` | Crops the bottom 18 px of each frame, auto-contrasts, sharpens, thresholds, and runs Tesseract with a hex-only whitelist. Sets `is_match_flag` if the OCR text contains **any 6-character window** of `display_sha256`. Saves the processed strips. | `BUNDLE_file_sha256_ocr.csv`, `ocr_png/` |
| 04 | `04_PROCESS_build_bundle_report.py` | Cross-checks what each bundle *says* against what the files *are*: `image_hash_match` (recorded frame hash = actual PNG hash), `prev_last_hash_matches_json_prev` (recorded previous hash = the actual hash of an earlier bundle file, and which one), `prev_last_hash_equals_display_sha256` (that hash is the one the frame should show), `prev_last_img_hash_equals_image`, `start_sha256_match`. Also derives 30 fps timecodes from the local clock. | `BUNDLE_report_master.csv`, `BUNDLE_report_overview.csv`, `BUNDLE_report_downloads.csv` |
| 05 | `05_PROCESS_build_streams_report.py` | Flattens the HTTP, HTTP-event and packet streams across all bundles with timecodes. Informational. | `BUNDLE_streams_http.csv`, `BUNDLE_streams_http_events.csv`, `BUNDLE_streams_net.csv` |
| 06 | `06_PROCESS_verify_bundle_timestamps.py` | For every file with a `.ots` receipt: upgrades a copy, reads the committed hash and the attested Bitcoin block from `ots info`, checks the committed hash equals the file's actual hash (`ots_info_file_sha256_matches_data`), fetches the block's hash and times, then verifies the Roughtime receipt (see below). | `BUNDLE_ts_master.csv`, `BUNDLE_ts_ots.csv`, `BUNDLE_ts_rt.csv` |

`verify_roughtime_bundle.py` is the Roughtime verifier stage 06 imports. It can
also be run alone on one receipt:

```bash
python3 verify/verify_roughtime_bundle.py 0001.json.ots__time-stamp.json \
    --bind-hash "$(sha256sum 0001.json.ots | cut -d' ' -f1)"
```

It checks, for a receipt from `roughtime.cloudflare.com`:

- `rt_ots_cert_sig_ok` — the delegation certificate is signed by the server's long-term Ed25519 key
- `rt_ots_srep_sig_ok` — the signed response is signed by the delegated online key
- `rt_ots_merkle_ok` — the nonce is included in the signed Merkle root
- `rt_ots_nonce_match` — the nonce equals `SHA-512("RTNONC" ‖ SHA-256(file))`, so the reply is about *this* `.ots` file
- `rt_ots_window_ok` — the midpoint lies inside the delegation's validity window

---

## Reading the reports

**`BUNDLE_report_overview.csv`** — one row per bundle. A clean session has
`True` in every check column for every bundle after `0000` (bundle `0000` has no
predecessor and no frame, so its chain columns are blank by design).

**`BUNDLE_file_sha256_ocr.csv`** — one row per file. Only `.png` rows carry OCR
results; `is_match_flag` is `False` on every non-PNG row by construction, not
because anything failed.

**`BUNDLE_ts_ots.csv`** — one row per timestamped file:

| column | meaning |
|---|---|
| `ots_receipt_status` | `anchored` once the receipt contains a Bitcoin block attestation, `pending` while the calendar has not yet committed it (calendars batch to Bitcoin every few hours; re-run later). |
| `ots_block_height`, `ots_block_hash` | The attesting block. |
| `ots_earliest_onchain_mtp_ct` | The block's median-time-past. By consensus rules the block's real time is **after** this. |
| `ots_block_header_time_ct` | The time in the block header (miner-supplied). |
| `ots_latest_onchain_upper_ct` | Header time + 2 h, the most a header time may run ahead of real time. The block's real time is **before** this. |

So a file whose receipt is anchored in block *B* provably existed before
`ots_latest_onchain_upper_ct` of *B*. Times with the `_ct` suffix are in
America/Chicago (the capture machine's zone); Roughtime times are UTC.

**`BUNDLE_ts_rt.csv`** — one row per timestamped file, the Roughtime checks
listed above plus `rt_ots_midp_utc` (the server's time) and `rt_ots_radius_us`
(its stated uncertainty; 1 000 000 µs = ±1 s in the example session).

For a worked example with real numbers, see [`../examples/`](../examples/).

---

## What a passing verification proves, and what it does not

Assume SHA-256 and Ed25519 are sound. If every check passes, you have established:

1. **Integrity.** The files in the directory are exactly the ones whose hashes
   are recorded in each other and in the receipts. Changing one byte of any
   bundle or frame breaks a check.
2. **Order and completeness.** The bundles form one chain in which each commits
   to its predecessor. Nothing can be inserted, removed or reordered without
   breaking a link.
3. **Screen-to-data binding.** The frame captured for bundle *N* was showing the
   hash of bundle *N−1*, as read back by OCR. The chain was being displayed on
   screen while the recording ran, so the video recording, which shows the same
   overlay, can be checked against the chain frame by frame (that is what the
   playback site is for).
4. **Time, as an upper bound.** Every bundle file and frame existed before the
   Bitcoin block that attests it, and every receipt existed at the Roughtime
   midpoint ± radius. In the example session that puts all 280 bundles inside a
   window of a few hours on 13-14 May 2026, with a Roughtime reading every few
   seconds and Bitcoin blocks 949313-949319.

It does **not** establish:

1. **A lower bound on time.** Both timestamp mechanisms prove "existed no later
   than". Nothing here proves the material was not produced *earlier* than the
   timestamps say. `start_time.json` is a list of local clock readings, not an
   unpredictable public value. See
   [Roadmap: proving "no earlier than"](#roadmap-proving-no-earlier-than) below
   for the planned fix.
2. **That the remote websites served genuine content.** TLS is terminated by
   mitmproxy on the capture machine. The HAR and stream data record what the
   browser sent and received, not a signature from the origin server. The
   session proves what was displayed and downloaded on this machine at that
   time; it is no stronger than a screen recording on that particular point.
3. **Anything about the MP4 itself.** The pipeline hashes and OCRs the PNG frame
   captures. The video recording is linked to the chain only visually: the same
   overlay is visible in it. No stage hashes or timestamps the MP4.
4. **The local clock.** `sys_time_*` and the derived timecodes come from the
   capture machine's clock and are for navigation only. The times to rely on are
   the Bitcoin block bounds and the Roughtime midpoints.

Known weaknesses of the checks themselves:

- **OCR is a tolerance check, not a transcription.** In the example session
  Tesseract read the full 64-character hash exactly in 107 of 279 frames; the
  rest dropped or doubled a character or two. The flag therefore uses a 6-hex
  character window. A random 64-character string matches a given hash on some
  6-character window with probability about 3.5 × 10⁻⁶, so a false `True` is
  unlikely, but a `True` does not certify that all 64 characters were legible.
  The processed strips in `ocr_png/` exist so a person can look.
- **The Bitcoin attestation is read, not recomputed.** Stage 06 parses the block
  height out of `ots info` on the upgraded receipt and looks the block up on
  `blockstream.info`. It does not itself walk the receipt's Merkle path to the
  block header. `ots verify <file>.ots` against your own Bitcoin Core node does
  that; the original receipts are shipped unmodified so you can.
- **The Roughtime key comes from the receipt.** The verifier checks the
  signatures against the long-term key stated *in* the receipt. Before trusting
  the times, confirm that key is the one Cloudflare publishes:
  `0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg=` (`Cloudflare-Roughtime-2`, from
  the [roughtime ecosystem list](https://github.com/cloudflare/roughtime/blob/master/ecosystem.json)).
  Every receipt in the example session carries that key.
- **Stage 05 timecodes** were wrong before v2.0: the script assumed a fixed CST
  offset (one hour off for daylight-time sessions) and, on Python 3.10, silently
  dropped every `Z`-suffixed timestamp. Both are fixed in this version. The
  hash-chain and timestamp checks were never affected.

---

## Roadmap: proving "no earlier than"

**The gap.** Every proof above is an upper bound. A Bitcoin block or a Roughtime
reply says "this hash existed by time *T*". Neither says "and not before". Bundle
*N* does carry `prev.last_timestamp`, the midpoint of the previous Roughtime
reply, but that is a plain time string, and a time string is predictable: anyone
can write one. It binds nothing.

**The fix.** Bind each bundle to the previous Roughtime reply's *signature*
instead of (or as well as) its time. The `srep_sig` in a receipt is a 64-byte
Ed25519 signature produced by Cloudflare over a nonce derived from a file that
did not exist until the capture loop created it. It cannot be predicted before
the server returns it. If bundle *N* includes that signature, and bundle *N* is
itself hashed into the chain and timestamped, then bundle *N* was provably
assembled **after** the previous reply's midpoint and **before** its own Bitcoin
block. Every bundle is then sandwiched between two independent clocks:

```
midpoint(N−1) − radius   <   bundle N created   <   block-time upper bound(N)
```

Chained over the whole session, that turns "this recording existed by 03:42
UTC" into "this recording was made between 03:41:58 and 03:42:14 UTC, one
bundle at a time".

**What it costs.** One new field written by the capture loop (for example
`prev.last_rt_sig`, the hex `srep_sig` of the most recent reply), and one new
column in stage 04 that checks it equals `proof.srep_sig` in the receipt whose
midpoint is `prev.last_timestamp`. No change to the timestamping, the overlay,
or the playback engine. Existing sessions stay verifiable exactly as they are;
they simply lack the extra column.

This is the highest-value improvement on the list and it is not implemented yet.
Until it is, read every time in these reports as "no later than".

---

## Checking by hand, without this code

```bash
cd /path/to/bundles

# the chain link between two bundles
sha256sum 0001.json
python3 -c "import json;print(json.load(open('0002.json'))['prev']['last_hash']['value'])"

# what a receipt commits to, and which block attests it
ots info 0001.json.ots
ots verify 0001.json.ots          # needs a local Bitcoin Core node (bitcoind RPC)

# the Roughtime nonce formula
python3 - <<'PY'
import hashlib
h = open('0001.json.ots','rb').read()
print(hashlib.sha512(b'RTNONC' + hashlib.sha256(h).digest()).hexdigest())
PY
# compare with "proof.nonce" in 0001.json.ots__time-stamp.json
```
