"""Exact-version retention enforcement primitive for protected Bronze evidence.

This module deliberately does not commit an original/manifest pair or advance an
ingestion state.  BRONZE_PAIR_READY owns that later orchestration boundary.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from packages.smartcoat_logging.operational_logging import log_event
from retention_policy import (
    CANONICAL_RETENTION_CLASSES,
    RETENTION_POLICY_VERSION,
    normalize_storage_timestamp,
    plan_assignment,
)


APPROVED_BUCKETS = frozenset(
    {"sc-rd-bronze-originals", "sc-rd-bronze-manifests"}
)
VERSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
OBJECT_KINDS = {"sc-rd-bronze-originals": "ORIGINAL", "sc-rd-bronze-manifests": "MANIFEST"}


class RetentionEnforcementError(RuntimeError):
    """Fail-closed exact-version enforcement error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExactVersionTarget:
    bucket_name: str
    object_key: str
    object_version_id: str
    object_kind: str

    def validate(self) -> None:
        if self.bucket_name not in APPROVED_BUCKETS:
            raise RetentionEnforcementError("UNAUTHORIZED_BUCKET")
        if self.object_kind != OBJECT_KINDS[self.bucket_name]:
            raise RetentionEnforcementError("OBJECT_KIND_MISMATCH")
        if (
            not self.object_key.startswith("rd/")
            or len(self.object_key) > 1024
            or any(ord(character) < 32 for character in self.object_key)
            or any(part in {"", ".", ".."} for part in self.object_key.split("/"))
        ):
            raise RetentionEnforcementError("MALFORMED_OBJECT_KEY")
        if not VERSION_ID.fullmatch(self.object_version_id):
            raise RetentionEnforcementError("MALFORMED_VERSION_ID")


@dataclass(frozen=True)
class StorageVersionMetadata:
    object_version_id: str
    last_modified_utc: datetime


@dataclass(frozen=True)
class StorageRetention:
    mode: str
    retain_until_utc: datetime


@dataclass(frozen=True)
class RetentionEnforcementEvidence:
    retention_assignment_id: str
    bucket_name: str
    object_key: str
    object_kind: str
    object_version_id: str
    data_category: str
    retention_class: str
    retention_policy_version: str
    accepted_storage_at_utc: datetime
    requested_retention_mode: str
    requested_retain_until_utc: datetime
    requested_legal_hold_status: str
    observed_object_version_id: str
    observed_retention_mode: str
    observed_retain_until_utc: datetime
    observed_legal_hold_status: str
    enforcement_verified_at_utc: datetime
    enforcement_verification_result: str
    failure_code: str | None
    enforced_by: str
    details_json: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


class ExactVersionRetentionStorage(Protocol):
    def stat_exact(self, target: ExactVersionTarget) -> StorageVersionMetadata: ...
    def get_retention_exact(self, target: ExactVersionTarget) -> StorageRetention | None: ...
    def set_retention_exact(
        self, target: ExactVersionTarget, retain_until_utc: datetime
    ) -> None: ...


class LegalHoldMediator(Protocol):
    def apply_on(self, target: ExactVersionTarget) -> str: ...
    def read_status(self, target: ExactVersionTarget) -> str: ...


