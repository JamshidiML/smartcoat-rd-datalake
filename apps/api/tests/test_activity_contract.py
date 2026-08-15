from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class ActivityContractTests(unittest.TestCase):
    def test_upload_activity_exposes_latest_worker_state(self) -> None:
        database = (ROOT / "apps/api/src/database.py").read_text()
        self.assertIn("j.status AS ocr_job_status", database)
        self.assertIn("j.started_at_utc AS ocr_started_at_utc", database)
        self.assertIn("LEFT JOIN LATERAL", database)

    def test_web_polling_follows_active_ocr_into_review(self) -> None:
        app = (ROOT / "apps/web/src/app.js").read_text()
        self.assertIn('includes(upload.ocr_job_status)', app)
        self.assertIn("setTimeout(() => refreshActivity(), 2000)", app)
        self.assertIn("OCR PROCESSING", app)
        self.assertIn("updates automatically", app)


if __name__ == "__main__":
    unittest.main()
