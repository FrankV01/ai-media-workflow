"""
tests.test_cloudflare_r2 — Cloudflare R2 upload block + service + link docs

Covers the cloudflare.r2 Setting service, the /settings/general R2 section,
the best-effort upload flow against an injected fake R2Storage (no live R2,
no moto), object-key/URL derivation, skip/failure semantics, idempotency,
upload-only retry, and the PDF/Markdown link-document builder.
"""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import app.database
import app.services.cloudflare_r2 as r2
import app.services.cloudflare_r2_settings as rs
from app.config import settings
from app.models.cloudflare import CloudflareUpload, CloudflareUploadStatus
from app.models.delivery import DeliveryArchive, DeliveryArchiveStatus
from app.models.job import Job, JobStatus, JobStep
from app.pipeline.naming import output_subdir
from app.services.link_document import LinkDocumentError, build_link_document
from app.services.r2_storage import R2UploadResult


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path / "output")
    return tmp_path / "output"


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_r2_access_key_id", "r2-key")
    monkeypatch.setattr(settings, "cloudflare_r2_secret_access_key", "r2-secret")


@pytest.fixture
async def uploadable(output_dir, credentials):
    """Credentials + a public base URL — everything an upload needs."""
    async with app.database.async_session() as session:
        await rs.save_r2_settings(
            session, public_base_url="https://pub-abc.r2.dev"
        )
        await session.commit()


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("img.png", b"\x89PNG fake")
        archive.writestr("report.md", "# Report\n")
    return buffer.getvalue()


async def _upload(job_id: int) -> CloudflareUpload | None:
    async with app.database.async_session() as session:
        return (
            (
                await session.execute(
                    select(CloudflareUpload)
                    .where(CloudflareUpload.job_id == job_id)
                    .order_by(CloudflareUpload.attempt.desc())
                )
            )
            .scalars()
            .first()
        )


