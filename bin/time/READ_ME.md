
1. What’s new (drop-in usage):

  --bind-file <path>`: hashes the file (SHA-256) up front and derives the NONC from it.
  --reveal-hash`: includes the file’s SHA-256 (outside the signed bytes) in the JSON bundle under `artifact.bind_sha256_hex`.
  --last-time <path>`: writes the trimmed MIDP timestamp (`YYYY-MM-DDTHH:MM:SS+00:00`) after a successful query.
  --last-imghash <path>`: writes the file’s SHA-256 **before** querying.
  --json-out <dir>`: directory to save the JSON proof. If combined with `--bind-file`, the name becomes `roughtime_proof_<basename>.json` (e.g., `png-01.png` → `roughtime_proof_png-01.json`). Otherwise it uses a timestamped filename.


2. Example:

python3 roughtime_client.py query -v \
  --server roughtime.cloudflare.com --port 2003 \
  --pubkey-base64 0GD7c3yP8xEc4Zl2zeuN2SlLvDVVocjsPSL8/Rl/7zg= \
  --timeout 8 --retries 6 \
  --reveal-hash \
  --bind-file "~/evidence-capture/png/png-01.png" \
  --last-time "~/evidence-capture/run/last_roughtime.txt" \
  --last-imghash "~/evidence-capture/run/last_img_hash.txt" \
  --json-out "~/evidence-capture/png/"


3. What it does:

1. Computes SHA-256 of `--bind-file` immediately and writes it to `--last-imghash`.
2. Derives the 64-byte NONC as `SHA-512("RTNONC" || sha256(file))`.
3. Queries and verifies the Roughtime response (unchanged protocol logic).
4. Writes `MIDP` trimmed to seconds (`…+00:00`) to `--last-time`.
5. Saves the JSON bundle into `--json-out`, named after your file’s basename; if `--reveal-hash` is set, includes the file hash in the bundle under `artifact`.


4. Notes:

* If you don’t pass `--bind-file`, you can still use `--json-out` and it will save a timestamped filename.
* You can still use `--bind-hash <hex>` directly if you already have the SHA-256; `--bind-file` is just convenience around that.
