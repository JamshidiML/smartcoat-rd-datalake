"""Fail-safe structured operational logging for local SmartCoat services.

Operational logs explain runtime behavior. They never replace or modify the
append-only ``audit_events`` evidentiary record.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, Callable, Iterator


LOG_LEVEL_ENV = "SMARTCOAT_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
LEVEL_VALUES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
REDACTED = "[REDACTED]"

_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "smartcoat_correlation_id", default=None
)
_FORBIDDEN_FIELDS = {
    "authorization",
    "connection_url",
    "database_url",
    "extracted_text",
    "file_content",
    "ocr_text",
    "raw_output",
    "verified_text",
}
_FORBIDDEN_FIELD_PARTS = ("credential", "password", "secret", "session", "token")
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def current_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


def bind_correlation(correlation_id: str) -> Token[str | None]:
    return _CORRELATION_ID.set(correlation_id)


def reset_correlation(token: Token[str | None]) -> None:
    _CORRELATION_ID.reset(token)


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    value = correlation_id or new_correlation_id()
    token = bind_correlation(value)
    try:
        yield value
    finally:
        reset_correlation(token)


def configured_level(environment: dict[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    configured = source.get(LOG_LEVEL_ENV, DEFAULT_LEVEL).upper()
    return configured if configured in VALID_LEVELS else DEFAULT_LEVEL


def _contains_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)


def _sanitize(value: Any, field_name: str = "") -> Any:
    lowered = field_name.lower()
    if lowered in _FORBIDDEN_FIELDS or any(
        part in lowered for part in _FORBIDDEN_FIELD_PARTS
    ):
        return REDACTED
    if isinstance(value, dict):
        return {
            (REDACTED if _contains_credential(str(key)) else str(key)): _sanitize(
                item, str(key)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, field_name) for item in value]
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and _contains_credential(value):
        return REDACTED
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


class StructuredLogger:
    def __init__(
        self,
        service: str,
        *,
        level: str | None = None,
        sink: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        selected = (level or configured_level()).upper()
        self.level = selected if selected in VALID_LEVELS else DEFAULT_LEVEL
        self._sink = sink or self._stdout_sink

    @staticmethod
    def _stdout_sink(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def emit(self, level: str, event: str, **fields: Any) -> None:
        """Emit one JSON line, swallowing all telemetry-layer failures."""

        try:
            normalized_level = level.upper()
            if normalized_level not in VALID_LEVELS:
                normalized_level = "ERROR"
            if LEVEL_VALUES[normalized_level] < LEVEL_VALUES[self.level]:
                return
            correlation_id = current_correlation_id() or new_correlation_id()
            record = {
                "timestamp_utc": utc_timestamp(),
                "level": normalized_level,
                "event": event,
                "service": self.service,
                "correlation_id": correlation_id,
                **{
                    key: _sanitize(value, key)
                    for key, value in fields.items()
                    if key
                    not in {
                        "timestamp_utc",
                        "level",
                        "event",
                        "service",
                        "correlation_id",
                    }
                },
            }
            self._sink(json.dumps(record, sort_keys=True, separators=(",", ":")))
        except Exception:
            return


_LOGGER = StructuredLogger("api")


def configure_service(service: str, level: str | None = None) -> None:
    global _LOGGER
    _LOGGER = StructuredLogger(service, level=level)


def log_event(level: str, event: str, **fields: Any) -> None:
    _LOGGER.emit(level, event, **fields)
