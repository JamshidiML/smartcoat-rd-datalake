from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps


def normalize_image(source: Path, destination: Path, max_side: int) -> Path:
    image = Image.open(source)
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        resized = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(resized, Image.Resampling.LANCZOS)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(1.1)

    pixels = np.array(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    points = np.column_stack(np.where(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1] > 0))
    if len(points) > 20:
        angle = cv2.minAreaRect(points)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if 0.25 < abs(angle) < 15:
            height, width = pixels.shape[:2]
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            pixels = cv2.warpAffine(
                pixels,
                matrix,
                (width, height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
            image = Image.fromarray(pixels)

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination
