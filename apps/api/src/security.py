from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class InvalidSession(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(user_id: str, secret: str, lifetime_seconds: int = 12 * 60 * 60) -> str:
    payload = json.dumps(
        {"sub": user_id, "exp": int(time.time()) + lifetime_seconds},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = _b64(payload)
    signature = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_session(token: str, secret: str) -> str:
    try:
        encoded, provided = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(provided, expected):
            raise InvalidSession("Invalid session signature")
        payload = json.loads(_unb64(encoded))
        if int(payload["exp"]) < int(time.time()):
            raise InvalidSession("Session expired")
        return str(payload["sub"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, InvalidSession):
            raise
        raise InvalidSession("Malformed session") from exc
