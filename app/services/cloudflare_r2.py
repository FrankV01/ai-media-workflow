"""app.services.cloudflare_r2 — Cloudflare R2 delivery-hosting service

Uploads a job's verified delivery archive to Cloudflare R2 so buyers can
download it — the delivery ZIP is too large for FourthWall's digital-file
limit, so the storefront hands out a link document pointing at the public
R2 object URL instead.

The block is best-effort: this service never raises for upload outcomes.
It records a CloudflareUpload row per attempt (RUNNING → READY | FAILED)
and returns a result dict whose `cloudflare_upload_status` is "ready",
"failed", or "skipped" (unconfigured, nothing to upload, or already
uploaded). Only infrastructure-level surprises (missing job) raise.

Retry mirrors the listing-only retry flow: reserve → schedule → run under
the workload guard, appending a `cloudflare_r2_uploader` JobStep without
rerunning any creative steps. Because upload failures are best-effort,
the job's terminal status is preserved regardless of the outcome.

Object keys are deterministic — `<key_prefix><archive sha256[:16]>-<name>`
— so re-uploads of the same archive hit the same object and keys are not
enumerable. The public URL is `<public_base_url>/<object_key>`; the base
is the bucket's r2.dev dev URL or a custom domain, configured under
Settings → Store Fronts → Cloudflare R2.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.database
from app.config import settings
from app.models.cloudflare import CloudflareUpload, CloudflareUploadStatus
from app.models.delivery import DeliveryArchive
from app.models.job import Job, JobStatus, JobStep
from app.services.cloudflare_r2_settings import get_r2_settings
from app.services.delivery_archive import archive_download_path, latest_ready_archive
from app.services.r2_storage import Boto3R2Storage, R2Storage
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

UPLOAD_BLOCK_NAME = "cloudflare_r2_uploader"


class CloudflareError(RuntimeError):
    """An R2 upload could not be produced."""


class UploadRetryError(RuntimeError):
    """Upload-only retry was requested for an ineligible job."""

    def __init__(self, detail: str, status_code: int = 409) -> None:
        self.status_code = status_code
        super().__init__(detail)


def configured_credentials() -> tuple[str, str] | None:
    """Env-held R2 API credentials, or None when unset."""
    key_id = settings.cloudflare_r2_access_key_id.strip()
    secret = settings.cloudflare_r2_secret_access_key.strip()
    if not key_id or not secret:
        return None
    return key_id, secret


def object_key_for(key_prefix: str, archive_sha256: str | None, archive_name: str) -> str:
    """Deterministic, unguessable-ish object key for an archive."""
    digest = (archive_sha256 or "nosha")[:16]
    return f"{key_prefix}{digest}-{archive_name}"


def public_url_for(public_base_url: str, object_key: str) -> str:
    """Public object URL under the bucket's public access base."""
    return f"{public_base_url.rstrip('/')}/{object_key}"


