# postprocess/ — from a verified session to playback data

`verify/` answers "is this recording authentic?". This directory answers "how do
I turn a session into something a viewer can watch?". It is only needed by
whoever publishes a session; a third party checking one never has to run it.

Kept separate from `verify/` on purpose: different audience, and heavier
dependencies (`pandas`, `numpy`, and the mitmproxy venv for HAR conversion).

## `final_conversion.py` — the one-shot pipeline

```bash
python3 postprocess/final_conversion.py --session-dir /path/to/sessions/<name>
```

Expects `<session>/bundles/` and `<session>/dumps/` (the mitmproxy `.dump`).
Creates `<session>/post_processing_pipeline/` and runs, in order:

1. sanity checks (bundles present, `start_time.json`, a `.dump` file)
2. the six `verify/` stages, with the bundles directory as working directory
3. HAR conversion: `bin/dump_to_redacted_har_sanitized.py` under the mitmproxy
   venv (`$EVCAP_MITM_PY`), producing `dumps/processed/<name>-redacted.har.gz`
   with request/response bodies kept
4. `00_MCRO_extract_html_from_har/HAR_extract_form_subs.py`: pulls form
   submissions (or MCRO case lookups, with the sibling script) out of the HAR
   into CSVs plus one readable HTML rendering per submission
5. staging into `01__source/`: the verify reports renamed to the numbered
   names `process_timecodes.py` expects, the network log unzipped, the dump copied
6. `process_timecodes.py`: aligns everything to the video's 30 fps timeline
   and writes `03__data_auto/`: `bundles.events.json`, `downloads.events.json`,
   `har.events.json`, `meta.json`, and chunked `http_events/`, `network_stream/`

Everything in `03__data_auto/`, plus the bundle files themselves, is what the
playback engine reads from `data/<slug>/` (see [`../playback/`](../playback/)).

## Supplemental converters

Two scripts patch gaps that showed up in the *Fraud-Reports* session. Their
docstrings say exactly why; in short:

- `build_downloads_from_bundles.py` — that session recorded downloads inside
  each bundle's `downloads.files_recent` rather than in a top-level CSV, so
  `process_timecodes.py` produced no download events. This rebuilds
  `downloads.events.json` from `combined.json`.
- `build_http_streams.py` — the bundle writer in that session populated
  `streams.http_events` from a stale pre-session file (a capture bug, recorded
  here rather than hidden), while `streams.http` was correct. This emits an
  `http_streams/` chunked stream from the correct data.

Both take `--session-dir` and run after `final_conversion.py`. They are
idempotent and patch `meta.json` so the playback engine picks the streams up.

## HAR extractors (`00_MCRO_extract_html_from_har/`)

- `HAR_extract_form_subs.py` — form submissions (IC3, USPIS, FTC, JotForm,
  Formstack, WordPress form plugins, …). See its README for detection rules.
- `MCRO_extract_html_from_har.py` — Minnesota Court Records Online case
  searches: request payloads, result pages, extracted case numbers.

Both write the same CSV schema so the timecode step ingests either.

## Dependencies

`pip install -r requirements.txt` covers `pandas`, `numpy`, `requests`, and
`pytesseract`. HAR conversion additionally needs the mitmproxy venv described in
`requirements-mitm.txt`, pointed to by `EVCAP_MITM_PY`.
