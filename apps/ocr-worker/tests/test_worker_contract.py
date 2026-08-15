from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class WorkerContractTests(unittest.TestCase):
    def test_worker_versions_are_pinned(self) -> None:
        requirements = (ROOT / "apps/ocr-worker/src/requirements.txt").read_text().splitlines()
        self.assertIn("paddleocr==3.7.0", requirements)
        self.assertIn("paddlepaddle==3.3.1", requirements)
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

    def test_paddle_cpu_runtime_avoids_broken_onednn_pir_path(self) -> None:
        engine = (ROOT / "apps/ocr-worker/src/extract/paddle_engine.py").read_text()
        dockerfile = (ROOT / "apps/ocr-worker/src/Dockerfile").read_text()
        self.assertLess(engine.index("FLAGS_enable_pir_api"), engine.index("from paddleocr import PaddleOCR"))
        self.assertIn('"enable_mkldnn": False', engine)
        self.assertIn("FLAGS_enable_pir_api=0", dockerfile)
        self.assertIn("enable_mkldnn=False", dockerfile)

    def test_no_external_ocr_or_vision_api(self) -> None:
        source = "\n".join(
            path.read_text(errors="replace")
            for path in (ROOT / "apps/ocr-worker/src").rglob("*.py")
        )
        external_api = re.compile(r"https?://|requests\.(?:post|get)|openai|anthropic|gemini", re.IGNORECASE)
        self.assertIsNone(external_api.search(source))


if __name__ == "__main__":
    unittest.main()
