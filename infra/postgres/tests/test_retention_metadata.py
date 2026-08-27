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
        self.assertEqual([1, 5], [item.version for item in discovered])
        self.assertEqual("expand_retention_metadata", discovered[-1].name)

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
