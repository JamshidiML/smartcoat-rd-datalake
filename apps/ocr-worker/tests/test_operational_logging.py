from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src"
LOGGER_SOURCE = SOURCE / "operational_logging.py"


def load_logging_module():
    spec = importlib.util.spec_from_file_location("ocr_operational_logging", LOGGER_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load OCR operational logging module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OCRWorkerOperationalLoggingTests(unittest.TestCase):
    def test_worker_uses_propagated_job_id_as_correlation(self) -> None:
        module = load_logging_module()
        lines: list[str] = []
        logger = module.StructuredLogger("ocr-worker", sink=lines.append)
        job_id = "0198a000-0000-7000-8000-000000000777"

        with module.correlation_scope(job_id):
            logger.emit(
                "INFO",
                "ocr.job.started",
                ingestion_id="0198a000-0000-7000-8000-000000000111",
                ocr_job_id=job_id,
            )
            logger.emit("INFO", "ocr.job.completed", duration_ms=12.5)

        records = [json.loads(line) for line in lines]
        self.assertEqual({job_id}, {record["correlation_id"] for record in records})
        self.assertEqual({"ocr-worker"}, {record["service"] for record in records})

    def test_worker_binds_queue_identity_and_reads_exact_bronze_version(self) -> None:
        source = (SOURCE / "jobs/worker.py").read_text()
        self.assertIn('correlation_scope(str(job["ocr_job_id"]))', source)
        self.assertIn(
            "storage.get_exact(ORIGINALS_BUCKET, source_key, source_version_id)",
            source,
        )
        self.assertNotIn("verified_text", source)


if __name__ == "__main__":
    unittest.main()
