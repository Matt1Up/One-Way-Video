# examples/ — what "verified" looks like

One real session, run through the verification pipeline from scratch, with the
reports it produced. You can read these without installing anything, and you
can reproduce them with the commands at the end.

## The session

`fraud-reports-session/` is a 36-minute capture from the evening of
13 May 2026 (US Central time) in which six fraud reports were filed in one
sitting: FBI IC3, the FTC, the US Postal Inspection Service, the Minnesota
Attorney General, the Minnesota Office of the Legislative Auditor, and the
Minnesota House Republicans whistleblower portal. The write-up is at
<https://mncourtfraud.com/closing-the-loop/>. It was chosen as the example
because it is short, self-contained, and each filing ended in a downloaded
confirmation that the capture sealed into a signed vault PDF.

| | |
|---|---|
| bundles | 280 (`0000`-`0279`; the last has a frame but no JSON, the capture was stopped mid-cycle) |
| frames | 279 PNG, 700×393, bottom 18 px = the overlay |
| duration | 00:00:03:12 to 00:35:59:14 at 30 fps |
| bundle interval | median 2.6 s of assembly time per bundle |
| OpenTimestamps receipts | 558 (279 bundle files, 278 frames, `start_time.json`) |
| Roughtime receipts | 558, one per `.ots` file, from `roughtime.cloudflare.com` |
| downloads sealed | 6 confirmation PDFs |

## What is in the directory

```
fraud-reports-session/
  reports/                 the CSVs written by verify/ (see below)
  sample-bundle-files/     bundles 0000-0002, frame 0001, start_time.json, and every receipt for them
  ocr-strips/              7 of the 279 processed overlay crops that stage 03 fed to Tesseract
  filed-complaints/        the six form submissions the HAR extractor recovered, rendered as HTML
                           (USPIS appears twice; the FTC form did not match the extractor's rules and
                           is represented only by its confirmation PDF in the downloads report)
```

The full bundle directory is 20 MB and is not in the repository. The sample
files are enough to check that the reports are about real files: hash one and
find it.

```bash
sha256sum fraud-reports-session/sample-bundle-files/0001.json
grep -c <that hash> fraud-reports-session/reports/BUNDLE_file_sha256.csv   # 2: its own row, and display_sha256 of 0002.png
python3 -c "import json;print(json.load(open('fraud-reports-session/sample-bundle-files/0002.json'))['prev']['last_hash']['value'])"   # the same hash
python3 verify/verify_roughtime_bundle.py fraud-reports-session/sample-bundle-files/0001.json.ots__time-stamp.json
ots info fraud-reports-session/sample-bundle-files/0001.json.ots
```

## Results

Every check passed. Numbers below come from `reports/`; column meanings are in
[`verify/README.md`](../verify/README.md).

**Chain and frames** (`BUNDLE_report_overview.csv`, one row per bundle)

| check | result |
|---|---|
| `prev_last_hash_matches_json_prev` — each bundle names the real hash of an earlier bundle file | 278 / 278 True (bundle `0000` has no predecessor) |
| `prev_last_hash_prev_id` — and it is always the immediately preceding one | 278 / 278 |
| `image_hash_match` — the frame hash recorded in the bundle equals the PNG on disk | 278 / 278 True |
| `prev_last_hash_equals_display_sha256` — the hash the frame should show is the previous bundle's | 278 / 278 True |
| `ocr_match_flag` — Tesseract read that hash off the frame (6-character window) | 279 / 279 True |
| `start_sha256_match` — bundle `0000` names the real hash of `start_time.json` | True |

Of the 279 OCR reads, 107 reproduced all 64 characters exactly; the rest dropped
or doubled one or two characters, which is why the flag uses a window. Look at
`ocr-strips/` to see what Tesseract saw.

**Timestamps** (`BUNDLE_ts_ots.csv`, `BUNDLE_ts_rt.csv`, one row per receipt)

| check | result |
|---|---|
| `ots_receipt_status` | 558 / 558 `anchored` |
| `ots_info_file_sha256_matches_data` — the receipt commits to the file's actual hash | 558 / 558 yes |
| Bitcoin blocks attesting the session | 949313 (32 receipts), 949315 (322), 949316 (29), 949319 (175) |
| `rt_ots_cert_sig_ok`, `rt_ots_srep_sig_ok` — Cloudflare's delegation and response signatures | 558 / 558 yes |
| `rt_ots_merkle_ok`, `rt_ots_nonce_match` — the reply is about this `.ots` file | 558 / 558 yes |
| `rt_ots_window_ok` | 558 / 558 yes |
| Roughtime midpoints | 03:41:58 to 04:17:56 UTC on 14 May 2026, radius ±1 s |

