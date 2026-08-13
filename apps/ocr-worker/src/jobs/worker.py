from __future__ import annotations

import mimetypes
import os
import tempfile
import time
from pathlib import Path

from database import PostgresRepository
from domain import ARTIFACTS_BUCKET, ORIGINALS_BUCKET, OCRDomainService
from extract.excel import extract_workbook
from extract.paddle_engine import CONFIGURATION, ENGINE_VERSION, PaddleEngine
from extract.tesseract_benchmark import benchmark
from minio import Minio
from storage import MinioObjectStorage

PADDLE: PaddleEngine | None = None


def main() -> None:
    repository = PostgresRepository(os.environ["DATABASE_URL"])
    client = Minio(
        os.getenv("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )
    storage = MinioObjectStorage(client)
    service = OCRDomainService(repository)
    delay = int(os.getenv("OCR_POLL_SECONDS", "3"))
    while True:
        job = repository.claim_next_job()
        if not job:
            time.sleep(delay)
            continue
        try:
            process_job(job, service, storage)
        except Exception as exc:  # worker boundary records a terminal, auditable failure
            repository.mark_ocr_failed(job["ingestion_id"], f"{type(exc).__name__}: {exc}")


def process_job(job: dict[str, object], service: OCRDomainService, storage: MinioObjectStorage) -> None:
    global PADDLE
    ingestion_id = str(job["ingestion_id"])
    source_key = str(job["stored_object_key"])
    mime_type = str(job["detected_mime_type"])
    extension = mimetypes.guess_extension(mime_type) or ".bin"
    source_bytes = storage.get(ORIGINALS_BUCKET, source_key)

    if str(job["declared_file_type"]) == "EXCEL":
        configuration = {"engine": "openpyxl", "tesseract_benchmark": "not-applicable"}
        run_id = service.start(ingestion_id, "openpyxl", "3.1.5", configuration)
        with tempfile.TemporaryDirectory(prefix="sc-rd-ocr-") as directory:
            source = Path(directory) / f"source{extension}"
            source.write_bytes(source_bytes)
            text, blocks, raw = extract_workbook(source)
        artifact_key = f"rd/{source_key.split('/')[1]}/{source_key.split('/')[2]}/{ingestion_id}/ocr-run/{run_id}.json"
        storage.put_once(ARTIFACTS_BUCKET, artifact_key, raw, "application/json", locked=False)
        service.complete(ingestion_id, run_id, text, blocks, raw, artifact_key)
        return

    run_id = service.start(ingestion_id, "paddleocr", ENGINE_VERSION, CONFIGURATION)
    with tempfile.TemporaryDirectory(prefix="sc-rd-ocr-") as directory:
        root = Path(directory)
        source = root / f"source{extension}"
        source.write_bytes(source_bytes)
        from preprocess.documents import preprocess_source

        images = preprocess_source(source, mime_type, root / "preprocessed")
        year, month = source_key.split("/")[1:3]
        for index, image in enumerate(images, start=1):
            key = f"rd/{year}/{month}/{ingestion_id}/preprocessed/page-{index:04d}.png"
            storage.put_once(ARTIFACTS_BUCKET, key, image.read_bytes(), "image/png", locked=False)

        if PADDLE is None:
            PADDLE = PaddleEngine()
        text, blocks, raw = PADDLE.extract(images)
        tesseract_raw = benchmark(images)

    base = f"rd/{year}/{month}/{ingestion_id}/ocr-run"
    artifact_key = f"{base}/{run_id}.json"
    benchmark_key = f"{base}/{run_id}-tesseract-benchmark.json"
    storage.put_once(ARTIFACTS_BUCKET, artifact_key, raw, "application/json", locked=False)
    storage.put_once(ARTIFACTS_BUCKET, benchmark_key, tesseract_raw, "application/json", locked=False)
    service.complete(ingestion_id, run_id, text, blocks, raw, artifact_key)


if __name__ == "__main__":
    main()
