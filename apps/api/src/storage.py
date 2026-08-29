from __future__ import annotations

import io
from typing import Any

from minio import Minio
from minio.error import S3Error

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
        del locked
        try:
            self.client.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
        else:
            raise StateConflict(f"Immutable object already exists: {bucket}/{key}")

        # BRONZE_PAIR_READY protects the returned exact version through the
        # policy enforcer.  Do not attach an arbitrary key-level duration here.
        retention = None
        result = self.client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
            retention=retention,
        )
        return {"etag": result.etag, "version_id": result.version_id}

    def get_exact(self, bucket: str, key: str, version_id: str) -> bytes:
        response = self.client.get_object(bucket, key, version_id=version_id)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def list_exact_versions(self, bucket: str, key: str) -> list[str]:
        versions = [
            item.version_id
            for item in self.client.list_objects(
                bucket,
                prefix=key,
                recursive=True,
                include_version=True,
            )
            if item.object_name == key
            and not item.is_delete_marker
            and isinstance(item.version_id, str)
            and item.version_id
        ]
        return sorted(set(versions))

    def get(self, bucket: str, key: str) -> bytes:
        """Read non-Bronze mutable artifacts; never use as Bronze proof."""
        response = self.client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
