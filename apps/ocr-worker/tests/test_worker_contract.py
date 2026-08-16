from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class WorkerContractTests(unittest.TestCase):
    def test_worker_versions_are_pinned(self) -> None:
        requirements = (ROOT / "apps/ocr-worker/src/requirements.txt").read_text().splitlines()
        self.assertIn("paddleocr==3.7.0", requirements)
        self.assertIn("paddlepaddle==3.2.2", requirements)
        self.assertTrue(all("==" in line for line in requirements if line.strip()))

    def test_worker_build_context_is_source_only(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("context: ./apps/ocr-worker/src", compose)
        self.assertIn("api-source: ./apps/api/src", compose)
        self.assertNotIn("context: .\n", compose)

    def test_tesseract_uses_same_preprocessed_images(self) -> None:
        worker = (ROOT / "apps/ocr-worker/src/jobs/worker.py").read_text()
        self.assertIn("PADDLE.extract(images)", worker)
        self.assertIn("benchmark(images)", worker)

    def test_retry_reuses_only_identical_preprocessed_artifacts(self) -> None:
        worker = (ROOT / "apps/ocr-worker/src/jobs/worker.py").read_text()
        self.assertIn("except StateConflict:", worker)
        self.assertIn("storage.get(ARTIFACTS_BUCKET, key) != image_bytes", worker)

    def test_worker_recovers_interrupted_jobs_on_startup(self) -> None:
        worker = (ROOT / "apps/ocr-worker/src/jobs/worker.py").read_text()
        database = (ROOT / "apps/api/src/database.py").read_text()
        self.assertIn("repository.recover_interrupted_ocr_jobs()", worker)
        self.assertIn("OCR_JOB_RECOVERED", database)
        self.assertIn("SET status = 'QUEUED', started_at_utc = NULL", database)
        self.assertIn("UPDATE ocr_runs SET status = 'FAILED'", database)

    def test_paddle_cpu_runtime_uses_pre_regression_onednn(self) -> None:
        engine = (ROOT / "apps/ocr-worker/src/extract/paddle_engine.py").read_text()
        dockerfile = (ROOT / "apps/ocr-worker/src/Dockerfile").read_text()
        self.assertLess(engine.index("FLAGS_enable_pir_api"), engine.index("from paddleocr import PaddleOCR"))
        self.assertIn('"enable_mkldnn": True', engine)
        self.assertIn("FLAGS_enable_pir_api=0", dockerfile)
        self.assertIn("enable_mkldnn=True", dockerfile)

    def test_fast_profile_skips_redundant_document_models(self) -> None:
        engine = (ROOT / "apps/ocr-worker/src/extract/paddle_engine.py").read_text()
        compose = (ROOT / "compose.yaml").read_text()
        dockerfile = (ROOT / "apps/ocr-worker/src/Dockerfile").read_text()
        self.assertIn('os.getenv("OCR_PIPELINE_PROFILE", "fast")', engine)
        self.assertIn('"profile": PROFILE', engine)
        self.assertIn("OCR_PIPELINE_PROFILE: ${OCR_PIPELINE_PROFILE:-fast}", compose)
        self.assertIn("use_doc_orientation_classify=False", dockerfile)
        self.assertIn("use_doc_unwarping=False", dockerfile)
        self.assertIn("use_textline_orientation=False", dockerfile)

    def test_preprocessing_caps_camera_images_and_versions_artifacts(self) -> None:
        images = (ROOT / "apps/ocr-worker/src/preprocess/images.py").read_text()
        documents = (ROOT / "apps/ocr-worker/src/preprocess/documents.py").read_text()
        worker = (ROOT / "apps/ocr-worker/src/jobs/worker.py").read_text()
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn('os.getenv("OCR_MAX_IMAGE_SIDE", "2400")', documents)
        self.assertIn("Image.Resampling.LANCZOS", images)
        self.assertIn('PREPROCESSING_VERSION = "2"', documents)
        self.assertIn("v{PREPROCESSING_VERSION}-max-{MAX_IMAGE_SIDE}", worker)
        self.assertIn("OCR_MAX_IMAGE_SIDE: ${OCR_MAX_IMAGE_SIDE:-2400}", compose)

    def test_no_external_ocr_or_vision_api(self) -> None:
        source = "\n".join(
            path.read_text(errors="replace")
            for path in (ROOT / "apps/ocr-worker/src").rglob("*.py")
        )
        external_api = re.compile(r"https?://|requests\.(?:post|get)|openai|anthropic|gemini", re.IGNORECASE)
        self.assertIsNone(external_api.search(source))


if __name__ == "__main__":
    unittest.main()
