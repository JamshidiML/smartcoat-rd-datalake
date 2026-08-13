from __future__ import annotations

from pathlib import Path

from pdf2image import convert_from_path
from PIL import Image
from pillow_heif import register_heif_opener

from preprocess.images import normalize_image


register_heif_opener()


def preprocess_source(source: Path, mime_type: str, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if mime_type == "application/pdf":
        rendered = convert_from_path(source, dpi=300, fmt="png", output_folder=output_dir, paths_only=True)
        normalized: list[Path] = []
        for index, page in enumerate(rendered, start=1):
            destination = output_dir / f"page-{index:04d}.png"
            normalized.append(normalize_image(Path(page), destination))
        return normalized

    if mime_type == "image/heic":
        converted = output_dir / "heic-source.png"
        Image.open(source).convert("RGB").save(converted, format="PNG")
        source = converted
    return [normalize_image(source, output_dir / "page-0001.png")]