class MinioExactVersionRetentionStorage:
    """Pinned-SDK adapter with no key-only or latest-version operation."""

    def __init__(
        self,
        client: Any,
        *,
        retention_factory: Any | None = None,
        s3_error_type: type[Exception] | None = None,
    ) -> None:
        if retention_factory is None or s3_error_type is None:
            from minio.commonconfig import COMPLIANCE
            from minio.error import S3Error
            from minio.retention import Retention

            def retention_factory(retain_until: datetime) -> Any:
                return Retention(COMPLIANCE, retain_until)

            s3_error_type = S3Error
        self.client = client
        self.retention_factory = retention_factory
        self.s3_error_type = s3_error_type

    def stat_exact(self, target: ExactVersionTarget) -> StorageVersionMetadata:
        value = self.client.stat_object(
            target.bucket_name,
            target.object_key,
            version_id=target.object_version_id,
        )
        if value.version_id != target.object_version_id or value.last_modified is None:
            raise RetentionEnforcementError("EXACT_VERSION_METADATA_MISMATCH")
        return StorageVersionMetadata(
            object_version_id=value.version_id,
            last_modified_utc=normalize_storage_timestamp(value.last_modified),
        )

    def get_retention_exact(self, target: ExactVersionTarget) -> StorageRetention | None:
        try:
            value = self.client.get_object_retention(
                target.bucket_name,
                target.object_key,
                version_id=target.object_version_id,
            )
        except self.s3_error_type as exc:
            if exc.code in {"NoSuchObjectLockConfiguration", "NoSuchRetention"}:
                log_event(
                    "INFO",
                    "retention.readback.absent",
                    bucket=target.bucket_name,
                    object_key=target.object_key,
                    object_version_id=target.object_version_id,
                    error_type=type(exc).__name__,
                )
                return None
            log_event(
                "ERROR",
                "retention.readback.failed",
                bucket=target.bucket_name,
                object_key=target.object_key,
                object_version_id=target.object_version_id,
                error_type=type(exc).__name__,
            )
            raise
        if value is None or value.mode is None or value.retain_until_date is None:
            return None
        mode = str(value.mode).upper()
        if mode.endswith(".COMPLIANCE"):
            mode = "COMPLIANCE"
        return StorageRetention(
            mode=mode,
            retain_until_utc=normalize_storage_timestamp(value.retain_until_date),
        )

    def set_retention_exact(
        self, target: ExactVersionTarget, retain_until_utc: datetime
    ) -> None:
        self.client.set_object_retention(
            target.bucket_name,
            target.object_key,
            self.retention_factory(retain_until_utc),
            version_id=target.object_version_id,
        )


