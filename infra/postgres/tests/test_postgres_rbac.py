from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
POSTGRES_ROOT = ROOT / "infra/postgres"
MIGRATION = POSTGRES_ROOT / "migrations/0002__separate_runtime_roles.sql"
COMPATIBILITY_MIGRATION = (
    POSTGRES_ROOT / "migrations/0006__grant_review_audit_evidence_read.sql"
)
POSITIVE_PATH_MIGRATION = (
    POSTGRES_ROOT / "migrations/0010__grant_positive_path_bronze_reads.sql"
)
PROVISIONER = POSTGRES_ROOT / "provision_runtime_roles.py"
LIVE_ACCEPTANCE = POSTGRES_ROOT / "tests/live_postgres_rbac_acceptance.py"
RUN_EXTERNAL_TESTS = os.environ.get("SMARTCOAT_EXTERNAL_TESTS") == "1"

sys.path.insert(0, str(POSTGRES_ROOT))
import rbac_contract  # noqa: E402

spec = importlib.util.spec_from_file_location("provision_runtime_roles", PROVISIONER)
if spec is None or spec.loader is None:  # pragma: no cover - import boundary
    raise RuntimeError("Could not load PostgreSQL role provisioner")
provision_runtime_roles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision_runtime_roles)


def example_environment() -> dict[str, str]:
    return {
        name: value
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
        for name, value in (line.split("=", 1),)
    }


