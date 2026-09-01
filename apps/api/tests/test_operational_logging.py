from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

import operational_logging  # noqa: E402
from domain import Actor, IngestionService  # noqa: E402
from fakes import MemoryRepository, MemoryRetentionEnforcer, MemoryStorage  # noqa: E402


JPEG = b"\xff\xd8\xff\xe0structured-logging-fixture\xff\xd9"
ACTOR = Actor("usr_logging", "Synthetic Logging User")


class OperationalLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_logger = operational_logging._LOGGER
        self.lines: list[str] = []
        operational_logging._LOGGER = operational_logging.StructuredLogger(
            "api", sink=self.lines.append
        )

    def tearDown(self) -> None:
        operational_logging._LOGGER = self.original_logger

    def ingest(self) -> dict:
        return IngestionService(
            MemoryRepository(),
            MemoryStorage(),
            1024 * 1024,
            MemoryRetentionEnforcer(),
        ).ingest(
            ACTOR,
            "synthetic-logging.jpg",
            JPEG,
            "LAB_NOTE",
            "Synthetic structured logging fixture only.",
            None,
        )

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    def test_record_is_single_line_json_with_required_fields(self) -> None:
        with operational_logging.correlation_scope("corr-required-fields"):
            operational_logging.log_event(
                "INFO", "synthetic.event", ingestion_id="ing-1", note="line one\nline two"
            )

        self.assertEqual(1, len(self.lines))
        self.assertNotIn("\n", self.lines[0])
        record = self.records()[0]
        self.assertEqual(
            {
                "timestamp_utc",
                "level",
                "event",
                "service",
                "correlation_id",
            },
            {
                "timestamp_utc",
                "level",
                "event",
                "service",
                "correlation_id",
            }
            & set(record),
        )
        self.assertTrue(record["timestamp_utc"].endswith("Z"))
        self.assertEqual("INFO", record["level"])
        self.assertEqual("api", record["service"])

    def test_correlation_is_stable_within_request_and_distinct_between_requests(self) -> None:
        observed: list[str] = []
        for _ in range(2):
            with operational_logging.correlation_scope() as correlation_id:
                operational_logging.log_event("INFO", "request.received")
                operational_logging.log_event("INFO", "request.completed")
                observed.append(correlation_id)

        records = self.records()
        self.assertEqual(records[0]["correlation_id"], records[1]["correlation_id"])
        self.assertEqual(records[2]["correlation_id"], records[3]["correlation_id"])
        self.assertNotEqual(observed[0], observed[1])

    def test_upload_correlation_propagates_to_queued_ocr_job(self) -> None:
        correlation_id = "0198a000-0000-7000-8000-000000000777"
        with operational_logging.correlation_scope(correlation_id):
            result = self.ingest()

        self.assertEqual(correlation_id, result["ocr_job_id"])
        self.assertEqual(
            {correlation_id},
            {record["correlation_id"] for record in self.records()},
        )

    def test_credential_shaped_values_and_sensitive_payload_fields_are_redacted(self) -> None:
        access_key = "AKIA" + "A" * 16
        github_token = "ghp_" + "b" * 24
        with operational_logging.correlation_scope("corr-redaction"):
            operational_logging.log_event(
                "ERROR",
                "synthetic.redaction",
                password="synthetic-password",
                authorization="Bearer synthetic-token-value",
                database_url="postgresql://user:password@postgres/db",
                verified_text="prohibited verified payload",
                ocr_text="prohibited OCR payload",
                nested={"credential": github_token, "reference": access_key},
            )

        line = self.lines[0]
        record = self.records()[0]
        for prohibited in (
            "synthetic-password",
            "synthetic-token-value",
            "postgresql://user:password@postgres/db",
            "prohibited verified payload",
            "prohibited OCR payload",
            github_token,
            access_key,
        ):
            self.assertNotIn(prohibited, line)
        self.assertEqual(operational_logging.REDACTED, record["password"])
        self.assertEqual(operational_logging.REDACTED, record["nested"]["reference"])

    def test_logging_failure_does_not_break_ingestion_domain_logic(self) -> None:
        def failing_sink(_: str) -> None:
            raise OSError("synthetic logging sink failure")

        operational_logging._LOGGER = operational_logging.StructuredLogger(
            "api", sink=failing_sink
        )
        with operational_logging.correlation_scope("corr-failing-sink"):
            result = self.ingest()

        self.assertEqual("corr-failing-sink", result["ocr_job_id"])

    def test_level_configuration_is_honored_and_defaults_safely(self) -> None:
        self.assertEqual("INFO", operational_logging.configured_level({}))
        self.assertEqual(
            "INFO",
            operational_logging.configured_level(
                {operational_logging.LOG_LEVEL_ENV: "not-a-level"}
            ),
        )
        logger = operational_logging.StructuredLogger(
            "api", level="WARNING", sink=self.lines.append
        )
        logger.emit("INFO", "filtered.event")
        logger.emit("WARNING", "retained.event")
        self.assertEqual(["retained.event"], [record["event"] for record in self.records()])

    def test_complete_upload_trace_contains_no_payload_data(self) -> None:
        correlation_id = "0198a000-0000-7000-8000-000000000778"
        with operational_logging.correlation_scope(correlation_id):
            operational_logging.log_event(
                "INFO", "request.received", method="POST", path="/api/uploads"
            )
            result = self.ingest()
            operational_logging.log_event(
                "INFO",
                "request.completed",
                method="POST",
                path="/api/uploads",
                status_code=201,
                duration_ms=12.5,
            )

        records = self.records()
        events = [record["event"] for record in records]
        self.assertEqual("request.received", events[0])
        self.assertIn("bronze.original.committed", events)
        self.assertIn("bronze.manifest.committed", events)
        self.assertIn("bronze.pair.committed", events)
        self.assertIn("ocr.job.queued", events)
        self.assertEqual("request.completed", events[-1])
        self.assertEqual(correlation_id, result["ocr_job_id"])
        self.assertEqual({correlation_id}, {record["correlation_id"] for record in records})
        combined = "\n".join(self.lines)
        self.assertNotIn("Synthetic Logging User", combined)
        self.assertNotIn(JPEG.decode("latin-1"), combined)


if __name__ == "__main__":
    unittest.main()
