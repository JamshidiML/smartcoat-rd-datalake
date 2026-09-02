"""Authoritative M0-R02 PostgreSQL runtime-role contract.

The migration owns role creation and grants.  The one-shot credential
provisioner and acceptance tests import this module so role names and the
permission matrix cannot drift independently.  No credential value belongs in
this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


LEGACY_SHARED_ROLE = "smartcoat_app"
ADMIN_DATABASE_ENV = "POSTGRES_ROLE_ADMIN_URL"


@dataclass(frozen=True)
class RuntimeRole:
    name: str
    password_environment: str
    workflow: str


@dataclass(frozen=True)
class PositivePathRequirement:
    """One database privilege required by a production repository path."""

    role: str
    code_path: str
    repository_method: str
    table: str
    privilege: str
    columns: tuple[str, ...] = ()


RUNTIME_ROLES: Mapping[str, RuntimeRole] = MappingProxyType(
    {
        "ingestion": RuntimeRole(
            "smartcoat_ingestion",
            "POSTGRES_INGESTION_PASSWORD",
            "upload, Bronze registration, OCR queueing, and read-side API",
        ),
        "ocr": RuntimeRole(
            "smartcoat_ocr",
            "POSTGRES_OCR_PASSWORD",
            "OCR job execution and unverified Silver draft creation",
        ),
        "review": RuntimeRole(
            "smartcoat_review",
            "POSTGRES_REVIEW_PASSWORD",
            "human review decisions and verified Silver revisions",
        ),
        "backup": RuntimeRole(
            "smartcoat_backup",
            "POSTGRES_BACKUP_PASSWORD",
            "read-only PostgreSQL backup",
        ),
    }
)

ROLE_NAMES = tuple(role.name for role in RUNTIME_ROLES.values())

CORE_PUBLIC_TABLES = (
    "users",
    "uploads",
    "bronze_objects",
    "ocr_jobs",
    "ocr_runs",
    "silver_drafts",
    "review_decisions",
    "silver_verified_records",
    "audit_events",
)

RETENTION_TABLES = (
    "canonical_retention_classes",
    "retention_policy_versions",
    "retention_category_rules",
    "bronze_retention_assignments",
    "bronze_retention_enforcement_evidence",
)

BRONZE_PAIR_TABLES = (
    "bronze_pairs",
    "bronze_protected_orphans",
    "bronze_reconciliation_events",
)

PUBLIC_TABLES = CORE_PUBLIC_TABLES + RETENTION_TABLES + BRONZE_PAIR_TABLES

TABLE_PRIVILEGES = frozenset(
    {
        # The API read side uses the ingestion connection.  Read visibility is
        # intentionally broader than write authority in this single-user slice.
        *(("smartcoat_ingestion", table, "SELECT") for table in CORE_PUBLIC_TABLES),
        *(("smartcoat_ingestion", table, "SELECT") for table in (
            "bronze_retention_assignments",
            "bronze_retention_enforcement_evidence",
        )),
        *(("smartcoat_ingestion", table, "SELECT") for table in BRONZE_PAIR_TABLES),
        *(("smartcoat_backup", table, "SELECT") for table in PUBLIC_TABLES),
        *(("smartcoat_ingestion", table, "INSERT") for table in (
            "users", "uploads", "bronze_objects", "ocr_jobs", "audit_events",
            "bronze_retention_assignments",
            "bronze_retention_enforcement_evidence",
        )),
        *(("smartcoat_ingestion", table, "INSERT") for table in BRONZE_PAIR_TABLES),
        *(("smartcoat_ocr", table, "SELECT") for table in (
            "uploads", "bronze_pairs", "ocr_jobs", "ocr_runs"
        )),
        *(("smartcoat_ocr", table, "INSERT") for table in (
            "ocr_runs", "silver_drafts", "audit_events"
        )),
        *(("smartcoat_review", table, "SELECT") for table in (
            "uploads", "ocr_runs", "silver_drafts", "review_decisions",
            "silver_verified_records"
        )),
        *(("smartcoat_review", table, "INSERT") for table in (
            "silver_drafts", "review_decisions", "silver_verified_records",
            "audit_events"
        )),
    }
)

COLUMN_UPDATE_PRIVILEGES = frozenset(
    {
        *(("smartcoat_ingestion", "users", column) for column in (
            "display_name", "email", "active"
        )),
        ("smartcoat_ingestion", "uploads", "state"),
        ("smartcoat_ocr", "uploads", "state"),
        *(("smartcoat_ocr", "ocr_jobs", column) for column in (
            "status", "started_at_utc", "completed_at_utc", "attempt_count",
            "error_reason"
        )),
        *(("smartcoat_ocr", "ocr_runs", column) for column in (
            "status", "raw_output_sha256", "raw_artifact_key", "completed_at_utc"
        )),
        ("smartcoat_review", "uploads", "state"),
        ("smartcoat_review", "silver_drafts", "status"),
    }
)

COLUMN_SELECT_PRIVILEGES = frozenset(
    {
        # M0-R04 authenticates an exact review retry by counting its two
        # append-only audit facts.  The review role needs no other audit
        # columns and retains no table-level SELECT authority.
        *(('smartcoat_review', 'audit_events', column) for column in (
            'entity_type', 'entity_id', 'event_type', 'details_json', 'new_state'
        )),
        *(('smartcoat_ocr', 'bronze_objects', column) for column in (
            'bronze_object_id', 'ingestion_id', 'object_kind',
            'object_version_id'
        )),
        *(('smartcoat_review', 'bronze_objects', column) for column in (
            'ingestion_id', 'object_kind', 'object_version_id'
        )),
    }
)


def _requirements(
    role: str,
    code_path: str,
    repository_method: str,
    *items: tuple[str, str] | tuple[str, str, tuple[str, ...]],
) -> tuple[PositivePathRequirement, ...]:
    return tuple(
        PositivePathRequirement(
            role,
            code_path,
            repository_method,
            item[0],
            item[1],
            item[2] if len(item) == 3 else (),
        )
        for item in items
    )


POSITIVE_PATH_REQUIREMENTS = (
    *_requirements(
        "smartcoat_ingestion", "api.shared_audit", "_audit",
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr.shared_audit", "_audit",
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_review", "review.shared_audit", "_audit",
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload", "_insert_retention_evidence",
        ("bronze_retention_assignments", "INSERT"),
        ("bronze_retention_enforcement_evidence", "INSERT"),
    ),
    # API startup, ingestion, retention, reconciliation, and read-side paths.
    *_requirements(
        "smartcoat_ingestion", "api.startup", "ensure_local_user",
        ("users", "INSERT"),
        ("users", "UPDATE", ("display_name", "email", "active")),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload", "record_rejection",
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload", "first_ingestion_by_sha256",
        ("uploads", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload", "create_received",
        ("uploads", "INSERT"), ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload", "commit_bronze_pair",
        ("bronze_pairs", "SELECT"), ("bronze_pairs", "INSERT"),
        ("bronze_objects", "INSERT"),
        ("bronze_retention_assignments", "INSERT"),
        ("bronze_retention_enforcement_evidence", "INSERT"),
        ("uploads", "UPDATE", ("state",)), ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload_failure", "record_protected_orphans",
        ("bronze_protected_orphans", "INSERT"), ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.reconcile_bronze",
        "bronze_reconciliation_context", ("uploads", "SELECT"),
        ("bronze_pairs", "SELECT"), ("bronze_protected_orphans", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.reconcile_bronze", "record_reconciliation",
        ("bronze_reconciliation_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.retention", "record_retention_enforcement",
        ("bronze_retention_assignments", "SELECT"),
        ("bronze_retention_assignments", "INSERT"),
        ("bronze_retention_enforcement_evidence", "SELECT"),
        ("bronze_retention_enforcement_evidence", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.upload_and_reconcile", "ensure_ocr_queued",
        ("uploads", "SELECT"), ("uploads", "UPDATE", ("state",)),
        ("ocr_jobs", "SELECT"), ("ocr_jobs", "INSERT"),
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.read_upload_and_source", "get_upload",
        ("uploads", "SELECT"), ("bronze_objects", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.list_uploads", "list_uploads",
        ("uploads", "SELECT"), ("ocr_jobs", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.list_drafts", "list_drafts",
        ("silver_drafts", "SELECT"), ("uploads", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.review_context", "get_draft",
        ("silver_drafts", "SELECT"), ("ocr_runs", "SELECT"),
    ),
    *_requirements(
        "smartcoat_ingestion", "api.audit", "audit_events",
        ("audit_events", "SELECT"),
    ),
    # OCR API retry and worker paths.
    *_requirements(
        "smartcoat_ocr", "api.retry_ocr", "retry_failed_ocr",
        ("uploads", "SELECT"), ("uploads", "UPDATE", ("state",)),
        ("ocr_jobs", "SELECT"),
        ("ocr_jobs", "UPDATE", ("status", "started_at_utc", "completed_at_utc", "error_reason")),
        ("bronze_pairs", "SELECT"),
        ("bronze_objects", "SELECT", ("bronze_object_id", "object_version_id")),
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.startup", "recover_interrupted_ocr_jobs",
        ("ocr_jobs", "SELECT"),
        ("ocr_jobs", "UPDATE", ("status", "started_at_utc", "completed_at_utc", "error_reason")),
        ("uploads", "SELECT"),
        ("ocr_runs", "UPDATE", ("status", "completed_at_utc")),
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.poll", "claim_next_job",
        ("ocr_jobs", "SELECT"), ("uploads", "SELECT"),
        ("bronze_pairs", "SELECT"),
        ("bronze_objects", "SELECT", ("bronze_object_id", "object_version_id")),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.domain_start_and_complete", "get_upload",
        ("uploads", "SELECT"),
        ("bronze_objects", "SELECT", ("ingestion_id", "object_kind", "object_version_id")),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.domain_start", "start_ocr_run",
        ("ocr_jobs", "SELECT"),
        ("ocr_jobs", "UPDATE", ("status", "started_at_utc", "attempt_count")),
        ("ocr_runs", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.domain_complete", "complete_ocr_run",
        ("ocr_runs", "UPDATE", ("status", "raw_output_sha256", "raw_artifact_key", "completed_at_utc")),
        ("ocr_jobs", "UPDATE", ("status", "completed_at_utc")),
        ("silver_drafts", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.domain_complete", "transition",
        ("uploads", "UPDATE", ("state",)), ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_ocr", "ocr_worker.failure", "mark_ocr_failed",
        ("uploads", "SELECT"), ("uploads", "UPDATE", ("state",)),
        ("ocr_jobs", "SELECT"),
        ("ocr_jobs", "UPDATE", ("status", "completed_at_utc", "error_reason")),
        ("ocr_runs", "UPDATE", ("status", "completed_at_utc")),
        ("bronze_pairs", "SELECT"),
        ("bronze_objects", "SELECT", ("bronze_object_id", "object_version_id")),
        ("audit_events", "INSERT"),
    ),
    # Human review and verified-revision paths.
    *_requirements(
        "smartcoat_review", "api.review_and_revision", "get_upload",
        ("uploads", "SELECT"),
        ("bronze_objects", "SELECT", ("ingestion_id", "object_kind", "object_version_id")),
    ),
    *_requirements(
        "smartcoat_review", "api.review", "get_draft",
        ("silver_drafts", "SELECT"), ("ocr_runs", "SELECT"),
    ),
    *_requirements(
        "smartcoat_review", "api.review", "complete_review",
        ("uploads", "SELECT"), ("silver_drafts", "SELECT"),
        ("ocr_runs", "SELECT"), ("review_decisions", "SELECT"),
        ("silver_verified_records", "SELECT"),
        ("audit_events", "SELECT", ("entity_type", "entity_id", "event_type", "details_json", "new_state")),
        ("review_decisions", "INSERT"),
        ("silver_verified_records", "INSERT"),
        ("silver_drafts", "UPDATE", ("status",)),
        ("uploads", "UPDATE", ("state",)), ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_review", "api.revision", "create_revision_draft",
        ("uploads", "SELECT"), ("silver_drafts", "SELECT"),
        ("ocr_runs", "SELECT"), ("silver_drafts", "INSERT"),
        ("audit_events", "INSERT"),
    ),
    *_requirements(
        "smartcoat_review", "api.revision", "max_silver_revision",
        ("silver_verified_records", "SELECT"),
    ),
    *_requirements(
        "smartcoat_review", "api.revision", "transition",
        ("uploads", "UPDATE", ("state",)), ("audit_events", "INSERT"),
    ),
    # pg_dump backup path.
    *(
        PositivePathRequirement(
            "smartcoat_backup", "restore_drill.backup", "pg_dump",
            table, "SELECT"
        )
        for table in PUBLIC_TABLES
    ),
    *_requirements(
        "smartcoat_backup", "restore_drill.backup", "pg_dump",
        ("applied_migrations", "SELECT"), ("adoption_decisions", "SELECT"),
    ),
)

MIGRATION_METADATA_PRIVILEGES = frozenset(
    {
        ("smartcoat_backup", "applied_migrations", "SELECT"),
        ("smartcoat_backup", "adoption_decisions", "SELECT"),
    }
)


def positive_requirement_is_granted(requirement: PositivePathRequirement) -> bool:
    """Return whether the exact runtime grant matrix satisfies a requirement."""

    table_grant = (requirement.role, requirement.table, requirement.privilege)
    if table_grant in TABLE_PRIVILEGES or table_grant in MIGRATION_METADATA_PRIVILEGES:
        return True
    if not requirement.columns:
        return False
    if requirement.privilege == "SELECT":
        column_grants = COLUMN_SELECT_PRIVILEGES
    elif requirement.privilege == "UPDATE":
        column_grants = COLUMN_UPDATE_PRIVILEGES
    else:
        return False
    return all(
        (requirement.role, requirement.table, column) in column_grants
        for column in requirement.columns
    )


def missing_positive_path_requirements() -> tuple[PositivePathRequirement, ...]:
    """Return required production paths not authorized by the grant contract."""

    return tuple(
        requirement
        for requirement in POSITIVE_PATH_REQUIREMENTS
        if not positive_requirement_is_granted(requirement)
    )

PROTECTED_APPEND_ONLY_TABLES = (
    "bronze_objects",
    "silver_verified_records",
    "review_decisions",
    "audit_events",
    *RETENTION_TABLES,
    *BRONZE_PAIR_TABLES,
)

ROLE_ATTRIBUTE_QUERY = """
    SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
           rolcanlogin, rolreplication, rolbypassrls
    FROM pg_roles
    WHERE rolname = ANY(%s)
    ORDER BY rolname
"""

ROLE_MEMBERSHIP_QUERY = """
    SELECT granted.rolname AS granted_role, member.rolname AS member_role
    FROM pg_auth_members AS membership
    JOIN pg_roles AS granted ON granted.oid = membership.roleid
    JOIN pg_roles AS member ON member.oid = membership.member
    WHERE granted.rolname = ANY(%s) OR member.rolname = ANY(%s)
    ORDER BY granted.rolname, member.rolname
"""


def expected_role_attributes() -> tuple[tuple[object, ...], ...]:
    """Return the exact non-administrative attributes for every runtime role."""

    return tuple(
        (name, False, True, False, False, True, False, False)
        for name in sorted(ROLE_NAMES)
    )


def password_values(environment: Mapping[str, str]) -> dict[str, str]:
    """Validate and return role-password values without logging them."""

    values: dict[str, str] = {}
    for role in RUNTIME_ROLES.values():
        value = environment.get(role.password_environment, "")
        if len(value) < 32 or "\n" in value or "\x00" in value:
            raise ValueError(
                f"{role.password_environment} must be a single-line value of at least 32 characters"
            )
        values[role.name] = value
    if len(set(values.values())) != len(values):
        raise ValueError("PostgreSQL runtime-role passwords must be distinct")
    return values