async def latest_ready_upload(
    session: AsyncSession, delivery_archive_id: int
) -> CloudflareUpload | None:
    """Newest ready upload covering a delivery archive, or None."""
    result = await session.execute(
        select(CloudflareUpload)
        .where(
            CloudflareUpload.delivery_archive_id == delivery_archive_id,
            CloudflareUpload.status == CloudflareUploadStatus.READY,
        )
        .order_by(CloudflareUpload.attempt.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_uploads(session: AsyncSession, job_id: int) -> list[CloudflareUpload]:
    """All upload attempts for a job, newest first."""
    result = await session.execute(
        select(CloudflareUpload)
        .where(CloudflareUpload.job_id == job_id)
        .order_by(CloudflareUpload.attempt.desc())
    )
    return list(result.scalars().all())


def _result(
    status: str,
    *,
    upload_id: int | None = None,
    object_key: str | None = None,
    object_url: str | None = None,
    note: str = "",
    warning: str | None = None,
) -> dict[str, Any]:
    """Uniform block-result payload; `brief` keeps the chain readable."""
    return {
        "brief": note,
        "cloudflare_upload_id": upload_id,
        "cloudflare_r2_url": object_url,
        "cloudflare_r2_object_key": object_key,
        "cloudflare_upload_status": status,
        "cloudflare_r2_output": note,
        "_warning": warning,
    }


async def _fail_upload(upload_id: int, exc: BaseException) -> None:
    """Best-effort: record the failure on the attempt row in a fresh session."""
    try:
        async with app.database.async_session() as session:
            upload = await session.get(CloudflareUpload, upload_id)
            if upload is not None:
                upload.status = CloudflareUploadStatus.FAILED
                upload.error = str(exc)
                upload.error_type = type(exc).__name__
                upload.finished_at = datetime.now(UTC)
                await session.commit()
    except Exception:
        logger.exception("Could not record failure for cloudflare upload %s", upload_id)


async def create_cloudflare_upload(
    job_id: int,
    *,
    job_step_id: int | None = None,
    storage: R2Storage | None = None,
) -> dict[str, Any]:
    """Upload the job's latest ready delivery archive to Cloudflare R2.

    Returns a result dict for the pipeline context; `cloudflare_upload_status`
    is "ready", "failed", or "skipped". Never raises for upload outcomes —
    failures land on the attempt row and in `_warning` for the block to
    surface as a job warning.
    """
    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise CloudflareError(f"Job {job_id} not found")
        workflow_id = job.workflow_id
        r2 = await get_r2_settings(session)
        archive = await latest_ready_archive(session, job_id)
        existing = (
            await latest_ready_upload(session, archive.id) if archive is not None else None
        )

    credentials = configured_credentials()
    if credentials is None:
        note = (
            "Cloudflare R2 upload skipped: CLOUDFLARE_R2_ACCESS_KEY_ID / "
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY are not configured"
        )
        return _result("skipped", note=note, warning=note)
    if archive is None:
        note = (
            "Cloudflare R2 upload skipped: no ready delivery archive — "
            "run delivery_archive first"
        )
        return _result("skipped", note=note, warning=note)
    if existing is not None:
        note = f"Cloudflare R2 upload skipped: archive already uploaded ({existing.object_key})"
        return _result(
            "skipped",
            upload_id=existing.id,
            object_key=existing.object_key,
            object_url=existing.object_url,
            note=note,
        )
    if not r2.public_base_url.strip():
        note = (
            "Cloudflare R2 upload skipped: no public base URL configured — enable the "
            "bucket's r2.dev public URL (or a custom domain) and set it under "
            "settings → Store Fronts → Cloudflare R2"
        )
        return _result("skipped", note=note, warning=note)

    object_key = object_key_for(r2.key_prefix, archive.sha256, archive.archive_name)
    object_url = public_url_for(r2.public_base_url, object_key)

    # Commit the attempt row before any network/filesystem work so failures
    # always have an audit record (same contract as create_delivery_archive).
    async with app.database.async_session() as session:
        attempt = (
            await session.execute(
                select(func.max(CloudflareUpload.attempt)).where(
                    CloudflareUpload.job_id == job_id
                )
            )
        ).scalar() or 0
        attempt += 1
        record = CloudflareUpload(
            job_id=job_id,
            workflow_id=workflow_id,
            job_step_id=job_step_id,
            delivery_archive_id=archive.id,
            attempt=attempt,
            status=CloudflareUploadStatus.RUNNING,
            started_at=datetime.now(UTC),
            settings_json=json.dumps(r2.to_dict(), ensure_ascii=False),
            archive_name=archive.archive_name,
            archive_byte_size=archive.byte_size,
            archive_sha256=archive.sha256,
            bucket=r2.bucket,
            object_key=object_key,
            object_url=object_url,
        )
        session.add(record)
        await session.commit()
        upload_id = record.id

    try:
        archive_path = await asyncio.to_thread(archive_download_path, archive)
        owns_storage = storage is None
        if owns_storage:
            storage = Boto3R2Storage(
                access_key_id=credentials[0],
                secret_access_key=credentials[1],
                endpoint_url=r2.endpoint_url,
            )
        assert storage is not None
        result = await storage.upload_file(
            bucket=r2.bucket,
            key=object_key,
            path=archive_path,
            content_type="application/zip",
        )

        async with app.database.async_session() as session:
            record = await session.get(CloudflareUpload, upload_id)
            record.status = CloudflareUploadStatus.READY
            record.finished_at = datetime.now(UTC)
            record.etag = result.etag
            record.archive_byte_size = result.byte_size or archive.byte_size
            await session.commit()
        note = (
            f"Uploaded {archive.archive_name} to R2 "
            f"({r2.bucket}/{object_key}, {result.byte_size // (1024 * 1024)} MB)"
        )
        return _result(
            "ready",
            upload_id=upload_id,
            object_key=object_key,
            object_url=object_url,
            note=note,
        )
    except Exception as exc:
        await _fail_upload(upload_id, exc)
        note = f"Cloudflare R2 upload failed: {exc}"
        return _result("failed", upload_id=upload_id, note=note, warning=note)


# ── Upload-only retry ─────────────────────────────────────────────────────


def upload_retry_ineligibility(
    job: Job,
    steps: list[JobStep],
    archive: DeliveryArchive | None,
    uploads: list[CloudflareUpload],
) -> str | None:
    """None when an upload-only retry may be reserved; otherwise the reason."""
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        return "job is still running"
    if any(s.status in (JobStatus.PENDING, JobStatus.RUNNING) for s in steps):
        return "job has unfinished steps"
    if archive is None:
        return "job has no ready delivery archive to upload"
    if any(
        upload.status == CloudflareUploadStatus.READY
        and upload.delivery_archive_id == archive.id
        for upload in uploads
    ):
        return "the latest archive is already uploaded"
    if any(upload.status == CloudflareUploadStatus.RUNNING for upload in uploads):
        return "an upload attempt is already in progress"
    return None


async def upload_retryable(session: AsyncSession, job_id: int) -> bool:
    """Read-only eligibility check shared by the web/API surfaces."""
    job = await session.get(Job, job_id)
    if job is None:
        return False
    steps = list(
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
            )
        ).scalars()
    )
    archive = await latest_ready_archive(session, job_id)
    uploads = await list_uploads(session, job_id)
    return upload_retry_ineligibility(job, steps, archive, uploads) is None


