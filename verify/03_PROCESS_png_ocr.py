#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path

from PIL import Image, ImageOps, ImageFilter
import pytesseract


# --- Image processing helpers -------------------------------------------------

CROP_HEIGHT = 18   # bottom strip height
OUT_HEIGHT = 26    # final image height (with padding)


def preprocess_bottom_strip(image_path: Path, out_path: Path) -> tuple[str, str]:
    """
    Crop the bottom CROP_HEIGHT pixels of the PNG, normalize it for OCR,
    save the processed image to out_path, and run OCR.

    Returns (raw_ocr_text, normalized_ocr_text).
    """
    img = Image.open(image_path)

    # Ensure we're in full color (undo palette).
    img = img.convert("RGB")
    w, h = img.size

    # 1. Crop bottom strip (700 x 18)
    crop = img.crop((0, h - CROP_HEIGHT, w, h))

    # 2. Convert to grayscale, autocontrast, sharpen a bit
    g = crop.convert("L")
    g = ImageOps.autocontrast(g)
    g = g.filter(ImageFilter.SHARPEN)

    # 3. Threshold to get a clean binary image
    thresh = 128
    bin_img = g.point(lambda p: 255 if p > thresh else 0, mode="L")

    # 4. Make sure background is white and text is black
    hist = bin_img.histogram()
    black = hist[0]
    white = hist[255]
    # If black dominates, background is likely black -> invert
    if black > white:
        bin_img = ImageOps.invert(bin_img)

    # 5. Paste onto a 700x26 white background, vertically centered
    out = Image.new("L", (w, OUT_HEIGHT), 255)  # white bg
    top_offset = (OUT_HEIGHT - CROP_HEIGHT) // 2
    out.paste(bin_img, (0, top_offset))

    # Save processed image
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path)

    # 6. OCR with Tesseract: single line, hex characters only
    config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789abcdefABCDEF'
    raw_text = pytesseract.image_to_string(out, config=config)
    # Normalize: remove whitespace/newlines, lower-case
    norm_text = "".join(raw_text.split()).lower()

    return raw_text.strip(), norm_text


def six_char_match(expected_hash: str, ocr_text: str) -> bool:
    """
    Return True if any 6-character substring of expected_hash (lowercased)
    appears in ocr_text (lowercased).
    """
    expected = (expected_hash or "").lower()
    text = (ocr_text or "").lower()

    if len(expected) < 6 or not text:
        return False

    for i in range(len(expected) - 5):
        sub = expected[i : i + 6]
        if sub in text:
            return True
    return False


# --- CSV processing -----------------------------------------------------------

def process_bundle_csv(
    directory: Path,
    bundle_csv: Path,
    output_csv: Path,
    ocr_subdir_name: str = "ocr_png",
):
    if not directory.is_dir():
        raise SystemExit(f"Not a directory: {directory}")

    if not bundle_csv.is_file():
        raise SystemExit(f"CSV file not found: {bundle_csv}")

    ocr_dir = directory / ocr_subdir_name

    # Read all rows first
    with bundle_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    # Add new columns if they don't exist yet
    extra_cols = ["ocr_sha256", "is_match_flag"]
    for col in extra_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    # Process each row
    for row in rows:
        filename = row.get("filename", "")
        display_sha = row.get("display_sha256", "") or ""

        # Default for non-PNG rows
        row["ocr_sha256"] = ""
        row["is_match_flag"] = "False"

        # Only care about actual PNG images (not *.png.ots)
        if not filename.lower().endswith(".png"):
            continue

        image_path = directory / filename
        if not image_path.is_file():
            print(f"Warning: PNG not found on disk: {image_path}")
            continue

        # Where to save processed OCR image
        out_path = ocr_dir / filename

        try:
            raw_ocr, norm_ocr = preprocess_bottom_strip(image_path, out_path)
        except Exception as e:
            print(f"Error processing {image_path}: {e}")
            continue

        # Store normalized OCR characters (no spaces/newlines, lowercased)
        row["ocr_sha256"] = norm_ocr

        # Compare with expected hash from display_sha256
        is_match = six_char_match(display_sha, norm_ocr)
        row["is_match_flag"] = "True" if is_match else "False"

    # Write updated CSV with the new columns
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Wrote updated CSV with OCR columns to: {output_csv}")
    print(f"Processed OCR images saved in: {ocr_dir}")


# --- CLI ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run OCR on bottom hash strips of PNG files and annotate "
            "BUNDLE_file_sha256.csv with OCR results and match flags."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing the PNGs and BUNDLE_file_sha256.csv (default: current directory).",
    )
    parser.add_argument(
        "--csv",
        default="BUNDLE_file_sha256.csv",
        help="Input CSV filename (default: BUNDLE_file_sha256.csv).",
    )
    parser.add_argument(
        "--out-csv",
        default="BUNDLE_file_sha256_ocr.csv",
        help="Output CSV filename (default: BUNDLE_file_sha256_ocr.csv).",
    )
    parser.add_argument(
        "--ocr-dir",
        default="ocr_png",
        help="Subdirectory to store processed OCR PNGs (default: ocr_png).",
    )

    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    bundle_csv = (directory / args.csv).resolve()
    output_csv = (directory / args.out_csv).resolve()

    process_bundle_csv(directory, bundle_csv, output_csv, ocr_subdir_name=args.ocr_dir)


if __name__ == "__main__":
    main()