class RuntimeRoleContractTests(unittest.TestCase):
    def test_four_workflow_roles_are_distinct_non_admin_logins(self) -> None:
        self.assertEqual(
            {
                "smartcoat_ingestion",
                "smartcoat_ocr",
                "smartcoat_review",
                "smartcoat_backup",
            },
            set(rbac_contract.ROLE_NAMES),
        )
        self.assertEqual(4, len(rbac_contract.expected_role_attributes()))
        for row in rbac_contract.expected_role_attributes():
            self.assertEqual((False, True, False, False, True, False, False), row[1:])

    def test_cross_boundary_writes_are_absent_from_contract(self) -> None:
        privileges = rbac_contract.TABLE_PRIVILEGES
        for role in ("smartcoat_ingestion", "smartcoat_ocr"):
            for table in ("review_decisions", "silver_verified_records"):
                self.assertNotIn((role, table, "INSERT"), privileges)
        self.assertNotIn(("smartcoat_ingestion", "silver_drafts", "INSERT"), privileges)
        self.assertIn(("smartcoat_ocr", "silver_drafts", "INSERT"), privileges)
        self.assertNotIn(("smartcoat_ocr", "silver_drafts", "UPDATE"), privileges)
        self.assertIn(("smartcoat_review", "review_decisions", "INSERT"), privileges)
        self.assertIn(("smartcoat_review", "silver_verified_records", "INSERT"), privileges)

    def test_review_retry_evidence_is_column_select_only(self) -> None:
        self.assertEqual(
            {
                ("smartcoat_review", "audit_events", "entity_type"),
                ("smartcoat_review", "audit_events", "entity_id"),
                ("smartcoat_review", "audit_events", "event_type"),
                ("smartcoat_review", "audit_events", "details_json"),
                ("smartcoat_review", "audit_events", "new_state"),
            },
            {
                item
                for item in rbac_contract.COLUMN_SELECT_PRIVILEGES
                if item[:2] == ("smartcoat_review", "audit_events")
            },
        )
        self.assertNotIn(
            ("smartcoat_review", "audit_events", "SELECT"),
            rbac_contract.TABLE_PRIVILEGES,
        )
        sql = COMPATIBILITY_MIGRATION.read_text()
        self.assertIn(
            "GRANT SELECT (entity_type, entity_id, event_type, details_json, new_state)",
            sql,
        )
        self.assertIn("ON audit_events TO smartcoat_review", sql)
        self.assertNotRegex(
            sql,
            r"(?i)\b(UPDATE|DELETE|TRUNCATE|ALTER|CREATE|DROP|EXECUTE|OWNERSHIP)\b",
        )

    def test_positive_path_requirements_are_fully_granted(self) -> None:
        self.assertGreater(len(rbac_contract.POSITIVE_PATH_REQUIREMENTS), 100)
        self.assertEqual((), rbac_contract.missing_positive_path_requirements())
        self.assertEqual(
            {
                ("smartcoat_ocr", "bronze_objects", "bronze_object_id"),
                ("smartcoat_ocr", "bronze_objects", "ingestion_id"),
                ("smartcoat_ocr", "bronze_objects", "object_kind"),
                ("smartcoat_ocr", "bronze_objects", "object_version_id"),
                ("smartcoat_review", "bronze_objects", "ingestion_id"),
                ("smartcoat_review", "bronze_objects", "object_kind"),
                ("smartcoat_review", "bronze_objects", "object_version_id"),
            },
            {
                item
                for item in rbac_contract.COLUMN_SELECT_PRIVILEGES
                if item[1] == "bronze_objects"
            },
        )
        for role in ("smartcoat_ocr", "smartcoat_review"):
            self.assertNotIn(
                (role, "bronze_objects", "SELECT"),
                rbac_contract.TABLE_PRIVILEGES,
            )

    def test_repository_sql_tables_are_declared_by_positive_path_contract(self) -> None:
        source = (ROOT / "apps/api/src/database.py").read_text()
        methods = {
            node.name: node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        declared_methods = {
            requirement.repository_method
            for requirement in rbac_contract.POSITIVE_PATH_REQUIREMENTS
            if requirement.repository_method != "pg_dump"
        }
        known_tables = set(rbac_contract.PUBLIC_TABLES)
        for method_name in declared_methods:
            self.assertIn(method_name, methods)
            sql_text = "\n".join(
                node.value
                for node in ast.walk(methods[method_name])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ).lower()
            observed = {
                table
                for table in known_tables
                if table in sql_text
            }
            declared = {
                requirement.table
                for requirement in rbac_contract.POSITIVE_PATH_REQUIREMENTS
                if requirement.repository_method == method_name
            }
            self.assertLessEqual(
                observed,
                declared,
                f"{method_name} touches an undeclared positive-path table",
            )

    def test_positive_path_migration_is_column_read_only(self) -> None:
        sql = POSITIVE_PATH_MIGRATION.read_text()
        self.assertIn(
            "REVOKE SELECT ON TABLE bronze_objects FROM smartcoat_ocr, smartcoat_review",
            sql,
        )
        self.assertIn("ON bronze_objects TO smartcoat_ocr", sql)
        self.assertIn("ON bronze_objects TO smartcoat_review", sql)
        self.assertNotRegex(
            sql,
            r"(?i)GRANT\s+(?:ALL|INSERT|UPDATE|DELETE|TRUNCATE|REFERENCES|TRIGGER)",
        )
        self.assertNotRegex(
            sql,
            r"(?i)GRANT\s+SELECT\s+ON\s+(?:TABLE\s+)?bronze_objects",
        )

    def test_backup_is_select_only_and_append_only_tables_have_no_mutation_grants(self) -> None:
        backup = {
            item for item in rbac_contract.TABLE_PRIVILEGES if item[0] == "smartcoat_backup"
        }
        self.assertEqual(
            {
                ("smartcoat_backup", table, "SELECT")
                for table in rbac_contract.PUBLIC_TABLES
            },
            backup,
        )
        self.assertFalse(
            any(item[0] == "smartcoat_backup" for item in rbac_contract.COLUMN_UPDATE_PRIVILEGES)
        )
        for role in rbac_contract.ROLE_NAMES:
            for table in rbac_contract.PROTECTED_APPEND_ONLY_TABLES:
                self.assertNotIn((role, table, "UPDATE"), rbac_contract.TABLE_PRIVILEGES)
                self.assertNotIn((role, table, "DELETE"), rbac_contract.TABLE_PRIVILEGES)

    def test_migration_is_password_free_and_disables_legacy_shared_login(self) -> None:
        sql = MIGRATION.read_text()
        self.assertIn("ALTER ROLE smartcoat_app NOLOGIN PASSWORD NULL", sql)
        self.assertNotIn("change-me", sql)
        for role in rbac_contract.ROLE_NAMES:
            self.assertIn(role, sql)
        for environment_name in (
            rbac_contract.ADMIN_DATABASE_ENV,
            *(role.password_environment for role in rbac_contract.RUNTIME_ROLES.values()),
        ):
            self.assertNotIn(environment_name, sql)
        self.assertIn("REVOKE EXECUTE ON FUNCTION public.reject_immutable_mutation()", sql)

    def test_existing_four_append_only_triggers_remain_authoritative(self) -> None:
        init_sql = (POSTGRES_ROOT / "init.sql").read_text()
        for trigger in (
            "bronze_objects_append_only",
            "verified_records_append_only",
            "review_decisions_append_only",
            "audit_events_append_only",
        ):
            self.assertIn(f"CREATE TRIGGER {trigger}", init_sql)
        self.assertNotRegex(MIGRATION.read_text(), r"(?i)DROP\s+TRIGGER|DISABLE\s+TRIGGER")

    def test_retention_tables_extend_the_exact_runtime_privilege_shape(self) -> None:
        self.assertEqual(
            {
                "canonical_retention_classes",
                "retention_policy_versions",
                "retention_category_rules",
                "bronze_retention_assignments",
                "bronze_retention_enforcement_evidence",
            },
            set(rbac_contract.RETENTION_TABLES),
        )
        retention_privileges = {
            item
            for item in rbac_contract.TABLE_PRIVILEGES
            if item[1] in rbac_contract.RETENTION_TABLES
        }
        expected = {
            *(('smartcoat_backup', table, 'SELECT')
              for table in rbac_contract.RETENTION_TABLES),
            ('smartcoat_ingestion', 'bronze_retention_assignments', 'SELECT'),
            ('smartcoat_ingestion', 'bronze_retention_assignments', 'INSERT'),
            (
                'smartcoat_ingestion',
                'bronze_retention_enforcement_evidence',
                'SELECT',
            ),
            (
                'smartcoat_ingestion',
                'bronze_retention_enforcement_evidence',
                'INSERT',
            ),
        }
        self.assertEqual(expected, retention_privileges)
        for role in ("smartcoat_ocr", "smartcoat_review"):
            self.assertFalse(any(item[0] == role for item in retention_privileges))
        self.assertFalse(
            any(
                item[1] in rbac_contract.RETENTION_TABLES
                for item in rbac_contract.COLUMN_UPDATE_PRIVILEGES
            )
        )

    def test_bronze_pair_tables_are_ingestion_write_backup_read_only(self) -> None:
        self.assertEqual(
            {
                "bronze_pairs",
                "bronze_protected_orphans",
                "bronze_reconciliation_events",
            },
            set(rbac_contract.BRONZE_PAIR_TABLES),
        )
        pair_privileges = {
            item
            for item in rbac_contract.TABLE_PRIVILEGES
            if item[1] in rbac_contract.BRONZE_PAIR_TABLES
        }
        self.assertEqual(
            {
                *(("smartcoat_ingestion", table, privilege)
                  for table in rbac_contract.BRONZE_PAIR_TABLES
                  for privilege in ("SELECT", "INSERT")),
                *(("smartcoat_backup", table, "SELECT")
                  for table in rbac_contract.BRONZE_PAIR_TABLES),
                ("smartcoat_ocr", "bronze_pairs", "SELECT"),
            },
            pair_privileges,
        )
        for role in ("smartcoat_review", "smartcoat_app"):
            self.assertFalse(any(item[0] == role for item in pair_privileges))
        self.assertFalse(any(
            item[0] == "smartcoat_ocr"
            and (item[1] != "bronze_pairs" or item[2] != "SELECT")
            for item in pair_privileges
        ))
        self.assertFalse(
            any(
                item[1] in rbac_contract.BRONZE_PAIR_TABLES
                for item in rbac_contract.COLUMN_UPDATE_PRIVILEGES
            )
        )


class CredentialProvisioningBoundaryTests(unittest.TestCase):
    def test_admin_url_is_explicit_and_database_url_is_not_a_fallback(self) -> None:
        with self.assertRaises(provision_runtime_roles.ProvisioningError):
            provision_runtime_roles.admin_database_url(
                {"DATABASE_URL": "postgresql://ordinary-runtime"}
            )
        self.assertEqual(
            "postgresql://explicit-admin",
            provision_runtime_roles.admin_database_url(
                {rbac_contract.ADMIN_DATABASE_ENV: "postgresql://explicit-admin"}
            ),
        )

    def test_passwords_are_required_long_and_distinct(self) -> None:
        environment = {
            role.password_environment: f"{workflow}-" + ("x" * 40)
            for workflow, role in rbac_contract.RUNTIME_ROLES.items()
        }
        values = rbac_contract.password_values(environment)
        self.assertEqual(set(rbac_contract.ROLE_NAMES), set(values))

        missing = dict(environment)
        missing.pop(rbac_contract.RUNTIME_ROLES["ocr"].password_environment)
        with self.assertRaises(ValueError):
            rbac_contract.password_values(missing)

        duplicate = dict(environment)
        duplicate[rbac_contract.RUNTIME_ROLES["ocr"].password_environment] = duplicate[
            rbac_contract.RUNTIME_ROLES["ingestion"].password_environment
        ]
        with self.assertRaises(ValueError):
            rbac_contract.password_values(duplicate)

        provision_runtime_roles.validate_admin_password_separation(
            "postgresql://admin:distinct-admin-password@postgres:5432/smartcoat_rd",
            values,
        )
        reused = dict(values)
        reused["smartcoat_ocr"] = "same-admin-password"
        with self.assertRaises(provision_runtime_roles.ProvisioningError):
            provision_runtime_roles.validate_admin_password_separation(
                "postgresql://admin:same-admin-password@postgres:5432/smartcoat_rd",
                reused,
            )

    @unittest.skipUnless(
        RUN_EXTERNAL_TESTS,
        "requires an external Python process; enabled by manual live acceptance",
    )
    def test_live_acceptance_is_explicitly_opt_in(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(LIVE_ACCEPTANCE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("BLOCKED_M0_R02_ISOLATION", completed.stdout)
        self.assertIn("--confirm-disposable-synthetic-rbac-run", completed.stdout)


@unittest.skipUnless(
    RUN_EXTERNAL_TESTS,
    "requires Docker Compose; enabled by manual live acceptance",
)
class ComposeCredentialBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        environment = os.environ.copy()
        environment.pop("COMPOSE_FILE", None)
        rendered = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / ".env.example"),
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if rendered.returncode != 0:
            raise AssertionError((rendered.stderr or rendered.stdout).strip())
        cls.services = json.loads(rendered.stdout)["services"]
        cls.environment = example_environment()

    def test_runtime_urls_name_distinct_expected_identities(self) -> None:
        expected = {
            "DATABASE_INGESTION_URL": "smartcoat_ingestion",
            "DATABASE_OCR_URL": "smartcoat_ocr",
            "DATABASE_REVIEW_URL": "smartcoat_review",
            "DATABASE_BACKUP_URL": "smartcoat_backup",
        }
        self.assertNotIn("DATABASE_URL", self.environment)
        observed_passwords: set[str] = set()
        for name, username in expected.items():
            parsed = urlsplit(self.environment[name])
            self.assertEqual(username, unquote(parsed.username or ""))
            observed_passwords.add(unquote(parsed.password or ""))
        self.assertEqual(4, len(observed_passwords))

    def test_api_and_ocr_receive_only_their_workflow_database_urls(self) -> None:
        api = self.services["api"]["environment"]
        worker = self.services["ocr-worker"]["environment"]
        self.assertEqual(self.environment["DATABASE_INGESTION_URL"], api["DATABASE_URL"])
        self.assertEqual(self.environment["DATABASE_OCR_URL"], api["OCR_DATABASE_URL"])
        self.assertEqual(self.environment["DATABASE_REVIEW_URL"], api["REVIEW_DATABASE_URL"])
        self.assertEqual(self.environment["DATABASE_OCR_URL"], worker["DATABASE_URL"])
        for runtime in (api, worker):
            self.assertNotIn("MIGRATION_DATABASE_URL", runtime)
            self.assertNotIn("POSTGRES_ROLE_ADMIN_URL", runtime)

    def test_provisioner_is_backend_only_one_shot_and_admin_is_not_runtime(self) -> None:
        service = self.services["postgres-role-provision"]
        self.assertEqual("no", service["restart"])
        self.assertEqual({"backend"}, set(service["networks"]))
        self.assertNotIn("ports", service)
        self.assertEqual(
            {
                "POSTGRES_ROLE_ADMIN_URL",
                "POSTGRES_INGESTION_PASSWORD",
                "POSTGRES_OCR_PASSWORD",
                "POSTGRES_REVIEW_PASSWORD",
                "POSTGRES_BACKUP_PASSWORD",
            },
            set(service["environment"]),
        )
        for runtime_name in ("api", "ocr-worker"):
            dependency = self.services[runtime_name]["depends_on"]["postgres-role-provision"]
            self.assertEqual("service_completed_successfully", dependency["condition"])

    def test_application_wiring_and_backup_use_narrow_identities(self) -> None:
        main = (ROOT / "apps/api/src/main.py").read_text()
        worker = (ROOT / "apps/ocr-worker/src/jobs/worker.py").read_text()
        restore = (ROOT / "scripts/restore-drill.sh").read_text()
        self.assertIn('REVIEW_DATABASE_URL = os.environ["REVIEW_DATABASE_URL"]', main)
        self.assertIn('OCR_DATABASE_URL = os.environ["OCR_DATABASE_URL"]', main)
        self.assertIn("OCRRecoveryService(ocr_repository", main)
        self.assertIn("ReviewService(review_repository", main)
        self.assertIn("with review_repository.connection() as connection:", main)
        self.assertIn('PostgresRepository(os.environ["DATABASE_URL"])', worker)
        self.assertIn('PGPASSWORD="$POSTGRES_BACKUP_PASSWORD"', restore)
        self.assertIn("-U smartcoat_backup", restore)
        self.assertNotIn("POSTGRES_APP_PASSWORD", restore)


if __name__ == "__main__":
    unittest.main()
