#!/usr/bin/env python3
"""Operator-only exact-version legal-hold OFF with immutable audit evidence."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.error import S3Error


ALLOWED_BUCKETS = {"sc-rd-bronze-originals", "sc-rd-bronze-manifests"}
AUDIT_BUCKET = "sc-rd-legal-hold-audit"
CONFIRMATION = "CONFIRM_BREAK_GLASS_LEGAL_HOLD_CLEAR"
SAFE_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class BoundaryError(RuntimeError):
    pass


def required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise BoundaryError(f"required value absent: {name}")
    return value


def safe_text(name: str, value: str, *, identifier: bool = False) -> str:
    if not value or len(value) > 500 or any(ord(character) < 32 for character in value):
        raise BoundaryError(f"unsafe value rejected: {name}")
    if any(character in value for character in ('"', "\\")):
        raise BoundaryError(f"unsafe value rejected: {name}")
    if identifier and not SAFE_ID.fullmatch(value):
        raise BoundaryError(f"unsafe identifier rejected: {name}")
    return value


def exact_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise BoundaryError("timestamp_utc must use UTC second precision") from exc
    if parsed > datetime.now(timezone.utc):
        raise BoundaryError("timestamp_utc cannot be in the future")
    return parsed


def secure_transport() -> bool:
    value = os.environ.get("MINIO_HOLD_SECURE", "false").lower()
    if value not in {"true", "false"}:
        raise BoundaryError("MINIO_HOLD_SECURE must be true or false")
    return value == "true"


def read_exact(client: Minio, bucket: str, key: str, version_id: str) -> bytes:
    response = client.get_object(bucket, key, version_id=version_id)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def write_audit(client: Minio, request: dict[str, str], stage: str, observed: str) -> str:
    key = f"break-glass/{request['decision_id']}/{stage.lower()}.json"
    try:
        client.stat_object(AUDIT_BUCKET, key)
    except S3Error as exc:
        if exc.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
            raise
    else:
        raise BoundaryError("audit decision path already exists")
    body = json.dumps(
        {
            "schema_version": "1.0",
            "event_type": f"LEGAL_HOLD_CLEAR_{stage}",
            "decision_id": request["decision_id"],
            "actor": request["actor"],
            "reason": request["reason"],
            "timestamp_utc": request["timestamp_utc"],
            "bucket": request["bucket"],
            "object_key": request["object_key"],
            "version_id": request["version_id"],
            "observed_legal_hold": observed,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    result = client.put_object(AUDIT_BUCKET, key, io.BytesIO(body), len(body))
    if not result.version_id or read_exact(client, AUDIT_BUCKET, key, result.version_id) != body:
        raise BoundaryError("exact audit receipt read-back failed")
    return result.version_id


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.confirm != CONFIRMATION:
        raise BoundaryError("exact break-glass confirmation absent")
    request = {
        "decision_id": safe_text("decision_id", arguments.decision_id, identifier=True),
        "actor": safe_text("actor", arguments.actor, identifier=True),
        "reason": safe_text("reason", arguments.reason),
        "timestamp_utc": arguments.timestamp_utc,
        "bucket": arguments.bucket,
        "object_key": safe_text("object_key", arguments.object_key),
        "version_id": arguments.version_id,
    }
    exact_timestamp(request["timestamp_utc"])
    if request["bucket"] not in ALLOWED_BUCKETS:
        raise BoundaryError("bucket is outside the approved Bronze boundary")
    if (
        not request["object_key"].startswith("rd/")
        or any(part in {"", ".", ".."} for part in request["object_key"].split("/"))
    ):
        raise BoundaryError("object key is outside the approved rd/ prefix")
    if not SAFE_VERSION.fullmatch(request["version_id"]):
        raise BoundaryError("version_id is missing or malformed")

    client = Minio(
        required_environment("MINIO_HOLD_ENDPOINT"),
        access_key=required_environment("MINIO_HOLD_BREAK_GLASS_ACCESS_KEY"),
        secret_key=required_environment("MINIO_HOLD_BREAK_GLASS_SECRET_KEY"),
        secure=secure_transport(),
    )
    retention = client.get_object_retention(
        request["bucket"], request["object_key"], version_id=request["version_id"]
    )
    if (
        retention is None
        or retention.mode != COMPLIANCE
        or retention.retain_until_date <= datetime.now(timezone.utc)
    ):
        raise BoundaryError("active exact-version COMPLIANCE floor was not proven")
    if not client.is_object_legal_hold_enabled(
        request["bucket"], request["object_key"], version_id=request["version_id"]
    ):
        raise BoundaryError("exact-version legal hold ON was not proven")

    requested_version = write_audit(client, request, "REQUESTED", "ON")
    client.disable_object_legal_hold(
        request["bucket"], request["object_key"], version_id=request["version_id"]
    )
    if client.is_object_legal_hold_enabled(
        request["bucket"], request["object_key"], version_id=request["version_id"]
    ):
        raise BoundaryError("exact-version legal hold OFF was not confirmed")
    completed_version = write_audit(client, request, "COMPLETED", "OFF")
    return {
        "classification": "PASS_LEGAL_HOLD_BREAK_GLASS_CLEAR",
        "decision_id": request["decision_id"],
        "bucket": request["bucket"],
        "object_key": request["object_key"],
        "version_id": request["version_id"],
        "request_audit_version": requested_version,
        "completed_audit_version": completed_version,
        "legal_hold": "OFF",
        "compliance_floor": "ACTIVE_AND_UNCHANGED",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--decision-id", required=True)
    value.add_argument("--actor", required=True)
    value.add_argument("--reason", required=True)
    value.add_argument("--timestamp-utc", required=True)
    value.add_argument("--bucket", required=True)
    value.add_argument("--key", dest="object_key", required=True)
    value.add_argument("--version-id", required=True)
    value.add_argument("--confirm", required=True)
    return value


def main() -> int:
    try:
        evidence = execute(parser().parse_args())
    except Exception as exc:
        classification = "BLOCKED_AUTHORITY_BOUNDARY"
        message = str(exc) if isinstance(exc, BoundaryError) else type(exc).__name__
        print(json.dumps({"classification": classification, "message": message}), file=sys.stderr)
        return 2
    print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
