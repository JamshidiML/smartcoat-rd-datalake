"""Fail-closed request contract for the internal legal-hold mediator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


ALLOWED_BUCKETS = frozenset(
    {"sc-rd-bronze-originals", "sc-rd-bronze-manifests"}
)
REQUIRED_FIELDS = frozenset({"bucket", "object_key", "version_id"})
VERSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
LEGAL_HOLD_ON = True


class RequestRejected(ValueError):
    """Raised before storage access when a request escapes the ON-only contract."""


@dataclass(frozen=True)
class HoldTarget:
    bucket: str
    object_key: str
    version_id: str


def validate_request(value: Any) -> HoldTarget:
    if not isinstance(value, dict) or frozenset(value) != REQUIRED_FIELDS:
        raise RequestRejected("request must contain exactly bucket, object_key, version_id")
    if not all(isinstance(value[field], str) for field in REQUIRED_FIELDS):
        raise RequestRejected("request fields must be strings")
    bucket = value["bucket"]
    object_key = value["object_key"]
    version_id = value["version_id"]
    if bucket not in ALLOWED_BUCKETS:
        raise RequestRejected("bucket is outside the approved Bronze boundary")
    if (
        not object_key.startswith("rd/")
        or len(object_key) > 1024
        or any(ord(character) < 32 for character in object_key)
        or any(part in {"", ".", ".."} for part in object_key.split("/"))
    ):
        raise RequestRejected("object key is outside the approved rd/ prefix")
    if not VERSION_ID.fullmatch(version_id):
        raise RequestRejected("version_id is missing or malformed")
    return HoldTarget(bucket, object_key, version_id)
