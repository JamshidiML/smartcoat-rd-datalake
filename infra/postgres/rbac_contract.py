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

PUBLIC_TABLES = (
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

TABLE_PRIVILEGES = frozenset(
    {
        # The API read side uses the ingestion connection.  Read visibility is
        # intentionally broader than write authority in this single-user slice.
        *(("smartcoat_ingestion", table, "SELECT") for table in PUBLIC_TABLES),
        *(("smartcoat_backup", table, "SELECT") for table in PUBLIC_TABLES),
        *(("smartcoat_ingestion", table, "INSERT") for table in (
            "users", "uploads", "bronze_objects", "ocr_jobs", "audit_events"
        )),
        *(("smartcoat_ocr", table, "SELECT") for table in (
            "uploads", "ocr_jobs", "ocr_runs"
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
    }
)

MIGRATION_METADATA_PRIVILEGES = frozenset(
    {
        ("smartcoat_backup", "applied_migrations", "SELECT"),
        ("smartcoat_backup", "adoption_decisions", "SELECT"),
    }
)

PROTECTED_APPEND_ONLY_TABLES = (
    "bronze_objects",
    "silver_verified_records",
    "review_decisions",
    "audit_events",
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
