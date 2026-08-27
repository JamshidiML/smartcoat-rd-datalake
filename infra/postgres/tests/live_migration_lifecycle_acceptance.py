#!/usr/bin/env python3
"""Opt-in M0-R01.4.1 acceptance against disposable synthetic PostgreSQL.

This module is intentionally not named ``test_*.py``. It runs only through the
deliberate command below and never reads the repository's real ``.env`` file::

    python3 infra/postgres/tests/live_migration_lifecycle_acceptance.py \
        --confirm-disposable-synthetic-run
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = ROOT / "compose.yaml"
POSTGRES_IMAGE_REF = "postgres:17.6-alpine"
MIGRATION_IMAGE_DISCOVERY_REF = "smartcoat-rd-datalake-api:latest"
MIGRATION_MOUNT_TARGET = "/opt/smartcoat-postgres/migrations"
BASELINE_MIGRATION_SOURCE = (
    ROOT
    / "infra/postgres/migrations/0001__validate_bootstrap_prerequisites.sql"
)
PENDING_MIGRATION_FILENAME = "0002__live_acceptance_commit_probe.sql"
PENDING_MIGRATION_NAME = "live_acceptance_commit_probe"
PROBE_SCHEMA = "m0r0141_acceptance_probe"
PROBE_TABLE = "commit_probe"
PENDING_MIGRATION_CONTENT = b"""CREATE SCHEMA m0r0141_acceptance_probe;

CREATE TABLE m0r0141_acceptance_probe.commit_probe (
    probe_id text PRIMARY KEY,
    probe_value text NOT NULL,
    observed_at_utc timestamptz NOT NULL
);

INSERT INTO m0r0141_acceptance_probe.commit_probe (
    probe_id,
    probe_value,
    observed_at_utc
) VALUES (
    'm0-r01-4-1-commit-probe',
    'synthetic-pending-migration-applied',
    TIMESTAMPTZ '2026-01-01T00:00:00Z'
);
"""
RESULT_PASS = "PASS_M0_R01_4_1"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"

PROJECT_PATTERN = re.compile(r"^m0r0141-[0-9a-f]{12}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
EXPECTED_BASELINE_VERSION = 1
EXPECTED_BASELINE_NAME = "validate_bootstrap_prerequisites"
EXPECTED_BASELINE_SHA256 = (
    "7f34c9aba3819a49a5bb6c83f75bceaf436009d36c1c62eb46d0ddfa425529e5"
)
EXPECTED_INIT_SQL_SHA256 = (
    "9733855250e800c9a1e44f90d72711b8af49d8c940e89c6751020c1c5292e272"
)
EXPECTED_COMPOSE_SHA256 = (
    "464902d8c47a82feaee43e3c2611141f800cab32c3501dda406d4763a044e5f1"
)
EXPECTED_STRUCTURAL_FINGERPRINT = (
    "0e0bf94276807c20ba3f961346ac7f5ba0c5f92b54cc20aebd2e2f5ba7839f7c"
)
EXPECTED_CONTRACT_VERSION = "smartcoat-bootstrap-v1"
EXPECTED_ADOPTION_ACTION = "ADOPT_BOOTSTRAP_BASELINE"
EXPECTED_ADOPTION_AUTHORIZATION = (
    "explicit adopt command with matching database identity"
)
EXPECTED_PUBLIC_TABLES = {
    "audit_events",
    "bronze_objects",
    "ocr_jobs",
    "ocr_runs",
    "review_decisions",
    "silver_drafts",
    "silver_verified_records",
    "uploads",
    "users",
}
EXPECTED_PUBLIC_TRIGGERS = {
    ("audit_events", "audit_events_append_only"),
    ("bronze_objects", "bronze_objects_append_only"),
    ("review_decisions", "review_decisions_append_only"),
    ("silver_verified_records", "verified_records_append_only"),
}
EXPECTED_METADATA_COLUMNS = {
    "applied_migrations": [
        "version",
        "name",
        "sha256",
        "applied_at_utc",
        "applied_by",
    ],
    "adoption_decisions": [
        "action_identifier",
        "database_name",
        "database_oid",
        "migration_actor",
        "database_server_version",
        "adopted_at_utc",
        "expected_structural_fingerprint",
        "observed_structural_fingerprint",
        "init_sql_sha256",
        "baseline_version",
        "baseline_name",
        "baseline_sha256",
        "contract_version",
        "compared_categories_json",
        "authorization_statement",
    ],
}
EXPECTED_COMPARED_CATEGORIES = [
    {"category": "check_constraints", "expected_rows": 28},
    {"category": "column_privileges", "expected_rows": 14},
    {"category": "columns", "expected_rows": 101},
    {"category": "enums", "expected_rows": 0},
    {"category": "indexes", "expected_rows": 2},
    {"category": "key_constraints", "expected_rows": 30},
    {"category": "role_memberships", "expected_rows": 0},
    {"category": "roles", "expected_rows": 1},
    {"category": "schema_privileges", "expected_rows": 2},
    {"category": "schemas", "expected_rows": 1},
    {"category": "table_privileges", "expected_rows": 18},
    {"category": "tables", "expected_rows": 9},
    {"category": "trigger_functions", "expected_rows": 1},
    {"category": "triggers", "expected_rows": 4},
]

PUBLIC_CATALOG_QUERIES = {
    "tables": """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               c.relkind::text AS relation_kind,
               c.relpersistence::text AS persistence
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        ORDER BY n.nspname, c.relname
    """,
    "columns": """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               a.attnum AS ordinal_position, a.attname AS column_name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null,
               COALESCE(pg_get_expr(d.adbin, d.adrelid, false), '') AS default_expression
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef AS d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY n.nspname, c.relname, a.attnum
    """,
    "constraints": """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               con.conname AS constraint_name, con.contype::text AS constraint_type,
               pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
        ORDER BY n.nspname, c.relname, con.conname
    """,
    "indexes": """
        SELECT n.nspname AS schema_name, table_c.relname AS table_name,
               index_c.relname AS index_name,
               pg_get_indexdef(index_c.oid, 0, false) AS definition
        FROM pg_index AS i
        JOIN pg_class AS table_c ON table_c.oid = i.indrelid
        JOIN pg_class AS index_c ON index_c.oid = i.indexrelid
        JOIN pg_namespace AS n ON n.oid = table_c.relnamespace
        WHERE n.nspname = 'public'
        ORDER BY n.nspname, table_c.relname, index_c.relname
    """,
    "triggers": """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               t.tgname AS trigger_name, t.tgenabled AS enabled,
               pg_get_triggerdef(t.oid, false) AS definition,
               function_n.nspname AS function_schema,
               p.proname AS function_name
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_namespace AS function_n ON function_n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND NOT t.tgisinternal
        ORDER BY n.nspname, c.relname, t.tgname
    """,
    "trigger_functions": """
        SELECT n.nspname AS schema_name, p.proname AS function_name,
               pg_get_function_result(p.oid) AS result_type,
               l.lanname AS language_name, p.provolatile AS volatility,
               p.prosecdef AS security_definer, p.prosrc AS source
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE n.nspname = 'public'
          AND p.proname = 'reject_immutable_mutation'
        ORDER BY n.nspname, p.proname
    """,
}

METADATA_CATALOG_QUERIES = {
    "tables": """
        SELECT c.relname AS table_name, c.relkind::text AS relation_kind,
               c.relpersistence::text AS persistence
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'smartcoat_migrations' AND c.relkind IN ('r', 'p')
        ORDER BY c.relname
    """,
    "columns": """
        SELECT c.relname AS table_name, a.attnum AS ordinal_position,
               a.attname AS column_name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null,
               COALESCE(pg_get_expr(d.adbin, d.adrelid, false), '') AS default_expression
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef AS d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = 'smartcoat_migrations' AND c.relkind IN ('r', 'p')
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "constraints": """
        SELECT c.relname AS table_name, con.conname AS constraint_name,
               con.contype::text AS constraint_type,
               pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'smartcoat_migrations'
        ORDER BY c.relname, con.conname
    """,
    "triggers": """
        SELECT c.relname AS table_name, t.tgname AS trigger_name,
               t.tgenabled AS enabled, t.tgisinternal AS internal,
               function_n.nspname AS function_schema,
               p.proname AS function_name,
               pg_get_function_identity_arguments(p.oid) AS function_arguments,
               (t.tgtype & 1) <> 0 AS row_level,
               (t.tgtype & 2) <> 0 AS before_timing,
               (t.tgtype & 4) <> 0 AS insert_event,
               (t.tgtype & 8) <> 0 AS delete_event,
               (t.tgtype & 16) <> 0 AS update_event,
               (t.tgtype & 32) <> 0 AS truncate_event
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_namespace AS function_n ON function_n.oid = p.pronamespace
        WHERE n.nspname = 'smartcoat_migrations' AND NOT t.tgisinternal
        ORDER BY c.relname, t.tgname
    """,
    "functions": """
        SELECT n.nspname AS function_schema, p.proname AS function_name,
               pg_get_function_identity_arguments(p.oid) AS function_arguments,
               pg_get_function_result(p.oid) AS result_type,
               l.lanname AS language_name, p.prosecdef AS security_definer,
               p.provolatile AS volatility, p.prosrc AS source
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE n.nspname = 'smartcoat_migrations'
          AND p.proname = 'reject_metadata_mutation'
        ORDER BY n.nspname, p.proname
    """,
}


class AcceptanceError(RuntimeError):
    result = RESULT_PRODUCT_FAILURE


class ProductContractFailure(AcceptanceError):
    result = RESULT_PRODUCT_FAILURE


class IsolationBlocked(AcceptanceError):
    result = RESULT_ISOLATION_BLOCKED


class EnvironmentBlocked(AcceptanceError):
    result = RESULT_ENVIRONMENT_BLOCKED


@dataclass(frozen=True)
class ImageIdentity:
    reference: str
    image_id: str
    operating_system: str
    architecture: str
    created: str


@dataclass(frozen=True)
class DockerInventory:
    containers: tuple[str, ...]
    images: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    projects: str

    def counts(self) -> dict[str, int]:
        return {
            "containers": len(self.containers),
            "images": len(self.images),
            "networks": len(self.networks),
            "volumes": len(self.volumes),
            "projects": len(json.loads(self.projects)),
        }


def canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_sql(value: str) -> str:
    return " ".join(value.split())


def quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class LiveMigrationLifecycleAcceptance:
    def __init__(self) -> None:
        token = secrets.token_hex(6)
        self.project = f"m0r0141-{token}"
        self.database_name = f"m0r0141_{token}"
        self.backend_network = f"{self.project}-backend"
        self.edge_network = f"{self.project}-edge-unused"
        self.postgres_volume = f"{self.project}-postgres-data"
        self.admin_user = "m0r0141_admin"
        self.app_user = "smartcoat_app"
        self.synthetic_user_id = "usr_m0_r01_4_1_synthetic"
        self.admin_password = secrets.token_hex(24)
        self.app_password = secrets.token_hex(24)
        self.minio_root_password = secrets.token_hex(24)
        self.minio_app_secret = secrets.token_hex(24)
        self.minio_ocr_secret = secrets.token_hex(24)
        self.minio_backup_secret = secrets.token_hex(24)
        self.local_user_password = secrets.token_hex(24)
        self.session_secret = secrets.token_hex(32)
        self.migration_database_url = (
            f"postgresql://{self.admin_user}:{self.admin_password}"
            f"@postgres:5432/{self.database_name}"
        )
        self.application_database_url = (
            f"postgresql://{self.app_user}:{self.app_password}"
            f"@postgres:5432/{self.database_name}"
        )
        self.secret_values = {
            self.admin_password,
            self.app_password,
            self.minio_root_password,
            self.minio_app_secret,
            self.minio_ocr_secret,
            self.minio_backup_secret,
            self.local_user_password,
            self.session_secret,
            self.migration_database_url,
            self.application_database_url,
        }
        self.temporary_directory = Path(
            tempfile.mkdtemp(prefix=f"{self.project}-")
        )
        self.environment_file = self.temporary_directory / "synthetic.env"
        self.override_file = self.temporary_directory / "isolation.compose.yaml"
        self.migration_fixture_directory = self.temporary_directory / "migrations"
        self.baseline_fixture = (
            self.migration_fixture_directory / BASELINE_MIGRATION_SOURCE.name
        )
        self.pending_migration_fixture = (
            self.migration_fixture_directory / PENDING_MIGRATION_FILENAME
        )
        self.recovery_file = self.temporary_directory / "RECOVERY.json"
        self.docker_environment = self._minimal_docker_environment()
        self.command_results: list[dict[str, Any]] = []
        self.evidence: dict[str, Any] = {
            "project": self.project,
            "database": self.database_name,
            "skips": [],
            "lifecycle_checks": [],
        }
        self.inventory_before: DockerInventory | None = None
        self.postgres_image: ImageIdentity | None = None
        self.migration_image: ImageIdentity | None = None
        self.migration_image_immutable_ref: str | None = None
        self.pending_migration_sha256: str | None = None
        self.state_change_attempted = False
        self.cleanup_complete = False
        self.inventory_unchanged_verified = False
        self.cleanup_failure_recorded = False
        self.temporary_files_removed = False
        self.cleanup_installed = False
        self.signal_handlers: dict[int, Any] = {}

    @staticmethod
    def _minimal_docker_environment() -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "XDG_CONFIG_HOME",
            "TMPDIR",
        )
        environment = {
            name: os.environ[name]
            for name in allowed
            if name in os.environ
        }
        environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
        environment["DOCKER_CLI_HINTS"] = "false"
        return environment

    def _sanitized_arguments(self, arguments: list[str]) -> list[str]:
        for argument in arguments:
            if any(secret in argument for secret in self.secret_values):
                raise ProductContractFailure(
                    "A lifecycle command argument exposed a synthetic credential value"
                )
        return list(arguments)

    def _record_command(
        self,
        label: str,
        arguments: list[str],
        completed: subprocess.CompletedProcess[str],
        *,
        input_from_stdin: bool,
    ) -> None:
        self.command_results.append(
            {
                "label": label,
                "arguments": self._sanitized_arguments(arguments),
                "exit": completed.returncode,
                "input_from_stdin": input_from_stdin,
                "stdout_characters": len(completed.stdout),
                "stderr_characters": len(completed.stderr),
            }
        )

    def _assert_no_secret_output(self, completed: subprocess.CompletedProcess[str]) -> None:
        combined = completed.stdout + completed.stderr
        if any(secret in combined for secret in self.secret_values):
            raise ProductContractFailure(
                "A lifecycle command exposed a synthetic credential value"
            )

    def _run(
        self,
        label: str,
        arguments: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 120,
        allow_secret_output: bool = False,
        record: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                arguments,
                cwd=ROOT,
                env=self.docker_environment,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise EnvironmentBlocked(f"Required executable is unavailable: {arguments[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProductContractFailure(f"Timed out during {label}") from exc
        self._sanitized_arguments(arguments)
        if not allow_secret_output:
            self._assert_no_secret_output(completed)
        if record:
            self._record_command(
                label,
                arguments,
                completed,
                input_from_stdin=input_text is not None,
            )
        return completed

    def _compose_arguments(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.environment_file),
            "--file",
            str(COMPOSE_FILE),
            "--file",
            str(self.override_file),
            *arguments,
        ]

    def _compose(
        self,
        label: str,
        *arguments: str,
        input_text: str | None = None,
        timeout: int = 120,
        allow_secret_output: bool = False,
        record: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            label,
            self._compose_arguments(*arguments),
            input_text=input_text,
            timeout=timeout,
            allow_secret_output=allow_secret_output,
            record=record,
        )

    @staticmethod
    def _require(
        condition: bool,
        message: str,
        error_type: type[AcceptanceError] = ProductContractFailure,
    ) -> None:
        if not condition:
            raise error_type(message)

    def _write_synthetic_configuration(self) -> None:
        self._require(
            self.migration_image_immutable_ref is not None
            and IMAGE_ID_PATTERN.fullmatch(self.migration_image_immutable_ref)
            is not None,
            "Migration image is not bound to an immutable local image ID",
            EnvironmentBlocked,
        )
        environment_values = {
            "POSTGRES_DB": self.database_name,
            "POSTGRES_USER": self.admin_user,
            "POSTGRES_PASSWORD": self.admin_password,
            "POSTGRES_APP_USER": self.app_user,
            "POSTGRES_APP_PASSWORD": self.app_password,
            "DATABASE_URL": self.application_database_url,
            "MIGRATION_DATABASE_URL": self.migration_database_url,
            "MINIO_ROOT_USER": "m0r0141-root",
            "MINIO_ROOT_PASSWORD": self.minio_root_password,
            "MINIO_APP_ACCESS_KEY": "m0r0141-app",
            "MINIO_APP_SECRET_KEY": self.minio_app_secret,
            "MINIO_OCR_ACCESS_KEY": "m0r0141-ocr",
            "MINIO_OCR_SECRET_KEY": self.minio_ocr_secret,
            "MINIO_BACKUP_ACCESS_KEY": "m0r0141-backup",
            "MINIO_BACKUP_SECRET_KEY": self.minio_backup_secret,
            "LOCAL_USER_PASSWORD": self.local_user_password,
            "SESSION_SECRET": self.session_secret,
            "M0_R01_4_PROJECT": self.project,
            "M0_R01_4_MIGRATION_IMAGE": self.migration_image_immutable_ref,
            "M0_R01_4_MIGRATIONS_DIR": str(self.migration_fixture_directory),
            "M0_R01_4_BACKEND_NETWORK": self.backend_network,
            "M0_R01_4_EDGE_NETWORK": self.edge_network,
            "M0_R01_4_POSTGRES_VOLUME": self.postgres_volume,
        }
        content = "".join(
            f"{name}={value}\n" for name, value in environment_values.items()
        )
        self.environment_file.write_text(content, encoding="utf-8")
        self.environment_file.chmod(0o600)

        override = """services:
  postgres:
    pull_policy: never
    ports: !reset []
    volumes:
      - live-postgres-data:/var/lib/postgresql/data
    networks: !override
      - backend
    labels:
      com.smartcoat.acceptance.run: ${M0_R01_4_PROJECT:?}
  postgres-migrate:
    image: ${M0_R01_4_MIGRATION_IMAGE:?}
    pull_policy: never
    volumes:
      - type: bind
        source: ${M0_R01_4_MIGRATIONS_DIR:?}
        target: /opt/smartcoat-postgres/migrations
        read_only: true
    labels:
      com.smartcoat.acceptance.run: ${M0_R01_4_PROJECT:?}
networks:
  backend:
    name: ${M0_R01_4_BACKEND_NETWORK:?}
    labels:
      com.smartcoat.acceptance.run: ${M0_R01_4_PROJECT:?}
  edge:
    name: ${M0_R01_4_EDGE_NETWORK:?}
    labels:
      com.smartcoat.acceptance.run: ${M0_R01_4_PROJECT:?}
volumes:
  live-postgres-data:
    name: ${M0_R01_4_POSTGRES_VOLUME:?}
    labels:
      com.smartcoat.acceptance.run: ${M0_R01_4_PROJECT:?}
"""
        self.override_file.write_text(override, encoding="utf-8")
        self.override_file.chmod(0o600)

    @staticmethod
    def _sha256_path(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _prepare_baseline_fixture(self) -> None:
        self._require(
            BASELINE_MIGRATION_SOURCE.is_file(),
            "Accepted baseline migration is unavailable",
            EnvironmentBlocked,
        )
        source_content = BASELINE_MIGRATION_SOURCE.read_bytes()
        self._require(
            hashlib.sha256(source_content).hexdigest() == EXPECTED_BASELINE_SHA256,
            "Accepted baseline migration checksum changed before fixture creation",
            ProductContractFailure,
        )
        self.migration_fixture_directory.mkdir(mode=0o700)
        self.baseline_fixture.write_bytes(source_content)
        self.baseline_fixture.chmod(0o400)
        self._require(
            self.baseline_fixture.read_bytes() == source_content
            and self._sha256_path(self.baseline_fixture) == EXPECTED_BASELINE_SHA256,
            "Temporary baseline migration is not an exact byte-for-byte copy",
            ProductContractFailure,
        )
        self._assert_migration_fixture_host_ownership(expect_pending=False)

    def _create_pending_migration_fixture(self) -> str:
        self._require(
            not self.pending_migration_fixture.exists(),
            "Synthetic pending migration already exists before its controlled phase",
        )
        self.pending_migration_fixture.write_bytes(PENDING_MIGRATION_CONTENT)
        self.pending_migration_fixture.chmod(0o400)
        checksum = self._sha256_path(self.pending_migration_fixture)
        self._require(
            checksum == hashlib.sha256(PENDING_MIGRATION_CONTENT).hexdigest(),
            "Synthetic pending migration bytes changed during fixture creation",
        )
        self.pending_migration_sha256 = checksum
        self._assert_migration_fixture_host_ownership(expect_pending=True)
        return checksum

    def _assert_migration_fixture_host_ownership(
        self, *, expect_pending: bool
    ) -> None:
        directory = self.migration_fixture_directory.resolve()
        self._require(
            directory.parent == self.temporary_directory.resolve()
            and directory.stat().st_uid == os.getuid()
            and stat.S_IMODE(directory.stat().st_mode) == 0o700,
            "Temporary migration fixture directory ownership or mode is unsafe",
            IsolationBlocked,
        )
        expected_files = {self.baseline_fixture.resolve()}
        if expect_pending:
            expected_files.add(self.pending_migration_fixture.resolve())
        observed_files = {item.resolve() for item in directory.iterdir()}
        self._require(
            observed_files == expected_files,
            "Temporary migration fixture contains an unexpected file",
            IsolationBlocked,
        )
        for path in expected_files:
            metadata = path.stat()
            self._require(
                metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o400,
                "Temporary migration fixture file ownership or mode is unsafe",
                IsolationBlocked,
            )

    def _image_identity(self, reference: str) -> ImageIdentity:
        completed = self._run(
            f"inspect_local_image:{reference}",
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}|{{.Os}}|{{.Architecture}}|{{.Created}}",
                reference,
            ],
            record=False,
        )
        if completed.returncode != 0:
            raise EnvironmentBlocked(
                f"Required pinned local image is unavailable without a pull: {reference}"
            )
        fields = completed.stdout.strip().split("|", 3)
        self._require(
            len(fields) == 4 and fields[0].startswith("sha256:"),
            f"Local image identity is unreadable: {reference}",
            EnvironmentBlocked,
        )
        return ImageIdentity(reference, *fields)

    def _inventory(self) -> DockerInventory:
        container_ids = self._run(
            "inventory_container_ids",
            ["docker", "container", "ls", "--all", "--quiet", "--no-trunc"],
            record=False,
        )
        self._require(
            container_ids.returncode == 0,
            "Docker container inventory failed",
            EnvironmentBlocked,
        )
        ids = [item for item in container_ids.stdout.splitlines() if item]
        containers: tuple[str, ...] = ()
        if ids:
            inspected = self._run(
                "inventory_containers",
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    "{{.Id}}|{{.Name}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}|{{index .Config.Labels \"com.docker.compose.project\"}}",
                    *ids,
                ],
                record=False,
            )
            self._require(
                inspected.returncode == 0,
                "Docker container detail inventory failed",
                EnvironmentBlocked,
            )
            containers = tuple(sorted(inspected.stdout.splitlines()))

        images_result = self._run(
            "inventory_images",
            [
                "docker",
                "image",
                "ls",
                "--no-trunc",
                "--digests",
                "--format",
                "{{.ID}}|{{.Repository}}|{{.Tag}}|{{.Digest}}",
            ],
            record=False,
        )
        self._require(
            images_result.returncode == 0,
            "Docker image inventory failed",
            EnvironmentBlocked,
        )

        network_ids = self._run(
            "inventory_network_ids",
            ["docker", "network", "ls", "--quiet", "--no-trunc"],
            record=False,
        )
        self._require(
            network_ids.returncode == 0,
            "Docker network inventory failed",
            EnvironmentBlocked,
        )
        network_id_values = [item for item in network_ids.stdout.splitlines() if item]
        networks: tuple[str, ...] = ()
        if network_id_values:
            inspected_networks = self._run(
                "inventory_networks",
                [
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    "{{.Id}}|{{.Name}}|{{.Driver}}|{{.Internal}}|{{index .Labels \"com.docker.compose.project\"}}",
                    *network_id_values,
                ],
                record=False,
            )
            self._require(
                inspected_networks.returncode == 0,
                "Docker network detail inventory failed",
                EnvironmentBlocked,
            )
            networks = tuple(sorted(inspected_networks.stdout.splitlines()))

        volume_names = self._run(
            "inventory_volume_names",
            ["docker", "volume", "ls", "--quiet"],
            record=False,
        )
        self._require(
            volume_names.returncode == 0,
            "Docker volume inventory failed",
            EnvironmentBlocked,
        )
        volume_name_values = [item for item in volume_names.stdout.splitlines() if item]
        volumes: tuple[str, ...] = ()
        if volume_name_values:
            inspected_volumes = self._run(
                "inventory_volumes",
                [
                    "docker",
                    "volume",
                    "inspect",
                    "--format",
                    "{{.Name}}|{{.Driver}}|{{.Scope}}|{{index .Labels \"com.docker.compose.project\"}}",
                    *volume_name_values,
                ],
                record=False,
            )
            self._require(
                inspected_volumes.returncode == 0,
                "Docker volume detail inventory failed",
                EnvironmentBlocked,
            )
            volumes = tuple(sorted(inspected_volumes.stdout.splitlines()))

        projects_result = self._run(
            "inventory_compose_projects",
            ["docker", "compose", "ls", "--all", "--format", "json"],
            record=False,
        )
        self._require(
            projects_result.returncode == 0,
            "Docker Compose project inventory failed",
            EnvironmentBlocked,
        )
        projects = json.dumps(
            sorted(json.loads(projects_result.stdout), key=lambda item: item["Name"]),
            sort_keys=True,
            separators=(",", ":"),
        )
        return DockerInventory(
            containers=containers,
            images=tuple(sorted(images_result.stdout.splitlines())),
            networks=networks,
            volumes=volumes,
            projects=projects,
        )

    def _resource_names(self, kind: str) -> set[str]:
        if kind == "container":
            arguments = [
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.Names}}",
            ]
        elif kind == "network":
            arguments = [
                "docker",
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.Name}}",
            ]
        elif kind == "volume":
            arguments = [
                "docker",
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.Name}}",
            ]
        else:  # pragma: no cover - internal misuse guard
            raise ValueError(kind)
        completed = self._run(
            f"list_owned_{kind}s",
            arguments,
            record=False,
        )
        self._require(
            completed.returncode == 0,
            f"Could not inspect owned {kind} resources",
            IsolationBlocked,
        )
        return {line for line in completed.stdout.splitlines() if line}

    def _resource_label(self, kind: str, name: str, label: str) -> str:
        noun = "container" if kind == "container" else kind
        completed = self._run(
            f"inspect_{kind}_label",
            [
                "docker",
                noun,
                "inspect",
                "--format",
                f"{{{{index .Labels \"{label}\"}}}}"
                if kind != "container"
                else f"{{{{index .Config.Labels \"{label}\"}}}}",
                name,
            ],
            record=False,
        )
        self._require(
            completed.returncode == 0,
            f"Could not inspect {kind} ownership label",
            IsolationBlocked,
        )
        return completed.stdout.strip()

    def _assert_no_owned_resources_exist(self) -> None:
        for kind in ("container", "network", "volume"):
            self._require(
                not self._resource_names(kind),
                f"Generated project unexpectedly collides with an existing {kind}",
                IsolationBlocked,
            )
        for kind, name in (
            ("network", self.backend_network),
            ("network", self.edge_network),
            ("volume", self.postgres_volume),
        ):
            listing = self._run(
                f"check_unique_{kind}_name",
                ["docker", kind, "ls", "--format", "{{.Name}}"],
                record=False,
            )
            self._require(
                listing.returncode == 0 and name not in listing.stdout.splitlines(),
                f"Generated {kind} name collides with an existing resource",
                IsolationBlocked,
            )

    def _assert_immutable_migration_image_binding(
        self, rendered_image: str
    ) -> None:
        self._require(
            self.migration_image_immutable_ref is not None
            and IMAGE_ID_PATTERN.fullmatch(self.migration_image_immutable_ref)
            is not None
            and rendered_image == self.migration_image_immutable_ref,
            "Rendered migration image is not the resolved immutable local image ID",
            IsolationBlocked,
        )
    def _validate_rendered_configuration(self, config: dict[str, Any]) -> None:
        self._require(
            PROJECT_PATTERN.fullmatch(self.project) is not None,
            "Generated Compose project name does not satisfy the isolation format",
            IsolationBlocked,
        )
        self._require(
            config.get("name") == self.project,
            "Rendered Compose project name is not the generated isolated name",
            IsolationBlocked,
        )
        services = config.get("services", {})
        self._require(
            {"postgres", "postgres-migrate"}.issubset(services),
            "Rendered configuration is missing an authorized lifecycle service",
            IsolationBlocked,
        )
        postgres = services["postgres"]
        migration = services["postgres-migrate"]
        self._require(
            postgres.get("image") == POSTGRES_IMAGE_REF,
            "Rendered PostgreSQL image is not the accepted pinned image",
            IsolationBlocked,
        )
        self._assert_immutable_migration_image_binding(migration.get("image", ""))
        self._require(
            postgres.get("pull_policy") == "never"
            and migration.get("pull_policy") == "never",
            "An authorized service could pull an image",
            IsolationBlocked,
        )
        self._require(
            not postgres.get("ports") and not migration.get("ports"),
            "An authorized service would publish a host port",
            IsolationBlocked,
        )
        self._require(
            set(postgres.get("networks", {})) == {"backend"}
            and set(migration.get("networks", {})) == {"backend"},
            "An authorized service would join a non-isolated network",
            IsolationBlocked,
        )
        networks = config.get("networks", {})
        self._require(
            networks.get("backend", {}).get("name") == self.backend_network
            and networks.get("backend", {}).get("internal") is True,
            "The backend network is not uniquely named and internal",
            IsolationBlocked,
        )
        self._require(
            networks.get("edge", {}).get("name") == self.edge_network,
            "The explicit base edge-network name was not isolated",
            IsolationBlocked,
        )
        data_mounts = [
            mount
            for mount in postgres.get("volumes", [])
            if mount.get("target") == "/var/lib/postgresql/data"
        ]
        self._require(
            len(data_mounts) == 1
            and data_mounts[0].get("type") == "volume"
            and data_mounts[0].get("source") == "live-postgres-data",
            "PostgreSQL storage does not resolve to the unique disposable volume",
            IsolationBlocked,
        )
        init_mounts = [
            mount
            for mount in postgres.get("volumes", [])
            if mount.get("target") == "/docker-entrypoint-initdb.d/001-init.sql"
        ]
        self._require(
            len(init_mounts) == 1
            and init_mounts[0].get("type") == "bind"
            and init_mounts[0].get("read_only") is True
            and Path(init_mounts[0].get("source", "")).resolve()
            == (ROOT / "infra/postgres/init.sql").resolve(),
            "The authoritative init.sql mount was not preserved read-only",
            IsolationBlocked,
        )
        volume_config = config.get("volumes", {}).get("live-postgres-data", {})
        self._require(
            volume_config.get("name") == self.postgres_volume,
            "The disposable volume name is not unique",
            IsolationBlocked,
        )
        postgres_environment = postgres.get("environment", {})
        self._require(
            postgres_environment.get("POSTGRES_DB") == self.database_name
            and postgres_environment.get("POSTGRES_USER") == self.admin_user
            and postgres_environment.get("POSTGRES_PASSWORD") == self.admin_password,
            "PostgreSQL did not render exclusively synthetic identity values",
            IsolationBlocked,
        )
        self._require(
            migration.get("environment")
            == {"MIGRATION_DATABASE_URL": self.migration_database_url},
            "Migration credential isolation changed in the rendered configuration",
            IsolationBlocked,
        )
        credential_consumers = {
            name
            for name, service in services.items()
            if "MIGRATION_DATABASE_URL" in service.get("environment", {})
        }
        self._require(
            credential_consumers == {"postgres-migrate"},
            "MIGRATION_DATABASE_URL escaped the migration service boundary",
            IsolationBlocked,
        )
        self._require(
            migration.get("entrypoint")
            == ["python", "/opt/smartcoat-postgres/migrate.py"]
            and migration.get("command") == ["apply"],
            "The accepted migration entrypoint or default apply command changed",
            IsolationBlocked,
        )
        migration_mounts = [
            mount
            for mount in migration.get("volumes", [])
            if mount.get("target") == MIGRATION_MOUNT_TARGET
        ]
        self._require(
            len(migration_mounts) == 1
            and migration_mounts[0].get("type") == "bind"
            and migration_mounts[0].get("read_only") is True
            and Path(migration_mounts[0].get("source", "")).resolve()
            == self.migration_fixture_directory.resolve(),
            "Temporary migration fixture is not mounted from the generated directory read-only",
            IsolationBlocked,
        )
        self._assert_migration_fixture_host_ownership(expect_pending=False)

    def _install_cleanup_handlers(self, *, recovering: bool = False) -> None:
        self._require(
            recovering or not self.state_change_attempted,
            "Cleanup handlers were installed after a state-change attempt",
            IsolationBlocked,
        )
        atexit.register(self._atexit_cleanup)
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            self.signal_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._handle_signal)
        self.cleanup_installed = True

    def _unregister_cleanup_handler(self) -> None:
        if self.cleanup_installed:
            atexit.unregister(self._atexit_cleanup)
            self.cleanup_installed = False

    def _handle_signal(self, signal_number: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signal_number}")

    def _restore_signal_handlers(self) -> None:
        for signal_number, handler in self.signal_handlers.items():
            signal.signal(signal_number, handler)
        self.signal_handlers.clear()

    def _atexit_cleanup(self) -> None:
        if (
            self.state_change_attempted
            and not self.cleanup_complete
            and not self.cleanup_failure_recorded
        ):
            try:
                self.cleanup()
                if self.inventory_before is not None:
                    self.verify_inventory_unchanged()
                self.remove_temporary_files()
            except Exception as exc:
                self.preserve_recovery_files(
                    f"Fallback cleanup failed: {type(exc).__name__}: {exc}"
                )

    def preflight(self) -> None:
        self._require(
            COMPOSE_FILE.is_file(),
            "Accepted compose.yaml is unavailable",
            EnvironmentBlocked,
        )
        self._require(
            shutil.which("docker") is not None,
            "Docker CLI is unavailable",
            EnvironmentBlocked,
        )
        docker_version = self._run(
            "docker_version",
            [
                "docker",
                "version",
                "--format",
                "client={{.Client.Version}} server={{.Server.Version}} server_api={{.Server.APIVersion}}",
            ],
        )
        self._require(
            docker_version.returncode == 0,
            "Docker Engine is unavailable",
            EnvironmentBlocked,
        )
        compose_version = self._run(
            "compose_version",
            ["docker", "compose", "version", "--short"],
        )
        self._require(
            compose_version.returncode == 0,
            "Docker Compose is unavailable",
            EnvironmentBlocked,
        )
        self.postgres_image = self._image_identity(POSTGRES_IMAGE_REF)
        self.migration_image = self._image_identity(MIGRATION_IMAGE_DISCOVERY_REF)
        self._require(
            IMAGE_ID_PATTERN.fullmatch(self.migration_image.image_id) is not None,
            "The local migration image did not resolve to an immutable image ID",
            EnvironmentBlocked,
        )
        self.migration_image_immutable_ref = self.migration_image.image_id
        self._prepare_baseline_fixture()
        self._write_synthetic_configuration()
        self.inventory_before = self._inventory()
        self._assert_no_owned_resources_exist()
        rendered = self._compose(
            "render_isolated_config",
            "config",
            "--format",
            "json",
            allow_secret_output=True,
        )
        self._require(
            rendered.returncode == 0,
            "Isolated Compose configuration could not be rendered",
            IsolationBlocked,
        )
        try:
            config = json.loads(rendered.stdout)
        except json.JSONDecodeError as exc:
            raise IsolationBlocked("Rendered Compose configuration is not JSON") from exc
        self._validate_rendered_configuration(config)
        self.evidence.update(
            {
                "docker_version": docker_version.stdout.strip(),
                "compose_version": compose_version.stdout.strip(),
                "postgres_image": self.postgres_image.__dict__,
                "migration_image": self.migration_image.__dict__,
                "migration_image_binding": {
                    "discovery_reference": MIGRATION_IMAGE_DISCOVERY_REF,
                    "resolved_immutable_image": self.migration_image_immutable_ref,
                    "rendered_service_image": config["services"]["postgres-migrate"][
                        "image"
                    ],
                },
                "migration_fixture": {
                    "baseline_source_sha256": EXPECTED_BASELINE_SHA256,
                    "baseline_copy_sha256": self._sha256_path(
                        self.baseline_fixture
                    ),
                    "mount_source": str(self.migration_fixture_directory),
                    "mount_target": MIGRATION_MOUNT_TARGET,
                    "read_only": True,
                    "owner_uid": os.getuid(),
                },
                "inventory_before": self.inventory_before.counts(),
                "isolation": {
                    "project_name_validated": True,
                    "real_env_disabled": True,
                    "synthetic_credentials_only": True,
                    "unique_labeled_volume": self.postgres_volume,
                    "unique_internal_backend": self.backend_network,
                    "base_edge_name_overridden": self.edge_network,
                    "published_ports": 0,
                    "authorized_services": ["postgres", "postgres-migrate"],
                    "pull_policy": "never",
                },
            }
        )

    def _verify_migration_execution_image_boundary(self) -> None:
        self._require(
            self.migration_image is not None
            and self.migration_image_immutable_ref == self.migration_image.image_id,
            "Immutable migration image identity was not established before execution",
            EnvironmentBlocked,
        )
        probe_name = f"{self.project}-migration-image-probe"
        executed = self._compose(
            "execute_migration_image_identity_probe",
            "run",
            "--no-deps",
            "--pull",
            "never",
            "--name",
            probe_name,
            "--entrypoint",
            "/bin/true",
            "postgres-migrate",
        )
        self._require(
            executed.returncode == 0,
            "Could not execute the isolated migration-service identity probe",
            EnvironmentBlocked,
        )
        self._verify_owned_resources()
        identity = self._run(
            "verify_migration_execution_image",
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.Image}}|{{.Config.Image}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}",
                probe_name,
            ],
        )
        self._require(
            identity.returncode == 0,
            "Could not inspect the migration-service image probe",
            IsolationBlocked,
        )
        image_id, configured_image, project, service = (
            identity.stdout.strip().split("|", 3)
        )
        self._require(
            image_id == self.migration_image.image_id
            and configured_image == self.migration_image_immutable_ref
            and project == self.project
            and service == "postgres-migrate",
            "Migration execution boundary is not bound to the resolved immutable image ID",
            IsolationBlocked,
        )
        removed = self._run(
            "remove_migration_image_identity_probe",
            ["docker", "container", "rm", probe_name],
        )
        self._require(
            removed.returncode == 0 and not self._project_container_rows(),
            "Migration-service identity probe did not cleanly remove its owned container",
            IsolationBlocked,
        )
        self.evidence["migration_image_binding"].update(
            {
                "execution_container_config_image": configured_image,
                "execution_container_image_id": image_id,
                "execution_boundary_verified": True,
            }
        )

    def _project_container_rows(self) -> list[dict[str, str]]:
        completed = self._run(
            "list_project_containers",
            [
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                '{{json .}}',
            ],
            record=False,
        )
        self._require(
            completed.returncode == 0,
            "Could not inspect isolated project containers",
            IsolationBlocked,
        )
        return [json.loads(line) for line in completed.stdout.splitlines() if line]

    def _verify_owned_resources(self) -> None:
        containers = self._project_container_rows()
        for container in containers:
            name = container["Names"]
            self._require(
                self._resource_label(
                    "container", name, "com.docker.compose.project"
                )
                == self.project,
                "A cleanup container lacks generated project ownership",
                IsolationBlocked,
            )
            self._require(
                self._resource_label(
                    "container", name, "com.smartcoat.acceptance.run"
                )
                == self.project,
                "A cleanup container lacks the acceptance-run ownership label",
                IsolationBlocked,
            )
            service = self._resource_label(
                "container", name, "com.docker.compose.service"
            )
            self._require(
                service in {"postgres", "postgres-migrate"},
                "An unauthorized service exists in the isolated project",
                IsolationBlocked,
            )

        network_names = self._resource_names("network")
        volume_names = self._resource_names("volume")
        self._require(
            network_names.issubset({self.backend_network}),
            "An unexpected project-owned network exists",
            IsolationBlocked,
        )
        self._require(
            volume_names.issubset({self.postgres_volume}),
            "An unexpected project-owned volume exists",
            IsolationBlocked,
        )
        for kind, names in (("network", network_names), ("volume", volume_names)):
            for name in names:
                self._require(
                    self._resource_label(kind, name, "com.docker.compose.project")
                    == self.project,
                    f"A cleanup {kind} lacks generated project ownership",
                    IsolationBlocked,
                )
                self._require(
                    self._resource_label(kind, name, "com.smartcoat.acceptance.run")
                    == self.project,
                    f"A cleanup {kind} lacks the acceptance-run ownership label",
                    IsolationBlocked,
                )

    def _wait_for_postgres(self, timeout_seconds: int = 120) -> str:
        deadline = time.monotonic() + timeout_seconds
        container_id = ""
        while time.monotonic() < deadline:
            container_result = self._compose(
                "postgres_container_id",
                "ps",
                "--quiet",
                "postgres",
                record=False,
            )
            if container_result.returncode == 0:
                container_id = container_result.stdout.strip()
            if container_id:
                health = self._run(
                    "postgres_health",
                    [
                        "docker",
                        "container",
                        "inspect",
                        "--format",
                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                        container_id,
                    ],
                    record=False,
                )
                self._require(
                    health.returncode == 0,
                    "Could not inspect isolated PostgreSQL health",
                )
                status = health.stdout.strip()
                if status == "healthy":
                    return container_id
                if status == "unhealthy":
                    raise ProductContractFailure(
                        "Isolated PostgreSQL became unhealthy during bootstrap"
                    )
            time.sleep(1)
        raise ProductContractFailure(
            "Isolated PostgreSQL did not become healthy before timeout"
        )

    def _verify_running_postgres_isolation(self, container_id: str) -> None:
        self._require(self.postgres_image is not None, "PostgreSQL image was not recorded")
        identity = self._run(
            "verify_postgres_container_identity",
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.Image}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}|{{json .HostConfig.PortBindings}}",
                container_id,
            ],
        )
        self._require(
            identity.returncode == 0,
            "Could not inspect isolated PostgreSQL identity",
        )
        image_id, project, service, port_bindings = identity.stdout.strip().split("|", 3)
        self._require(
            image_id == self.postgres_image.image_id
            and project == self.project
            and service == "postgres",
            "Running PostgreSQL does not match the isolated project identity",
            IsolationBlocked,
        )
        self._require(
            json.loads(port_bindings) in ({}, None),
            "Running PostgreSQL published a host port",
            IsolationBlocked,
        )
        networks = self._run(
            "verify_postgres_networks",
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{\"\\n\"}}{{end}}",
                container_id,
            ],
        )
        self._require(
            {line for line in networks.stdout.splitlines() if line}
            == {self.backend_network},
            "Running PostgreSQL joined an unexpected network",
            IsolationBlocked,
        )
        project_services = {
            self._resource_label(
                "container", row["Names"], "com.docker.compose.service"
            )
            for row in self._project_container_rows()
        }
        self._require(
            project_services == {"postgres"},
            "A service other than PostgreSQL started during isolated bootstrap",
            IsolationBlocked,
        )
        self._verify_owned_resources()

    def _psql(
        self,
        label: str,
        sql: str,
        *,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        return self._compose(
            label,
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-X",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            self.admin_user,
            "--dbname",
            self.database_name,
            "--no-align",
            "--tuples-only",
            input_text=sql + "\n",
            timeout=timeout,
        )

    def _psql_success(self, label: str, sql: str) -> str:
        completed = self._psql(label, sql)
        self._require(
            completed.returncode == 0,
            f"PostgreSQL assertion query failed: {label}",
        )
        return completed.stdout.strip()

    def _psql_rows(self, label: str, query: str) -> list[dict[str, Any]]:
        query_without_semicolon = query.strip().rstrip(";")
        json_query = (
            "SELECT row_to_json(_row)::text FROM ("
            + query_without_semicolon
            + ") AS _row"
        )
        output = self._psql_success(label, json_query)
        return [json.loads(line) for line in output.splitlines() if line]

    def _catalog_snapshot(
        self, label_prefix: str, queries: dict[str, str]
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            category: self._psql_rows(
                f"{label_prefix}:{category}",
                query,
            )
            for category, query in queries.items()
        }

    def _business_snapshot(self, label_prefix: str) -> dict[str, list[dict[str, Any]]]:
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table_name in sorted(EXPECTED_PUBLIC_TABLES):
            identifier = quoted_identifier(table_name)
            snapshot[table_name] = self._psql_rows(
                f"{label_prefix}:{table_name}",
                f"""
                    SELECT to_jsonb(t) AS row
                    FROM public.{identifier} AS t
                    ORDER BY to_jsonb(t)::text
                """,
            )
        return snapshot

    def _metadata_state(self, label: str) -> list[bool]:
        output = self._psql_success(
            label,
            """
                SELECT json_build_array(
                    to_regnamespace('smartcoat_migrations') IS NOT NULL,
                    to_regclass('smartcoat_migrations.applied_migrations') IS NOT NULL,
                    to_regclass('smartcoat_migrations.adoption_decisions') IS NOT NULL,
                    to_regprocedure('smartcoat_migrations.reject_metadata_mutation()') IS NOT NULL,
                    EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgrelid = to_regclass('smartcoat_migrations.applied_migrations')
                          AND tgname = 'applied_migrations_append_only'
                    ),
                    EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgrelid = to_regclass('smartcoat_migrations.adoption_decisions')
                          AND tgname = 'adoption_decisions_append_only'
                    )
                )::text
            """,
        )
        state = json.loads(output)
        self._require(
            isinstance(state, list) and len(state) == 6,
            "Migration metadata state query was incomplete",
        )
        return state

    def _run_migration(
        self,
        label: str,
        *command: str,
    ) -> subprocess.CompletedProcess[str]:
        return self._compose(
            label,
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "postgres-migrate",
            *command,
            timeout=180,
        )

    def _assert_public_bootstrap(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        table_names = {row["table_name"] for row in snapshot["tables"]}
        self._require(
            table_names == EXPECTED_PUBLIC_TABLES,
            "init.sql did not create the exact accepted public table set",
        )
        trigger_pairs = {
            (row["table_name"], row["trigger_name"])
            for row in snapshot["triggers"]
        }
        self._require(
            trigger_pairs == EXPECTED_PUBLIC_TRIGGERS,
            "init.sql did not create the exact accepted public append-only triggers",
        )
        self._require(
            all(
                row["enabled"] == "O"
                and row["function_schema"] == "public"
                and row["function_name"] == "reject_immutable_mutation"
                for row in snapshot["triggers"]
            ),
            "A public append-only trigger has unexpected semantics",
        )
        self._require(
            len(snapshot["trigger_functions"]) == 1
            and normalized_sql(snapshot["trigger_functions"][0]["source"])
            == "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END;",
            "The public append-only trigger function differs from init.sql",
        )
        self._require(
            len(snapshot["constraints"]) >= 30,
            "The accepted public constraints are incomplete",
        )

    def _ledger_rows(self, label: str) -> list[dict[str, Any]]:
        return self._psql_rows(
            label,
            """
                SELECT version, name, sha256,
                       applied_at_utc::text AS applied_at_utc, applied_by
                FROM smartcoat_migrations.applied_migrations
                ORDER BY version
            """,
        )

    def _adoption_rows(self, label: str) -> list[dict[str, Any]]:
        return self._psql_rows(
            label,
            """
                SELECT action_identifier, database_name, database_oid,
                       migration_actor, database_server_version,
                       adopted_at_utc::text AS adopted_at_utc,
                       expected_structural_fingerprint,
                       observed_structural_fingerprint, init_sql_sha256,
                       baseline_version, baseline_name, baseline_sha256,
                       contract_version,
                       compared_categories_json AS compared_categories,
                       authorization_statement
                FROM smartcoat_migrations.adoption_decisions
                ORDER BY adopted_at_utc, action_identifier
            """,
        )

    def _probe_rows(self, label: str) -> list[dict[str, Any]]:
        return self._psql_rows(
            label,
            f"""
                SELECT probe_id, probe_value,
                       observed_at_utc::text AS observed_at_utc
                FROM {PROBE_SCHEMA}.{PROBE_TABLE}
                ORDER BY probe_id
            """,
        )

    def _assert_pending_migration_evidence(
        self,
        ledger: list[dict[str, Any]],
        probe_rows: list[dict[str, Any]],
    ) -> None:
        self._require(
            self.pending_migration_sha256 is not None,
            "Synthetic pending migration checksum was not recorded",
        )
        self._require(
            len(ledger) == 2,
            "Successful pending migration did not produce exactly two ledger rows",
        )
        baseline, pending = ledger
        self._require(
            baseline["version"] == EXPECTED_BASELINE_VERSION
            and baseline["name"] == EXPECTED_BASELINE_NAME
            and baseline["sha256"] == EXPECTED_BASELINE_SHA256,
            "Successful pending migration changed the accepted baseline ledger row",
        )
        self._require(
            pending["version"] == 2
            and pending["name"] == PENDING_MIGRATION_NAME
            and pending["sha256"] == self.pending_migration_sha256
            and bool(pending["applied_at_utc"])
            and pending["applied_by"] == self.admin_user,
            "Version-2 ledger evidence differs from the exact synthetic migration",
        )
        self._require(
            probe_rows
            == [
                {
                    "probe_id": "m0-r01-4-1-commit-probe",
                    "probe_value": "synthetic-pending-migration-applied",
                    "observed_at_utc": "2026-01-01 00:00:00+00",
                }
            ],
            "Independent synthetic migration probe effect is missing or changed",
        )

    def _assert_metadata_contract(
        self, snapshot: dict[str, list[dict[str, Any]]]
    ) -> None:
        self._require(
            [row["table_name"] for row in snapshot["tables"]]
            == ["adoption_decisions", "applied_migrations"],
            "Migration metadata table set is incorrect",
        )
        observed_columns: dict[str, list[str]] = {}
        for row in snapshot["columns"]:
            observed_columns.setdefault(row["table_name"], []).append(row["column_name"])
            self._require(
                row["not_null"] is True,
                "A migration metadata column unexpectedly permits null",
            )
        self._require(
            observed_columns == EXPECTED_METADATA_COLUMNS,
            "Migration metadata columns differ from the accepted definition",
        )
        constraint_names = {
            row["constraint_name"] for row in snapshot["constraints"]
        }
        self._require(
            {
                "applied_migrations_pkey",
                "applied_migrations_version_check",
                "applied_migrations_sha256_check",
                "adoption_decisions_pkey",
                "adoption_decisions_action_identifier_check",
                "adoption_decisions_authorization_statement_check",
            }.issubset(constraint_names),
            "Migration metadata constraints are incomplete",
        )
        expected_trigger_targets = {
            ("adoption_decisions", "adoption_decisions_append_only"),
            ("applied_migrations", "applied_migrations_append_only"),
        }
        self._require(
            {
                (row["table_name"], row["trigger_name"])
                for row in snapshot["triggers"]
            }
            == expected_trigger_targets,
            "Migration metadata trigger targets are incorrect",
        )
        self._require(
            all(
                row["enabled"] == "O"
                and row["internal"] is False
                and row["function_schema"] == "smartcoat_migrations"
                and row["function_name"] == "reject_metadata_mutation"
                and row["function_arguments"] == ""
                and row["row_level"] is True
                and row["before_timing"] is True
                and row["insert_event"] is False
                and row["delete_event"] is True
                and row["update_event"] is True
                and row["truncate_event"] is False
                for row in snapshot["triggers"]
            ),
            "A metadata mutation trigger has unexpected semantics",
        )
        functions = snapshot["functions"]
        self._require(
            len(functions) == 1
            and functions[0]["function_schema"] == "smartcoat_migrations"
            and functions[0]["function_name"] == "reject_metadata_mutation"
            and functions[0]["function_arguments"] == ""
            and functions[0]["result_type"] == "trigger"
            and functions[0]["language_name"] == "plpgsql"
            and functions[0]["security_definer"] is False
            and functions[0]["volatility"] == "v"
            and normalized_sql(functions[0]["source"])
            == "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END;",
            "The metadata mutation guard function differs from the accepted definition",
        )

    def _assert_adoption_rows(
        self,
        ledger: list[dict[str, Any]],
        adoption: list[dict[str, Any]],
        database_oid: int,
    ) -> None:
        self._require(len(ledger) == 1, "Baseline ledger row count is not exactly one")
        ledger_row = ledger[0]
        self._require(
            ledger_row["version"] == EXPECTED_BASELINE_VERSION
            and ledger_row["name"] == EXPECTED_BASELINE_NAME
            and ledger_row["sha256"] == EXPECTED_BASELINE_SHA256
            and bool(ledger_row["applied_at_utc"])
            and ledger_row["applied_by"] == self.admin_user,
            "Baseline ledger evidence differs from the accepted record",
        )
        self._require(
            len(adoption) == 1,
            "Adoption decision row count is not exactly one",
        )
        row = adoption[0]
        self._require(
            row["action_identifier"] == EXPECTED_ADOPTION_ACTION
            and row["database_name"] == self.database_name
            and row["database_oid"] == database_oid
            and row["migration_actor"] == self.admin_user
            and bool(row["database_server_version"])
            and bool(row["adopted_at_utc"])
            and row["expected_structural_fingerprint"]
            == EXPECTED_STRUCTURAL_FINGERPRINT
            and row["observed_structural_fingerprint"]
            == EXPECTED_STRUCTURAL_FINGERPRINT
            and row["init_sql_sha256"] == EXPECTED_INIT_SQL_SHA256
            and row["baseline_version"] == EXPECTED_BASELINE_VERSION
            and row["baseline_name"] == EXPECTED_BASELINE_NAME
            and row["baseline_sha256"] == EXPECTED_BASELINE_SHA256
            and row["contract_version"] == EXPECTED_CONTRACT_VERSION
            and row["compared_categories"] == EXPECTED_COMPARED_CATEGORIES
            and row["authorization_statement"] == EXPECTED_ADOPTION_AUTHORIZATION,
            "Adoption decision evidence differs from the accepted contract",
        )

    def _assert_mutation_rejected(self, label: str, statement: str) -> None:
        completed = self._psql(label, statement)
        self._require(
            completed.returncode != 0,
            f"PostgreSQL accepted forbidden metadata mutation: {label}",
        )
        self._require(
            "append-only" in (completed.stdout + completed.stderr),
            f"PostgreSQL rejected metadata mutation for an unexpected reason: {label}",
        )

    def lifecycle(self) -> None:
        self._install_cleanup_handlers()
        self.state_change_attempted = True
        self._verify_migration_execution_image_boundary()
        started = self._compose(
            "start_isolated_postgres",
            "up",
            "--detach",
            "--no-deps",
            "--pull",
            "never",
            "postgres",
            timeout=180,
        )
        self._require(
            started.returncode == 0,
            "The isolated PostgreSQL service could not start from the local image",
            EnvironmentBlocked,
        )
        container_id = self._wait_for_postgres()
        self._verify_running_postgres_isolation(container_id)
        self.evidence["lifecycle_checks"].append("postgres_healthy_and_isolated")

        public_before = self._catalog_snapshot(
            "public_catalog_before",
            PUBLIC_CATALOG_QUERIES,
        )
        self._assert_public_bootstrap(public_before)
        public_fingerprint_before = canonical_fingerprint(public_before)
        self._require(
            self._metadata_state("metadata_state_before_apply") == [False] * 6,
            "A fresh bootstrap unexpectedly contained migration metadata",
        )
        self._psql_success(
            "insert_fixed_synthetic_business_row",
            """
                INSERT INTO public.users (
                    user_id, display_name, email, role, active, created_at_utc
                ) VALUES (
                    'usr_m0_r01_4_1_synthetic',
                    'M0 R01 4 1 Synthetic User',
                    'm0-r01-4-1-synthetic@example.invalid',
                    'UPLOADER', true,
                    TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
            """,
        )
        business_before = self._business_snapshot("business_before_adoption")
        self._require(
            len(business_before["users"]) == 1
            and sum(len(rows) for rows in business_before.values()) == 1,
            "The fixed synthetic business fixture is not isolated",
        )
        self.evidence["lifecycle_checks"].append("bootstrap_catalog_verified")

        failed_apply = self._run_migration("apply_before_adoption", "apply")
        self._require(
            failed_apply.returncode != 0,
            "Ordinary apply unexpectedly accepted an unmanaged database",
        )
        self._require(
            "Database is unmanaged" in (failed_apply.stdout + failed_apply.stderr),
            "Ordinary apply failed without the unmanaged-database rejection",
        )
        self._require(
            self._metadata_state("metadata_state_after_failed_apply") == [False] * 6,
            "Failed ordinary apply created partial migration metadata",
        )
        self._require(
            self._business_snapshot("business_after_failed_apply") == business_before,
            "Failed ordinary apply changed application rows",
        )
        self.evidence["lifecycle_checks"].append("unmanaged_apply_rejected_cleanly")

        adoption_result = self._run_migration(
            "explicit_adoption",
            "adopt",
            self.database_name,
        )
        self._require(
            adoption_result.returncode == 0
            and "status=ADOPTED" in adoption_result.stdout
            and "evidence_inserted=true" in adoption_result.stdout,
            "Explicit bootstrap adoption did not succeed",
        )
        identity_rows = self._psql_rows(
            "database_identity_after_adoption",
            """
                SELECT current_database() AS database_name,
                       d.oid::bigint AS database_oid,
                       current_user AS database_user,
                       current_setting('server_version') AS server_version
                FROM pg_database AS d
                WHERE d.datname = current_database()
            """,
        )
        self._require(
            len(identity_rows) == 1
            and identity_rows[0]["database_name"] == self.database_name
            and identity_rows[0]["database_user"] == self.admin_user,
            "Independent PostgreSQL identity evidence is incorrect",
        )
        database_oid = int(identity_rows[0]["database_oid"])
        ledger_after_adoption = self._ledger_rows("ledger_after_adoption")
        evidence_after_adoption = self._adoption_rows("evidence_after_adoption")
        self._assert_adoption_rows(
            ledger_after_adoption,
            evidence_after_adoption,
            database_oid,
        )
        metadata_after_adoption = self._catalog_snapshot(
            "metadata_catalog_after_adoption",
            METADATA_CATALOG_QUERIES,
        )
        self._assert_metadata_contract(metadata_after_adoption)
        self._require(
            self._business_snapshot("business_after_adoption") == business_before,
            "Explicit adoption changed application rows",
        )
        self.evidence["lifecycle_checks"].append("adoption_evidence_verified")

        for label, statement in (
            (
                "update_adoption_evidence_rejected",
                "UPDATE smartcoat_migrations.adoption_decisions "
                "SET database_name = 'forbidden'",
            ),
            (
                "delete_adoption_evidence_rejected",
                "DELETE FROM smartcoat_migrations.adoption_decisions",
            ),
            (
                "update_migration_ledger_rejected",
                "UPDATE smartcoat_migrations.applied_migrations "
                "SET name = 'forbidden'",
            ),
            (
                "delete_migration_ledger_rejected",
                "DELETE FROM smartcoat_migrations.applied_migrations",
            ),
        ):
            self._assert_mutation_rejected(label, statement)
        self._require(
            self._ledger_rows("ledger_after_mutation_attempts")
            == ledger_after_adoption
            and self._adoption_rows("evidence_after_mutation_attempts")
            == evidence_after_adoption,
            "Rejected metadata mutations changed stored evidence",
        )
        self.evidence["lifecycle_checks"].append("metadata_rows_append_only")

        default_apply = self._run_migration("baseline_only_default_apply")
        self._require(
            default_apply.returncode == 0
            and "discovered=1" in default_apply.stdout
            and "already_applied=1" in default_apply.stdout
            and "applied_now=0" in default_apply.stdout,
            "Default one-shot ordinary apply failed after adoption",
        )
        self._require(
            self._ledger_rows("ledger_after_baseline_only_apply")
            == ledger_after_adoption,
            "Baseline-only ordinary apply changed the baseline ledger row",
        )
        self.evidence["lifecycle_checks"].append("baseline_only_apply_idempotent")

        repeated_adoption = self._run_migration(
            "repeated_explicit_adoption",
            "adopt",
            self.database_name,
        )
        self._require(
            repeated_adoption.returncode == 0
            and "status=ALREADY_ADOPTED" in repeated_adoption.stdout
            and "evidence_inserted=false" in repeated_adoption.stdout,
            "Repeated explicit adoption did not follow the accepted idempotent contract",
        )
        self._require(
            self._ledger_rows("ledger_after_repeated_adoption")
            == ledger_after_adoption
            and self._adoption_rows("evidence_after_repeated_adoption")
            == evidence_after_adoption,
            "Repeated explicit adoption created conflicting evidence",
        )

        self._psql_success(
            "install_synthetic_guard_drift",
            """
                CREATE OR REPLACE FUNCTION smartcoat_migrations.reject_metadata_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $guard$
                BEGIN
                    RETURN NEW;
                END;
                $guard$
            """,
        )
        drift_rejection = self._run_migration(
            "repeat_adoption_rejects_guard_drift",
            "adopt",
            self.database_name,
        )
        self._require(
            drift_rejection.returncode != 0
            and "guard function semantics" in (
                drift_rejection.stdout + drift_rejection.stderr
            ),
            "Repeated adoption bypassed metadata guard-contract validation",
        )
        self._require(
            self._ledger_rows("ledger_during_guard_drift") == ledger_after_adoption
            and self._adoption_rows("evidence_during_guard_drift")
            == evidence_after_adoption,
            "Guard-contract rejection changed migration evidence",
        )
        self._psql_success(
            "restore_accepted_metadata_guard",
            "CREATE OR REPLACE FUNCTION "
            "smartcoat_migrations.reject_metadata_mutation()\n"
            "RETURNS trigger LANGUAGE plpgsql AS $guard$\n"
            "BEGIN\n"
            "    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;\n"
            "END;\n"
            "$guard$",
        )
        restored_adoption = self._run_migration(
            "repeat_adoption_after_guard_restore",
            "adopt",
            self.database_name,
        )
        self._require(
            restored_adoption.returncode == 0
            and "status=ALREADY_ADOPTED" in restored_adoption.stdout,
            "Accepted repeat-adoption semantics were not restored after drift evidence",
        )
        self.evidence["lifecycle_checks"].append(
            "repeat_adoption_validates_metadata_contract"
        )

        pending_checksum = self._create_pending_migration_fixture()
        pending_apply = self._run_migration("successful_pending_migration_apply")
        self._require(
            pending_apply.returncode == 0
            and "discovered=2" in pending_apply.stdout
            and "already_applied=1" in pending_apply.stdout
            and "applied_now=1" in pending_apply.stdout,
            "Ordinary apply did not execute exactly one pending synthetic migration",
        )
        ledger_after_pending = self._ledger_rows("ledger_after_pending_apply")
        probe_after_pending = self._probe_rows("probe_after_pending_apply")
        self._assert_pending_migration_evidence(
            ledger_after_pending,
            probe_after_pending,
        )
        repeated_apply = self._run_migration("idempotent_apply_after_pending")
        self._require(
            repeated_apply.returncode == 0
            and "discovered=2" in repeated_apply.stdout
            and "already_applied=2" in repeated_apply.stdout
            and "applied_now=0" in repeated_apply.stdout,
            "Repeated ordinary apply did not preserve pending-migration idempotency",
        )
        ledger_after_repeated_apply = self._ledger_rows(
            "ledger_after_idempotent_pending_apply"
        )
        probe_after_repeated_apply = self._probe_rows(
            "probe_after_idempotent_pending_apply"
        )
        self._require(
            ledger_after_repeated_apply == ledger_after_pending
            and probe_after_repeated_apply == probe_after_pending,
            "Idempotent reapply changed the version-2 ledger or probe effect",
        )
        self._assert_pending_migration_evidence(
            ledger_after_repeated_apply,
            probe_after_repeated_apply,
        )
        self.evidence["lifecycle_checks"].append(
            "pending_migration_applied_and_idempotent"
        )

        public_after = self._catalog_snapshot(
            "public_catalog_after",
            PUBLIC_CATALOG_QUERIES,
        )
        public_fingerprint_after = canonical_fingerprint(public_after)
        metadata_final = self._catalog_snapshot(
            "metadata_catalog_final",
            METADATA_CATALOG_QUERIES,
        )
        self._assert_metadata_contract(metadata_final)
        self._require(
            metadata_final == metadata_after_adoption,
            "Final migration metadata definitions differ from accepted adoption state",
        )
        self._require(
            public_after == public_before
            and public_fingerprint_after == public_fingerprint_before,
            "Adoption or apply changed the public application schema",
        )
        self._require(
            self._business_snapshot("business_final") == business_before,
            "Adoption or apply changed application rows",
        )
        final_ledger = self._ledger_rows("ledger_final")
        final_adoption = self._adoption_rows("evidence_final")
        self._assert_adoption_rows(final_ledger[:1], final_adoption, database_oid)
        final_probe = self._probe_rows("probe_final")
        self._assert_pending_migration_evidence(final_ledger, final_probe)
        self._verify_owned_resources()
        self.evidence.update(
            {
                "public_schema_fingerprint_before": public_fingerprint_before,
                "public_schema_fingerprint_after": public_fingerprint_after,
                "business_rows_before": sum(
                    len(rows) for rows in business_before.values()
                ),
                "business_rows_after": sum(
                    len(rows)
                    for rows in self._business_snapshot("business_count_final").values()
                ),
                "ledger_rows": 2,
                "adoption_rows": 1,
                "metadata_tables": 2,
                "pending_migration": {
                    "version": 2,
                    "name": PENDING_MIGRATION_NAME,
                    "sha256": pending_checksum,
                    "applied_exactly_once": True,
                    "probe_schema": PROBE_SCHEMA,
                    "probe_rows": len(final_probe),
                    "idempotent_reapply": True,
                },
            }
        )
        self.evidence["lifecycle_checks"].append(
            "public_schema_and_business_state_unchanged"
        )

    def cleanup(self) -> None:
        if self.cleanup_complete:
            return
        self._require(
            PROJECT_PATTERN.fullmatch(self.project) is not None,
            "Cleanup refused an invalid generated project name",
            IsolationBlocked,
        )
        if self.state_change_attempted:
            self._verify_owned_resources()
            down = self._compose(
                "cleanup_owned_project",
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "10",
                timeout=120,
            )
            self._require(
                down.returncode == 0,
                "Ownership-validated isolated Compose cleanup failed",
                IsolationBlocked,
            )
            for kind in ("container", "network", "volume"):
                self._require(
                    not self._resource_names(kind),
                    f"A disposable {kind} remains after cleanup",
                    IsolationBlocked,
                )
            for kind, name in (
                ("network", self.backend_network),
                ("network", self.edge_network),
                ("volume", self.postgres_volume),
            ):
                listing = self._run(
                    f"verify_removed_{kind}_name",
                    ["docker", kind, "ls", "--format", "{{.Name}}"],
                    record=False,
                )
                self._require(
                    listing.returncode == 0 and name not in listing.stdout.splitlines(),
                    f"Disposable {kind} name remains after cleanup",
                    IsolationBlocked,
                )
        self.cleanup_complete = True

    def verify_inventory_unchanged(self) -> None:
        self._require(
            self.inventory_before is not None,
            "Pre-existing Docker inventory was not captured",
            IsolationBlocked,
        )
        inventory_after = self._inventory()
        self._require(
            inventory_after == self.inventory_before,
            "Pre-existing Docker inventory changed during isolated acceptance",
            IsolationBlocked,
        )
        self.evidence["inventory_after"] = inventory_after.counts()
        self.evidence["cleanup"] = {
            "ownership_validated_before_removal": True,
            "project_resources_remaining": 0,
            "preexisting_inventory_unchanged": True,
        }
        self.inventory_unchanged_verified = True

    def recovery_command_arguments(self) -> list[str]:
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--recover-owned-project",
            str(self.recovery_file.resolve()),
        ]

    def preserve_recovery_files(self, failure: str) -> None:
        self.cleanup_failure_recorded = True
        if not self.temporary_directory.exists():
            return
        self.temporary_directory.chmod(0o700)
        for control_file in (self.environment_file, self.override_file):
            if control_file.exists():
                control_file.chmod(0o600)
        recovery_arguments = self.recovery_command_arguments()
        self._sanitized_arguments(recovery_arguments)
        manifest = {
            "schema_version": 1,
            "project": self.project,
            "failure": failure,
            "control_directory": str(self.temporary_directory.resolve()),
            "environment_file": str(self.environment_file.resolve()),
            "environment_file_sha256": self._sha256_path(self.environment_file)
            if self.environment_file.exists()
            else None,
            "override_file": str(self.override_file.resolve()),
            "override_file_sha256": self._sha256_path(self.override_file)
            if self.override_file.exists()
            else None,
            "compose_file": str(COMPOSE_FILE.resolve()),
            "compose_file_sha256": self._sha256_path(COMPOSE_FILE),
            "backend_network": self.backend_network,
            "edge_network": self.edge_network,
            "postgres_volume": self.postgres_volume,
            "inventory_before": self.inventory_before.__dict__
            if self.inventory_before is not None
            else None,
            "recovery_command_arguments": recovery_arguments,
            "recovery_command": shlex.join(recovery_arguments),
            "safety": (
                "The recovery mode validates the generated project format and every "
                "Compose ownership label before removing only this project's resources."
            ),
        }
        serialized = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        if any(secret in serialized for secret in self.secret_values):
            raise IsolationBlocked("Recovery evidence contains a credential value")
        self.recovery_file.write_text(serialized, encoding="utf-8")
        self.recovery_file.chmod(0o600)
        self.evidence["recovery"] = {
            "required": True,
            "project": self.project,
            "control_directory": str(self.temporary_directory.resolve()),
            "manifest": str(self.recovery_file.resolve()),
            "command_arguments": recovery_arguments,
            "command": shlex.join(recovery_arguments),
            "control_files_preserved": True,
        }

    @classmethod
    def from_recovery_manifest(
        cls, manifest_path: Path
    ) -> "LiveMigrationLifecycleAcceptance":
        resolved_manifest = manifest_path.resolve()
        try:
            manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IsolationBlocked("Recovery manifest is unavailable or invalid") from exc
        project = manifest.get("project", "")
        control_directory = Path(manifest.get("control_directory", "")).resolve()
        environment_file = Path(manifest.get("environment_file", "")).resolve()
        override_file = Path(manifest.get("override_file", "")).resolve()
        cls._require(
            manifest.get("schema_version") == 1
            and PROJECT_PATTERN.fullmatch(project) is not None
            and resolved_manifest.parent == control_directory
            and resolved_manifest == control_directory / "RECOVERY.json"
            and environment_file == control_directory / "synthetic.env"
            and override_file == control_directory / "isolation.compose.yaml",
            "Recovery manifest identity or control paths are unsafe",
            IsolationBlocked,
        )
        cls._require(
            control_directory.stat().st_uid == os.getuid()
            and stat.S_IMODE(control_directory.stat().st_mode) == 0o700,
            "Recovery control directory ownership or mode is unsafe",
            IsolationBlocked,
        )
        for path, checksum_key in (
            (environment_file, "environment_file_sha256"),
            (override_file, "override_file_sha256"),
        ):
            cls._require(
                path.is_file()
                and path.stat().st_uid == os.getuid()
                and stat.S_IMODE(path.stat().st_mode) == 0o600
                and hashlib.sha256(path.read_bytes()).hexdigest()
                == manifest.get(checksum_key),
                "A preserved recovery control file is missing, unsafe, or changed",
                IsolationBlocked,
            )
        cls._require(
            manifest.get("compose_file") == str(COMPOSE_FILE.resolve())
            and manifest.get("compose_file_sha256") == EXPECTED_COMPOSE_SHA256
            and hashlib.sha256(COMPOSE_FILE.read_bytes()).hexdigest()
            == EXPECTED_COMPOSE_SHA256,
            "The accepted Compose file changed before recovery",
            IsolationBlocked,
        )
        cls._require(
            manifest.get("backend_network") == f"{project}-backend"
            and manifest.get("edge_network") == f"{project}-edge-unused"
            and manifest.get("postgres_volume") == f"{project}-postgres-data",
            "Recovery resource names do not derive from the generated project",
            IsolationBlocked,
        )
        inventory_data = manifest.get("inventory_before")
        cls._require(
            isinstance(inventory_data, dict),
            "Recovery manifest lacks the pre-existing Docker inventory",
            IsolationBlocked,
        )
        harness = cls.__new__(cls)
        harness.project = project
        harness.database_name = "recovery-only"
        harness.backend_network = manifest["backend_network"]
        harness.edge_network = manifest["edge_network"]
        harness.postgres_volume = manifest["postgres_volume"]
        harness.secret_values = set()
        harness.temporary_directory = control_directory
        harness.environment_file = environment_file
        harness.override_file = override_file
        harness.migration_fixture_directory = control_directory / "migrations"
        harness.baseline_fixture = (
            harness.migration_fixture_directory / BASELINE_MIGRATION_SOURCE.name
        )
        harness.pending_migration_fixture = (
            harness.migration_fixture_directory / PENDING_MIGRATION_FILENAME
        )
        harness.recovery_file = resolved_manifest
        harness.docker_environment = cls._minimal_docker_environment()
        harness.command_results = []
        harness.evidence = {
            "project": project,
            "recovery": {"required": True, "manifest": str(resolved_manifest)},
        }
        harness.inventory_before = DockerInventory(
            containers=tuple(inventory_data["containers"]),
            images=tuple(inventory_data["images"]),
            networks=tuple(inventory_data["networks"]),
            volumes=tuple(inventory_data["volumes"]),
            projects=inventory_data["projects"],
        )
        harness.postgres_image = None
        harness.migration_image = None
        harness.migration_image_immutable_ref = None
        harness.pending_migration_sha256 = None
        harness.state_change_attempted = True
        harness.cleanup_complete = False
        harness.inventory_unchanged_verified = False
        harness.cleanup_failure_recorded = False
        harness.temporary_files_removed = False
        harness.cleanup_installed = False
        harness.signal_handlers = {}
        return harness

    def remove_temporary_files(self) -> None:
        self._require(
            not self.state_change_attempted
            or (self.cleanup_complete and self.inventory_unchanged_verified),
            "Temporary control files cannot be removed before cleanup and inventory verification",
            IsolationBlocked,
        )
        if self.temporary_directory.exists():
            shutil.rmtree(self.temporary_directory)
        self.temporary_files_removed = True

    def sanitized_evidence(self) -> dict[str, Any]:
        evidence = dict(self.evidence)
        evidence["command_results"] = self.command_results
        serialized = json.dumps(evidence, sort_keys=True)
        if any(secret in serialized for secret in self.secret_values):
            raise ProductContractFailure(
                "Final structured evidence contains a synthetic credential value"
            )
        return evidence


def finalize_harness(
    harness: LiveMigrationLifecycleAcceptance,
    result: str,
    failure: str,
) -> tuple[str, str]:
    cleanup_failure = ""
    try:
        harness.cleanup()
        if harness.inventory_before is not None:
            harness.verify_inventory_unchanged()
    except Exception as exc:
        cleanup_failure = f"{type(exc).__name__}: {exc}"
        result = RESULT_ISOLATION_BLOCKED
        failure = f"Disposable-resource cleanup failed: {cleanup_failure}"
        try:
            harness.preserve_recovery_files(failure)
        except Exception as preservation_error:
            failure += (
                "; recovery evidence preservation also failed: "
                f"{type(preservation_error).__name__}: {preservation_error}"
            )
    finally:
        harness._restore_signal_handlers()
        harness._unregister_cleanup_handler()

    if not cleanup_failure:
        try:
            harness.remove_temporary_files()
        except Exception as exc:
            result = RESULT_ISOLATION_BLOCKED
            failure = (
                "Temporary control-file removal safety check failed: "
                f"{type(exc).__name__}: {exc}"
            )
            harness.preserve_recovery_files(failure)
    return result, failure


def run_focused_regression_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}

    image_harness = LiveMigrationLifecycleAcceptance()
    immutable_id = "sha256:" + ("a" * 64)
    image_harness.migration_image_immutable_ref = immutable_id
    image_harness._assert_immutable_migration_image_binding(immutable_id)
    mutable_rejected = False
    try:
        image_harness._assert_immutable_migration_image_binding(
            MIGRATION_IMAGE_DISCOVERY_REF
        )
    except IsolationBlocked:
        mutable_rejected = True
    if not mutable_rejected:
        raise AssertionError("Mutable migration image reference was not rejected")
    image_harness.remove_temporary_files()
    checks["immutable_image_id_accepted"] = True
    checks["mutable_image_tag_rejected"] = True

    successful = LiveMigrationLifecycleAcceptance()
    successful.environment_file.write_text("synthetic-control\n", encoding="utf-8")
    successful.override_file.write_text("synthetic-override\n", encoding="utf-8")
    successful.environment_file.chmod(0o600)
    successful.override_file.chmod(0o600)
    successful.state_change_attempted = True

    def successful_cleanup() -> None:
        successful.cleanup_complete = True

    def successful_inventory() -> None:
        successful.inventory_unchanged_verified = True

    successful.cleanup = successful_cleanup  # type: ignore[method-assign]
    successful.verify_inventory_unchanged = successful_inventory  # type: ignore[method-assign]
    successful.inventory_before = DockerInventory((), (), (), (), "[]")
    success_result, success_failure = finalize_harness(
        successful, RESULT_PASS, ""
    )
    if (
        success_result != RESULT_PASS
        or success_failure
        or successful.temporary_directory.exists()
    ):
        raise AssertionError("Successful cleanup did not remove temporary controls")
    checks["successful_cleanup_removes_control_files"] = True

    failed = LiveMigrationLifecycleAcceptance()
    failed.environment_file.write_text("synthetic-control\n", encoding="utf-8")
    failed.override_file.write_text("synthetic-override\n", encoding="utf-8")
    failed.environment_file.chmod(0o600)
    failed.override_file.chmod(0o600)
    failed.state_change_attempted = True
    failed.inventory_before = DockerInventory((), (), (), (), "[]")

    def failed_cleanup() -> None:
        raise IsolationBlocked("synthetic cleanup failure")

    failed.cleanup = failed_cleanup  # type: ignore[method-assign]
    failed_result, failed_message = finalize_harness(failed, RESULT_PASS, "")
    if failed_result != RESULT_ISOLATION_BLOCKED:
        raise AssertionError("Cleanup failure did not report BLOCKED_ISOLATION")
    if not failed.temporary_directory.exists() or not failed.recovery_file.is_file():
        raise AssertionError("Cleanup failure did not preserve recovery controls")
    recovery_text = failed.recovery_file.read_text(encoding="utf-8")
    if failed.project not in recovery_text or "--recover-owned-project" not in recovery_text:
        raise AssertionError("Recovery evidence lacks its exact project or command")
    if any(secret in recovery_text for secret in failed.secret_values):
        raise AssertionError("Recovery instructions exposed a credential value")
    if "synthetic cleanup failure" not in failed_message:
        raise AssertionError("Cleanup failure reason was not retained")
    checks["failed_cleanup_preserves_recovery_files"] = True
    checks["failed_cleanup_reports_blocked_isolation"] = True
    checks["recovery_instructions_exclude_credentials"] = True
    shutil.rmtree(failed.temporary_directory)

    ordered = LiveMigrationLifecycleAcceptance()
    ordered.state_change_attempted = True
    events: list[str] = []

    def record_ownership() -> None:
        events.append("ownership_validated")

    def record_down(
        label: str, *arguments: str, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        events.append(f"compose:{label}:{' '.join(arguments)}")
        return subprocess.CompletedProcess(["docker", "compose"], 0, "", "")

    def empty_listing(
        label: str, arguments: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        events.append(f"listing:{label}")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    ordered._verify_owned_resources = record_ownership  # type: ignore[method-assign]
    ordered._compose = record_down  # type: ignore[method-assign]
    ordered._resource_names = lambda _kind: set()  # type: ignore[method-assign]
    ordered._run = empty_listing  # type: ignore[method-assign]
    ordered.cleanup()
    if not events or events[0] != "ownership_validated":
        raise AssertionError("Cleanup attempted removal before ownership validation")
    ordered.inventory_unchanged_verified = True
    ordered.remove_temporary_files()
    checks["ownership_validation_precedes_removal"] = True

    return checks


def recover_owned_project(manifest_path: Path) -> int:
    try:
        harness = LiveMigrationLifecycleAcceptance.from_recovery_manifest(
            manifest_path
        )
        harness._install_cleanup_handlers(recovering=True)
        result, failure = finalize_harness(harness, RESULT_PASS, "")
        evidence = harness.sanitized_evidence()
        if failure:
            evidence["failure"] = failure
        print(json.dumps(evidence, sort_keys=True, indent=2))
        if result == RESULT_PASS:
            print("RECOVERY_COMPLETE")
            return 0
        print(result)
        return 2
    except AcceptanceError as exc:
        print(json.dumps({"failure": str(exc)}, sort_keys=True, indent=2))
        print(RESULT_ISOLATION_BLOCKED)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run M0-R01.4.1 only in a generated disposable synthetic Compose project."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--confirm-disposable-synthetic-run",
        action="store_true",
        help="required explicit authorization flag; no real .env or existing project is used",
    )
    mode.add_argument(
        "--run-focused-regression-checks",
        action="store_true",
        help="run no-Docker regression checks for isolation safeguards",
    )
    mode.add_argument(
        "--recover-owned-project",
        type=Path,
        help="recover only an ownership-validated generated project from RECOVERY.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        print(json.dumps(run_focused_regression_checks(), sort_keys=True, indent=2))
        print("FOCUSED_REGRESSION_CHECKS_PASS")
        return 0
    if args.recover_owned_project is not None:
        return recover_owned_project(args.recover_owned_project)
    if not args.confirm_disposable_synthetic_run:
        print(
            "Explicit --confirm-disposable-synthetic-run is required; no action taken.",
            file=sys.stderr,
        )
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    harness = LiveMigrationLifecycleAcceptance()
    result = RESULT_PASS
    failure = ""
    try:
        harness.preflight()
        harness.lifecycle()
    except AcceptanceError as exc:
        result = exc.result
        failure = str(exc)
    except KeyboardInterrupt:
        result = RESULT_ISOLATION_BLOCKED
        failure = "Acceptance run was interrupted"
    except Exception as exc:  # fail closed on an unclassified harness defect
        result = RESULT_PRODUCT_FAILURE
        failure = f"Unclassified harness failure: {type(exc).__name__}: {exc}"
    finally:
        result, failure = finalize_harness(harness, result, failure)

    try:
        evidence = harness.sanitized_evidence()
    except AcceptanceError as evidence_error:
        result = evidence_error.result
        failure = str(evidence_error)
        evidence = {
            "project": harness.project,
            "failure": failure,
            "structured_evidence_rejected": True,
        }
    if failure:
        evidence["failure"] = failure
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print(result)
    return 0 if result == RESULT_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
