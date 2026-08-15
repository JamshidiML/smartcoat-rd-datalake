from __future__ import annotations

import csv
import io
import json
import subprocess
from pathlib import Path
from typing import Any


ENGINE_VERSION = "tesseract-5.3.0"


def benchmark(images: list[Path]) -> bytes:
    pages: list[dict[str, Any]] = []
    for page, image in enumerate(images, start=1):
        process = subprocess.run(
            ["tesseract", str(image), "stdout", "-l", "deu+eng", "--psm", "6", "tsv"],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = list(csv.DictReader(io.StringIO(process.stdout), delimiter="\t"))
        words = [row for row in rows if row.get("text", "").strip()]
        pages.append({"page": page, "words": words})
    return json.dumps(
        {
            "engine": ENGINE_VERSION,
            "configuration": {"languages": "deu+eng", "psm": 6, "input": "shared-preprocessed"},
            "pages": pages,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
