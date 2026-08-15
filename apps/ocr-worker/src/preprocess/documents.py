from __future__ import annotations

import os
from pathlib import Path

from pdf2image import convert_from_path
from pillow_heif import register_heif_opener

from preprocess.images import normalize_image


register_heif_opener()
PREPROCESSING_VERSION = "2"
MAX_IMAGE_SIDE = int(os.getenv("OCR_MAX_IMAGE_SIDE", "2400"))
if not 1200 <= MAX_IMAGE_SIDE <= 4000:
    raise ValueError("OCR_MAX_IMAGE_SIDE must be between 1200 and 4000")


def preprocess_source(source: Path, mime_type: str, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if mime_type == "application/pdf":
        rendered = convert_from_path(source, dpi=300, fmt="png", output_folder=output_dir, paths_only=True)
        normalized: list[Path] = []
        for index, page in enumerate(rendered, start=1):
            destination = output_dir / f"page-{index:04d}.png"
            normalized.append(normalize_image(Path(page), destination, MAX_IMAGE_SIDE))
        return normalized

    return [normalize_image(source, output_dir / "page-0001.png", MAX_IMAGE_SIDE)]
