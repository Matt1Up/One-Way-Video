# HAR_extract_form_subs.py

Extracts website **form submissions** from a HAR file (with intact bodies) and
emits phase-1 CSVs in the exact column shape that `process_timecodes.py`
expects, so the downstream timecode pipeline ingests them without any further
modification.

This is the form-submission sibling of `MCRO_extract_html_from_har.py`. It
shares the same output filenames and CSV schema; only the *meaning* of certain
columns changes (see "Column reuse" below).

## What counts as a form submission

A HAR entry is kept iff **all** of the following hold:

- request method = `POST`
- request `Content-Type` is one of:
  - `multipart/form-data`
  - `application/x-www-form-urlencoded`
  - `application/json` (or any `*+json`)
- host/path is **not** in the noise allow-list (Google/GA, GTM, Sentry, Datadog,
  reCAPTCHA, hCaptcha, Hotjar, FullStory, telemetry endpoints, etc.)
- decoded body contains at least one recognizable email address

POSTs that don't hit those gates (analytics beacons, CSP reports, telemetry,
captcha verification calls) are filtered out automatically.

## Engines detected

`detect_form_type` recognizes:

| Tag | Match rule |
|-----|------------|
| `ic3` | host contains `ic3.gov` OR field key `IC3ComplaintForm` present |
| `ftc_reportfraud` | host contains `reportfraud.ftc.gov` |
| `uspis` | URL path contains `/fcsexternal/` OR field key prefix `bo_ExternalComplaint` |
| `jotform` | URL contains `jotform.com/submit/` OR field naming `qN_…` present |
| `cf7` | WordPress Contact Form 7 |
| `wpforms` | WPForms |
| `ninja` | Ninja Forms |
| `freeform` | Solspace Freeform (Craft) |
| `formidable` | Formidable Forms |
| `joomla` | `com_requestforhelp` payload |
| `hubspot` | `hscollectedforms.net` / `hubspotutk` cookie |
| `gravity` | Gravity Forms (`input_N` keys) |
| `other` | Fallback — row still emitted; row tagged generically |

`detect_form_id` then pulls an engine-specific id where one is exposed in the
URL or payload (e.g. JotForm form id from the `/submit/<id>` path, IC3
`COMPLAINT_SESSION`, CF7 `_wpcf7`, etc.). For sites where no stable per-form
id is surfaced (FTC, USPIS), the row's `result_case_number` falls back to the
host string.

## Column reuse (form-subs vs MCRO)

The script writes the same CSV schema as the MCRO extractor. When the source
is form-subs, the columns are reinterpreted:

| Column | MCRO meaning | Form-subs meaning |
|--------|--------------|-------------------|
| `request_FormType` | MCRO form type | Form engine (`jotform`, `ic3`, `cf7`, …) |
| `request_CaseSearchNumber` | Search number | Site host (e.g. `mnago.jotform.com`) |
| `result_case_number` | Case number | Form id if known, else host |
| `result_defendant` | Defendant name | Submitter name parsed from form fields |
| `html_filename` | Per-case HTML page | Per-submission HTML rendering of the body |

The `09__HAR_case_details.csv` and `10__HAR_mcro_form.csv` files are written
as header-only stubs (form-subs rows live entirely in `11__HAR_case_search.csv`),
which is fine — `process_timecodes.py` reads all three regardless.

## Outputs

In `--out-dir`:

```
09__HAR_case_details.csv          (header only)
10__HAR_mcro_form.csv             (header only)
11__HAR_case_search.csv           (one row per form submission)
form_html/<idx>_<host>.formsub.html   (readable per-row HTML render)
```

Each `formsub.html` file contains:
- request metadata (entry index, started, host, form type, form id, submitter,
  HTTP status, full URL)
- a `<dl>` of every parsed form field
- the verbatim raw request body in a `<pre>` block (so the bytes that
  hash-match the HAR's `body_sha256` are recoverable from disk)
- the response body if textual

## Submitter-name detection

Heuristics, in order:
1. `firstName` + `lastName` field pair (substring match, handles JotForm
   `qN_name[first]`/`[last]` and USPIS `cFirstName`/`cLastName` variants)
2. Single full-name field (`name`, `fullname`, `Complainant.Name`, etc.)
3. Just one of first/last
4. Gravity-style `input_N = "Matt Guertin"` full-name values
5. Two-consecutive-capitalized-single-words value pattern (catches forms with
   custom labels like `q14_contactPerson14`)
6. Single field whose value matches the `[A-Z][a-z]+ [A-Z][a-z]+` pattern
7. Last resort: email local-part (`matt@…` → `matt`)

Returns `None` if nothing parses cleanly. Row is still emitted; only
`result_defendant` ends up empty.

## Usage

```bash
python3 HAR_extract_form_subs.py \
  --in /path/to/<session>/dumps/processed-bodies/<session>-redacted.har \
  --out-dir /path/to/<session>/conversion/01__source
```

Input may be `.har` or `.har.gz`. Output directory is created if missing.
The default `--html-subdir` is `form_html`; override if you need a different
name.

## Gotcha: JotForm-hosted forms (e.g. MN Attorney General)

The MN AG "Consumer Assistance Request Form" page at
`https://www.ag.state.mn.us/Office/Forms/ConsumerAssistanceRequest.asp` is a
common confusion point. If you "Save Page As" that URL, the saved HTML will
appear to contain **no submission form** — only a SiteLevel search box.

That's because the actual form is injected at runtime by:

```html
<script src="https://mnago.jotform.com/jsform/91345668832163">
```

JotForm's `jsform/<id>` endpoint returns JavaScript that iframes
`https://mnago.jotform.com/<id>` into the page on load. A static save misses
the post-render DOM. In a live browser, the page is fully a JotForm and
submissions POST to `https://mnago.jotform.com/submit/<id>` as
`multipart/form-data` — which this extractor recognizes and tags as
`form_type=jotform`.

So: if you're auditing whether a site is "covered" by reading static HTML, a
missing `<form>` tag does not mean the form isn't there. Open it in a real
browser (or use a headless renderer) to see the JS-injected form, or just
inspect the HAR after a live capture.
