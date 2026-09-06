# Configuration

Everything is configured through environment variables. `setup.sh` writes a
`.env` for you at its last step; `.env.example` documents the same variables.
Load it before running the controller:

```bash
source .env
evcap interactive          # or: python3 bin/control_all.py interactive
```

`.env` is git-ignored and must stay that way: it holds your OBS password and
the paths to your signing key.

## Variables

| variable | read by | meaning | default |
|---|---|---|---|
| `EVCAP_HOME` | `evidence_capture.paths`, `evcap` | Repo root, if the code is run from somewhere other than a checkout. | the directory containing `evidence_capture/` |
| `EVCAP_PY` | controller, download watcher | Interpreter used to spawn worker scripts. | the running interpreter |
| `EVCAP_MITM_PY` | `stop_json.py`, `postprocess/final_conversion.py` | Python of the mitmproxy venv, needed to convert `.dump` files to HAR. | `~/.venvs/mitm/bin/python` if present |
| `EVCAP_MITMDUMP` | `mitm_dump_control.py` | The `mitmdump` binary. | found next to `EVCAP_MITM_PY`, then on `PATH` |
| `EVCAP_FFOX` | controller | Firefox binary. | `firefox` on `PATH` |
| `EVCAP_FFOX_PROFILE` | controller | Firefox profile directory pre-configured for the proxy (`setup.sh` step 5 creates one). | `~/.firefox-mitmproxy` |
| `EVCAP_DUMP_DIR` | `start_json.py`, `stop_json.py` | Where mitmproxy `.dump` files go. | `run/dumps` |
| `EVCAP_HAR_DIR` | `stop_json.py` | Where the derived HAR goes at session end. | `run/dumps/processed` |
| `EVCAP_KEY_PEM`, `EVCAP_CERT_PEM` | `sign_pdfs.py` | PEM key and certificate used to sign vault PDFs. | unset (signing fails until set) |
| `EVCAP_P12`, `EVCAP_P12_PASS` | `sign_pdfs.py` | PKCS#12 alternative to the PEM pair. | unset |
| `EVCAP_SIGN_CONTACT`, `EVCAP_SIGN_LOCATION`, `EVCAP_SIGN_REASON` | `sign_pdfs.py` | Metadata written into each signature. | empty |
| `EVCAP_OBS_RECORD_DIR` | `archive_session.py` | Where OBS writes recordings, so the newest one can be moved into the session. | `~/Videos` |
| `OBS_HOST`, `OBS_PORT`, `OBS_PASSWORD` | `obs_studio_ctrl.py`, `check_deps.py` | OBS WebSocket server (OBS → Tools → WebSocket Server Settings). | `127.0.0.1`, `4455`, empty |
| `OBS_TIMEOUT` | `obs_studio_ctrl.py` | Request timeout, seconds. | `5` |
| `OBS_SCENE`, `OBS_INPUT` | `obs_studio_ctrl.py` | Optional scene / input names for the OBS helper commands. | unset |
| `STATE_JSON_FILE`, `STATE_LOCK_FILE` | proxy addon, packet streamer | Override the shared state file (rarely needed). | `run/state.json`, `run/state.lock` |
| `HTTP_FIFO_MAX` | proxy addon | Compact HTTP rows kept in `streams.http`. | `5` |
| `HTTP_EVENTS_MAX` | proxy addon | Detailed flow records kept in `streams.http_events`. | `2000` |
| `HTTP_BODY_HASH_MAX_MB` | proxy addon | Largest body to hash; `0` hashes everything. | `0` |
| `HTTP_FORM_BODY_MAX_BYTES` | proxy addon | Largest form body to keep verbatim. | `10485760` |
| `NET_FIFO_MAX` | packet streamer | Packet events kept in `streams.net`. | `5` |

Signing credentials can also be set as constants in
`evidence_capture/config.py` (`SIGN_KEY_PEM` etc.); they ship as `None` and the
environment takes precedence. Command-line flags on `sign_pdfs.py` override both.

## The Firefox profile

The capture browser must be a **separate profile** whose proxy is
`127.0.0.1:18080` for HTTP and HTTPS and which trusts the mitmproxy CA
(`~/.mitmproxy/mitmproxy-ca-cert.pem`). `setup.sh` steps 4 and 5 generate the
CA and create such a profile, writing `user.js` with the proxy preferences and
importing the CA with `certutil`. Point `EVCAP_FFOX_PROFILE` at it. Its
download directory must be this repository's `downloads/` folder, or the vault
builder will never see the files.

## OBS

Two things must be true before a session:

1. **WebSocket server enabled** (Tools → WebSocket Server Settings), with the
   host, port and password copied into `OBS_HOST`, `OBS_PORT`, `OBS_PASSWORD`.
   `python3 bin/check_deps.py` connects and prints the OBS version when this is
   right.
2. **Virtual camera available.** `setup.sh` step 2 installs the `v4l2loopback`
   module, creates `/usr/local/sbin/obs_vcam_setup.sh` (from
   `bin/obs_vcam_setup.sh`) and adds a passwordless-sudo rule for it. It creates
   `/dev/video2` and grants your user access. The frame grabber reads that
   device (`--v4l-device`).

Recording start/stop is issued over the WebSocket by `start_session` /
`stop_session`. The OBS scene should show the capture browser and the overlay;
how you lay that out is up to you, but the overlay strip must be fully visible
and unscaled at the bottom of the frame for the OCR stage to read it (see
`CROP_HEIGHT` in `verify/03_PROCESS_png_ocr.py`).

## Capture loop parameters

`start_session "<name>" [interval_sec]` — interval defaults to 8 s. Directly:

```bash
python3 bin/start_json.py --session-name "<name>" --interval 8 [--no-net] [--no-http] [--no-download-watcher] [--v4l-device /dev/video2]
```

`capture_randomized_save_json.py` flags (frame size, palette, overlay font, the
region grabbed when no v4l device is used) are listed by `--help`; the defaults
are the ones the published sessions used.

## Playback and verification

`playback/config.json` is documented in [`playback/README.md`](../playback/README.md).
The verify stages take command-line arguments only; see `--help` on each and
[`verify/README.md`](../verify/README.md).
