# Evidence-Capture Session Controller

A reproducible capture stack for browser/network activity and on-screen proof, orchestrated by a single controller. It couples **mitmproxy**, **Firefox (custom profile)**, **OBS Studio (virtual camera + recording control)**, and **local viewers** with a **bundle-hash loop** and dual timestamping (**OpenTimestamps + Roughtime**).
[![One-Way Video flow](images/One-Way-Video-FLOW_200_border.png)](images/One-Way-Video-FLOW_200_border.png)

<p align="center">
  <a href="https://matt1up.substack.com/p/one-way-video">
    <img alt="Watch the demo" src="https://img.shields.io/badge/Watch%20the%20demo-▶%20Substack-2ea44f">
  </a>
</p>


> **Main entrypoint:** `control-all.py`

---

## Table of Contents

- [Overview](#overview)  
- [Key Features](#key-features)  
- [Architecture](#architecture)  
- [Prerequisites](#prerequisites)  
- [Configuration](#configuration)  
  - [OBS Studio WebSocket & Virtual Camera](#obs-studio-websocket--virtual-camera)  
  - [Firefox Profile & Downloads](#firefox-profile--downloads)  
  - [PDF Signing Keys (Evidence Vaults)](#pdf-signing-keys-evidence-vaults)  
- [Commands (via `control-all.py`)](#commands-via-control-allpy)  
  - [`ffox`](#ffox)  
  - [`hash`](#hash)  
  - [`vcam`](#vcam)  
  - [`mitm`](#mitm)  
  - [`net`](#net)  
  - [`watch-start`](#watch-start)  
  - [`watch-clear`](#watch-clear)  
  - [`ws_dl`](#ws_dl)  
  - [`ws_bf1`](#ws_bf1)  
  - [`ws_bf2`](#ws_bf2)  
  - [`start_session`](#start_session)  
  - [`stop_session`](#stop_session)  
- [Quickstart: Typical Capture Flow](#quickstart-typical-capture-flow)  
- [Local Web Viewers](#local-web-viewers)  
- [Timestamping Method (OTS + Roughtime)](#timestamping-method-ots--roughtime)  
- [Safety Notes & Troubleshooting](#safety-notes--troubleshooting)  
- [Roadmap / Ideas](#roadmap--ideas)  
- [License](#license)

---

## Overview

The system orchestrates a **repeatable capture loop** (“bundle” every *X* seconds) that:

1. Runs a **Firefox** session pinned to a **mitmproxy** proxy.
2. Records **frame images via OBS virtual camera**.
3. Writes **JSON bundles** to `RUN/bundles/` (`0000.json`, `0001.json`, …) and shows the **previous bundle SHA-256** in a narrow overlay window to create a visible **hash-chain** across time.
4. Continuously monitors `~/downloads` to seal files into **digitally signed “evidence vault” PDFs**.
5. Offers **live viewers** at `http://127.0.0.1:8050/` for the hash loop, state, and bundle cards.

---

## Key Features

- Isolated **Firefox** profile routed through **mitmproxy**  
- **OBS** virtual camera auto-enable + start/stop recording by command  
- **Bundle hash-chain** with visible on-screen previous-hash overlay  
- **Download watcher**: auto-hash, `pdfsig`, `exiftool`, wrap into **signed PDFs**  
- **Wireshark-like** JSON lines stream (`network_stream.jsonl.zip`)  
- **Local web viewers** (Dash) for state, live hash, and bundle “cards”  
- **Dual timestamping**: OpenTimestamps (BTC) + Roughtime receipts

---

## Architecture

```
control-all.py
├─ ffox → custom-profile Firefox (proxy: mitm)
├─ mitm → mitm_dump_control.py + httpstream_json.py
├─ vcam → enable OBS Virtual Camera (via obs_studio_ctrl.py)
├─ hash → overlay_json.py (1920×72, Liberation Mono 36, refresh 0.5s)
├─ net  → netstream_json.py (Wireshark-like stream; zipped on stop)
├─ watch-start / watch-clear → download_watcher_json.py + housekeeping
├─ ws_* → local web servers and feeders @ http://127.0.0.1:8050/
├─ start_session "<name>" <interval> → bin/start_json.py
└─ stop_session → bin/stop_json.py (finalize .dump → sanitized HAR)
```

---

## Prerequisites

- **Ubuntu** (tested) or comparable Linux environment  
- **Python 3.x** with typical CLI tooling  
- **OBS Studio** with:
  - **OBS WebSocket** enabled (Tools → WebSocket Server Settings)
  - **OBS Virtual Camera** available
- **mitmproxy**
- **exiftool**, **pdfsig**, **zip**  
- **Firefox** with **custom profile** (proxy set to mitm)  
- Fonts: **Liberation Mono** (for the overlay)  

> Some actions (e.g., turning on OBS Virtual Camera) may require passwordless `sudo` via a small setup script (see below).

---

## Configuration

### OBS Studio WebSocket & Virtual Camera

1. In OBS: **Tools → WebSocket Server Settings → Show Connect Info → Copy Server IP**.  
2. Update `OBS_HOST` in `obs_studio_ctrl.py` with that IP/port.
3. Ensure the helper script to start the virtual camera has the right privileges (example):
   - `/usr/local/sbin/obs_vcam_setup.sh` (the working copy may be in `/bin`)
   - Configure passwordless sudo as needed.

### Firefox Profile & Downloads

- The **custom Firefox profile** used by `ffox` must:
  - Route through the local **mitmproxy**.
  - Use `~/downloads` (or your chosen path) as the **default download folder**.

### PDF Signing Keys (Evidence Vaults)

- `signature/sign_pdfs.py` must be configured with your signing materials:
  - `DEFAULT_KEY_PEM`  
  - `DEFAULT_CERT_PEM`  
  - `DEFAULT_P12` (if using a PKCS#12)

---

## Commands (via `control-all.py`)

> Command names below match the controller. Where hyphen/underscore variants exist in your environment, use the exact spellings wired in your `control-all.py`.

### `ffox`
Launches **Firefox** with a **custom profile** that is pre-configured to use the **mitmproxy** server.  
This keeps capture browsing separate from your normal Firefox.  
**OBS** should capture **this** window for the stream.

### `hash`
Runs `overlay_json.py` to open a **1920×72 px** overlay window with **Liberation Mono Bold 36pt**, refreshing **every 0.5s**.  
Shows the **last bundle hash** so viewers can **visually verify** the hash-chain in the OBS recording.

### `vcam`
Turns on the **OBS Virtual Camera**.  
Requires `OBS_HOST` to be set correctly in `obs_studio_ctrl.py`.  
Remains active; start/stop **recording** is controlled by session start/stop.

### `mitm`
Starts the **mitmproxy** stack (`mitm_dump_control.py` + `httpstream_json.py`).  
Provides internet to the `ffox` browser and writes `.dump` for later HAR conversion.

### `net`
Runs `netstream_json.py`, creating a **Wireshark-like** TCP/IP stream (JSON lines).  
- Included in each bundle  
- Also persisted as `RUN/bundles/network_stream.jsonl.zip` on session stop

### `watch-start`
Starts `download_watcher_json.py`, monitoring the **Downloads folder**.  
Each new file is:
- **Hashed**, run through **pdfsig**/**exiftool**
- Logged into `RUN/downloaded_files.json`
- **Embedded into a signed PDF “evidence vault”** (via `signature/sign_pdfs.py`)  
> ⚠️ **Only one watcher instance should run**. Multiple instances cause duplicates/breakage. This is why it’s commented out in `start_json.py` by default.

### `watch-clear`
**One-shot cleanup**: clears the downloads folder and logs, resets state for a fresh session, and updates `RUN/state.json`.  
Safe to run even if `watch-start` is active.  
> ⚠️ **Destructive**. Move/archive any prior evidence first.

### `ws_dl`
Starts `RUN/web-server/local_web_server_5.py`.  
Main viewer at **http://127.0.0.1:8050/**.  
Uses config at `ws_dir/viewer.config.json`.

### `ws_bf1`
Runs `RUN/web-server/live_hash_loop.py`.  
- Watches `RUN/bundles/` (default poll **0.5s**)  
- **Prepends** updates to `--out RUN/web-server/live_hash_loop.txt`  
- Drives the **Live Hash-Loop** panel in the viewer

### `ws_bf2`
Runs `RUN/web-server/combined.py`.  
- Watches `RUN/bundles/` (default poll **0.5s**)  
- **Prepends** to `--out RUN/web-server/combined.json`  
- Backs the **Cards** (collapsible bundles) view in the viewer

### `start_session`
`bin/start_json.py`

Usage:
```bash
start_session "<Session Name Here>" 10.0
# i.e., start_session "<name>" <interval_sec>
```

What happens:
- Initializes `RUN/state.json` and writes **`0000.json`** from a 100-row system-time sample (`start_time.json`).
- **SHA-256** of `start_time.json` becomes the **initial nonce**.
- The **interval** controls the loop that triggers `loop_json.py`, which writes each subsequent bundle.
- The overlay shows **previous bundle hash** to maintain a visible **hash-chain** across captured frames.
- `mitm_dump_control.py` `--out-dir` determines where the main `.dump` lives; the **session name** standardizes filenames for `.dump` and derived HAR.

### `stop_session`
`bin/stop_json.py`

What happens:
- Gracefully stops `capture_randomized_save_json.py` (waits up to one interval to finish the last loop).
- Stops all capture services and **OBS recording**.
- Waits for `DEFAULT_OUT_DIR/<SESSION>.dump.ready`.
- Converts the main `.dump` into a **sanitized/redacted HAR** written to `DEFAULT_HAR_OUTDIR`.

---

## Quickstart: Typical Capture Flow

```bash
# 1) Start virtual cam & overlay
vcam
hash

# 2) Start proxy and browser
mitm
ffox

# 3) (Optional) Start the download watcher
watch-start

# 4) (Optional) Launch local viewers
ws_dl
ws_bf1
ws_bf2

# 5) Begin a capture session (every 10s)
start_session "Case Study – MCRO" 10.0

# ...perform your browsing / downloads...

# 6) Stop the session (finalizes dump → sanitized HAR; zips network stream)
stop_session
```

> **Cleanup:** If preparing for a fresh run, use `watch-clear` (destructive).

---

## Local Web Viewers

Accessible at **http://127.0.0.1:8050/** when `ws_dl` (and optionally `ws_bf1`, `ws_bf2`) are running.

- **VIEW: JSON Viewer, PROFILE: File Downloads**  
  See all session downloads + `pdfsig`/`exiftool` details.
- **VIEW: Live Feed, PROFILE: File Downloads**  
  See the **Live Hash-Loop** (Bundle → Frame → Bundle → Frame …).
- **VIEW: JSON Viewer, PROFILE: State**  
  Inspect current `RUN/state.json`.
- **VIEW: Cards, PROFILE: Cards**  
  Collapsible tiles for **each bundle** in `RUN/bundles`.

---

## Timestamping Method (OTS + Roughtime)

Every bundle (and key artifacts like `start_time.json` and frame PNGs) is timestamped by:

1. **OpenTimestamps (.ots)** → anchors to **Bitcoin** for immutability.  
2. **Roughtime** → generates a JSON receipt binding the **SHA-256 of the `.ots` file itself** to a time-stamped proof.

Artifacts appear as:
- `xxxx.json.ots`
- `xxxx.json.ots__time-stamp.json`
- `xxxx.png.ots__time-stamp.json`

Tools live under `/time/` (e.g., `time/roughtime_client.py`).

---

## Safety Notes & Troubleshooting

- **Single watcher instance:** Ensure only **one** `download_watcher_json.py` is running to avoid duplicates.  
- **OBS control:** If `vcam` or recording toggles fail, confirm:
  - `OBS_HOST` (IP/port) in `obs_studio_ctrl.py`
  - OBS is running; WebSocket enabled; permissions set for virtual camera.
- **Destructive cleanup:** `watch-clear` **wipes** downloads/logs. **Backup first**.  
- **Firefox profile:** Proxy and default download folder must be correct; otherwise downloads won’t be captured/sealed.  
- **Keys & certs:** `signature/sign_pdfs.py` must point to valid signing materials.

---

## Roadmap / Ideas

- Replace visible 64-char hash string with a **V1 QR code (21×21 px)** anchored at a fixed pixel region in the frame (no quiet zone required if pixel-perfect).  
- Optionally **encode entire bundles into QR tiles** in a pre-defined area of the frame (or hide bits within odd/even pixel parity for **invisible watermarking**).  
- Lower capture frequency (e.g., **4× per minute**) while keeping high verification value.  
- Add strict **pixel-accuracy validation** for the overlay region (useful for automated verification).

---

## License

Specify your license here (e.g., MIT, Apache-2.0).  

---

**Notes**

- This system was designed to document and verify **judicial-record irregularities** (e.g., MCRO) and to cryptographically bind **CourtListener** archive states to a **time-stamped**, **chain-verifiable** capture.  
- The large overlayed hash promotes **human-readable verification** during review. For compact archives, move to QR or embedded bit-schemes later.
