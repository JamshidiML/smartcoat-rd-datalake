from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "infra/minio/tests/live_retention_enforcement_acceptance.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("retention_live_offline", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("retention live harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetentionLiveHarnessOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HARNESS.read_text()
        cls.module = load_harness()

    def test_missing_explicit_authorization_fails_before_docker(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.module.main([])
        self.assertEqual(2, result)
        self.assertIn("BLOCKED_ISOLATION", output.getvalue())

    def test_fresh_and_upgraded_scenarios_cover_all_classes_and_kinds(self) -> None:
        self.assertIn('(\"fresh_volume\", \"upgraded_volume\")', self.source)
        self.assertIn("('permanent', 'long_term_10y', 'short_90d')", self.source)
        self.assertIn("('ORIGINAL', 'sc-rd-bronze-originals')", self.source)
        self.assertIn("('MANIFEST', 'sc-rd-bronze-manifests')", self.source)

    def test_negative_contract_is_explicit(self) -> None:
        for marker in (
            "unauthorized_bucket",
            "wrong_bucket",
            "malformed_metadata",
            "malformed_version",
            "missing_version",
            "unknown_policy",
            "mediator_failure",
            "interrupted_execution",
            "retention_api_denial",
            "readback_mismatch",
        ):
            self.assertIn(marker, self.source)

    def test_harness_uses_immutable_images_without_pull_or_ports(self) -> None:
        self.assertIn('"--pull=never"', self.source)
        self.assertIn("legal.image_id(retention_image)", self.source)
        self.assertIn("legal.image_id(legal_image)", self.source)
        self.assertNotIn('"--publish"', self.source)
        self.assertNotIn('"-p"', self.source)

    def test_no_pair_commit_state_or_ocr_or_real_env(self) -> None:
        for forbidden in (
            "BRONZE_COMMITTED",
            "OCR_QUEUED",
            "LEGACY_RECONCILED_OR_QUARANTINED",
            'ROOT / ".env"',
        ):
            self.assertNotIn(forbidden, self.source)

    def test_pinned_runtime_contract_is_unchanged(self) -> None:
        requirements = (ROOT / "apps/api/src/requirements.txt").read_text()
        self.assertIn("minio==7.2.16", requirements)
        self.assertIn("minio/minio:RELEASE.2025-07-23T15-54-02Z", self.module.legal.SERVER_REF)


if __name__ == "__main__":
    unittest.main()