class HttpLegalHoldMediator:
    """Calls only the mediator's fixed ON and read-only status operations."""

    def __init__(
        self,
        base_url: str,
        call_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if len(call_token) < 32 or "\n" in call_token or "\x00" in call_token:
            raise RetentionEnforcementError("LEGAL_HOLD_CALL_TOKEN_INVALID")
        self.base_url = base_url.rstrip("/")
        self.call_token = call_token
        self.timeout_seconds = timeout_seconds

    def _request(self, operation: str, target: ExactVersionTarget) -> str:
        payload = json.dumps(
            {
                "bucket": target.bucket_name,
                "object_key": target.object_key,
                "version_id": target.object_version_id,
            },
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/{operation}",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.call_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        log_event(
            "INFO",
            "legal_hold.call.started",
            operation=operation,
            bucket=target.bucket_name,
            object_key=target.object_key,
            object_version_id=target.object_version_id,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read())
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            log_event(
                "ERROR",
                "legal_hold.call.failed",
                operation=operation,
                bucket=target.bucket_name,
                object_key=target.object_key,
                object_version_id=target.object_version_id,
                error_type=type(exc).__name__,
            )
            raise RetentionEnforcementError("LEGAL_HOLD_MEDIATOR_UNAVAILABLE") from exc
        status = value.get("legal_hold")
        if status not in {"ON", "OFF"}:
            raise RetentionEnforcementError("LEGAL_HOLD_READBACK_UNAVAILABLE")
        log_event(
            "INFO",
            "legal_hold.call.completed",
            operation=operation,
            bucket=target.bucket_name,
            object_key=target.object_key,
            object_version_id=target.object_version_id,
            legal_hold_status=status,
        )
        return status

    def apply_on(self, target: ExactVersionTarget) -> str:
        status = self._request("apply", target)
        if status != "ON":
            raise RetentionEnforcementError("LEGAL_HOLD_ON_MISMATCH")
        return status

    def read_status(self, target: ExactVersionTarget) -> str:
        return self._request("status", target)


class ExactVersionRetentionEnforcer:
    def __init__(
        self,
        storage: ExactVersionRetentionStorage,
        legal_hold_mediator: LegalHoldMediator,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.storage = storage
        self.legal_hold_mediator = legal_hold_mediator
        self.clock = clock

    def enforce(
        self,
        *,
        target: ExactVersionTarget,
        retention_assignment_id: str,
        data_category: str,
        retention_class: str,
        retention_policy_version: str,
        enforced_by: str,
    ) -> RetentionEnforcementEvidence:
        target.validate()
        if retention_class not in CANONICAL_RETENTION_CLASSES:
            raise RetentionEnforcementError("UNKNOWN_RETENTION_CLASS")
        if retention_policy_version != RETENTION_POLICY_VERSION:
            raise RetentionEnforcementError("UNKNOWN_RETENTION_POLICY")
        if not retention_assignment_id or not enforced_by.strip():
            raise RetentionEnforcementError("MALFORMED_ENFORCEMENT_CONTEXT")

        metadata = self.storage.stat_exact(target)
        plan = plan_assignment(
            data_category,
            metadata.last_modified_utc,
            retention_policy_version=retention_policy_version,
        )
        if plan.retention_class != retention_class:
            raise RetentionEnforcementError("RETENTION_CLASS_POLICY_MISMATCH")

        current = self.storage.get_retention_exact(target)
        requested_until = plan.expected_retain_until_utc
        if current is not None:
            if current.mode != "COMPLIANCE":
                raise RetentionEnforcementError("RETENTION_MODE_MISMATCH")
            requested_until = max(requested_until, current.retain_until_utc)
        if current is None or current.retain_until_utc < requested_until:
            self.storage.set_retention_exact(target, requested_until)

        requested_hold = "ON" if plan.legal_hold_required else "UNCHANGED"
        if plan.legal_hold_required:
            self.legal_hold_mediator.apply_on(target)
        observed_hold = self.legal_hold_mediator.read_status(target)

        observed_metadata = self.storage.stat_exact(target)
        observed_retention = self.storage.get_retention_exact(target)
        if observed_retention is None:
            raise RetentionEnforcementError("RETENTION_READBACK_UNAVAILABLE")
        if observed_metadata.object_version_id != target.object_version_id:
            raise RetentionEnforcementError("EXACT_VERSION_READBACK_MISMATCH")
        if observed_metadata.last_modified_utc != plan.accepted_storage_at_utc:
            raise RetentionEnforcementError("STORAGE_TIMESTAMP_CHANGED")
        if observed_retention.mode != "COMPLIANCE":
            raise RetentionEnforcementError("RETENTION_MODE_MISMATCH")
        if observed_retention.retain_until_utc < requested_until:
            raise RetentionEnforcementError("RETAIN_UNTIL_MISMATCH")
        if plan.legal_hold_required and observed_hold != "ON":
            raise RetentionEnforcementError("LEGAL_HOLD_ON_MISMATCH")

        return RetentionEnforcementEvidence(
            retention_assignment_id=retention_assignment_id,
            bucket_name=target.bucket_name,
            object_key=target.object_key,
            object_kind=target.object_kind,
            object_version_id=target.object_version_id,
            data_category=plan.data_category,
            retention_class=plan.retention_class,
            retention_policy_version=plan.retention_policy_version,
            accepted_storage_at_utc=plan.accepted_storage_at_utc,
            requested_retention_mode="COMPLIANCE",
            requested_retain_until_utc=requested_until,
            requested_legal_hold_status=requested_hold,
            observed_object_version_id=observed_metadata.object_version_id,
            observed_retention_mode=observed_retention.mode,
            observed_retain_until_utc=observed_retention.retain_until_utc,
            observed_legal_hold_status=observed_hold,
            enforcement_verified_at_utc=normalize_storage_timestamp(self.clock()),
            enforcement_verification_result="SUCCESS",
            failure_code=None,
            enforced_by=enforced_by,
            details_json={"exact_version_readback": True},
        )
