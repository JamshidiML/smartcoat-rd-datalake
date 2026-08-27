from __future__ import annotations

import ast
import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

import retention_policy as retention  # noqa: E402


class RetentionPolicyTests(unittest.TestCase):
    def test_canonical_classes_are_exact(self) -> None:
        self.assertEqual(
            ("permanent", "long_term_10y", "short_90d"),
            retention.CANONICAL_RETENTION_CLASSES,
        )

    def test_approved_rnd_categories_are_permanent(self) -> None:
        for category in (
            "LAB_NOTE",
            "TEST_RESULT",
            "FORMULATION_SCREEN",
            "MATERIAL_DOCUMENT",
            "TRIAL_VIDEO",
        ):
            with self.subTest(category=category):
                rule = retention.resolve_category_rule(category)
                self.assertEqual(retention.PERMANENT, rule.retention_class)
                self.assertTrue(rule.legal_hold_required)

    def test_operational_logs_are_exactly_short_90d(self) -> None:
        for category in ("PLATFORM_OPERATIONAL_LOG", "PLATFORM_DEBUG_LOG"):
            with self.subTest(category=category):
                rule = retention.resolve_category_rule(category)
                self.assertEqual(retention.SHORT_90D, rule.retention_class)
                self.assertFalse(rule.legal_hold_required)

    def test_unknown_other_and_personal_categories_fail_closed(self) -> None:
        for category in ("", "OTHER", "UNKNOWN", "CUSTOMER_EMAIL", "CONTACT_PERSON"):
            with self.subTest(category=category), self.assertRaises(
                retention.ClassificationPending
            ):
                retention.resolve_category_rule(category)

    def test_unapproved_policy_version_fails_closed(self) -> None:
        with self.assertRaises(retention.PolicyVersionUnavailable):
            retention.resolve_category_rule(
                "LAB_NOTE",
                retention_policy_version="smartcoat_retention_unapproved",
            )

    def test_normal_ten_calendar_year_deadline_preserves_utc_fields(self) -> None:
        source = datetime(2026, 8, 20, 14, 3, 2, 999999, tzinfo=UTC)
        self.assertEqual(
            datetime(2036, 8, 20, 14, 3, 2, tzinfo=UTC),
            retention.retain_until_for(retention.LONG_TERM_10Y, source),
        )

    def test_february_29_clamps_to_last_valid_day(self) -> None:
        source = datetime(2024, 2, 29, 23, 59, 58, tzinfo=UTC)
        expected = datetime(2034, 2, 28, 23, 59, 58, tzinfo=UTC)
        self.assertEqual(expected, retention.add_calendar_years(source, 10))
        self.assertEqual(
            expected,
            retention.retain_until_for(retention.PERMANENT, source),
        )

    def test_short_90d_is_exactly_2160_utc_hours(self) -> None:
        source = datetime(
            2026,
            3,
            29,
            1,
            30,
            45,
            987654,
            tzinfo=timezone(timedelta(hours=1)),
        )
        accepted = retention.normalize_storage_timestamp(source)
        deadline = retention.retain_until_for(retention.SHORT_90D, source)

        self.assertEqual(timedelta(hours=2160), deadline - accepted)
        self.assertEqual(0, deadline.microsecond)
        self.assertIs(UTC, deadline.tzinfo)

    def test_naive_timestamp_and_noncanonical_class_fail_closed(self) -> None:
        with self.assertRaises(retention.RetentionPolicyError):
            retention.normalize_storage_timestamp(datetime(2026, 1, 1))
        with self.assertRaises(retention.RetentionPolicyError):
            retention.retain_until_for("365_days", datetime.now(UTC))

    def test_assignment_plan_links_exact_policy_and_normalized_anchor(self) -> None:
        source = datetime(2026, 8, 20, 1, 2, 3, 456789, tzinfo=UTC)
        plan = retention.plan_assignment("lab_note", source)

        self.assertEqual("LAB_NOTE", plan.data_category)
        self.assertEqual(retention.RETENTION_POLICY_VERSION, plan.retention_policy_version)
        self.assertEqual(datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC), plan.accepted_storage_at_utc)
        self.assertEqual(datetime(2036, 8, 20, 1, 2, 3, tzinfo=UTC), plan.expected_retain_until_utc)
        self.assertTrue(plan.legal_hold_required)

    def test_policy_registry_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            retention.approved_rules()["NEW"] = retention.CategoryRule(  # type: ignore[index]
                "NEW", retention.PERMANENT, "x", "y"
            )

    def test_module_has_no_storage_or_external_client_import(self) -> None:
        tree = ast.parse((SOURCE / "retention_policy.py").read_text())
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            imported.isdisjoint({"minio", "boto3", "requests", "httpx", "storage"})
        )


if __name__ == "__main__":
    unittest.main()
