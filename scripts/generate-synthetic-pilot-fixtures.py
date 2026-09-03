#!/usr/bin/env python3
"""Generate the deterministic, synthetic-only WP10 pilot fixture set."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zlib
import zipfile
from pathlib import Path


FIXED_ZIP_TIME = (2026, 9, 3, 0, 0, 0)
OVERSIZED_BYTES = 50 * 1024 * 1024 + 1

FONT = {
    " ": ("000",) * 5,
    "-": ("000", "000", "111", "000", "000"),
    ".": ("000", "000", "000", "000", "010"),
    "/": ("001", "001", "010", "100", "100"),
    ":": ("000", "010", "000", "010", "000"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "N": ("1001", "1101", "1011", "1001", "1001"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "011", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("10001", "10001", "10101", "11011", "10001"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}


class Canvas:
    def __init__(self, width: int, height: int, colour: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(colour * (width * height))

    def pixel(self, x: int, y: int, colour: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(colour)

    def rectangle(
        self, x0: int, y0: int, x1: int, y1: int, colour: tuple[int, int, int]
    ) -> None:
        for y in range(max(y0, 0), min(y1, self.height)):
            start = (y * self.width + max(x0, 0)) * 3
            end = (y * self.width + min(x1, self.width)) * 3
            self.pixels[start:end] = bytes(colour) * max(0, min(x1, self.width) - max(x0, 0))

    def line(
        self, x0: int, y0: int, x1: int, y1: int, colour: tuple[int, int, int], width: int = 1
    ) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        while True:
            radius = max(0, width // 2)
            self.rectangle(x0 - radius, y0 - radius, x0 + radius + 1, y0 + radius + 1, colour)
            if x0 == x1 and y0 == y1:
                return
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    def text(
        self,
        x: int,
        y: int,
        value: str,
        scale: int,
        colour: tuple[int, int, int],
        handwritten: bool = False,
    ) -> None:
        cursor = x
        for index, character in enumerate(value.upper()):
            glyph = FONT.get(character, FONT[" "])
            glyph_width = len(glyph[0])
            wobble = ((index * 7) % 5) - 2 if handwritten else 0
            for row, pattern in enumerate(glyph):
                slant = (4 - row) if handwritten else 0
                for column, enabled in enumerate(pattern):
                    if enabled == "1":
                        self.rectangle(
                            cursor + (column * scale) + slant,
                            y + (row * scale) + wobble,
                            cursor + ((column + 1) * scale) + slant,
                            y + ((row + 1) * scale) + wobble,
                            colour,
                        )
            cursor += (glyph_width + 1) * scale

    def png(self) -> bytes:
        raw = b"".join(
            b"\x00" + bytes(self.pixels[row * self.width * 3 : (row + 1) * self.width * 3])
            for row in range(self.height)
        )

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b"")
        )


def notebook_canvas() -> Canvas:
    canvas = Canvas(1200, 1600, (211, 214, 205))
    canvas.rectangle(100, 70, 1100, 1530, (250, 246, 224))
    for y in range(180, 1480, 70):
        canvas.line(130, y, 1070, y, (175, 205, 220), 2)
    canvas.line(235, 100, 235, 1500, (225, 135, 135), 3)
    ink = (28, 49, 74)
    rows = (
        "LAB NOTE 2026-09-03",
        "BATCH SC-104",
        "TEMP 23.5 C",
        "PRESSURE 1.20 BAR",
        "VISCOSITY 840 MPA S",
        "PH 7.4",
        "RESULT PASS",
    )
    for index, row in enumerate(rows):
        canvas.text(285, 215 + index * 150, row, 12, ink, handwritten=True)
    canvas.line(285, 1300, 760, 1330, ink, 5)
    canvas.line(760, 1330, 820, 1290, ink, 5)
    return canvas


def dark_skewed(source: Canvas) -> Canvas:
    result = Canvas(source.width, source.height, (35, 38, 40))
    for y in range(result.height):
        shade = 0.35 + 0.35 * (y / result.height)
        for x in range(result.width):
            source_x = int(x - 0.12 * (y - result.height / 2))
            if 0 <= source_x < source.width:
                source_offset = (y * source.width + source_x) * 3
                colour = tuple(int(source.pixels[source_offset + channel] * shade) for channel in range(3))
                result.pixel(x, y, colour)
    return result


def rotate_clockwise(source: Canvas) -> Canvas:
    result = Canvas(source.height, source.width, (230, 230, 230))
    for y in range(source.height):
        for x in range(source.width):
            source_offset = (y * source.width + x) * 3
            result.pixel(source.height - 1 - y, x, tuple(source.pixels[source_offset : source_offset + 3]))
    return result


def screenshot_canvas() -> Canvas:
    canvas = Canvas(1280, 800, (40, 44, 52))
    canvas.rectangle(65, 45, 1215, 750, (242, 244, 247))
    canvas.rectangle(65, 45, 1215, 110, (70, 105, 160))
    canvas.text(105, 67, "SMARTCOAT TEST REPORT", 6, (255, 255, 255))
    canvas.text(130, 165, "FORMULATION SC-104", 8, (30, 40, 55))
    canvas.text(130, 270, "DRY TIME 18.5 MIN", 7, (30, 40, 55))
    canvas.text(130, 360, "GLOSS 87.2 GU", 7, (30, 40, 55))
    canvas.text(130, 450, "HARDNESS 4 H", 7, (30, 40, 55))
    canvas.rectangle(885, 610, 1110, 690, (40, 145, 90))
    canvas.text(930, 632, "PASS", 6, (255, 255, 255))
    return canvas


def pdf_bytes() -> bytes:
    streams = (
        b"BT /F1 18 Tf 72 760 Td (SMARTCOAT SYNTHETIC TECHNICAL REPORT) Tj 0 -36 Td /F1 12 Tf (Batch SC-104 - Page 1 of 2) Tj 0 -28 Td (Purpose: local-only pipeline verification.) Tj 0 -28 Td (Result: PASS) Tj ET",
        b"BT /F1 18 Tf 72 760 Td (MEASUREMENT TABLE) Tj 0 -42 Td /F1 11 Tf (Property     Value     Unit) Tj 0 -24 Td (Temperature  23.5      C) Tj 0 -24 Td (Pressure     1.20      bar) Tj 0 -24 Td (Viscosity    840       mPa.s) Tj 0 -24 Td (Gloss        87.2      GU) Tj ET 70 735 m 500 735 l S 70 695 m 500 695 l S",
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(streams[0]), streams[0]),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(streams[1]), streams[1]),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(body)


def xlsx_bytes(destination: Path) -> bytes:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Measurements" sheetId="1" r:id="rId1"/></sheets></workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Measurement</t></is></c><c r="B1" t="inlineStr"><is><t>Value</t></is></c><c r="C1" t="inlineStr"><is><t>Unit</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Temperature</t></is></c><c r="B2"><v>23.5</v></c><c r="C2" t="inlineStr"><is><t>C</t></is></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Pressure</t></is></c><c r="B3"><v>1.20</v></c><c r="C3" t="inlineStr"><is><t>bar</t></is></c></row><row r="4"><c r="A4" t="inlineStr"><is><t>Viscosity</t></is></c><c r="B4"><v>840.0</v></c><c r="C4" t="inlineStr"><is><t>mPa.s</t></is></c></row></sheetData></worksheet>""",
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name].encode())
    return destination.read_bytes()


