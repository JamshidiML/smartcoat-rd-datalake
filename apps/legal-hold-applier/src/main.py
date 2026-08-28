"""Internal exact-version legal-hold ON mediator; intentionally not a proxy."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from minio import Minio

from contract import LEGAL_HOLD_ON, RequestRejected, validate_request


MAX_REQUEST_BYTES = 16_384


def required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required mediator configuration is absent: {name}")
    return value


def secure_transport() -> bool:
    value = os.environ.get("MINIO_HOLD_APPLIER_SECURE", "false").lower()
    if value not in {"true", "false"}:
        raise RuntimeError("MINIO_HOLD_APPLIER_SECURE must be true or false")
    return value == "true"


CLIENT = Minio(
    required_environment("MINIO_HOLD_APPLIER_ENDPOINT"),
    access_key=required_environment("MINIO_HOLD_APPLIER_ACCESS_KEY"),
    secret_key=required_environment("MINIO_HOLD_APPLIER_SECRET_KEY"),
    secure=secure_transport(),
)


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartCoatLegalHoldApplier/1"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def respond(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.respond(HTTPStatus.OK, {"status": "ok"})
            return
        self.respond(HTTPStatus.NOT_FOUND, {"classification": "REQUEST_REJECTED"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/apply", "/status"}:
            self.respond(HTTPStatus.NOT_FOUND, {"classification": "REQUEST_REJECTED"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 2 or length > MAX_REQUEST_BYTES:
                raise RequestRejected("request size is invalid")
            target = validate_request(json.loads(self.rfile.read(length)))
        except (RequestRejected, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.respond(HTTPStatus.BAD_REQUEST, {"classification": "REQUEST_REJECTED"})
            return
        try:
            if self.path == "/apply":
                CLIENT.enable_object_legal_hold(
                    target.bucket, target.object_key, version_id=target.version_id
                )
            observed = CLIENT.is_object_legal_hold_enabled(
                target.bucket, target.object_key, version_id=target.version_id
            )
        except Exception:
            self.respond(
                HTTPStatus.BAD_GATEWAY,
                {"classification": "LEGAL_HOLD_APPLY_FAILED"},
            )
            return
        if self.path == "/apply" and observed is not LEGAL_HOLD_ON:
            self.respond(
                HTTPStatus.CONFLICT,
                {"classification": "LEGAL_HOLD_READBACK_FAILED"},
            )
            return
        self.respond(
            HTTPStatus.OK,
            {
                "classification": (
                    "LEGAL_HOLD_APPLIED"
                    if self.path == "/apply"
                    else "LEGAL_HOLD_STATUS_OBSERVED"
                ),
                "bucket": target.bucket,
                "object_key": target.object_key,
                "version_id": target.version_id,
                "legal_hold": "ON" if observed else "OFF",
            },
        )


def main() -> None:
    port = int(os.environ.get("LEGAL_HOLD_APPLIER_PORT", "8090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
