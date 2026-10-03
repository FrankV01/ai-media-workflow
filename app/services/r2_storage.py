"""app.services.r2_storage — Object-storage abstraction for Cloudflare R2

R2Storage is the seam the upload service depends on (dependency inversion):
the orchestration code in app.services.cloudflare_r2 knows nothing about
boto3, and tests inject a fake implementation instead of a live client.

Boto3R2Storage is the production adapter — R2 speaks the S3 API, so the
standard boto3 client with a custom endpoint_url works. boto3 is a
synchronous library; calls are wrapped in asyncio.to_thread so the event
loop is never blocked by a large transfer. upload_file() drives
S3 multipart automatically for large objects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class R2UploadResult:
    """What the storage layer reports after a successful upload."""

    etag: str | None
    byte_size: int


@runtime_checkable
class R2Storage(Protocol):
    """Minimal object-put contract the upload service relies on."""

    async def upload_file(
        self,
        *,
        bucket: str,
        key: str,
        path: Path,
        content_type: str,
    ) -> R2UploadResult:
        """Upload `path` to `bucket`/`key`; raises on any failure."""
        ...


class R2StorageError(RuntimeError):
    """The storage layer could not complete an upload."""


class Boto3R2Storage:
    """R2Storage adapter backed by boto3's S3 client against Cloudflare R2."""

    def __init__(
        self,
        *,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._client_kwargs = {
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }
        self._client = None  # created lazily inside the worker thread

    def _get_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                region_name="auto",
                config=Config(signature_version="s3v4"),
                **self._client_kwargs,
            )
        return self._client

    async def upload_file(
        self,
        *,
        bucket: str,
        key: str,
        path: Path,
        content_type: str,
    ) -> R2UploadResult:
        def _put() -> R2UploadResult:
            try:
                client = self._get_client()
                client.upload_file(
                    str(path),
                    bucket,
                    key,
                    ExtraArgs={"ContentType": content_type},
                )
                head = client.head_object(Bucket=bucket, Key=key)
            except Exception as exc:  # botocore raises many concrete types
                raise R2StorageError(f"R2 upload failed: {exc}") from exc
            etag = head.get("ETag")
            if isinstance(etag, str):
                etag = etag.strip('"')
            return R2UploadResult(
                etag=etag or None,
                byte_size=int(head.get("ContentLength") or path.stat().st_size),
            )

        return await asyncio.to_thread(_put)
