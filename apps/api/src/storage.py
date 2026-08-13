from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.error import S3Error
from minio.retention import Retention

from domain import StateConflict


class MinioObjectStorage:
    def __init__(self, client: Minio) -> None:
        self.client = client

    def put_once(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str,
        locked: bool,
    ) -> dict[str, Any]:
        try:
            self.client.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
        else:
            raise StateConflict(f"Immutable object already exists: {bucket}/{key}")

        retention = None
        if locked:
            retention = Retention(COMPLIANCE, datetime.now(UTC) + timedelta(days=365))
        result = self.client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
            retention=retention,
        )
        return {"etag": result.etag, "version_id": result.version_id}

    def get(self, bucket: str, key: str) -> bytes:
        response = self.client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
