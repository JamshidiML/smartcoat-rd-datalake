from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# PaddlePaddle 3.3.x's PIR executor cannot convert some PP-OCR model
# attributes for oneDNN CPU inference. Set the PIR flag before importing
# PaddleOCR and also disable oneDNN explicitly in the pipeline options.
RUNTIME_FLAGS = {"FLAGS_enable_pir_api": "0"}
for name, value in RUNTIME_FLAGS.items():
    os.environ.setdefault(name, value)

from paddleocr import PaddleOCR


ENGINE_VERSION = "paddleocr-3.7.0+paddlepaddle-3.3.1"
PIPELINE_KWARGS = {
    "device": "cpu",
    "lang": "german",
    "use_doc_orientation_classify": True,
    "use_doc_unwarping": True,
    "use_textline_orientation": True,
    "enable_mkldnn": False,
}
CONFIGURATION = {
    **PIPELINE_KWARGS,
    "pipeline": "general_ocr",
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