def oversized_jpeg_bytes() -> bytes:
    marker = b"SMARTCOAT-SYNTHETIC-OVERSIZE\x00"
    body_size = OVERSIZED_BYTES - 6
    body = (marker * ((body_size + len(marker) - 1) // len(marker)))[:body_size]
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"


def write_fixtures(output: Path) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=True)
    clean = notebook_canvas().png()
    fixtures: list[tuple[str, bytes, str, str]] = [
        ("01-clean-handwritten-lab-note.png", clean, "LAB_NOTE", "VERIFIED_OR_OCR_FAILED"),
        ("02-poor-light-skewed-lab-note.png", dark_skewed(notebook_canvas()).png(), "LAB_NOTE", "VERIFIED_OR_OCR_FAILED"),
        ("03-rotated-90-scan.png", rotate_clockwise(notebook_canvas()).png(), "LAB_NOTE", "VERIFIED_OR_OCR_FAILED"),
        ("04-multipage-technical-report.pdf", pdf_bytes(), "TEST_RESULT", "VERIFIED_OR_OCR_FAILED"),
        ("05-measurement-sheet.xlsx", b"", "TEST_RESULT", "VERIFIED_OR_OCR_FAILED"),
        ("06-photo-of-screen.png", screenshot_canvas().png(), "FORMULATION_SCREEN", "VERIFIED_OR_OCR_FAILED"),
        ("07-byte-identical-duplicate.png", clean, "LAB_NOTE", "DUPLICATE_SEPARATE_PROVENANCE_PAIR"),
        ("08-over-50mb.jpg", oversized_jpeg_bytes(), "OTHER", "FILE_TOO_LARGE"),
        ("09-unsupported.txt", b"SMARTCOAT SYNTHETIC UNSUPPORTED TYPE\n", "OTHER", "UNSUPPORTED_TYPE"),
        ("10-corrupt-valid-extension.pdf", b"%PDF-1.7\nsynthetic truncated payload without terminal marker\n", "MATERIAL_DOCUMENT", "CORRUPT_FILE_OR_OCR_FAILED"),
    ]
    xlsx_path = output / fixtures[4][0]
    xlsx = xlsx_bytes(xlsx_path)
    fixtures[4] = (fixtures[4][0], xlsx, fixtures[4][2], fixtures[4][3])
    records: list[dict[str, object]] = []
    for index, (filename, payload, category, expected) in enumerate(fixtures, start=1):
        path = output / filename
        path.write_bytes(payload)
        records.append(
            {
                "fixture": index,
                "filename": filename,
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "document_category": category,
                "expected_outcome": expected,
                "synthetic": True,
            }
        )
    manifest = {
        "generator": "scripts/generate-synthetic-pilot-fixtures.py",
        "generator_contract": "WP10-P0-16",
        "fixtures": records,
    }
    (output / "FIXTURE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Output directory outside company-data paths")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if output.exists():
        parser.error("output already exists; choose a new generated fixture directory")
    records = write_fixtures(output)
    print(json.dumps({"output": str(output), "fixtures": records}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
