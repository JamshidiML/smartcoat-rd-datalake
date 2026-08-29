#!/usr/bin/env python3
"""Candidate Legacy Bronze 365-day reconciliation.

Dry-run inventories every PostgreSQL Bronze row and every exact MinIO version
without mutation. Apply mode is explicitly authorized, uses a dedicated MinIO
identity plus an explicit operator database URL, and records only append-only
reconciliation evidence. It never updates Bronze rows or object content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
API_SOURCE = ROOT / "apps/api/src"
if str(API_SOURCE) not in sys.path:
    sys.path.insert(0, str(API_SOURCE))

from retention_enforcement import (  # noqa: E402
    ExactVersionTarget,
    HttpLegalHoldMediator,
    MinioExactVersionRetentionStorage,
    RetentionEnforcementError,
)


POLICY_VERSION = "candidate_legacy_bronze_365d_v1"
CONFIRM_FLAG = "--confirm-legacy-365-day-reconciliation"
APPROVED_BUCKETS = (
    "sc-rd-bronze-originals",
    "sc-rd-bronze-manifests",
)
EXPECTED_KIND = {
    "sc-rd-bronze-originals": "ORIGINAL",
    "sc-rd-bronze-manifests": "MANIFEST",
}
RECONCILE_CANDIDATE = "RECONCILE_CANDIDATE"
RECONCILED = "RECONCILED"
QUARANTINED = "QUARANTINED"


class LegacyReconciliationError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def add_calendar_years(value: datetime, years: int) -> datetime:
    value = value.astimezone(UTC)
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, month=2, day=28)


@dataclass(frozen=True)
class DatabaseBronzeRow:
    bronze_object_id: str
    ingestion_id: str
    bucket_name: str
    object_key: str
    object_kind: str
    sha256: str
    object_version_id: str | None

    @property
    def identity(self) -> str:
        return canonical_sha256({
            "entity": "DATABASE_ROW",
            "bronze_object_id": self.bronze_object_id,
        })


@dataclass(frozen=True)
class StorageVersion:
    bucket_name: str
    object_key: str
    object_version_id: str
    sha256: str | None
    last_modified_utc: datetime | None
    is_delete_marker: bool = False

    @property
    def entity_type(self) -> str:
        return "DELETE_MARKER" if self.is_delete_marker else "STORAGE_VERSION"

    @property
    def identity(self) -> str:
        return canonical_sha256({
            "entity": self.entity_type,
            "bucket": self.bucket_name,
            "key": self.object_key,
            "version_id": self.object_version_id,
        })


@dataclass(frozen=True)
class MappingCandidate:
    row: DatabaseBronzeRow
    version: StorageVersion
    matching_basis: str

    @property
    def identity(self) -> str:
        return canonical_sha256({
            "bronze_object_id": self.row.bronze_object_id,
            "bucket": self.version.bucket_name,
            "key": self.version.object_key,
            "version_id": self.version.object_version_id,
            "sha256": self.version.sha256,
        })


@dataclass(frozen=True)
class OutcomeItem:
    entity_type: str
    entity_identity_sha256: str
    bronze_object_id: str | None
    ingestion_id: str | None
    bucket_name: str | None
    object_key: str | None
    object_kind: str | None
    object_version_id: str | None
    content_sha256: str | None
    mapping_identity_sha256: str | None
    matching_basis: str | None
    outcome: str
    classification: str
    prior_retention_mode: str | None = None
    prior_retain_until_utc: datetime | None = None
    prior_legal_hold_status: str | None = None
    requested_retention_mode: str | None = None
    requested_retain_until_utc: datetime | None = None
    requested_legal_hold_status: str | None = None
    observed_retention_mode: str | None = None
    observed_retain_until_utc: datetime | None = None
    observed_legal_hold_status: str | None = None
    details_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class InventoryPlan:
    rows: tuple[DatabaseBronzeRow, ...]
    versions: tuple[StorageVersion, ...]
    mappings: tuple[MappingCandidate, ...]
    items: tuple[OutcomeItem, ...]
    inventory_sha256: str

    def counts(self) -> dict[str, int]:
        classifications: dict[str, int] = {}
        for item in self.items:
            classifications[item.classification] = classifications.get(item.classification, 0) + 1
        return {
            "database_rows": len(self.rows),
            "original_versions": sum(
                1 for value in self.versions
                if not value.is_delete_marker and value.bucket_name.endswith("originals")
            ),
            "manifest_versions": sum(
                1 for value in self.versions
                if not value.is_delete_marker and value.bucket_name.endswith("manifests")
            ),
            "delete_markers": sum(value.is_delete_marker for value in self.versions),
            "exact_reconciliations": len(self.mappings),
            "null_version_candidates": sum(
                mapping.row.object_version_id is None for mapping in self.mappings
            ),
            "ambiguous_matches": classifications.get("AMBIGUOUS_VERSION_MATCH", 0),
            "hash_mismatches": classifications.get("HASH_MISMATCH", 0),
            "storage_orphans": classifications.get("ORPHAN_STORAGE_VERSION", 0),
            "database_orphans": classifications.get("ORPHAN_DATABASE_ROW", 0),
            "contradictory_metadata": classifications.get("CONTRADICTORY_METADATA", 0),
            "enforcement_failures": classifications.get("ENFORCEMENT_FAILURE", 0),
            "unresolved_or_quarantined": sum(
                item.outcome == QUARANTINED for item in self.items
            ),
        }


def row_item(
    row: DatabaseBronzeRow,
    outcome: str,
    classification: str,
    mapping: MappingCandidate | None = None,
) -> OutcomeItem:
    return OutcomeItem(
        entity_type="DATABASE_ROW",
        entity_identity_sha256=row.identity,
        bronze_object_id=row.bronze_object_id,
        ingestion_id=row.ingestion_id,
        bucket_name=row.bucket_name,
        object_key=row.object_key,
        object_kind=row.object_kind,
        object_version_id=row.object_version_id,
        content_sha256=row.sha256,
        mapping_identity_sha256=mapping.identity if mapping else None,
        matching_basis=mapping.matching_basis if mapping else None,
        outcome=outcome,
        classification=classification,
        details_json={},
    )


def version_item(
    version: StorageVersion,
    outcome: str,
    classification: str,
    mapping: MappingCandidate | None = None,
) -> OutcomeItem:
    return OutcomeItem(
        entity_type=version.entity_type,
        entity_identity_sha256=version.identity,
        bronze_object_id=mapping.row.bronze_object_id if mapping else None,
        ingestion_id=mapping.row.ingestion_id if mapping else None,
        bucket_name=version.bucket_name,
        object_key=version.object_key,
        object_kind=EXPECTED_KIND[version.bucket_name],
        object_version_id=version.object_version_id,
        content_sha256=version.sha256,
        mapping_identity_sha256=mapping.identity if mapping else None,
        matching_basis=mapping.matching_basis if mapping else None,
        outcome=outcome,
        classification=classification,
        details_json={"delete_marker_retained": version.is_delete_marker},
    )


def plan_inventory(
    rows: Iterable[DatabaseBronzeRow],
    versions: Iterable[StorageVersion],
) -> InventoryPlan:
    ordered_rows = tuple(sorted(rows, key=lambda row: row.bronze_object_id))
    ordered_versions = tuple(sorted(
        versions,
        key=lambda value: (
            value.bucket_name, value.object_key, value.object_version_id,
            value.is_delete_marker,
        ),
    ))
    exact = {
        (value.bucket_name, value.object_key, value.object_version_id): value
        for value in ordered_versions if not value.is_delete_marker
    }
    by_key: dict[tuple[str, str], list[StorageVersion]] = {}
    for value in ordered_versions:
        if not value.is_delete_marker:
            by_key.setdefault((value.bucket_name, value.object_key), []).append(value)

    consumed: set[str] = set()
    items: list[OutcomeItem] = []
    mappings: list[MappingCandidate] = []
    for row in ordered_rows:
        if (
            row.bucket_name not in EXPECTED_KIND
            or EXPECTED_KIND[row.bucket_name] != row.object_kind
        ):
            items.append(row_item(row, QUARANTINED, "CONTRADICTORY_METADATA"))
            continue

        candidate: StorageVersion | None = None
        basis = ""
        if row.object_version_id:
            candidate = exact.get((row.bucket_name, row.object_key, row.object_version_id))
            basis = "EXISTING_EXACT_VERSION_ID"
            if candidate is None:
                items.append(row_item(row, QUARANTINED, "ORPHAN_DATABASE_ROW"))
                continue
            consumed.add(candidate.identity)
            if candidate.sha256 != row.sha256:
                items.append(row_item(row, QUARANTINED, "HASH_MISMATCH"))
                items.append(version_item(candidate, QUARANTINED, "HASH_MISMATCH"))
                continue
        else:
            candidates = by_key.get((row.bucket_name, row.object_key), [])
            matches = [value for value in candidates if value.sha256 == row.sha256]
            if len(matches) > 1:
                items.append(row_item(row, QUARANTINED, "AMBIGUOUS_VERSION_MATCH"))
                for value in matches:
                    consumed.add(value.identity)
                    items.append(version_item(value, QUARANTINED, "AMBIGUOUS_VERSION_MATCH"))
                continue
            if len(matches) == 0:
                items.append(row_item(
                    row,
                    QUARANTINED,
                    "HASH_MISMATCH" if candidates else "ORPHAN_DATABASE_ROW",
                ))
                continue
            candidate = matches[0]
            consumed.add(candidate.identity)
            basis = (
                "NULL_VERSION_SINGLE_HASH_MATCH"
                if len(candidates) == 1
                else "NULL_VERSION_UNIQUE_HASH_MATCH_AMONG_VERSIONS"
            )

        mapping = MappingCandidate(row, candidate, basis)
        mappings.append(mapping)
        items.append(row_item(row, RECONCILE_CANDIDATE, basis, mapping))
        items.append(version_item(candidate, RECONCILE_CANDIDATE, basis, mapping))

    for value in ordered_versions:
        if value.is_delete_marker:
            items.append(version_item(value, RECONCILED, "DELETE_MARKER_RETAINED"))
        elif value.identity not in consumed:
            items.append(version_item(value, QUARANTINED, "ORPHAN_STORAGE_VERSION"))

    identities = [item.entity_identity_sha256 for item in items]
    if len(identities) != len(set(identities)):
        raise LegacyReconciliationError("inventory entity was accounted more than once")
    expected = len(ordered_rows) + len(ordered_versions)
    if len(items) != expected:
        raise LegacyReconciliationError("inventory entity accounting is incomplete")
    inventory_sha256 = canonical_sha256({
        "database_rows": [asdict(row) for row in ordered_rows],
        "storage_versions": [asdict(value) for value in ordered_versions],
    })
    return InventoryPlan(
        ordered_rows, ordered_versions, tuple(mappings), tuple(items), inventory_sha256
    )


class InventoryAdapters:
    def __init__(self, database_url: str, minio_client: Any) -> None:
        self.database_url = database_url
        self.minio_client = minio_client

    def database_rows(self) -> tuple[DatabaseBronzeRow, ...]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            values = connection.execute(
                """
                SELECT bronze_object_id::text, ingestion_id::text, bucket_name,
                    object_key, object_kind, sha256, object_version_id
                FROM bronze_objects
                WHERE bucket_name = ANY(%s)
                ORDER BY bronze_object_id
                """,
                (list(APPROVED_BUCKETS),),
            ).fetchall()
        return tuple(DatabaseBronzeRow(**dict(value)) for value in values)

    def storage_versions(self) -> tuple[StorageVersion, ...]:
        result: list[StorageVersion] = []
        for bucket in APPROVED_BUCKETS:
            for value in self.minio_client.list_objects(
                bucket, prefix="", recursive=True, include_version=True,
            ):
                version_id = getattr(value, "version_id", None)
                if not isinstance(version_id, str) or not version_id:
                    raise LegacyReconciliationError("storage inventory lacks exact version identity")
                is_delete_marker = bool(getattr(value, "is_delete_marker", False))
                digest = None
                last_modified = getattr(value, "last_modified", None)
                if not is_delete_marker:
                    response = self.minio_client.get_object(
                        bucket, value.object_name, version_id=version_id,
                    )
                    try:
                        body = response.read()
                    finally:
                        response.close()
                        response.release_conn()
                    digest = hashlib.sha256(body).hexdigest()
                result.append(StorageVersion(
                    bucket_name=bucket,
                    object_key=value.object_name,
                    object_version_id=version_id,
                    sha256=digest,
                    last_modified_utc=(
                        last_modified.astimezone(UTC) if last_modified else None
                    ),
                    is_delete_marker=is_delete_marker,
                ))
        return tuple(result)


class EvidenceRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def existing_run(self, inventory_sha256: str) -> dict[str, Any] | None:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            value = connection.execute(
                """
                SELECT reconciliation_run_id::text, outcome_sha256, summary_json
                FROM legacy_reconciliation_runs
                WHERE inventory_sha256=%s AND remediation_policy_version=%s
                """,
                (inventory_sha256, POLICY_VERSION),
            ).fetchone()
            return dict(value) if value else None

    def existing_successes(self) -> dict[str, dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT * FROM legacy_reconciliation_successes"
            ).fetchall()
        return {str(row["mapping_identity_sha256"]): dict(row) for row in rows}

    def existing_mapping_outcomes(self, run_id: str) -> dict[str, tuple[str, ...]]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT mapping_identity_sha256, classification
                FROM legacy_reconciliation_items
                WHERE reconciliation_run_id=%s
                  AND mapping_identity_sha256 IS NOT NULL
                ORDER BY mapping_identity_sha256, entity_type
                """,
                (run_id,),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(str(row["mapping_identity_sha256"]), []).append(
                str(row["classification"])
            )
        return {identity: tuple(values) for identity, values in grouped.items()}

    def persist(
        self,
        *,
        run_id: str,
        plan: InventoryPlan,
        items: list[OutcomeItem],
        successes: list[dict[str, Any]],
        started_at: datetime,
        completed_at: datetime,
        summary: dict[str, Any],
    ) -> None:
        import psycopg

        outcome_sha256 = canonical_sha256([
            {
                "entity": item.entity_identity_sha256,
                "outcome": item.outcome,
                "classification": item.classification,
                "mapping": item.mapping_identity_sha256,
            }
            for item in sorted(items, key=lambda value: value.entity_identity_sha256)
        ])
        attempt_identity = canonical_sha256({
            "run_id": run_id,
            "inventory_sha256": plan.inventory_sha256,
            "outcome_sha256": outcome_sha256,
        })
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """
                INSERT INTO legacy_reconciliation_runs (
                    reconciliation_run_id, inventory_sha256, outcome_sha256,
                    remediation_policy_version, started_at_utc, completed_at_utc,
                    database_row_count, storage_version_count, delete_marker_count,
                    reconciled_entity_count, quarantined_entity_count, summary_json,
                    executed_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    run_id, plan.inventory_sha256, outcome_sha256, POLICY_VERSION,
                    started_at, completed_at, len(plan.rows),
                    sum(not value.is_delete_marker for value in plan.versions),
                    sum(value.is_delete_marker for value in plan.versions),
                    sum(item.outcome == RECONCILED for item in items),
                    sum(item.outcome == QUARANTINED for item in items),
                    json.dumps(summary, sort_keys=True), "legacy-reconciliation-operator",
                ),
            )
            for item in items:
                connection.execute(
                    """
                    INSERT INTO legacy_reconciliation_items (
                        reconciliation_item_id, reconciliation_run_id, entity_type,
                        entity_identity_sha256, bronze_object_id, ingestion_id,
                        bucket_name, object_key, object_kind, object_version_id,
                        content_sha256, mapping_identity_sha256, matching_basis,
                        prior_retention_mode, prior_retain_until_utc,
                        prior_legal_hold_status, requested_retention_mode,
                        requested_retain_until_utc, requested_legal_hold_status,
                        observed_retention_mode, observed_retain_until_utc,
                        observed_legal_hold_status, outcome, classification,
                        attempt_identity_sha256, recorded_at_utc, details_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    )
                    """,
                    (
                        str(uuid.uuid4()), run_id, item.entity_type,
                        item.entity_identity_sha256, item.bronze_object_id,
                        item.ingestion_id, item.bucket_name, item.object_key,
                        item.object_kind, item.object_version_id, item.content_sha256,
                        item.mapping_identity_sha256, item.matching_basis,
                        item.prior_retention_mode, item.prior_retain_until_utc,
                        item.prior_legal_hold_status, item.requested_retention_mode,
                        item.requested_retain_until_utc,
                        item.requested_legal_hold_status,
                        item.observed_retention_mode,
                        item.observed_retain_until_utc,
                        item.observed_legal_hold_status, item.outcome,
                        item.classification, attempt_identity, completed_at,
                        json.dumps(item.details_json or {}, sort_keys=True),
                    ),
                )
            for success in successes:
                connection.execute(
                    """
                    INSERT INTO legacy_reconciliation_successes (
                        mapping_identity_sha256, reconciliation_run_id,
                        bronze_object_id, ingestion_id, bucket_name, object_key,
                        object_kind, object_version_id, content_sha256,
                        matching_basis, retention_class, remediation_policy_version,
                        requested_retention_mode, requested_retain_until_utc,
                        requested_legal_hold_status, observed_retention_mode,
                        observed_retain_until_utc, observed_legal_hold_status,
                        reconciled_at_utc, details_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'permanent',%s,
                        'COMPLIANCE',%s,'ON','COMPLIANCE',%s,'ON',%s,%s::jsonb
                    ) ON CONFLICT (mapping_identity_sha256) DO NOTHING
                    """,
                    (
                        success["mapping_identity_sha256"], run_id,
                        success["bronze_object_id"], success["ingestion_id"],
                        success["bucket_name"], success["object_key"],
                        success["object_kind"], success["object_version_id"],
                        success["content_sha256"], success["matching_basis"],
                        POLICY_VERSION, success["requested_retain_until_utc"],
                        success["observed_retain_until_utc"], completed_at,
                        json.dumps({"exact_version_readback": True}, sort_keys=True),
                    ),
                )


def enforce_mapping(
    mapping: MappingCandidate,
    storage: MinioExactVersionRetentionStorage,
    mediator: HttpLegalHoldMediator,
    remediation_time: datetime,
    existing_success: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = ExactVersionTarget(
        mapping.version.bucket_name,
        mapping.version.object_key,
        mapping.version.object_version_id,
        mapping.row.object_kind,
    )
    target.validate()
    metadata = storage.stat_exact(target)
    before_retention = storage.get_retention_exact(target)
    before_hold = mediator.read_status(target)
    if before_retention is not None and before_retention.mode != "COMPLIANCE":
        raise RetentionEnforcementError("RETENTION_MODE_MISMATCH")
    requested_until = (
        existing_success["requested_retain_until_utc"]
        if existing_success
        else add_calendar_years(remediation_time, 10)
    )
    if before_retention is not None:
        requested_until = max(requested_until, before_retention.retain_until_utc)
    if before_retention is None or before_retention.retain_until_utc < requested_until:
        storage.set_retention_exact(target, requested_until)
    mediator.apply_on(target)
    observed_metadata = storage.stat_exact(target)
    observed_retention = storage.get_retention_exact(target)
    observed_hold = mediator.read_status(target)
    if (
        observed_metadata.object_version_id != target.object_version_id
        or observed_retention is None
        or observed_retention.mode != "COMPLIANCE"
        or observed_retention.retain_until_utc < requested_until
        or observed_hold != "ON"
    ):
        raise RetentionEnforcementError("LEGACY_EXACT_VERSION_READBACK_MISMATCH")
    evidence = {
        "mapping_identity_sha256": mapping.identity,
        "bronze_object_id": mapping.row.bronze_object_id,
        "ingestion_id": mapping.row.ingestion_id,
        "bucket_name": mapping.version.bucket_name,
        "object_key": mapping.version.object_key,
        "object_kind": mapping.row.object_kind,
        "object_version_id": mapping.version.object_version_id,
        "content_sha256": mapping.version.sha256,
        "matching_basis": mapping.matching_basis,
        "requested_retain_until_utc": requested_until,
        "observed_retain_until_utc": observed_retention.retain_until_utc,
    }
    observations = {
        "prior_retention_mode": before_retention.mode if before_retention else None,
        "prior_retain_until_utc": (
            before_retention.retain_until_utc if before_retention else None
        ),
        "prior_legal_hold_status": before_hold,
        "requested_retention_mode": "COMPLIANCE",
        "requested_retain_until_utc": requested_until,
        "requested_legal_hold_status": "ON",
        "observed_retention_mode": observed_retention.mode,
        "observed_retain_until_utc": observed_retention.retain_until_utc,
        "observed_legal_hold_status": observed_hold,
        "details_json": {
            "storage_last_modified_utc": metadata.last_modified_utc.isoformat(),
            "existing_success_reused": existing_success is not None,
        },
    }
    return evidence, observations


def apply_plan(
    plan: InventoryPlan,
    repository: EvidenceRepository,
    storage: MinioExactVersionRetentionStorage,
    mediator: HttpLegalHoldMediator,
    clock: datetime,
) -> dict[str, Any]:
    clock = clock.astimezone(UTC).replace(microsecond=0)
    existing_run = repository.existing_run(plan.inventory_sha256)
    successes_by_mapping = repository.existing_successes()
    existing_mapping_outcomes: dict[str, tuple[str, ...]] = {}
    if existing_run:
        existing_mapping_outcomes = repository.existing_mapping_outcomes(
            existing_run["reconciliation_run_id"]
        )
        missing = sorted(
            mapping.identity
            for mapping in plan.mappings
            if mapping.identity not in successes_by_mapping
            and existing_mapping_outcomes.get(mapping.identity)
            != ("ENFORCEMENT_FAILURE", "ENFORCEMENT_FAILURE")
        )
        if missing:
            raise LegacyReconciliationError(
                "durable run is missing exact-version success evidence"
            )
    final_items = list(plan.items)
    success_rows: list[dict[str, Any]] = []
    for mapping in plan.mappings:
        indexes = [
            index for index, item in enumerate(final_items)
            if item.mapping_identity_sha256 == mapping.identity
        ]
        if (
            existing_run
            and mapping.identity not in successes_by_mapping
            and existing_mapping_outcomes.get(mapping.identity)
            == ("ENFORCEMENT_FAILURE", "ENFORCEMENT_FAILURE")
        ):
            for index in indexes:
                final_items[index] = replace(
                    final_items[index],
                    outcome=QUARANTINED,
                    classification="ENFORCEMENT_FAILURE",
                    details_json={"existing_failure_outcome_reused": True},
                )
            continue
        try:
            success, observations = enforce_mapping(
                mapping, storage, mediator, clock,
                successes_by_mapping.get(mapping.identity),
            )
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            for index in indexes:
                final_items[index] = replace(
                    final_items[index],
                    outcome=QUARANTINED,
                    classification="ENFORCEMENT_FAILURE",
                    details_json={"failure_code": str(code)[:200]},
                )
        else:
            success_rows.append(success)
            for index in indexes:
                final_items[index] = replace(
                    final_items[index],
                    outcome=RECONCILED,
                    classification=mapping.matching_basis,
                    **observations,
                )
    if any(item.outcome == RECONCILE_CANDIDATE for item in final_items):
        raise LegacyReconciliationError("candidate outcome survived apply")
    summary = plan.counts()
    summary.update({
        "reconciled_entities": sum(item.outcome == RECONCILED for item in final_items),
        "quarantined_entities": sum(item.outcome == QUARANTINED for item in final_items),
        "enforcement_failures": sum(
            item.classification == "ENFORCEMENT_FAILURE" for item in final_items
        ),
        "accounted_entities": len(final_items),
        "expected_entities": len(plan.rows) + len(plan.versions),
    })
    if existing_run:
        if summary != existing_run["summary_json"]:
            raise LegacyReconciliationError(
                "repeated inventory produced a different final classification"
            )
        return {
            "mode": "apply",
            "classification": "PASS_LEGACY_RECONCILED_OR_QUARANTINED",
            "reused_run": True,
            "reconciliation_run_id": existing_run["reconciliation_run_id"],
            "inventory_sha256": plan.inventory_sha256,
            "summary": summary,
        }
    completed = datetime.now(UTC)
    run_id = str(uuid.uuid4())
    repository.persist(
        run_id=run_id,
        plan=plan,
        items=final_items,
        successes=success_rows,
        started_at=clock,
        completed_at=completed,
        summary=summary,
    )
    return {
        "mode": "apply",
        "classification": "PASS_LEGACY_RECONCILED_OR_QUARANTINED",
        "reused_run": False,
        "reconciliation_run_id": run_id,
        "inventory_sha256": plan.inventory_sha256,
        "summary": summary,
    }


def require_environment(name: str, minimum: int = 1) -> str:
    value = os.environ.get(name, "")
    if len(value) < minimum or "\n" in value or "\x00" in value:
        raise LegacyReconciliationError(f"{name} is required")
    return value


def build_adapters() -> tuple[InventoryAdapters, EvidenceRepository, Any]:
    from minio import Minio

    database_url = require_environment("LEGACY_RECONCILIATION_DATABASE_URL")
    client = Minio(
        require_environment("MINIO_ENDPOINT"),
        access_key=require_environment("MINIO_LEGACY_RECONCILIATION_ACCESS_KEY"),
        secret_key=require_environment("MINIO_LEGACY_RECONCILIATION_SECRET_KEY", 32),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )
    return InventoryAdapters(database_url, client), EvidenceRepository(database_url), client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not getattr(args, CONFIRM_FLAG[2:].replace("-", "_")):
        print(json.dumps({
            "classification": "BLOCKED_EXPLICIT_CONFIRMATION_REQUIRED",
            "mutated": False,
        }, sort_keys=True))
        return 2
    try:
        adapters, repository, client = build_adapters()
        plan = plan_inventory(adapters.database_rows(), adapters.storage_versions())
        if args.dry_run:
            print(json.dumps({
                "mode": "dry-run",
                "classification": "DRY_RUN_COMPLETE_NO_MUTATION",
                "inventory_sha256": plan.inventory_sha256,
                "counts": plan.counts(),
                "mutated": False,
            }, sort_keys=True))
            return 0
        storage = MinioExactVersionRetentionStorage(client)
        mediator = HttpLegalHoldMediator(
            require_environment("LEGAL_HOLD_APPLIER_URL"),
            require_environment("LEGAL_HOLD_APPLIER_CALL_TOKEN", 32),
        )
        result = apply_plan(plan, repository, storage, mediator, datetime.now(UTC))
        print(json.dumps(result, sort_keys=True, default=str))
        print("PASS_LEGACY_RECONCILED_OR_QUARANTINED")
        return 0
    except Exception as exc:
        print(json.dumps({
            "classification": "FAIL_LEGACY_RECONCILIATION",
            "reason": getattr(exc, "code", type(exc).__name__),
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
