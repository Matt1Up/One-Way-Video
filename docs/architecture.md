# Architecture

One-Way-Video is three stages that share one data format. Each stage is a
separate directory in this repository and can be used without the other two.

```
 CAPTURE (bin/, evidence_capture/)          VERIFY (verify/)              PUBLISH (postprocess/, playback/)
 ───────────────────────────────────        ──────────────────────        ─────────────────────────────────
 Firefox ─proxy─▶ mitmproxy ──┐             re-hash every file            final_conversion.py
 OBS ─▶ virtual camera ─▶ frames │          re-derive the chain           ├─ verify stages 01-06
 tshark ─▶ packet stream       ├─▶ bundles  OCR the overlay strip          ├─ .dump → HAR (bodies kept)
 download folder ─▶ vault PDFs │            upgrade + read OTS receipts    ├─ HAR → form / case CSVs
 every N s: loop_json.py ──────┘            verify Roughtime receipts     └─ process_timecodes.py → JSON streams
   ├─ NNNN.png  (frame, shows hash N-1)     ─▶ 11 CSV reports                  ─▶ data/<slug>/ for the browser player
   ├─ NNNN.json (bundle N)
   ├─ *.ots     (OpenTimestamps)
   └─ *.ots__time-stamp.json (Roughtime)
```

## Stage 1: capture

A controller (`bin/control_all.py`, or `evcap` after `pip install -e .`)
starts and stops a set of long-running components, each guarded by a lock file
in `run/locks/` so only one instance can exist:

| component | script | what it does |
|---|---|---|
| `ffox` | Firefox with a dedicated profile | Browses through the proxy at `127.0.0.1:18080`; downloads land in `downloads/`. |
| `mitm` | `mitm_dump_control.py` + `httpstream_json.py` | mitmproxy in dump mode, writing a `.dump` of every flow; the addon pushes compact rows (`streams.http`) and detailed flow records (`streams.http_events`) into `run/state.json`. |
| `net` | `netstream_json.py` | tshark packet events, newest first, into `streams.net`; the full stream is kept as `network_stream.jsonl` and zipped at session end. |
| `hash` / `vcam-overlay` | `overlay_json.py` / `overlay_vcam_hash.py` | The 1920×72 overlay showing `last_hash` from state. `vcam-overlay` renders it into a second virtual camera device so OBS composites it. |
| `vcam` | `obs_studio_ctrl.py` | Turns OBS's virtual camera on and starts/stops recording over the OBS WebSocket. |
| `watch-start` | `download_watcher_json.py` | Watches `downloads/`; each new file is hashed, inspected (`pdfsig`, `exiftool`), timestamped, and embedded into a PDF signed by `sign_pdfs.py`. |
| `ws-dl`, `ws-bf1`, `ws-bf2` | `run/web-server/*` | Optional local viewers at `http://127.0.0.1:8050/`. |

`start_session "<name>" [interval]` runs `start_json.py`, which resets state,
writes `start_time.json` (100 clock readings) and bundle `0000` committing to
its hash, starts OBS recording, and launches `capture_randomized_save_json.py`.
That script grabs a frame from the virtual camera every *interval* seconds and
calls `loop_json.py` once per frame.

`loop_json.py` is the heart of the capture. For frame *N* it:

1. hashes the PNG; writes the hash into state as `last_img_hash`
2. requests an OpenTimestamps receipt for the PNG and a Roughtime receipt
   whose nonce is derived from the SHA-256 of that `.ots` file
3. snapshots the newest HTTP, network and download entries from state
4. writes bundle `N.json` containing `prev.last_hash` (the SHA-256 of
   `N-1.json`), the frame hash, and the snapshots
5. hashes `N.json`, writes that hash into state as `last_hash`, which the
   overlay displays until the next bundle, so frame `N+1` will show it
6. requests OpenTimestamps and Roughtime receipts for `N.json`

Every write to `run/state.json` goes through `evidence_capture.state`, a
locked, atomic read-modify-write, because several processes (the proxy addon,
the packet streamer, the download watcher and the loop) update it concurrently.

