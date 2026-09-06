# Troubleshooting

Start with `python3 bin/check_deps.py`. It checks every Python package, every
system tool, the fonts, the signing credentials, and actually connects to the
OBS WebSocket.

## Capture

**`start ffox` opens Firefox but pages do not load, or show certificate errors.**
The profile is not using the proxy, or does not trust the mitmproxy CA. Check
`EVCAP_FFOX_PROFILE` points at the profile `setup.sh` created, that `mitm` is
running (`ps` in the controller), and that `~/.mitmproxy/mitmproxy-ca-cert.pem`
exists. Re-run `setup.sh`; steps 4 and 5 are idempotent.

**`start net` dies immediately / "cannot exec dumpcap".**
tshark needs your user in the `wireshark` group, and the group must be active in
the shell that launched the controller. After `sudo usermod -a -G wireshark $USER`
you must log out and back in (or start a new login shell); `newgrp wireshark`
in the current terminal also works. The controller checks this and prints the
same advice.

**`vcam` fails, or `/dev/video2` does not exist.**
`sudo /usr/local/sbin/obs_vcam_setup.sh` should load `v4l2loopback` and create
the device. If the module is missing, `sudo apt install v4l2loopback-dkms
v4l-utils` and re-run `setup.sh` step 2. Secure Boot machines need the DKMS
module signed or Secure Boot disabled. `dmesg | tail` shows module errors.

**OBS commands time out / "authentication failed".**
`OBS_HOST`, `OBS_PORT`, `OBS_PASSWORD` must match OBS → Tools → WebSocket
Server Settings exactly, and OBS must be running. `check_deps.py` distinguishes
"nothing listening" from "listening but the password is wrong".

**The controller says components are still running from a prior session.**
Lock files in `run/locks/` belong to processes that were not stopped cleanly.
`stop all` in the controller ends what is still alive; a lock whose process is
gone is released automatically on the next `start`.

**Two download watchers, duplicate vault PDFs.**
Only one `download_watcher_json.py` may run. `watch-start` refuses to start a
second one when the lock is held; if you started one by hand, stop it first.

**Downloads are not sealed.**
The Firefox profile's download directory must be this repository's
`downloads/` (see [configuration](configuration.md)). Also confirm the signing
credentials: `EVCAP_KEY_PEM` + `EVCAP_CERT_PEM`, or `EVCAP_P12`. Without them
the watcher hashes files but cannot produce signed vaults.

**The overlay window shows nothing / a stale hash.**
It displays `last_hash` from `run/state.json`; it is blank until the first
bundle is written. If it never updates, the loop is not running: check
`run/logs/capture.err` and `run/logs/loop.log`.

**`stop_session` hangs on "waiting for .dump.ready".**
mitmproxy flushes the dump on shutdown; give it a moment. If it never appears,
`stop mitm` and run `bin/stop_json.py --timeout 60` again. HAR conversion also
needs `EVCAP_MITM_PY`; without it the session still stops, but no HAR is made.

**`overlay_pw_hash.py` cannot import `gi`.**
That is the optional GStreamer/PipeWire overlay, not the default. It needs the
system packages `python3-gi gir1.2-gstreamer-1.0`, which the venv sees only when
created with `--system-site-packages` (as `setup.sh` does).

## Verify

**Stage 03 fails with `TesseractNotFoundError`.**
`sudo apt install tesseract-ocr`. The Python package `pytesseract` is only a
wrapper.

**`ots_receipt_status` is `pending`.**
The OpenTimestamps calendars commit to Bitcoin every few hours. Re-run stage 06
later; it upgrades only the cached copies in `ots_upgraded_bundle/` and never
modifies an original `.ots`. `postprocess/final_conversion.py` retries pending
upgrades automatically on each run.

**Stage 06 is slow or errors on `blockstream.info`.**
It makes one HTTP request per distinct block height (usually a handful) and
one `ots upgrade` per receipt. Use `--sleep 0.2` to pace the API calls. It
needs outbound network access; stages 01-05 do not.

**Timecodes in `BUNDLE_streams_*.csv` are empty or an hour off.**
Both were bugs before v2.0 (a `Z` suffix Python 3.10 rejects, and a fixed CST
offset). Re-run with the current `verify/05_PROCESS_build_streams_report.py`.

**`ocr_match_flag` is `False` on a few frames.**
Look at the strip in `ocr_png/` for that frame. If the overlay was covered,
scaled, or the frame was captured mid-redraw, the OCR cannot read it; the
chain checks in `BUNDLE_report_overview.csv` are unaffected. A `False` on
*every* frame means the overlay is not where stage 03 expects it (bottom 18 px
of the PNG at the capture scale).

## Playback

**Opening `index.html` directly shows an error.**
ES modules and `fetch()` do not work over `file://`. Serve the directory:
`python3 playback/serve.py --port 8000`.

**Video plays but nothing scrolls.**
The player fetches `data/<slug>/data/meta.json` and the files it names. Open the
browser console: a 404 tells you which file is missing. See the data layout in
[`playback/README.md`](../playback/README.md).

**Seeking does not work.**
The web server must support HTTP range requests. `serve.py` does; some minimal
static servers do not.

## Packaging

**`evcap: controller not found`.**
The command was installed from a checkout that has since moved. Either
reinstall (`pip install -e .` in the new location) or set `EVCAP_HOME`.

**`pip install -e .` pulls in PyQt5 and fails to build.**
Use the distribution package instead: `sudo apt install python3-pyqt5` and
create the venv with `--system-site-packages`, which is what `setup.sh` does.
