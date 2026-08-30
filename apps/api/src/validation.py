from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from operational_logging import log_event


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_CATEGORIES = {
    "LAB_NOTE",
    "TEST_RESULT",
    "FORMULATION_SCREEN",
    "MATERIAL_DOCUMENT",
    "OTHER",
}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".xlsx", ".xls"}


class UploadValidationError(ValueError):
    def __init__(self, reason: str, code: str = "INVALID_UPLOAD") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class ValidatedUpload:
    mime_type: str
    file_type: str
    sanitized_filename: str


def sanitize_filename(filename: str) -> str:
    leaf = Path(filename.replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._")
    return stem[:180] or "upload"


def _detect_mime(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "PHOTO"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "PHOTO"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
    }:
        return "image/heic", "PHOTO"
    if data.startswith(b"%PDF-"):
        return "application/pdf", "PDF"
    if data.startswith(b"PK\x03\x04"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "EXCEL"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/vnd.ms-excel", "EXCEL"
    raise UploadValidationError("File bytes do not match a supported format", "UNSUPPORTED_TYPE")


def _validate_pdf(data: bytes) -> None:
    if b"%%EOF" not in data[-4096:]:
        raise UploadValidationError("PDF is corrupt or incomplete", "CORRUPT_FILE")
    if b"/Encrypt" in data:
        raise UploadValidationError("Password-protected PDFs are not supported", "PASSWORD_PROTECTED")


def _validate_xlsx(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as workbook:
            names = set(workbook.namelist())
            if "EncryptedPackage" in names or "EncryptionInfo" in names:
                raise UploadValidationError(
                    "Password-protected Excel files are not supported", "PASSWORD_PROTECTED"
                )
            if "[Content_Types].xml" not in names or not any(name.startswith("xl/") for name in names):
                raise UploadValidationError("Excel workbook is corrupt", "CORRUPT_FILE")
            if workbook.testzip() is not None:
                raise UploadValidationError("Excel workbook is corrupt", "CORRUPT_FILE")
    except zipfile.BadZipFile as exc:
        log_event(
            "WARNING",
            "validation.exception.caught",
            reason="CORRUPT_EXCEL_ARCHIVE",
            validation_code="CORRUPT_FILE",
            error_type=type(exc).__name__,
        )
        raise UploadValidationError("Excel workbook is corrupt", "CORRUPT_FILE") from exc


def validate_upload(
    filename: str,
    data: bytes,
    document_category: str,
    context_note: str,
    capture_date: str | None,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> ValidatedUpload:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(f"Unsupported file extension: {extension or '(none)'}", "UNSUPPORTED_TYPE")
    if not data:
        raise UploadValidationError("File is empty", "CORRUPT_FILE")
    if len(data) > max_upload_bytes:
        raise UploadValidationError("File exceeds the 50 MB pilot limit", "FILE_TOO_LARGE")
    if document_category not in ALLOWED_CATEGORIES:
        raise UploadValidationError("Invalid document category", "INVALID_METADATA")
    if not 10 <= len(context_note.strip()) <= 500:
        raise UploadValidationError("Context note must contain 10–500 characters", "INVALID_METADATA")
    if capture_date:
        try:
            date.fromisoformat(capture_date)
        except ValueError as exc:
            log_event(
                "WARNING",
                "validation.exception.caught",
                reason="INVALID_CAPTURE_DATE",
                validation_code="INVALID_METADATA",
                error_type=type(exc).__name__,
            )
            raise UploadValidationError("Capture date must be an ISO-8601 date", "INVALID_METADATA") from exc

    mime_type, file_type = _detect_mime(data)
    if extension == ".xlsx" and mime_type == "application/vnd.ms-excel":
        raise UploadValidationError("Password-protected Excel files are not supported", "PASSWORD_PROTECTED")
    expected_extensions = {
        "image/jpeg": {".jpg", ".jpeg"},
        "image/png": {".png"},
        "image/heic": {".heic"},
        "application/pdf": {".pdf"},
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
        "application/vnd.ms-excel": {".xls"},
    }
    if extension not in expected_extensions[mime_type]:
        raise UploadValidationError("Filename extension does not match detected file bytes", "TYPE_MISMATCH")
    if file_type == "PDF":
        _validate_pdf(data)
    elif mime_type == "image/jpeg" and not data.endswith(b"\xff\xd9"):
        raise UploadValidationError("JPEG is corrupt or incomplete", "CORRUPT_FILE")
    elif mime_type == "image/png" and not data.endswith(b"IEND\xaeB`\x82"):
        raise UploadValidationError("PNG is corrupt or incomplete", "CORRUPT_FILE")
    elif mime_type.endswith("spreadsheetml.sheet"):
        _validate_xlsx(data)
    elif mime_type == "application/vnd.ms-excel":
        if len(data) < 512 or data[28:30] != b"\xfe\xff" or data[30:32] not in {b"\x09\x00", b"\x0c\x00"}:
            raise UploadValidationError("Excel workbook is corrupt", "CORRUPT_FILE")
        encrypted_markers = (b"EncryptedPackage", "EncryptedPackage".encode("utf-16le"))
        if any(marker in data for marker in encrypted_markers):
            raise UploadValidationError("Password-protected Excel files are not supported", "PASSWORD_PROTECTED")

    return ValidatedUpload(mime_type, file_type, sanitize_filename(filename))