What that establishes about time, precisely: the first bundle existed by
03:41:58 UTC and the last by 04:17:56 UTC according to Cloudflare's clock, and
every file existed before its Bitcoin block, whose real time lies between the
block's median-time-past and its header time + 2 h (both columns are in the
report). Nothing here establishes that the material was not created *earlier*;
see the roadmap section of `verify/README.md`.

**Downloads** (`BUNDLE_report_downloads.csv`)

Six PDFs were detected in the download folder during the session, at bundles
`0080`, `0123`, `0124`, `0155`, `0194` and `0272`, each with its SHA-256 and the
name and hash of the signed vault copy.

## The reports, file by file

| file | one row per | what to look at |
|---|---|---|
| `BUNDLE_file_sha256.csv` | file | SHA-256 of everything; `display_sha256` on PNG rows is what the overlay should have shown |
| `BUNDLE_file_sha256_ocr.csv` | file | adds `ocr_sha256` (what Tesseract read) and `is_match_flag` |
| `BUNDLE_report_master.csv` | bundle | every cross-check plus timing and download summary |
| `BUNDLE_report_overview.csv` | bundle | the readable subset of the above |
| `BUNDLE_report_downloads.csv` | download | detection time, hashes, vault PDF |
| `BUNDLE_streams_http.csv` | HTTP request | the requests the browser made during the session, per bundle, with timecodes; includes the six form POSTs |
| `BUNDLE_streams_net.csv` | packet-level event | tshark-derived flow events with timecodes |
| `BUNDLE_ts_master.csv` | receipt | full timestamp detail |
| `BUNDLE_ts_ots.csv` | receipt | Bitcoin anchoring view |
| `BUNDLE_ts_rt.csv` | receipt | Roughtime view |

`BUNDLE_streams_http_events.csv` is deliberately **not** included. This session
was recorded before v2.0 fixed the capture-stage defect described in
`verify/README.md`, and that report is the direct artifact of it: the
`streams.http_events` field of these bundles holds browsing from about eight
hours before the session that has nothing to do with it. Leaving the report out
and saying so here demonstrates the defect rather than hiding it. It is
unrelated personal data, not evidence. The `streams.http` report above carries
the session's actual requests correctly, and it is what the session's published
playback stream was rebuilt from (`postprocess/build_http_streams.py`). No
verification result depends on `http_events`.

## Masking in `filed-complaints/`

The six HTML files are renderings of the form submissions extracted from the
HAR: the parsed fields, then the verbatim request body. They are derived
pipeline outputs, not part of the hash chain and not timestamped, so editing
them changes no verification result.

The complainant's home street address, unit number, ZIP code and phone number
have been replaced with `[REDACTED]` wherever they appear, including inside the
raw request bodies and one narrative where the phone number was typed in a
different format. Everything else is as submitted: the complainant's name,
business name, email, city and state; the full complaint narratives; and the
addresses of the public bodies complained about. The originals are unchanged
on the capture machine and, being reproducible from the HAR, would be
disclosed unredacted in any proceeding that required them.

## Other things you should know when reading these

- Columns ending in `_ct` are America/Chicago; Roughtime times are UTC.
- `sys_time_*` columns are the capture machine's clock and are for navigation,
  not proof.
- Each bundle JSON records the absolute path of its frame on the capture
  machine (`image.path`). That is inside the hashed content and cannot be
  edited; it is what it is.
- The `*_path` columns in `BUNDLE_ts_master.csv` were rewritten from the
  absolute path of the scratch directory the pipeline ran in to `bundles/…`.
  No other cell was touched.

## Reproducing

```bash
# needs: pip install -r requirements.txt ; sudo apt install tesseract-ocr ; network access
cp -r /path/to/the/full/bundles ./bundles          # 1676 files: NNNN.json/png, *.ots, *.ots__time-stamp.json, start_time.json
python3 verify/00_RUN_all_bundle_processing.py --bundles-dir ./bundles
```

About five minutes: the OCR pass is a minute or two, and stage 06 runs
`ots upgrade` on 558 receipts (one calendar round-trip each, originals left
untouched, upgraded copies in `bundles/ots_upgraded_bundle/`) and four block
lookups on `blockstream.info`. The eleven CSVs land in `./bundles/`. The hash
columns will be identical to the ones here; `data_file_mtime_ct` and the
`*_path` columns will reflect your copy.
