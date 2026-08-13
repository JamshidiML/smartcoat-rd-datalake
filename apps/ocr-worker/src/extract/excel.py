from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl


def extract_workbook(path: Path) -> tuple[str, list[dict[str, Any]], bytes]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    text_lines: list[str] = []
    blocks: list[dict[str, Any]] = []
    sheets: list[dict[str, Any]] = []
    block_number = 0
    for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
        cells: list[dict[str, Any]] = []
        text_lines.append(f"[{sheet.title}]")
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                block_number += 1
                displayed = str(cell.value)
                formula = displayed if displayed.startswith("=") else None
                cell_data = {"coordinate": cell.coordinate, "displayed_value": displayed, "formula": formula}
                cells.append(cell_data)
                text_lines.append(f"{cell.coordinate}: {displayed}")
                blocks.append(
                    {
                        "block_id": f"b-{block_number:04d}",
                        "page": sheet_index,
                        "bounding_box": [0, 0, 0, 0],
                        "text": f"{cell.coordinate}: {displayed}",
                        "ocr_confidence": 1.0,
                    }
                )
        sheets.append({"sheet_name": sheet.title, "cells": cells})
    raw = json.dumps(
        {
            "engine": "openpyxl-3.1.5",
            "warning": "Values are extracted, not interpreted as laboratory facts.",
            "sheets": sheets,
            "tesseract_benchmark": "not-applicable-to-native-workbook",
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "\n".join(text_lines), blocks, raw
