from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/disaster-recovery.sh"


class DisasterRecoveryScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_requires_explicit_authorization_and_owned_absolute_paths(self) -> None:
        self.assertIn("DESTROYED_FRESH_VOLUMES_SYNTHETIC_ONLY", self.source)
        self.assertIn(".smartcoat-disaster-recovery-owned", self.source)
        self.assertIn('case "$postgres_data" in "$scope_root"/*)', self.source)
        self.assertIn('case "$minio_data" in "$scope_root"/*)', self.source)

    def test_backup_preserves_acl_and_all_minio_system_metadata(self) -> None:
        self.assertIn("pg_dump --format=custom --no-owner", self.source)
        self.assertNotIn("--no-privileges", self.source)
        self.assertIn('cp -a "$minio_data/."', self.source)
        self.assertIn('minio-data/.minio.sys', self.source)

    def test_restore_requires_empty_replacement_directories(self) -> None:
        self.assertIn("PostgreSQL replacement directory is not empty", self.source)
        self.assertIn("MinIO replacement directory is not empty", self.source)

    def test_restore_creates_roles_before_replaying_database_acls(self) -> None:
        self.assertIn('compose run --rm postgres-migrate adopt "$POSTGRES_DB"', self.source)
        provision_before_restore = self.source.index(
            "compose run --rm postgres-role-provision"
        )
        restore = self.source.index("exec -T postgres pg_restore")
        self.assertLess(provision_before_restore, restore)
        self.assertIn("--clean --if-exists --exit-on-error --no-owner", self.source)

    def test_restored_contract_is_revalidated(self) -> None:
        self.assertEqual(
            self.source.count("compose run --rm postgres-role-provision"), 2
        )
        self.assertEqual(self.source.count("compose run --rm postgres-migrate apply"), 2)
        self.assertIn("compose_quiet run --rm minio-bootstrap", self.source)

    def test_output_is_checked_for_secret_values(self) -> None:
        self.assertIn("assert_secret_free", self.source)
        self.assertIn("LEGAL_HOLD_APPLIER_CALL_TOKEN", self.source)
        self.assertIn("appeared in captured output", self.source)
        self.assertIn('>/dev/null 2>&1', self.source)
        self.assertNotIn("set -x", self.source)


if __name__ == "__main__":
    unittest.main()