async def _seed_job(
    output_dir: Path,
    *,
    name: str = "Neon Shoot",
    with_archive: bool = True,
    status: JobStatus = JobStatus.COMPLETED,
) -> tuple[Job, Path, DeliveryArchive | None]:
    """Job row + output dir + a ready archive on disk."""
    async with app.database.async_session() as session:
        job = Job(
            workflow_name=name,
            status=status,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
            finished_at=datetime(2026, 1, 15, 0, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        source = output_dir / output_subdir(name, job.id, job.created_at.date())
        source.mkdir(parents=True, exist_ok=True)
        session.add(
            JobStep(
                job_id=job.id,
                block_name="delivery_archive",
                order=0,
                status=JobStatus.COMPLETED,
                output="{}",
            )
        )
        archive = None
        if with_archive:
            zip_path = source / f"neon-shoot-job{job.id}.zip"
            zip_path.write_bytes(_zip_bytes())
            archive = DeliveryArchive(
                job_id=job.id,
                attempt=1,
                status=DeliveryArchiveStatus.READY,
                source_dir=str(source),
                settings_json="{}",
                archive_path=str(zip_path),
                archive_name=zip_path.name,
                byte_size=zip_path.stat().st_size,
                sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest(),
            )
            session.add(archive)
        await session.commit()
        return job, source, archive


class _FakeStorage:
    """R2Storage test double: records calls, returns a canned result."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error

    async def upload_file(self, *, bucket, key, path, content_type):
        self.calls.append(
            {
                "bucket": bucket,
                "key": key,
                "path": path,
                "content_type": content_type,
            }
        )
        if self._error is not None:
            raise self._error
        return R2UploadResult(
            etag="deadbeef", byte_size=Path(path).stat().st_size
        )


# ── Settings service ──────────────────────────────────────────────────────


async def test_r2_settings_defaults(output_dir):
    async with app.database.async_session() as session:
        record = await rs.get_r2_settings(session)
    assert record.bucket == "ai-media"
    assert record.key_prefix == "fourthwall/"
    assert record.endpoint_url.endswith("r2.cloudflarestorage.com")
    assert record.public_base_url == ""


async def test_r2_settings_roundtrip(output_dir):
    async with app.database.async_session() as session:
        await rs.save_r2_settings(
            session,
            endpoint_url="https://acct.r2.cloudflarestorage.com",
            bucket="other-bucket",
            key_prefix="media/",
            public_base_url="https://cdn.example.com",
        )
        await session.commit()
    async with app.database.async_session() as session:
        record = await rs.get_r2_settings(session)
    assert record.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert record.bucket == "other-bucket"
    assert record.key_prefix == "media/"
    assert record.public_base_url == "https://cdn.example.com"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"endpoint_url": ""}, "endpoint_url"),
        ({"endpoint_url": "notaurl"}, "endpoint_url"),
        ({"bucket": ""}, "bucket"),
        ({"bucket": "a/b"}, "bucket"),
        ({"public_base_url": "ftp://x"}, "public_base_url"),
    ],
)
async def test_r2_settings_validation(output_dir, kwargs, match):
    async with app.database.async_session() as session:
        with pytest.raises(rs.R2SettingsError, match=match):
            await rs.save_r2_settings(session, **kwargs)


def _web_client():
    import app.main

    transport = httpx.ASGITransport(app=app.main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_settings_page_shows_r2(output_dir):
    async with _web_client() as client:
        page = await client.get("/settings/general")
    assert page.status_code == 200
    assert "Cloudflare R2" in page.text
    assert 'name="r2_public_base_url"' in page.text
    assert 'name="r2_endpoint_url"' in page.text


async def test_r2_save_via_web(output_dir):
    async with _web_client() as client:
        response = await client.post(
            "/settings/general/cloudflare/save",
            data={
                "r2_endpoint_url": "https://acct.r2.cloudflarestorage.com",
                "r2_bucket": "ai-media",
                "r2_key_prefix": "fourthwall/",
                "r2_public_base_url": "https://pub-xyz.r2.dev",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    async with app.database.async_session() as session:
        record = await rs.get_r2_settings(session)
    assert record.public_base_url == "https://pub-xyz.r2.dev"


# ── Key/URL derivation ────────────────────────────────────────────────────


def test_object_key_format():
    key = r2.object_key_for("fourthwall/", "ab" * 32, "shoot.zip")
    assert key == f"fourthwall/{'ab' * 8}-shoot.zip"
    assert len(key) - len("fourthwall/") - len("-shoot.zip") == 16


def test_object_key_without_sha():
    assert r2.object_key_for("p/", None, "a.zip") == "p/nosha-a.zip"


def test_public_url_strips_trailing_slash():
    assert (
        r2.public_url_for("https://pub-abc.r2.dev/", "fourthwall/x.zip")
        == "https://pub-abc.r2.dev/fourthwall/x.zip"
    )


# ── Upload flow ───────────────────────────────────────────────────────────


async def test_upload_happy_path(output_dir, uploadable):
    job, _, archive = await _seed_job(output_dir)
    storage = _FakeStorage()

    result = await r2.create_cloudflare_upload(
        job.id, job_step_id=11, storage=storage
    )

    assert result["cloudflare_upload_status"] == "ready"
    assert result["cloudflare_r2_url"].startswith("https://pub-abc.r2.dev/")
    assert archive.sha256[:16] in result["cloudflare_r2_object_key"]
    assert result["cloudflare_r2_object_key"].endswith(archive.archive_name)

    call = storage.calls[0]
    assert call["bucket"] == "ai-media"
    assert call["key"] == result["cloudflare_r2_object_key"]
    assert call["content_type"] == "application/zip"
    assert Path(call["path"]).read_bytes() == _zip_bytes()

    upload = await _upload(job.id)
    assert upload is not None
    assert upload.status == CloudflareUploadStatus.READY
    assert upload.attempt == 1
    assert upload.job_step_id == 11
    assert upload.delivery_archive_id == archive.id
    assert upload.object_url == result["cloudflare_r2_url"]
    assert upload.etag == "deadbeef"
    assert upload.archive_sha256 == archive.sha256


async def test_upload_skipped_without_credentials(output_dir, monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_r2_access_key_id", "")
    monkeypatch.setattr(settings, "cloudflare_r2_secret_access_key", "")
    job, _, _ = await _seed_job(output_dir)
    result = await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    assert result["cloudflare_upload_status"] == "skipped"
    assert "CLOUDFLARE_R2_ACCESS_KEY_ID" in result["_warning"]
    assert await _upload(job.id) is None


async def test_upload_skipped_without_archive(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir, with_archive=False)
    result = await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    assert result["cloudflare_upload_status"] == "skipped"
    assert "delivery archive" in result["_warning"]
    assert await _upload(job.id) is None


async def test_upload_skipped_without_public_url(output_dir, credentials):
    job, _, _ = await _seed_job(output_dir)
    result = await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    assert result["cloudflare_upload_status"] == "skipped"
    assert "public base URL" in result["_warning"]
    assert await _upload(job.id) is None


async def test_upload_is_idempotent(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir)
    first = await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    second = await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    assert first["cloudflare_upload_status"] == "ready"
    assert second["cloudflare_upload_status"] == "skipped"
    assert second["cloudflare_r2_url"] == first["cloudflare_r2_url"]
    assert second["cloudflare_upload_id"] == first["cloudflare_upload_id"]
    async with app.database.async_session() as session:
        count = (
            await session.execute(
                select(CloudflareUpload).where(CloudflareUpload.job_id == job.id)
            )
        ).scalars().all()
    assert len(count) == 1


async def test_upload_failure_is_best_effort(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir)
    storage = _FakeStorage(error=RuntimeError("r2 boom"))
    result = await r2.create_cloudflare_upload(job.id, storage=storage)
    assert result["cloudflare_upload_status"] == "failed"
    assert "r2 boom" in result["_warning"]
    upload = await _upload(job.id)
    assert upload.status == CloudflareUploadStatus.FAILED
    assert upload.error_type == "RuntimeError"
    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
    assert job_row.status == JobStatus.COMPLETED


async def test_upload_settings_snapshot_has_no_secrets(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir)
    await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    upload = await _upload(job.id)
    snapshot = json.loads(upload.settings_json)
    assert "key" not in snapshot  # no access-key fields at all
    assert "r2-secret" not in upload.settings_json
    assert snapshot["bucket"] == "ai-media"


# ── Block ─────────────────────────────────────────────────────────────────


async def test_block_requires_job_id():
    from app.blocks.cloudflare_r2_uploader import CloudflareR2Uploader

    block = CloudflareR2Uploader()
    assert block.meta.version == "0.1.0"
    assert block.meta.category == "production"
    with pytest.raises(ValueError, match="_job_id"):
        await block.validate({})
    await block.validate({"_job_id": 1})


async def test_block_run_surfaces_warning(output_dir, monkeypatch):
    from app.blocks.cloudflare_r2_uploader import CloudflareR2Uploader

    async def fake_create(job_id, *, job_step_id=None, storage=None):
        return {
            "brief": "nope",
            "cloudflare_upload_id": None,
            "cloudflare_r2_url": None,
            "cloudflare_r2_object_key": None,
            "cloudflare_upload_status": "skipped",
            "cloudflare_r2_output": "nope",
            "_warning": "creds missing",
        }

    monkeypatch.setattr(r2, "create_cloudflare_upload", fake_create)
    context: dict = {"_job_id": 3}
    result = await CloudflareR2Uploader().run(context)
    assert "_warning" not in result
    assert context["_warnings"] == ["creds missing"]


# ── Retry ─────────────────────────────────────────────────────────────────


async def test_upload_retry_reserve_and_run(output_dir, uploadable, monkeypatch):
    job, _, _ = await _seed_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await r2.reserve_upload_retry(session, job.id)
        job_row = await session.get(Job, job.id)
    assert job_row.status == JobStatus.RUNNING

    async def fake_create(job_id, *, job_step_id=None, storage=None):
        return {
            "cloudflare_upload_status": "ready",
            "cloudflare_r2_url": "https://pub-abc.r2.dev/k",
        }

    monkeypatch.setattr(r2, "create_cloudflare_upload", fake_create)
    await r2.run_upload_retry(job.id, step_id)

    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
        step = await session.get(JobStep, step_id)
    assert job_row.status == JobStatus.COMPLETED
    assert step.status == JobStatus.COMPLETED
    assert json.loads(step.output)["cloudflare_r2_url"].endswith("/k")


async def test_upload_retry_restores_failed_job(output_dir, uploadable, monkeypatch):
    job, _, _ = await _seed_job(output_dir, status=JobStatus.FAILED)
    async with app.database.async_session() as session:
        step_id = await r2.reserve_upload_retry(session, job.id)

    async def fake_create(job_id, *, job_step_id=None, storage=None):
        return {
            "cloudflare_upload_status": "failed",
            "cloudflare_r2_output": "boom",
        }

    monkeypatch.setattr(r2, "create_cloudflare_upload", fake_create)
    await r2.run_upload_retry(job.id, step_id)
    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
        step = await session.get(JobStep, step_id)
    assert job_row.status == JobStatus.FAILED
    assert step.status == JobStatus.FAILED


async def test_upload_retry_ineligible_cases(output_dir, uploadable):
    async with app.database.async_session() as session:
        job = Job(
            workflow_name="No Archive",
            status=JobStatus.COMPLETED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(job)
        await session.commit()
        with pytest.raises(r2.UploadRetryError, match="no ready delivery archive"):
            await r2.reserve_upload_retry(session, job.id)

    job2, _, archive2 = await _seed_job(output_dir, name="Uploaded")
    async with app.database.async_session() as session:
        session.add(
            CloudflareUpload(
                job_id=job2.id,
                delivery_archive_id=archive2.id,
                attempt=1,
                status=CloudflareUploadStatus.READY,
                settings_json="{}",
                object_url="https://pub-abc.r2.dev/k",
            )
        )
        await session.commit()
        with pytest.raises(r2.UploadRetryError, match="already uploaded"):
            await r2.reserve_upload_retry(session, job2.id)


async def test_upload_retry_api(output_dir, uploadable, monkeypatch):
    job, _, _ = await _seed_job(output_dir)
    scheduled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.api.workflows.schedule_upload_retry",
        lambda job_id, step_id: scheduled.append((job_id, step_id)),
    )
    async with _web_client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/uploads/retry")
    assert response.status_code == 202
    assert response.json() == {"job_id": job.id, "status": "retrying"}
    assert len(scheduled) == 1 and scheduled[0][0] == job.id


async def test_upload_retry_api_ineligible(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir, with_archive=False)
    async with _web_client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/uploads/retry")
    assert response.status_code == 409


async def test_uploads_list_api(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir)
    await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    async with _web_client() as client:
        response = await client.get(f"/api/workflows/jobs/{job.id}/uploads")
        job_response = await client.get(f"/api/workflows/jobs/{job.id}")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["status"] == "ready"
    assert rows[0]["bucket"] == "ai-media"
    assert rows[0]["object_url"].startswith("https://pub-abc.r2.dev/")
    assert job_response.json()["uploads"][0]["object_key"].startswith("fourthwall/")


async def test_job_detail_page_shows_upload(output_dir, uploadable):
    job, _, _ = await _seed_job(output_dir)
    await r2.create_cloudflare_upload(job.id, storage=_FakeStorage())
    async with _web_client() as client:
        page = await client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert "Cloudflare R2 uploads" in page.text
    assert "pub-abc.r2.dev" in page.text


# ── Link document builder ─────────────────────────────────────────────────


def test_link_document_markdown():
    doc = build_link_document(
        "markdown",
        title="Neon Shoot",
        url="https://pub-abc.r2.dev/fourthwall/k.zip",
        archive_name="neon.zip",
        byte_size=150 * 1024 * 1024,
        sha256="ab" * 32,
    )
    assert doc.filename == "neon-shoot-download.md"
    assert doc.content_type == "text/markdown"
    text = doc.data.decode()
    assert "https://pub-abc.r2.dev/fourthwall/k.zip" in text
    assert "neon.zip" in text
    assert "150 MB" in text
    assert "ab" * 32 in text


def test_link_document_pdf():
    doc = build_link_document(
        "pdf",
        title="Neon Shoot",
        url="https://pub-abc.r2.dev/k.zip",
        archive_name="neon.zip",
        byte_size=1024,
        sha256="cd" * 32,
    )
    assert doc.filename == "neon-shoot-download.pdf"
    assert doc.content_type == "application/pdf"
    assert doc.data.startswith(b"%PDF")
    assert len(doc.data) < 64 * 1024  # a genuinely small deliverable


def test_link_document_minimal_fields():
    doc = build_link_document(
        "markdown", title="X", url="https://u", archive_name="a.zip"
    )
    text = doc.data.decode()
    assert "https://u" in text
    assert "SHA-256" not in text


def test_link_document_rejects_bad_format():
    with pytest.raises(LinkDocumentError, match="unsupported"):
        build_link_document("epub", title="X", url="https://u", archive_name="a")


def test_link_document_sanitizes_filename():
    doc = build_link_document(
        "markdown",
        title="Weird Title!!! / slash",
        url="https://u",
        archive_name="a.zip",
    )
    assert "/" not in doc.filename
    assert doc.filename.endswith("-download.md")
