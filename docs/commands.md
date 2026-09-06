# Command reference

Spellings below are the ones the code accepts. Component names use hyphens
(`ws-dl`, `watch-start`); the two session commands use underscores
(`start_session`, `stop_session`).

## The controller

`evcap` (installed by `pip install -e .`) is exactly `python3 bin/control_all.py`.

```
evcap interactive                    # REPL; the normal way to drive a capture
evcap daemon                         # run in the background …
evcap --send "start ffox mitm"       # … and send it commands
evcap immediate -- start_session "Name" 8   # one command, then exit
```

### REPL commands

| command | effect |
|---|---|
| `help`, `?` | list commands and components |
| `list` | the component names below |
| `ps`, `status` | controller-tracked processes and their PIDs |
| `start <name> [<name> …]` | start components by name |
| `stop <name \| all>` | stop components tracked by this controller |
| `start_session "<name>" [interval_sec]` | full session start (default interval 8 s), steps below |
| `stop_session` | full session stop, steps below |
| `watch-start`, `watch-clear` | download watcher on / one-shot clear |
| `exit`, `quit` | leave the REPL (components keep running) |

### Components (`start <name>`)

| name | script | notes |
|---|---|---|
| `ffox` | Firefox, `EVCAP_FFOX_PROFILE` | the capture browser, proxied through mitmproxy |
| `mitm` | `bin/mitm_dump_control.py start --standby --script bin/httpstream_json.py` | proxy in dump mode + the state-writing addon; recording begins at `start_session` |
| `net` | `bin/netstream_json.py` | tshark → `streams.net` in state, `network_stream.jsonl`, websocat feed on 8765 |
| `hash`, `overlay-hash` | `bin/overlay_json.py` | 1920×72 window showing `last_hash` |
| `vcam` | `bin/obs_studio_ctrl.py` | turn on OBS's virtual camera |
| `vcam-overlay`, `hash-vcam` | `bin/overlay_vcam_hash.py` | render the hash overlay into a second v4l2 device for OBS to composite |
| `watch-start` | `bin/download_watcher_json.py` | seal downloads into signed vault PDFs (singleton) |
| `watch-clear` | `bin/download_watcher_json.py --clear` | one-shot: clear `downloads/` and the ledger. **Destructive.** |
| `start_json`, `stop_json` | `bin/start_json.py`, `bin/stop_json.py` | the session bootstrap / teardown scripts by themselves |
| `ws-dl` | `run/web-server/local_web_server_5.py` | local viewer at <http://127.0.0.1:8050/> |
| `ws-bf1` | `run/web-server/live_hash_loop.py` | feeds the viewer's live hash-loop panel |
| `ws-bf2`, `wf-bf2` | `run/web-server/combined.py` | feeds the viewer's bundle cards |

Every component holds a lock in `run/locks/`; starting one that is already
running is a no-op with a message, not a second copy.

### `start_session "<name>" [interval]`

1. `vcam` — OBS virtual camera on
2. `hash` — overlay window (skipped if running)
3. `ffox` — capture browser (skipped if running)
4. `net` — packet stream (skipped if running; must be up before step 6)
5. `watch-clear`, then `watch-start` — fresh download folder and ledger, watcher on
6. `bin/start_json.py --session-name "<name>" --interval <n>` — resets state,
   writes `start_time.json` and bundle `0000`, starts mitmproxy recording and
   OBS recording, launches the frame grabber
   (`capture_randomized_save_json.py`) which calls `loop_json.py` every
   *interval* seconds

### `stop_session`

1. stop the download watcher
2. `bin/stop_json.py` — stop the grabber (waits for the current bundle), the
   proxy, OBS recording; wait for the `.dump`; convert it to a sanitized HAR in
   `run/dumps/processed/`
3. stop the packet stream
4. `bin/archive_session.py --session-name "<name>"` — move bundles, dumps,
   downloads, logs, the newest OBS recording and a copy of `state.json` into
   `sessions/<name>/`

## Capture scripts (`bin/`)

All accept `--help`. Paths default to the repository's `run/` tree; override
with `EVCAP_HOME` or per-script flags.

| script | purpose |
|---|---|
| `control_all.py` | the controller above |
| `start_json.py` | session bootstrap; flags `--interval`, `--session-name`, `--no-net`, `--no-http`, `--no-obs`, `--no-download-watcher`, `--no-clean`, `--v4l-device`, `--dump-out-dir` |
| `stop_json.py` | orderly shutdown + HAR conversion; `--session-name`, `--out-dir`, `--har-out-dir`, `--timeout` |
| `archive_session.py` | move a finished session into `sessions/<name>/`; `--obs-dir`, `--video-window` |
| `capture_randomized_save_json.py` | frame grabber daemon (`start`/`stop`/`run`); `--interval`, `--v4l-device`, `--region`, palette and overlay-font options |
| `loop_json.py` | one bundle iteration: hash frame, receipts, snapshot streams, write bundle, hash bundle, receipts |
| `httpstream_json.py` | mitmproxy addon (not run directly); writes `streams.http` and `streams.http_events` |
| `mitm_dump_control.py` | start/stop/status of mitmdump with the addon; `--standby` waits for the START signal |
| `netstream_json.py`, `netstream_ctrl_json.py` | tshark streamer and its FIFO controller |
| `download_watcher_json.py` | watch `downloads/`, hash, `pdfsig`/`exiftool`, build and sign vault PDFs; `--clear` |
| `sign_pdfs.py` | sign one PDF or a folder; credentials from flags, env, or `evidence_capture.config` |
| `embed_pdf_files.py` | embed files into a PDF as attachments (used by the watcher) |
| `overlay_json.py`, `overlay_vcam_hash.py` | the hash overlay as a window / as a v4l2 device |
| `overlay_pw_hash.py` | optional GStreamer/PipeWire overlay; needs system `python3-gi` |
| `obs_studio_ctrl.py` | OBS WebSocket helper (`--host/--port/--password` or `OBS_*`) |
| `obs_vcam_setup.sh` | template for `/usr/local/sbin/obs_vcam_setup.sh`: load v4l2loopback, create `/dev/video2` |
| `time/roughtime_client.py` | Roughtime query with file binding; writes the `*.ots__time-stamp.json` receipts |
| `get_hash.py`, `get_ots_stamp.py`, `get_start_time_json.py` | small helpers: SHA-256 of a file, `ots stamp`, the 100-timestamp `start_time.json` |
| `combine_json_stripping_keys.py` | combine numbered bundle JSONs with selected keys removed |
| `dump_to_redacted_har_sanitized.py` | mitmproxy `.dump` → HAR; run under the mitm venv; `--include-bodies` for the post-processing HAR |
| `check_deps.py` | dependency, tool, font, credential and OBS WebSocket check |
| `script_A.py` | user hook fired once before the first capture |

## Verify (`verify/`)

```
python3 verify/00_RUN_all_bundle_processing.py --bundles-dir <bundles>   # all six stages
python3 verify/0N_PROCESS_*.py --help                                    # any single stage (run inside <bundles>)
python3 verify/verify_roughtime_bundle.py <receipt.json> [--bind-hash <sha256>]
```

See [`verify/README.md`](../verify/README.md).

## Post-process (`postprocess/`)

```
python3 postprocess/final_conversion.py --session-dir <session>          # verify + HAR + extract + timecodes
python3 postprocess/process_timecodes.py --source-dir <01__source> --output-dir <03__data_auto>
python3 postprocess/build_downloads_from_bundles.py --session-dir <session>
python3 postprocess/build_http_streams.py --session-dir <session>
python3 postprocess/00_MCRO_extract_html_from_har/HAR_extract_form_subs.py --in <har> --out-dir <dir>
python3 postprocess/00_MCRO_extract_html_from_har/MCRO_extract_html_from_har.py --har <har> --out-dir <dir>
```

## Playback (`playback/`)

```
python3 playback/serve.py --bind 127.0.0.1 --port 8000 [--dir playback]
```
