# signature/

The PDF signing code lives in `bin/sign_pdfs.py`. It is invoked automatically by
`bin/download_watcher_json.py` when a download is sealed into an evidence-vault PDF,
and can be run by hand on any PDF or folder.

This directory is a suggested, git-ignored home for your signing material:

    signature/keys/    <- your *.pem / *.p12 files (never committed; see .gitignore)

Tell the signer where your keys are with environment variables (see `.env.example`):

    EVCAP_KEY_PEM=/path/to/key.pem
    EVCAP_CERT_PEM=/path/to/cert.pem
    # or, for a PKCS#12 bundle:
    EVCAP_P12=/path/to/cert.p12
    EVCAP_P12_PASS=...

`setup.sh` (step 7) offers to generate a self-signed key/cert pair to get started.