async def reserve_upload_retry(session: AsyncSession, job_id: int) -> int:
    """Reserve an upload-only retry; returns the new pending JobStep id.

    Atomically claims the job back into RUNNING and appends a pending
    cloudflare_r2_uploader step in one transaction, so concurrent
    submissions cannot both enqueue. Raises UploadRetryError when
    ineligible.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise UploadRetryError(f"Job {job_id} not found", status_code=404)
    steps = list(
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
            )
        ).scalars()
    )
    archive = await latest_ready_archive(session, job_id)
    uploads = await list_uploads(session, job_id)
    ineligible = upload_retry_ineligibility(job, steps, archive, uploads)
    if ineligible is not None:
        raise UploadRetryError(ineligible)

    prior_status = job.status  # capture before the bulk update touches it
    claimed = await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.COMPLETED, JobStatus.FAILED]),
        )
        .values(status=JobStatus.RUNNING, finished_at=None)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise UploadRetryError("upload retry is already in progress")
    step = JobStep(
        job_id=job_id,
        block_name=UPLOAD_BLOCK_NAME,
        order=steps[-1].order + 1 if steps else 0,
        status=JobStatus.PENDING,
        input_context=json.dumps({"prior_job_status": prior_status.value}),
    )
    session.add(step)
    await session.commit()
    return step.id


async def run_upload_retry(job_id: int, job_step_id: int) -> None:
    """Background task: run only the upload block for a reserved retry.

    The job's prior terminal status is restored afterwards — a failed
    upload never re-fails an otherwise-completed job.
    """
    async with workload_guard.hold(f"upload-retry-job-{job_id}"):
        async with app.database.async_session() as session:
            job = await session.get(Job, job_id)
            step = await session.get(JobStep, job_step_id)
            if (
                job is None
                or step is None
                or job.status != JobStatus.RUNNING
                or step.status != JobStatus.PENDING
            ):
                return
            try:
                prior_status = JobStatus(
                    json.loads(step.input_context or "{}").get("prior_job_status")
                    or JobStatus.COMPLETED
                )
            except ValueError:
                prior_status = JobStatus.COMPLETED
            claimed = await session.execute(
                update(JobStep)
                .where(JobStep.id == job_step_id, JobStep.status == JobStatus.PENDING)
                .values(
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(UTC),
                    input_context=json.dumps(
                        {
                            "_job_id": job.id,
                            "_job_step_id": job_step_id,
                            "_workflow_id": job.workflow_id,
                            "photo_shoot_name": job.workflow_name,
                            "upload_retry": True,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return
            await session.commit()
            step.status = JobStatus.RUNNING
            try:
                result = await create_cloudflare_upload(job_id, job_step_id=job_step_id)
            except Exception as exc:
                logger.exception("Upload retry failed for job %s", job_id)
                step.status = JobStatus.FAILED
                step.error = str(exc)
                step.error_type = type(exc).__name__
            else:
                step.output = json.dumps(result, default=str, ensure_ascii=False)
                if result.get("cloudflare_upload_status") == "failed":
                    step.status = JobStatus.FAILED
                    step.error = result.get("cloudflare_r2_output")
                else:
                    step.status = JobStatus.COMPLETED
            finally:
                step.finished_at = datetime.now(UTC)
                job.status = prior_status
                job.finished_at = datetime.now(UTC)
                await session.commit()


def schedule_upload_retry(job_id: int, job_step_id: int) -> None:
    """Queue the reserved retry as a background task."""
    asyncio.create_task(
        run_upload_retry(job_id, job_step_id),
        name=f"upload-retry-job-{job_id}",
    )
