# Easiest: bundle already contains artifact.bind_sha256_hex
python3 verify_roughtime_bundle.py roughtime_proof_png-01.json

# If the bundle doesn’t reveal the hash, but the file path is present and accessible:
python3 verify_roughtime_bundle.py roughtime_proof_png-01.json

# If neither is present, provide the hash explicitly:
python3 verify_roughtime_bundle.py roughtime_proof_png-01.json \
  --bind-hash abc123def4567890deadbeefcafebabefeedface1234567890abcdef
