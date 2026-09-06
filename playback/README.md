# playback/ — Stage 3: synchronized playback in the browser

A static web app that plays a session's screen recording with everything the
capture stage produced scrolling alongside it, timecoded at 30 fps: the hash
chain, the captured frames, HTTP and packet streams, downloads, and links to
every raw file and verification report. It lets a viewer watch the on-screen
hash change and see the matching bundle at the same instant.

**Live demo with 15 real sessions: <https://mncourtfraud.com/evidence/>**

This directory is the engine only: HTML, CSS, JavaScript, a small range-request
server, and the config schema. No recordings or session data are included. The
code here is the version deployed on the live site, with the host site's
navigation bar, SEO tags and case-specific text removed.

## Quick start

```bash
cd playback
python3 serve.py --bind 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/
```

`serve.py` is a plain static server that supports HTTP range requests, which
browsers need for seeking inside a video. Any web server that supports ranges
(nginx, Apache, Caddy, …) works in production. Opening `index.html` directly as
a file does **not** work: the pages use ES modules and `fetch()`, which browsers
block on `file://`.

## Layout

```
playback/
  index.html                  session index; reads config.json and lists sessions
  pages/example-session.html  the player page; one copy per session, named <slug>.html
  config.json                 engine settings + the list of sessions
  serve.py                    range-enabled static server for local use
  assets/css/main.css         layout and theme
  assets/css/animations.css   frame / hash / card animations
  assets/js/
    config-loader.js          loads config.json; resolveUrl() is the single path resolver
    data-loader.js            loads data/meta.json and the event streams it points to
    bundle-loader.js          loads bundles.json (every bundle in one fetch)
    png-loader.js             preloads and displays the captured frames
    playback-engine.js        ties the video clock to the event streams; emits ticks
    animation-engine.js       hash zone, bundle cards, lifecycle animations
    hash-chain.js             chain state shown in the hash zone
    ui-controller.js          summary lines, provenance panel, PDF viewer, download links
    speed-control.js          playback speed
  data/                       NOT shipped; one directory per session (layout below)
```

The player page determines its session from its own filename: `pages/foo.html`
plays the session whose `slug` is `foo`. To add a session, copy
`pages/example-session.html` to `pages/<slug>.html`; the file contents do not
change.

## config.json

```json
{
  "bases": { "video": "data", "session": "data" },
  "fps": 30,
  "tick_ms": 33,
  "jump_threshold_ms": 500,
  "png_preload_ahead": 5,
  "png_buffer_behind": 3,
  "playback_speed_min": 0.25,
  "playback_speed_max": 4.0,
  "playback_speed_default": 1.0,
  "network_fifo_rows": 100,
  "http_fifo_rows": 20,
  "sessions": [
    { "slug": "example-session", "name": "Example Session",
      "video_file": "video_web.mp4", "bundle_count": 280, "downloads": 6 }
  ]
}
```

| key | used by | meaning |
|---|---|---|
| `bases.video`, `bases.session` | `config-loader` | Where `<slug>/…` is resolved from, for the video and for everything else. A path relative to `playback/` (default `data`) or an absolute `http(s)://` URL, so video can live on a CDN while metadata stays local. |
| `tick_ms` | `playback-engine` | Engine tick interval (default 33 ms ≈ one 30 fps frame). |
| `jump_threshold_ms` | `playback-engine` | A change in video time larger than this is treated as a seek: frames are shown immediately instead of animated. |
| `png_preload_ahead`, `png_buffer_behind` | `png-loader` | How many frames to keep decoded ahead of and behind the current bundle. |
| `playback_speed_min/max/default` | `speed-control`, `animation-engine` | Range and start value of the speed slider; animations scale with speed. |
| `network_fifo_rows`, `http_fifo_rows` | `playback-engine` | Number of most-recent rows kept in the packet and HTTP panels. |
| `fps` | no effect | Not read; timecodes are computed at a fixed 30 fps. |
| `animation_offsets` | no effect | Copied into `this.offsets` in `animation-engine.js` and never read again; the timing knobs are inert. |
| `bundle_preload_ahead`, `bundle_buffer_behind`, `screenshot_offset_ms`, `hide_network_cols`, `hide_http_cols` | no effect | Not referenced by any module. Kept so existing `config.json` files load unchanged. |
| `sessions[].slug` | everywhere | Directory name under `bases.*` **and** the page filename. |
| `sessions[].name` | index, player header | Display name. |
| `sessions[].video_file` | player | Filename of the recording inside the session directory. |
| `sessions[].bundle_count` | no effect | Not read by the engine; `null` is fine. |
| `sessions[].downloads` | index | Shown on the session card and summed into the total. |

## Session data layout

What the engine fetches for a session with slug `<slug>` (paths are relative to
`bases.session`, video to `bases.video`):

```
data/<slug>/
  video_web.mp4                         the recording (sessions[].video_file); H.264 MP4 works everywhere
  bundles.json                          all bundles as one JSON array (= combined.json from verify stage 01)
  NNNN.json, NNNN.png                   bundles and frames; linked from the hash zone and bundle cards
  NNNN.json.ots, NNNN.png.ots           OpenTimestamps receipts
  NNNN.*.ots__time-stamp.json           Roughtime receipts
  start_time.json (+ .ots, receipt)
  NNNN__<name>.pdf (+ .ots, receipt)    downloads and their signed vault copies; linked from download events
  data/
    meta.json                           written by postprocess/process_timecodes.py; indexes the files below
    bundles.events.json                 bundle lifecycle events (drives the animations)
    downloads.events.json
    har.events.json
    http_events/index.json, chunk_NNNN.json
    network_stream/index.json, chunk_NNNN.json
    http_streams/                       optional (see postprocess/build_http_streams.py)
```

The **Provenance** panel links to a fixed list of downloadable files in the
session directory (`00__report_master.csv`, `07__bundle_ots_report.csv`,
`reports.zip`, `bundles.zip`, `ocr_png.zip`, `ots_upgraded.zip`, …; the full
list is `PROVENANCE_FILES` at the top of `ui-controller.js`). They are optional:
a missing one is simply a dead link. They are the outputs of `verify/`, renamed
as `postprocess/final_conversion.py` stages them, plus zips of the raw
directories.

### From a session on disk to `data/<slug>/`

After `postprocess/final_conversion.py --session-dir <session>`:

| copy from | to |
|---|---|
| `<session>/post_processing_pipeline/03__data_auto/*` | `data/<slug>/data/` |
| `<session>/bundles/combined.json` | `data/<slug>/bundles.json` |
| `<session>/bundles/NNNN.*`, `start_time.json*` | `data/<slug>/` |
| `<session>/downloads/NNNN__*.pdf` and their receipts | `data/<slug>/` |
| the recording, transcoded for the web | `data/<slug>/video_web.mp4` |
| verify reports and zips (optional) | `data/<slug>/` |

Then add the session to `config.json` and copy `pages/example-session.html` to
`pages/<slug>.html`.

## Player URL parameters

- `?t=90`, `?t=1:30`, `?t=1h2m3s` — start at that point in the video.
- `?pdf` — start with the PDF viewer open instead of the frame view.

## Notes

- `config-loader.js` contains an optional hook for a `StorjSwitch` global that
  can redirect video URLs to a CDN mirror. Nothing here defines it; without it
  the hook is a no-op and video is served from `bases.video`.
- The player page is laid out for a 1920 px wide viewport (`viewport` meta), the
  width the recordings were made at.
- Fonts are loaded from Google Fonts with local monospace/sans fallbacks; the
  engine works offline, just in a different typeface.