`stop_session` stops the components, stops OBS recording, converts the
mitmproxy `.dump` into a sanitized HAR, and `archive_session.py` moves the
session into `sessions/<name>/`.

### The shared package

`evidence_capture/` is imported by every script:

| module | provides |
|---|---|
| `paths` | repo-relative paths (`ROOT`, `RUN`, `BIN`…), overridable with `EVCAP_HOME`; the interpreter used for workers (`EVCAP_PY`) |
| `state` | `Flock`, `json_update_locked`, the `state_*` helpers, stream snapshots |
| `hashing` | `sha256_file`, `sha256_bytes` |
| `timeutil` | `ts_local_ns()` (nanosecond local time with zone abbreviation), `now_utc_iso()` |
| `evidence` | `roughtime_bind()`, `ots_stamp()` wrappers around the CLI tools |
| `process` | logging helpers, `run_cmd`, `wait_for_file` |
| `config` | viewer config loading; signing-credential defaults (`None`; use env vars) |
| `cli` | the `evcap` entry point |

## Stage 2: verify

`verify/` is documented in full in [`verify/README.md`](../verify/README.md).
In one sentence: it re-derives every hash, checks every link in the chain,
OCRs the overlay off every frame and compares it to the chain, upgrades the
OpenTimestamps receipts and reads their Bitcoin anchoring, and verifies the
Ed25519 signatures and nonce binding of every Roughtime receipt, then writes
CSV reports. It needs only the `bundles/` directory of a session.

## Stage 3: publish

`postprocess/final_conversion.py` turns a session directory into the event
streams the browser player reads: it runs the verify stages, converts the
`.dump` to a HAR with bodies, extracts form submissions or court-record lookups
from the HAR, and aligns everything to the video's 30 fps timeline with
`process_timecodes.py`. `playback/` is the static web app that plays the
recording with all of it in sync. See [`postprocess/README.md`](../postprocess/README.md)
and [`playback/README.md`](../playback/README.md).

## Data formats

**Bundle `NNNN.json`** (see `examples/fraud-reports-session/sample-bundle-files/`):

```
id, index_raw, sys_time_in, sys_time_out
prev.last_hash        SHA-256 of the previous bundle file   ← the chain link
prev.last_img_hash    SHA-256 of this bundle's frame
prev.last_timestamp   midpoint of the previous Roughtime reply
image.path / image.sha256 / image.ots
streams.http, streams.net, streams.http_events   (newest entries since the last bundle)
last_files, downloads.files_recent               (name, size, SHA-256, vault PDF and its SHA-256)
```

Bundle `0000` has `start.sha256` (the hash of `start_time.json`) instead of a
previous bundle.

**Receipts.** `X.ots` is a standard OpenTimestamps receipt for file `X`.
`X.ots__time-stamp.json` is a Roughtime receipt: the server's signed reply plus
the delegation certificate and Merkle path, with `artifact.bind_sha256_hex` =
SHA-256 of `X.ots`, from which the nonce was derived as
`SHA-512("RTNONC" ‖ sha256)`.

**Vault PDFs.** Each download is embedded as an attachment in a PDF together
with its metadata, and the PDF is signed (invisible signature, whole document,
no changes allowed) with the operator's key.

## Ports and processes on the capture machine

| port | what |
|---|---|
| 18080 | mitmproxy (Firefox profile is configured to use it) |
| 4455 | OBS WebSocket (`OBS_HOST`/`OBS_PORT`) |
| 8050 | local viewer (`ws-dl`) |
| 8765 | websocat feed from tshark to the browser overlay (`net`) |
| `/dev/video2` | v4l2loopback device the frame grabber reads (`--v4l-device`) |

## Runtime directories (never committed)

```
run/bundles/     frames, bundles, receipts of the CURRENT session
run/dumps/       mitmproxy .dump files; processed/ holds derived HARs
run/logs/        per-component logs
run/locks/       singleton lock files
run/state.json   shared live state (+ state.lock)
downloads/       the browser's download folder, watched by the vault builder
captures/        optional scratch for screen captures
sessions/<name>/ where archive_session.py moves a finished session
```
