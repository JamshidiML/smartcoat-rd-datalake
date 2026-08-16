# OCR evaluation runbook

## Purpose

PaddleOCR is the production extractor; Tesseract is a benchmark against the exact same normalized images. Neither output is authoritative. The human reference transcription and Bronze source are the evaluation ground truth.

## Fixed processing record

- EXIF orientation normalization.
- PDF rasterization at 300 DPI.
- deterministic 2400-pixel long-edge cap, autocontrast, and bounded deskew attempt.
- PaddleOCR 3.7.0 / PaddlePaddle 3.2.2, CPU with oneDNN, German Latin model. The default `fast` profile relies on deterministic orientation/deskew preprocessing and runs text detection plus recognition. The optional `accurate` profile also runs Paddle orientation, unwarping, and text-line-orientation models. PaddlePaddle 3.3.x is intentionally excluded because its oneDNN/PIR regression breaks PP-OCR CPU inference.
- Tesseract 5.3.0, `deu+eng`, page segmentation mode 6.
- Native Excel extraction records sheets, coordinates, displayed/formula values and explicitly makes no laboratory interpretation; image OCR benchmarking is not applicable to a native workbook.

Raw engine result, configuration, version, time, source SHA-256, and output SHA-256 remain linked in PostgreSQL and `sc-rd-ocr-artifacts`.

Use one profile for an entire evaluation batch and record it in the report. `fast` is the Apple-Silicon local-pilot default. Switch to `accurate` only for a separate measured batch; do not mix profiles silently or claim a speed/quality comparison without human reference transcriptions.

## Batch procedure

1. Select 5–10 authorized sources outside Git following the acceptance template composition.
2. Upload via the web form; confirm locked original and manifest.
3. Independently transcribe the source without looking at either OCR output.
4. Compare Paddle and Tesseract word errors, including insertions, deletions, and substitutions.
5. Separately check every number, decimal separator, date, temperature, weight, unit, and material code.
6. Open Paddle draft in the UI, correct it against Bronze, time the review, then approve or reject explicitly.
7. Record glare, blur, skew, handwriting, small type, layout, and terminology failures. Do not omit failed files.

The standardized company iPhone 17 policy does not reduce the challenge set: include at least one glare/reflection sample from a lit screen.

## Reporting

Use `docs/governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md`. Compute per-file and aggregate metrics, compare engine medians, preserve all failures, and apply the documented GO/CONDITIONAL/BLOCKED thresholds. A passing automated suite is insufficient without this batch, sign-off, and restore drill.
