from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra/postgres/migrations/0005__expand_retention_metadata.sql"
POLICY_MODULE = ROOT / "apps/api/src/retention_policy.py"


def load_policy_module():
    spec = importlib.util.spec_from_file_location("retention_policy_contract", POLICY_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load retention-policy module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetentionMetadataMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text()
        cls.policy = load_policy_module()

    def test_reserved_migration_identity_and_offline_discovery(self) -> None:
        sys.path.insert(0, str(ROOT / "infra/postgres"))
        import migrate

        discovered = migrate.discover_migrations(ROOT / "infra/postgres/migrations")
        self.assertEqual(list(range(1, 11)), [item.version for item in discovered])
        by_version = {item.version: item for item in discovered}
        self.assertEqual("expand_retention_metadata", by_version[5].name)

    def test_schema_declares_exact_canonical_classes(self) -> None:
        expected = set(self.policy.CANONICAL_RETENTION_CLASSES)
        inserted = set(
            re.findall(
                r"\('((?:permanent|long_term_10y|short_90d))',",
                self.sql,
            )
        )
        self.assertEqual(expected, inserted)
        self.assertNotIn("365_days", self.sql)

    def test_policy_version_and_governance_hash_are_identical_to_code(self) -> None:
        self.assertIn(self.policy.RETENTION_POLICY_VERSION, self.sql)
        self.assertIn(self.policy.RETENTION_POLICY_DOCUMENT, self.sql)
        self.assertIn(self.policy.RETENTION_POLICY_DOCUMENT_SHA256, self.sql)

    def test_exact_original_and_manifest_version_assignment_is_representable(self) -> None:
        for required in (
            "bronze_object_id uuid NOT NULL UNIQUE",
            "ingestion_id uuid NOT NULL",
            "bucket_name text NOT NULL",
            "object_key text NOT NULL",
            "object_kind text NOT NULL",
            "object_version_id text NOT NULL",
            "accepted_storage_at_utc timestamptz NOT NULL",
            "UNIQUE (ingestion_id, object_kind)",
            "UNIQUE (bucket_name, object_key, object_version_id)",
        ):
            self.assertIn(required, self.sql)
        self.assertIn("('ORIGINAL', 'MANIFEST')", self.sql)
        self.assertIn("validate_bronze_retention_assignment", self.sql)
        self.assertIn("object_version_id <> NEW.object_version_id", self.sql)

    def test_database_deadline_is_bound_to_the_storage_anchor(self) -> None:
        self.assertIn("CREATE FUNCTION retention_deadline_utc", self.sql)
        self.assertIn("interval '2160 hours'", self.sql)
        self.assertIn("+ 10", self.sql)
        self.assertNotIn("::time(0)", self.sql)
        self.assertGreaterEqual(self.sql.count("date_trunc("), 3)
        self.assertRegex(
            self.sql,
            r"expected_retain_until_utc = retention_deadline_utc\(\s*"
            r"retention_class,\s*accepted_storage_at_utc\s*\)",
        )
        self.assertRegex(
            self.sql,
            r"accepted_storage_at_utc = date_trunc\(\s*"
            r"'second',\s*accepted_storage_at_utc\s*\)",
        )

    def test_approved_policy_version_seals_its_category_rules(self) -> None:
        self.assertLess(
            self.sql.index("INSERT INTO retention_category_rules"),
            self.sql.index("INSERT INTO retention_policy_versions"),
        )
        self.assertRegex(
            self.sql,
            r"FOREIGN KEY \(retention_policy_version\) REFERENCES "
            r"retention_policy_versions \(\s*retention_policy_version\s*\) "
            r"DEFERRABLE INITIALLY DEFERRED",
        )
        self.assertIn("reject_rule_for_approved_retention_policy", self.sql)
        self.assertIn("require_rules_before_retention_policy_approval", self.sql)
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn(
            "Retention category rules are sealed for approved policy version",
            self.sql,
        )

    def test_python_rules_match_seeded_database_rules_exactly(self) -> None:
        seeded_rows = {
            category: (
                retention_class,
                records_purpose,
                legal_basis_classification,
            )
            for category, retention_class, records_purpose, legal_basis_classification
            in re.findall(
                r"\(\s*'smartcoat_retention_2026_08_v1',\s*"
                r"'([A-Z][A-Z0-9_]+)',\s*"
                r"'(permanent|long_term_10y|short_90d)',\s*"
                r"'([^']+)',\s*'([^']+)'\s*\)",
                self.sql,
            )
        }
        python_rows = {
            category: (
                rule.retention_class,
                rule.records_purpose,
                rule.legal_basis_classification,
            )
            for category, rule in self.policy.approved_rules().items()
        }
        self.assertEqual(python_rows, seeded_rows)

    def test_policy_link_and_assignment_are_append_only(self) -> None:
        for table in (
            "canonical_retention_classes",
            "retention_policy_versions",
            "retention_category_rules",
            "bronze_retention_assignments",
        ):
            self.assertRegex(
                self.sql,
                rf"CREATE TRIGGER {table}_append_only\s+"
                rf"BEFORE UPDATE OR DELETE ON {table}\s+"
                r"FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation\(\);",
            )

    def test_unknown_categories_cannot_bypass_versioned_rule_foreign_key(self) -> None:
        self.assertRegex(
            self.sql,
            r"FOREIGN KEY \(\s*retention_policy_version,\s*"
            r"data_category,\s*retention_class\s*\) REFERENCES "
            r"retention_category_rules",
        )
        self.assertNotIn("DEFAULT 'permanent'", self.sql)

    def test_existing_bronze_rows_are_not_mutated_or_hardened_prematurely(self) -> None:
        self.assertNotRegex(self.sql, r"(?im)^\s*UPDATE\s+bronze_objects\b")
        self.assertNotRegex(self.sql, r"(?im)^\s*DELETE\s+FROM\s+bronze_objects\b")
        self.assertNotRegex(self.sql, r"(?im)^\s*ALTER\s+TABLE\s+bronze_objects\b")
        self.assertNotIn("SET NOT NULL", self.sql)

    def test_migration_contains_no_storage_enforcement_or_lifecycle_operation(self) -> None:
        lowered = self.sql.lower()
        for forbidden in (
            "put_object",
            "legalhold",
            "legal_hold_on",
            "mc retention",
            "lifecycle rule",
            "delete marker",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
