from __future__ import annotations

import os
import time
import uuid


def uuid7() -> str:
    """Return a standards-shaped UUIDv7 without relying on a Python-version feature."""
    timestamp_ms = int(time.time() * 1000)
    random_bytes = bytearray(os.urandom(10))
    raw = bytearray(timestamp_ms.to_bytes(6, "big") + random_bytes)
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))
