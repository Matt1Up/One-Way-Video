# Contributing

Thanks for looking. This is a small project with one maintainer and a narrow
purpose: record a browser session in a way a stranger can verify. Contributions
that sharpen that purpose are welcome; ones that broaden it will probably be
declined.

## Ground rules

- **Do not weaken a check.** Anything under `verify/` that decides whether a
  session passes is the reason the project exists. A change there needs a test
  in `tests/` that would fail without it, and a sentence in `verify/README.md`
  if it changes what a pass means.
- **Do not overclaim.** The "what this proves / does not prove" section of
  `verify/README.md` is deliberate. Documentation changes that make the system
  sound stronger than it is will be reverted.
- **Filenames stay.** Several scripts have names that fossilise how the project
  grew (`*_json.py`, numbered stages). Renaming them breaks other people's
  sessions and muscle memory for no verification gain. Leave them.
- **No case material, no machine details.** Never commit anything from a real
  `sessions/` directory, a `.env`, `run/`, or an editor's local settings. See
  `.gitignore`; `examples/` is the one place curated public output lives.

## Setting up

```bash
git clone https://github.com/Matt1Up/One-Way-Video.git
cd One-Way-Video
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[verify,postprocess,dev]"
sudo apt install tesseract-ocr        # only needed to run verify stage 03 for real
```

For the capture stage itself, run `setup.sh` on a fresh Ubuntu machine; it
installs OBS, mitmproxy in its own venv, the v4l2loopback module and the rest.

## Before opening a pull request

```bash
ruff check .
python -m compileall -q bin evidence_capture verify postprocess run/web-server
pytest
```

That is exactly what CI runs. `ruff` is configured for correctness rules only
(undefined names, unused imports and variables, syntax); style is not enforced.

If you touched anything under `verify/`, also run the pipeline against a real
bundles directory and confirm the example reports still say what
`examples/README.md` says they say:

```bash
python3 verify/00_RUN_all_bundle_processing.py --bundles-dir /path/to/bundles
```

## What a good pull request looks like

- One change, explained in the description in terms of what a verifier or a
  capture operator gains.
- Tests for behaviour, not for coverage numbers.
- No unrelated reformatting.

## Reporting problems

Open an issue with the command you ran, the output, and your Python and Ubuntu
versions. For anything that could let a fabricated session pass verification,
see `SECURITY.md` instead of opening a public issue.
