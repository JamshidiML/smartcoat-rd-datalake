from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from preprocess.documents import MAX_IMAGE_SIDE, PREPROCESSING_VERSION

# PaddlePaddle 3.3.x regressed oneDNN CPU inference for PP-OCR models.
# Pin 3.2.2 and keep the legacy IR flag explicit before importing PaddleOCR.
RUNTIME_FLAGS = {"FLAGS_enable_pir_api": "0"}
for name, value in RUNTIME_FLAGS.items():
    os.environ.setdefault(name, value)

from paddleocr import PaddleOCR  # noqa: E402 - runtime flag must be set before Paddle import


ENGINE_VERSION = "paddleocr-3.7.0+paddlepaddle-3.2.2"
PROFILE = os.getenv("OCR_PIPELINE_PROFILE", "fast").strip().lower()
PROFILE_OPTIONS = {
    "fast": {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    },
    "accurate": {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": True,
        "use_textline_orientation": True,
    },
}
if PROFILE not in PROFILE_OPTIONS:
    raise ValueError(f"Unsupported OCR_PIPELINE_PROFILE: {PROFILE}")

PIPELINE_KWARGS = {
    "device": "cpu",
    "lang": "german",
    "enable_mkldnn": True,
    **PROFILE_OPTIONS[PROFILE],
}
CONFIGURATION = {
    **PIPELINE_KWARGS,
    "pipeline": "general_ocr",
    "profile": PROFILE,
    "preprocessing": {"version": PREPROCESSING_VERSION, "max_image_side": MAX_IMAGE_SIDE},
    "runtime_flags": RUNTIME_FLAGS,
}


class PaddleEngine:
    def __init__(self) -> None:
        self.pipeline = PaddleOCR(**PIPELINE_KWARGS)

    def extract(self, images: list[Path]) -> tuple[str, list[dict[str, Any]], bytes]:
        raw_pages: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        all_text: list[str] = []
        block_number = 0
        for page_number, image in enumerate(images, start=1):
            for result in self.pipeline.predict(input=str(image)):
                payload = _plain(result.json)
                raw_pages.append(payload)
                content = payload.get("res", payload)
                texts = content.get("rec_texts", [])
                scores = content.get("rec_scores", [])
                boxes = content.get("rec_boxes", content.get("rec_polys", []))
                for index, text in enumerate(texts):
                    block_number += 1
                    box = boxes[index] if index < len(boxes) else [0, 0, 0, 0]
                    blocks.append(
                        {
                            "block_id": f"b-{block_number:04d}",
                            "page": page_number,
                            "bounding_box": _flat_box(box),
                            "text": str(text),
                            "ocr_confidence": float(scores[index]) if index < len(scores) else 0.0,
                        }
                    )
                    all_text.append(str(text))
        raw = json.dumps(
            {"engine": ENGINE_VERSION, "configuration": CONFIGURATION, "pages": raw_pages},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return "\n".join(all_text), blocks, raw


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _flat_box(value: Any) -> list[int]:
    plain = _plain(value)
    if not plain:
        return [0, 0, 0, 0]
    if isinstance(plain[0], list):
        xs = [int(point[0]) for point in plain]
        ys = [int(point[1]) for point in plain]
        return [min(xs), min(ys), max(xs), max(ys)]
    return [int(item) for item in plain[:4]]
