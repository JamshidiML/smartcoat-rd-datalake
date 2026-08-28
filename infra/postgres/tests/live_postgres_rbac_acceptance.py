#!/usr/bin/env python3
"""Opt-in disposable live acceptance for M0-R02 PostgreSQL RBAC.

The harness uses only already-present local images.  It creates two isolated
PostgreSQL clusters with no published ports, verifies fresh and upgraded
migration paths, exercises each runtime login with direct SQL, and removes only
resources carrying its generated ownership label.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
POSTGRES_ROOT = ROOT / "infra/postgres"
MIGRATIONS_ROOT = POSTGRES_ROOT / "migrations"
INIT_SQL = POSTGRES_ROOT / "init.sql"
POSTGRES_IMAGE = "postgres:17.6-alpine"
MIGRATION_IMAGE = "smartcoat-rd-datalake-api:latest"
OWNERSHIP_LABEL = "com.smartcoat.acceptance.r02"
PROJECT_PATTERN = re.compile(r"^m0r02-[0-9a-f]{12}-(fresh|upgraded)$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

PASS = "PASS_R02_R04_COMPATIBILITY_PROPOSAL"
FAIL = "FAIL_M0_R02_PRODUCT_CONTRACT"
BLOCKED_ISOLATION = "BLOCKED_M0_R02_ISOLATION"
BLOCKED_ENVIRONMENT = "BLOCKED_M0_R02_ENVIRONMENT"

RUNTIME_ROLES = {
    "smartcoat_ingestion": "POSTGRES_INGESTION_PASSWORD",
    "smartcoat_ocr": "POSTGRES_OCR_PASSWORD",
    "smartcoat_review": "POSTGRES_REVIEW_PASSWORD",
    "smartcoat_backup": "POSTGRES_BACKUP_PASSWORD",
}

sys.path.insert(0, str(POSTGRES_ROOT))
import rbac_contract  # noqa: E402


class AcceptanceError(RuntimeError):
    result = FAIL


class IsolationError(AcceptanceError):
    result = BLOCKED_ISOLATION


class EnvironmentError(AcceptanceError):
    result = BLOCKED_ENVIRONMENT


@dataclass(frozen=True)
class Inventory:
    containers: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    images: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        return {
            "containers": len(self.containers),
            "networks": len(self.networks),
            "volumes": len(self.volumes),
            "images": len(self.images),
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def require(condition: bool, message: str, error: type[AcceptanceError] = AcceptanceError) -> None:
    if not condition:
        raise error(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docker_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_CONFIG_HOME", "TMPDIR")
        if name in os.environ
    }
    environment["DOCKER_CLI_HINTS"] = "false"
    return environment


def inventory(environment: dict[str, str]) -> Inventory:
    def lines(arguments: list[str]) -> tuple[str, ...]:
        completed = subprocess.run(
            arguments,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            raise EnvironmentError(f"Docker inventory failed for {arguments[1]}")
        return tuple(sorted(line for line in completed.stdout.splitlines() if line))

    return Inventory(
        containers=lines(["docker", "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"]),
        networks=lines(["docker", "network", "ls", "--no-trunc", "--format", "{{.ID}}"]),
        volumes=lines(["docker", "volume", "ls", "--format", "{{.Name}}"]),
        images=lines(["docker", "image", "ls", "--no-trunc", "--format", "{{.ID}}"]),
    )


class Scenario:
    def __init__(self, mode: str, shared_inventory: Inventory) -> None:
        token = secrets.token_hex(6)
        self.mode = mode
        self.project = f"m0r02-{token}-{mode}"
        self.database = f"m0r02_{token}_{mode}"
        self.admin = f"m0r02_admin_{token}"
        self.network = f"{self.project}-backend"
        self.volume = f"{self.project}-postgres"
        self.container = f"{self.project}-postgres"
        self.control_directory = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.baseline_directory = self.control_directory / "baseline"
        self.full_directory = self.control_directory / "full"
        self.inventory_before = shared_inventory
        self.postgres_image_id = ""
        self.migration_image_id = ""
        self.state_changed = False
        self.cleaned = False
        self.finalized = False
        self.commands: list[dict[str, Any]] = []
        self.checks: list[str] = []
        self.ids = {name: str(uuid.uuid4()) for name in (
            "upload", "bronze_original", "bronze_manifest", "job", "run",
            "draft", "decision", "verified", "audit_ingestion", "audit_ocr",
            "audit_review", "request_ingestion", "request_ocr", "request_review",
            "upgrade_upload",
        )}
        self.passwords = {
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "POSTGRES_APP_PASSWORD": secrets.token_hex(24),
            **{environment_name: secrets.token_hex(24) for environment_name in RUNTIME_ROLES.values()},
        }
        self.secret_values = set(self.passwords.values())
        self.environment = docker_environment()

    def _run(
        self,
        label: str,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: int = 120,
        record: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        effective_environment = dict(self.environment)
        if environment:
            effective_environment.update(environment)
        try:
            completed = subprocess.run(
                arguments,
                cwd=ROOT,
                env=effective_environment,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentError(f"Command environment failed during {label}") from exc
        output = completed.stdout + completed.stderr
        require(
            not any(secret in output for secret in self.secret_values),
            f"Synthetic secret appeared in command output during {label}",
            IsolationError,
        )
        if record:
            self.commands.append(
                {
                    "label": label,
                    "exit": completed.returncode,
                    "stdout_characters": len(completed.stdout),
                    "stderr_characters": len(completed.stderr),
                }
            )
        return completed

    def _success(self, label: str, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        completed = self._run(label, arguments, **kwargs)
        require(completed.returncode == 0, f"Command failed during {label}")
        return completed

    def prepare(self) -> None:
        require(PROJECT_PATTERN.fullmatch(self.project) is not None, "Unsafe generated project name", IsolationError)
        require(shutil.which("docker") is not None, "Docker CLI is unavailable", EnvironmentError)
        for path in (INIT_SQL, POSTGRES_ROOT / "migrate.py", POSTGRES_ROOT / "bootstrap_contract.py"):
            require(path.is_file(), f"Required implementation path is missing: {path.name}", EnvironmentError)
        discovered = sorted(MIGRATIONS_ROOT.glob("*.sql"))
        require(
            [path.name for path in discovered] == [
                "0001__validate_bootstrap_prerequisites.sql",
                "0002__separate_runtime_roles.sql",
                "0006__grant_review_audit_evidence_read.sql",
            ],
            "Compatibility acceptance requires the exact version-1/version-2/version-6 migration plan",
        )
        self.baseline_directory.mkdir(mode=0o755)
        self.full_directory.mkdir(mode=0o755)
        for destination, sources in (
            (self.baseline_directory, discovered[:1]),
            (self.full_directory, discovered),
        ):
            for source in sources:
                target = destination / source.name
                target.write_bytes(source.read_bytes())
                target.chmod(0o444)
        self.control_directory.chmod(0o700)
        for directory in (self.baseline_directory, self.full_directory):
            require(directory.stat().st_uid == os.getuid(), "Fixture ownership is unsafe", IsolationError)
            require(stat.S_IMODE(directory.stat().st_mode) == 0o755, "Fixture mode is unsafe", IsolationError)

        identities: dict[str, str] = {}
        for reference in (POSTGRES_IMAGE, MIGRATION_IMAGE):
            completed = self._success(
                f"inspect_image:{reference}",
                ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                record=False,
            )
            image_id = completed.stdout.strip()
            require(IMAGE_ID_PATTERN.fullmatch(image_id) is not None, f"Local image unavailable: {reference}", EnvironmentError)
            identities[reference] = image_id
        self.postgres_image_id = identities[POSTGRES_IMAGE]
        self.migration_image_id = identities[MIGRATION_IMAGE]
        self.checks.append("local_images_resolved_to_immutable_ids")

    def install_cleanup(self) -> None:
        atexit.register(self._atexit_cleanup)
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, self._signal)

    def _signal(self, signal_number: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signal_number}")

    def _atexit_cleanup(self) -> None:
        if self.state_changed and not self.cleaned:
            try:
                self.cleanup()
            except Exception:
                pass

    def _label(self, resource: str, name: str) -> str:
        labels = ".Config.Labels" if resource == "container" else ".Labels"
        completed = self._success(
            f"inspect_owner:{resource}",
            ["docker", resource, "inspect", "--format", f"{{{{index {labels} \"{OWNERSHIP_LABEL}\"}}}}", name],
            record=False,
        )
        return completed.stdout.strip()

    def start(self) -> None:
        self.install_cleanup()
        self.state_changed = True
        self._success(
            "create_internal_network",
            ["docker", "network", "create", "--internal", "--label", f"{OWNERSHIP_LABEL}={self.project}", self.network],
        )
        self._success(
            "create_owned_volume",
            ["docker", "volume", "create", "--label", f"{OWNERSHIP_LABEL}={self.project}", self.volume],
        )
        run_environment = {
            "POSTGRES_DB": self.database,
            "POSTGRES_USER": self.admin,
            "POSTGRES_PASSWORD": self.passwords["POSTGRES_PASSWORD"],
            "POSTGRES_APP_USER": "smartcoat_app",
            "POSTGRES_APP_PASSWORD": self.passwords["POSTGRES_APP_PASSWORD"],
        }
        arguments = [
            "docker", "run", "--detach", "--pull", "never",
            "--name", self.container,
            "--label", f"{OWNERSHIP_LABEL}={self.project}",
            "--network", self.network,
            "--network-alias", "postgres",
            "--volume", f"{self.volume}:/var/lib/postgresql/data",
            "--volume", f"{INIT_SQL.resolve()}:/docker-entrypoint-initdb.d/001-init.sql:ro",
        ]
        for name in run_environment:
            arguments.extend(["--env", name])
        arguments.append(self.postgres_image_id)
        self._success("start_postgres", arguments, environment=run_environment)
        require(self._label("container", self.container) == self.project, "PostgreSQL ownership label mismatch", IsolationError)
        require(self._label("network", self.network) == self.project, "Network ownership label mismatch", IsolationError)
        require(self._label("volume", self.volume) == self.project, "Volume ownership label mismatch", IsolationError)
        ports = self._success(
            "inspect_no_ports",
            ["docker", "container", "inspect", "--format", "{{json .HostConfig.PortBindings}}", self.container],
            record=False,
        ).stdout.strip()
        require(ports in ("null", "{}"), "Disposable PostgreSQL published a host port", IsolationError)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = self._run(
                "wait_postgres",
                ["docker", "exec", self.container, "pg_isready", "--username", self.admin, "--dbname", self.database],
                record=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise EnvironmentError("Disposable PostgreSQL did not become ready")
        self.checks.extend(["internal_network_only", "no_published_ports", "bootstrap_ready"])

    def _migration(self, label: str, directory: Path, *command: str) -> subprocess.CompletedProcess[str]:
        migration_url = (
            f"postgresql://{self.admin}:{self.passwords['POSTGRES_PASSWORD']}"
            f"@postgres:5432/{self.database}"
        )
        environment = {"MIGRATION_DATABASE_URL": migration_url}
        self.secret_values.add(migration_url)
        return self._run(
            label,
            [
                "docker", "run", "--rm", "--pull", "never",
                "--network", self.network,
                "--env", "MIGRATION_DATABASE_URL",
                "--volume", f"{POSTGRES_ROOT.resolve()}:/opt/smartcoat-postgres:ro",
                "--volume", f"{directory.resolve()}:/opt/smartcoat-postgres/migrations:ro",
                "--entrypoint", "python",
                self.migration_image_id,
                "/opt/smartcoat-postgres/migrate.py",
                *command,
            ],
            environment=environment,
            timeout=180,
        )

    def migrate(self) -> None:
        initial_directory = self.full_directory if self.mode == "fresh" else self.baseline_directory
        upgraded_snapshot = ""
        if self.mode == "upgraded":
            upgraded_seed = self._psql(
                "seed_upgraded_volume_through_legacy_role",
                "smartcoat_app",
                self.passwords["POSTGRES_APP_PASSWORD"],
                f"""
                INSERT INTO users(user_id,display_name,email,role,active,created_at_utc)
                VALUES ('usr_upgrade_{self.project.split('-')[1]}','Synthetic Existing User',
                  'upgrade-{self.project}@invalid.example','UPLOADER',true,now());
                INSERT INTO uploads(ingestion_id,department,uploader_user_id,uploader_display_name,
                  uploaded_at_utc,original_filename,stored_object_key,manifest_object_key,
                  detected_mime_type,declared_file_type,document_category,context_note,byte_size,
                  source_sha256,source_channel,state)
                VALUES ('{self.ids['upgrade_upload']}','RND','usr_upgrade_{self.project.split('-')[1]}',
                  'Synthetic Existing User',now(),'existing.png',
                  'existing/{self.ids['upgrade_upload']}/original',
                  'existing/{self.ids['upgrade_upload']}/manifest','image/png','PHOTO','LAB_NOTE',
                  'Synthetic upgraded-volume preservation row',100,'{'b' * 64}','WEB_UPLOAD','RECEIVED');
                """,
            )
            require(upgraded_seed.returncode == 0, "Legacy upgraded-volume seed failed")
            upgraded_snapshot = self._admin_value(
                "business_snapshot_before_upgrade",
                """
                SELECT json_build_object(
                  'users',(SELECT json_agg(row_to_json(u) ORDER BY user_id) FROM users u),
                  'uploads',(SELECT json_agg(row_to_json(u) ORDER BY ingestion_id) FROM uploads u),
                  'bronze_objects',(SELECT count(*) FROM bronze_objects),
                  'ocr_jobs',(SELECT count(*) FROM ocr_jobs),
                  'ocr_runs',(SELECT count(*) FROM ocr_runs),
                  'silver_drafts',(SELECT count(*) FROM silver_drafts),
                  'review_decisions',(SELECT count(*) FROM review_decisions),
                  'silver_verified_records',(SELECT count(*) FROM silver_verified_records),
                  'audit_events',(SELECT count(*) FROM audit_events)
                )::text
                """,
            )
        adoption = self._migration("explicit_adoption", initial_directory, "adopt", self.database)
        require(adoption.returncode == 0 and "status=ADOPTED" in adoption.stdout, "Explicit synthetic adoption failed")
        if self.mode == "upgraded":
            pre_upgrade = self._admin_value("legacy_login_before_upgrade", "SELECT rolcanlogin::text FROM pg_roles WHERE rolname = 'smartcoat_app'")
            require(pre_upgrade == "true", "Upgraded-volume fixture did not begin with the legacy login")
            self.checks.append("upgraded_volume_started_from_accepted_version_1")
        applied = self._migration("ordinary_apply", self.full_directory, "apply")
        require(
            applied.returncode == 0 and "already_applied=1 applied_now=2" in applied.stdout,
            "Migrations 0002 and 0006 were not each applied exactly once",
        )
        if self.mode == "upgraded":
            require(
                self._admin_value(
                    "business_snapshot_after_upgrade",
                    """
                    SELECT json_build_object(
                      'users',(SELECT json_agg(row_to_json(u) ORDER BY user_id) FROM users u),
                      'uploads',(SELECT json_agg(row_to_json(u) ORDER BY ingestion_id) FROM uploads u),
                      'bronze_objects',(SELECT count(*) FROM bronze_objects),
                      'ocr_jobs',(SELECT count(*) FROM ocr_jobs),
                      'ocr_runs',(SELECT count(*) FROM ocr_runs),
                      'silver_drafts',(SELECT count(*) FROM silver_drafts),
                      'review_decisions',(SELECT count(*) FROM review_decisions),
                      'silver_verified_records',(SELECT count(*) FROM silver_verified_records),
                      'audit_events',(SELECT count(*) FROM audit_events)
                    )::text
                    """,
                ) == upgraded_snapshot,
                "Migration 0002 changed pre-existing application rows",
            )
            legacy_login = self._psql(
                "legacy_login_rejected_after_upgrade",
                "smartcoat_app",
                self.passwords["POSTGRES_APP_PASSWORD"],
                "SELECT 1",
            )
            require(legacy_login.returncode != 0, "Legacy shared credential still authenticates")
            self.checks.extend(["upgraded_rows_preserved_exactly", "legacy_credential_rejected"])
        repeated = self._migration("idempotent_apply", self.full_directory, "apply")
        require(
            repeated.returncode == 0 and "already_applied=3 applied_now=0" in repeated.stdout,
            "Migration reapplication was not idempotent",
        )
        self.checks.extend([
            "explicit_adoption",
            "migration_0002_applied_once",
            "migration_0006_applied_once",
            "migration_reapply_idempotent",
        ])

    def provision(self) -> None:
        admin_url = (
            f"postgresql://{self.admin}:{self.passwords['POSTGRES_PASSWORD']}"
            f"@postgres:5432/{self.database}"
        )
        self.secret_values.add(admin_url)
        environment = {"POSTGRES_ROLE_ADMIN_URL": admin_url}
        environment.update({name: self.passwords[name] for name in RUNTIME_ROLES.values()})
        arguments = [
            "docker", "run", "--rm", "--pull", "never",
            "--network", self.network,
            "--volume", f"{POSTGRES_ROOT.resolve()}:/opt/smartcoat-postgres:ro",
            "--entrypoint", "python",
        ]
        for name in environment:
            arguments.extend(["--env", name])
        arguments.extend([self.migration_image_id, "/opt/smartcoat-postgres/provision_runtime_roles.py"])
        completed = self._run("provision_runtime_credentials", arguments, environment=environment, timeout=180)
        require(
            completed.returncode == 0
            and "roles=4 credentials_updated=4" in completed.stdout,
            "Runtime credential provisioner failed",
        )
        self.checks.append("credential_provisioning_boundary_passed")

    def _psql(self, label: str, role: str, password: str, sql_text: str) -> subprocess.CompletedProcess[str]:
        payload = sql_text.strip()
        if not payload.endswith(";"):
            payload += ";"
        return self._run(
            label,
            [
                "docker", "exec", "--interactive", "--env", "PGPASSWORD", self.container,
                "psql", "-X", "--set", "ON_ERROR_STOP=1", "--no-align", "--tuples-only",
                "--host", "127.0.0.1", "--username", role, "--dbname", self.database,
            ],
            environment={"PGPASSWORD": password},
            input_text=payload + "\n",
        )

    def _admin_value(self, label: str, sql_text: str) -> str:
        completed = self._psql(label, self.admin, self.passwords["POSTGRES_PASSWORD"], sql_text)
        require(completed.returncode == 0, f"Administrative evidence query failed: {label}")
        return completed.stdout.strip()

    @staticmethod
    def _json_value(label: str, value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(
                f"PostgreSQL JSON evidence was invalid during {label}: characters={len(value)}"
            ) from exc

    def _role_sql(self, label: str, role: str, sql_text: str, *, allowed: bool, marker: str = "permission denied") -> None:
        password_name = RUNTIME_ROLES[role]
        completed = self._psql(label, role, self.passwords[password_name], sql_text)
        combined = (completed.stdout + completed.stderr).lower()
        if allowed:
            require(completed.returncode == 0, f"Allowed SQL was denied: {label}")
        else:
            require(
                completed.returncode != 0 and marker.lower() in combined,
                f"Forbidden SQL did not fail at the expected permission boundary: {label}",
            )
        self.checks.append(label)

    def verify_catalog(self) -> None:
        roles = self._admin_value(
            "catalog_role_attributes",
            """
            SELECT json_agg(row_to_json(r) ORDER BY rolname)::text
            FROM (
                SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                       rolcanlogin, rolreplication, rolbypassrls
                FROM pg_roles
                WHERE rolname IN ('smartcoat_ingestion','smartcoat_ocr','smartcoat_review','smartcoat_backup')
            ) r
            """,
        )
        role_rows = self._json_value("catalog_role_attributes", roles)
        require(
            len(role_rows) == 4
            and all(
                row["rolcanlogin"] is True
                and row["rolinherit"] is True
                and not any(row[key] for key in (
                    "rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls"
                ))
                for row in role_rows
            ),
            "Runtime role attributes are not least privilege",
        )
        require(
            self._admin_value(
                "catalog_memberships",
                """
                SELECT count(*) FROM pg_auth_members m
                JOIN pg_roles granted ON granted.oid = m.roleid
                JOIN pg_roles member ON member.oid = m.member
                WHERE granted.rolname LIKE 'smartcoat_%' OR member.rolname LIKE 'smartcoat_%'
                """,
            ) == "0",
            "Runtime roles unexpectedly inherit a membership",
        )
        table_privileges = self._json_value(
            "catalog_table_privileges",
            self._admin_value(
                "catalog_table_privileges",
                """
                SELECT COALESCE(json_agg(row_to_json(p) ORDER BY grantee,table_schema,table_name,privilege_type),'[]')::text
                FROM (
                    SELECT grantee,table_schema,table_name,privilege_type
                    FROM information_schema.table_privileges
                    WHERE grantee IN ('smartcoat_ingestion','smartcoat_ocr','smartcoat_review','smartcoat_backup')
                      AND table_schema IN ('public','smartcoat_migrations')
                ) p
                """,
            ),
        )
        observed_table_privileges = {
            (row["grantee"], row["table_schema"], row["table_name"], row["privilege_type"])
            for row in table_privileges
        }
        expected_table_privileges = {
            (role, "public", table, privilege)
            for role, table, privilege in rbac_contract.TABLE_PRIVILEGES
        } | {
            (role, "smartcoat_migrations", table, privilege)
            for role, table, privilege in rbac_contract.MIGRATION_METADATA_PRIVILEGES
        }
        require(
            observed_table_privileges == expected_table_privileges,
            "Catalog table grants differ from the exact M0-R02 matrix",
        )
        column_privileges = self._json_value(
            "catalog_column_privileges",
            self._admin_value(
                "catalog_column_privileges",
                """
                SELECT COALESCE(json_agg(row_to_json(p) ORDER BY grantee,table_name,column_name),'[]')::text
                FROM (
                    SELECT grantee,table_name,column_name
                    FROM information_schema.column_privileges
                    WHERE table_schema='public' AND privilege_type='UPDATE'
                      AND grantee IN ('smartcoat_ingestion','smartcoat_ocr','smartcoat_review','smartcoat_backup')
                ) p
                """,
            ),
        )
        require(
            {
                (row["grantee"], row["table_name"], row["column_name"])
                for row in column_privileges
            } == rbac_contract.COLUMN_UPDATE_PRIVILEGES,
            "Catalog column grants differ from the exact M0-R02 matrix",
        )
        select_column_privileges = self._json_value(
            "catalog_review_audit_column_privileges",
            self._admin_value(
                "catalog_review_audit_column_privileges",
                """
                SELECT COALESCE(json_agg(row_to_json(p) ORDER BY grantee,table_name,column_name),'[]')::text
                FROM (
                    SELECT grantee,table_name,column_name
                    FROM information_schema.column_privileges
                    WHERE table_schema='public' AND table_name='audit_events'
                      AND privilege_type='SELECT' AND grantee='smartcoat_review'
                ) p
                """,
            ),
        )
        require(
            {
                (row["grantee"], row["table_name"], row["column_name"])
                for row in select_column_privileges
            } == rbac_contract.COLUMN_SELECT_PRIVILEGES,
            "Catalog review audit column grants differ from the compatibility contract",
        )
        require(
            self._admin_value(
                "catalog_legacy_role",
                """
                SELECT json_build_array(
                    rolcanlogin,
                    has_table_privilege('smartcoat_app','public.uploads','SELECT'),
                    has_table_privilege('smartcoat_app','public.uploads','INSERT'),
                    has_schema_privilege('smartcoat_app','public','CREATE')
                )::text FROM pg_roles WHERE rolname='smartcoat_app'
                """,
            ) == "[false, false, false, false]",
            "Legacy shared role retains authority",
        )
        require(
            self._admin_value(
                "catalog_ledger",
                "SELECT json_agg(version ORDER BY version)::text FROM smartcoat_migrations.applied_migrations",
            ) == "[1, 2, 6]",
            "Migration ledger is not the exact accepted prefix",
        )
        require(
            self._admin_value(
                "catalog_append_only_triggers",
                """
                SELECT count(*) FROM pg_trigger
                WHERE tgname IN ('bronze_objects_append_only','verified_records_append_only',
                                 'review_decisions_append_only','audit_events_append_only')
                  AND tgenabled='O' AND NOT tgisinternal
                """,
            ) == "4",
            "Application append-only trigger catalog changed",
        )
        require(
            self._admin_value(
                "catalog_migration_guards",
                """
                SELECT count(*) FROM pg_trigger
                WHERE tgname IN ('applied_migrations_append_only','adoption_decisions_append_only')
                  AND tgenabled='O' AND NOT tgisinternal
                """,
            ) == "2",
            "Migration metadata append-only guards changed",
        )
        self.checks.extend([
            "exact_role_attributes", "no_role_memberships", "exact_table_grant_matrix",
            "exact_column_grant_matrix", "exact_review_audit_column_grant_matrix",
            "legacy_login_disabled",
            "exact_ledger_prefix", "application_append_only_triggers_preserved",
            "migration_metadata_guards_preserved",
        ])

    def positive_sql(self) -> None:
        i = self.ids
        digest = "a" * 64
        self._role_sql(
            "positive_ingestion_workflow",
            "smartcoat_ingestion",
            f"""
            BEGIN;
            INSERT INTO users(user_id,display_name,email,role,active,created_at_utc)
            VALUES ('usr_rbac_{self.project.split('-')[1]}','Synthetic Uploader','synthetic-{self.project}@invalid.example','UPLOADER',true,now());
            INSERT INTO users(user_id,display_name,email,role,active,created_at_utc)
            VALUES ('usr_review_{self.project.split('-')[1]}','Synthetic Reviewer','review-{self.project}@invalid.example','REVIEWER',true,now());
            INSERT INTO uploads(ingestion_id,department,uploader_user_id,uploader_display_name,uploaded_at_utc,
              original_filename,stored_object_key,manifest_object_key,detected_mime_type,declared_file_type,
              document_category,context_note,byte_size,source_sha256,source_channel,state)
            VALUES ('{i['upload']}','RND','usr_rbac_{self.project.split('-')[1]}','Synthetic Uploader',now(),
              'synthetic.png','synthetic/{i['upload']}/original','synthetic/{i['upload']}/manifest',
              'image/png','PHOTO','LAB_NOTE','Synthetic M0-R02 acceptance only',100,'{digest}','WEB_UPLOAD','OCR_QUEUED');
            INSERT INTO bronze_objects(bronze_object_id,ingestion_id,bucket_name,object_key,object_kind,sha256,
              object_version_id,retention_mode,retain_until_utc,created_at_utc)
            VALUES ('{i['bronze_original']}','{i['upload']}','synthetic','original-{i['upload']}','ORIGINAL','{digest}',
              'synthetic-version-original','COMPLIANCE',now()+interval '1 day',now()),
              ('{i['bronze_manifest']}','{i['upload']}','synthetic','manifest-{i['upload']}','MANIFEST','{digest}',
              'synthetic-version-manifest','COMPLIANCE',now()+interval '1 day',now());
            INSERT INTO ocr_jobs(ocr_job_id,ingestion_id,status,queued_at_utc,attempt_count)
            VALUES ('{i['job']}','{i['upload']}','QUEUED',now(),0);
            INSERT INTO audit_events(event_id,occurred_at_utc,system_actor,entity_type,entity_id,event_type,
              request_id,details_json) VALUES ('{i['audit_ingestion']}',now(),'system','UPLOAD','{i['upload']}',
              'SYNTHETIC_INGESTION','{i['request_ingestion']}','{{}}');
            COMMIT;
            """,
            allowed=True,
        )
        self._role_sql(
            "positive_ocr_workflow",
            "smartcoat_ocr",
            f"""
            BEGIN;
            UPDATE ocr_jobs SET status='RUNNING',started_at_utc=now(),attempt_count=attempt_count+1
              WHERE ocr_job_id='{i['job']}';
            INSERT INTO ocr_runs(ocr_run_id,ocr_job_id,ingestion_id,engine,engine_version,configuration_json,
              source_sha256,status,started_at_utc) VALUES ('{i['run']}','{i['job']}','{i['upload']}',
              'paddleocr','synthetic','{{}}','{digest}','RUNNING',now());
            UPDATE ocr_runs SET status='COMPLETED',raw_output_sha256='{digest}',
              raw_artifact_key='synthetic-artifact',completed_at_utc=now() WHERE ocr_run_id='{i['run']}';
            UPDATE ocr_jobs SET status='COMPLETED',completed_at_utc=now() WHERE ocr_job_id='{i['job']}';
            INSERT INTO silver_drafts(silver_draft_id,ingestion_id,source_sha256,ocr_run_id,status,
              extracted_text,text_blocks_json,source_file_type,document_category,extraction_engine,
              extraction_engine_version,created_at_utc) VALUES ('{i['draft']}','{i['upload']}','{digest}',
              '{i['run']}','DRAFT_UNVERIFIED','synthetic text','[]','PHOTO','LAB_NOTE','paddleocr','synthetic',now());
            INSERT INTO audit_events(event_id,occurred_at_utc,system_actor,entity_type,entity_id,event_type,
              request_id,details_json) VALUES ('{i['audit_ocr']}',now(),'ocr-worker','UPLOAD','{i['upload']}',
              'SYNTHETIC_OCR','{i['request_ocr']}','{{}}');
            UPDATE uploads SET state='SILVER_DRAFT_READY' WHERE ingestion_id='{i['upload']}';
            COMMIT;
            """,
            allowed=True,
        )
        self._role_sql(
            "positive_review_workflow",
            "smartcoat_review",
            f"""
            BEGIN;
            UPDATE uploads SET state='UNDER_HUMAN_REVIEW' WHERE ingestion_id='{i['upload']}';
            INSERT INTO review_decisions(review_decision_id,silver_draft_id,ingestion_id,reviewer_user_id,
              reviewed_at_utc,decision,explicit_confirmation,correction_summary,self_review_detected,
              solo_exception_applied) VALUES ('{i['decision']}','{i['draft']}','{i['upload']}',
              'usr_review_{self.project.split('-')[1]}',now(),'APPROVED_NO_CHANGES',true,'',false,false);
            UPDATE silver_drafts SET status='REVIEWED' WHERE silver_draft_id='{i['draft']}';
            INSERT INTO silver_verified_records(silver_record_id,silver_revision,ingestion_id,source_sha256,
              status,verified_text,reviewer_user_id,reviewed_at_utc,review_decision,correction_summary,
              source_object_key,ocr_artifact_key,review_decision_id) VALUES ('{i['verified']}',1,'{i['upload']}',
              '{digest}','VERIFIED','synthetic text','usr_review_{self.project.split('-')[1]}',now(),
              'APPROVED_NO_CHANGES','','synthetic/{i['upload']}/original','synthetic-artifact','{i['decision']}');
            INSERT INTO audit_events(event_id,occurred_at_utc,actor_user_id,entity_type,entity_id,event_type,
              request_id,details_json) VALUES ('{i['audit_review']}',now(),'usr_review_{self.project.split('-')[1]}',
              'UPLOAD','{i['upload']}','SYNTHETIC_REVIEW','{i['request_review']}','{{}}');
            UPDATE uploads SET state='VERIFIED' WHERE ingestion_id='{i['upload']}';
            COMMIT;
            """,
            allowed=True,
        )
        self._role_sql(
            "positive_review_retry_audit_evidence_read",
            "smartcoat_review",
            """
            SELECT
                count(*) FILTER (
                    WHERE entity_type='SILVER_DRAFT'
                      AND event_type='HUMAN_REVIEW_RECORDED'
                      AND details_json->>'review_request_sha256' IS NOT NULL
                ),
                count(*) FILTER (
                    WHERE entity_type='UPLOAD'
                      AND event_type='UPLOAD_STATE_CHANGED'
                      AND new_state='VERIFIED'
                      AND details_json->>'review_request_sha256' IS NOT NULL
                )
            FROM audit_events
            """,
            allowed=True,
        )
        self._role_sql(
            "positive_backup_reads_all_evidence",
            "smartcoat_backup",
            """
            SELECT count(*) FROM users;
            SELECT count(*) FROM uploads;
            SELECT count(*) FROM bronze_objects;
            SELECT count(*) FROM ocr_jobs;
            SELECT count(*) FROM ocr_runs;
            SELECT count(*) FROM silver_drafts;
            SELECT count(*) FROM review_decisions;
            SELECT count(*) FROM silver_verified_records;
            SELECT count(*) FROM audit_events;
            SELECT count(*) FROM smartcoat_migrations.applied_migrations;
            SELECT count(*) FROM smartcoat_migrations.adoption_decisions;
            """,
            allowed=True,
        )

    def negative_sql(self) -> None:
        i = self.ids
        expected_uploads = 2 if self.mode == "upgraded" else 1
        denials = (
            ("deny_ingestion_review", "smartcoat_ingestion", "INSERT INTO review_decisions DEFAULT VALUES"),
            ("deny_ingestion_verified", "smartcoat_ingestion", "INSERT INTO silver_verified_records DEFAULT VALUES"),
            ("deny_ocr_review", "smartcoat_ocr", "INSERT INTO review_decisions DEFAULT VALUES"),
            ("deny_ocr_verified", "smartcoat_ocr", "INSERT INTO silver_verified_records DEFAULT VALUES"),
            ("deny_review_bronze", "smartcoat_review", "INSERT INTO bronze_objects DEFAULT VALUES"),
            ("deny_review_ungranted_audit_column", "smartcoat_review", "SELECT event_id FROM audit_events"),
            ("deny_review_audit_update", "smartcoat_review", "UPDATE audit_events SET event_type='forbidden'"),
            ("deny_review_audit_delete", "smartcoat_review", "DELETE FROM audit_events"),
            ("deny_backup_write", "smartcoat_backup", f"UPDATE uploads SET state='REJECTED' WHERE ingestion_id='{i['upload']}'"),
        )
        for label, role, sql_text in denials:
            self._role_sql(label, role, sql_text, allowed=False)
        for role in RUNTIME_ROLES:
            self._role_sql(
                f"deny_{role}_migration_metadata_write",
                role,
                "INSERT INTO smartcoat_migrations.applied_migrations(version,name,sha256) VALUES (99,'forbidden',repeat('a',64))",
                allowed=False,
            )
            self._role_sql(
                f"deny_{role}_schema_create",
                role,
                "CREATE TABLE public.forbidden_rbac_probe(value integer)",
                allowed=False,
            )
            self._role_sql(
                f"deny_{role}_temporary_object",
                role,
                "CREATE TEMPORARY TABLE forbidden_rbac_temp(value integer)",
                allowed=False,
                marker="permission denied to create temporary tables",
            )
        append_only = self._psql(
            "admin_append_only_trigger_proof",
            self.admin,
            self.passwords["POSTGRES_PASSWORD"],
            f"UPDATE bronze_objects SET object_key='forbidden' WHERE bronze_object_id='{i['bronze_original']}'",
        )
        require(
            append_only.returncode != 0 and "append-only" in (append_only.stdout + append_only.stderr),
            "Append-only trigger did not reject an administrative direct SQL mutation",
        )
        require(
            self._admin_value(
                "final_positive_row_counts",
                """
                SELECT json_build_array(
                  (SELECT count(*) FROM uploads),
                  (SELECT count(*) FROM bronze_objects),
                  (SELECT count(*) FROM silver_drafts),
                  (SELECT count(*) FROM review_decisions),
                  (SELECT count(*) FROM silver_verified_records),
                  (SELECT count(*) FROM smartcoat_migrations.applied_migrations),
                  (SELECT count(*) FROM smartcoat_migrations.adoption_decisions)
                )::text
                """,
            ) == f"[{expected_uploads}, 2, 1, 1, 1, 3, 1]",
            "Denied SQL changed accepted synthetic state",
        )
        self.checks.extend(["append_only_direct_sql_rejected", "denials_left_state_unchanged"])

    def cleanup(self) -> None:
        if self.cleaned:
            return
        require(PROJECT_PATTERN.fullmatch(self.project) is not None, "Cleanup project identity is unsafe", IsolationError)
        for resource, name in (("container", self.container), ("network", self.network), ("volume", self.volume)):
            listing = self._run(
                f"cleanup_presence:{resource}",
                ["docker", resource, "ls", *( ["--all"] if resource == "container" else [] ), "--format", "{{.Names}}" if resource == "container" else "{{.Name}}"],
                record=False,
            )
            if name in listing.stdout.splitlines():
                require(self._label(resource, name) == self.project, f"Cleanup refused unowned {resource}", IsolationError)
        container_names = self._run(
            "cleanup_container_names", ["docker", "container", "ls", "--all", "--format", "{{.Names}}"], record=False
        ).stdout.splitlines()
        if self.container in container_names:
            self._success("cleanup_container", ["docker", "container", "rm", "--force", self.container])
        network_names = self._run(
            "cleanup_network_names", ["docker", "network", "ls", "--format", "{{.Name}}"], record=False
        ).stdout.splitlines()
        if self.network in network_names:
            self._success("cleanup_network", ["docker", "network", "rm", self.network])
        volume_names = self._run(
            "cleanup_volume_names", ["docker", "volume", "ls", "--format", "{{.Name}}"], record=False
        ).stdout.splitlines()
        if self.volume in volume_names:
            self._success("cleanup_volume", ["docker", "volume", "rm", self.volume])
        self.cleaned = True

    def finalize(self) -> dict[str, Any]:
        require(not self.finalized, "Scenario finalizer ran more than once", IsolationError)
        self.finalized = True
        self.cleanup()
        shutil.rmtree(self.control_directory)
        observed = inventory(self.environment)
        require(observed == self.inventory_before, "Pre-existing Docker inventory changed", IsolationError)
        return {
            "mode": self.mode,
            "project": self.project,
            "database": self.database,
            "checks": self.checks,
            "commands": self.commands,
            "cleanup": {
                "owned_resources_remaining": 0,
                "finalizer_calls": 1,
                "preexisting_inventory_unchanged": True,
            },
        }


def run_scenario(mode: str, expected_inventory: Inventory) -> dict[str, Any]:
    scenario = Scenario(mode, expected_inventory)
    try:
        scenario.prepare()
        scenario.start()
        scenario.migrate()
        scenario.provision()
        scenario.verify_catalog()
        scenario.positive_sql()
        scenario.negative_sql()
        return scenario.finalize()
    except BaseException as original:
        cleanup_failure: Exception | None = None
        if not scenario.finalized:
            scenario.finalized = True
            try:
                scenario.cleanup()
                if scenario.control_directory.exists():
                    shutil.rmtree(scenario.control_directory)
            except Exception as exc:
                cleanup_failure = exc
        if cleanup_failure is not None:
            raise IsolationError(
                f"Owned-resource cleanup failed after {type(original).__name__}: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}"
            ) from original
        raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="M0-R02 disposable PostgreSQL RBAC acceptance")
    value.add_argument("--confirm-disposable-synthetic-rbac-run", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.confirm_disposable_synthetic_rbac_run:
        print(f"{BLOCKED_ISOLATION}: explicit --confirm-disposable-synthetic-rbac-run is required")
        return 2
    result = PASS
    failure = ""
    evidence: dict[str, Any] = {"scenarios": []}
    try:
        environment = docker_environment()
        before = inventory(environment)
        evidence["inventory_before"] = {
            "counts": before.counts(),
            "fingerprint": before.fingerprint(),
        }
        for mode in ("fresh", "upgraded"):
            evidence["scenarios"].append(run_scenario(mode, before))
        after = inventory(environment)
        require(after == before, "Final Docker inventory changed", IsolationError)
        evidence["inventory_after"] = {
            "counts": after.counts(),
            "fingerprint": after.fingerprint(),
        }
    except AcceptanceError as exc:
        result = exc.result
        failure = str(exc)
    except KeyboardInterrupt as exc:
        result = BLOCKED_ISOLATION
        failure = f"Acceptance interrupted: {exc}"
    except Exception as exc:
        result = FAIL
        failure = f"Unexpected harness failure: {type(exc).__name__}: {exc}"
    evidence["result"] = result
    if failure:
        evidence["failure"] = failure
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print(result)
    return 0 if result == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
