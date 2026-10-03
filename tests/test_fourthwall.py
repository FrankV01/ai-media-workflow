"""
tests.test_fourthwall — FourthWall storefront listing block + service

Covers the storefronts.fourthwall Setting service, the /settings/general
Store Fronts section, and the best-effort listing flow against an
httpx.MockTransport fake API — product creation, the signed-upload header
contract, digital-file confirm, preview image upload/attach, skip/failure
semantics, idempotency, and listing-only retry.
"""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

import app.database
import app.services.fourthwall as fw
import app.services.storefront_settings as ss
from app.config import settings
from app.models.cloudflare import CloudflareUpload, CloudflareUploadStatus
from app.models.delivery import DeliveryArchive, DeliveryArchiveStatus
from app.models.job import Job, JobStatus, JobStep
from app.models.setting import Setting
from app.models.storefront import StorefrontListing, StorefrontListingStatus
from app.pipeline.naming import output_subdir
from app.services import general_settings as gs


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path / "output")
    return tmp_path / "output"


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr(settings, "fourthwall_api_user", "fw-user")
    monkeypatch.setattr(settings, "fourthwall_api_password", "fw-pass")


@pytest.fixture
async def listable(output_dir, credentials):
    """Credentials + a configured price — everything a listing needs."""
    async with app.database.async_session() as session:
        await ss.save_storefront_settings(session, price_usd=12.50)
        await session.commit()


def _png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="PNG")
    return buffer.getvalue()


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("img.png", _png_bytes())
        archive.writestr("report.md", "# Report\n")
    return buffer.getvalue()


async def _listing(job_id: int) -> StorefrontListing | None:
    async with app.database.async_session() as session:
        return (
            (
                await session.execute(
                    select(StorefrontListing)
                    .where(StorefrontListing.job_id == job_id)
                    .order_by(StorefrontListing.attempt.desc())
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
    with_steps: bool = True,
    with_upload: bool = True,
    status: JobStatus = JobStatus.COMPLETED,
) -> tuple[Job, Path, DeliveryArchive | None]:
    """Job row + output dir + step snapshots + ready archive + R2 upload."""
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

        if with_steps:
            small = source / "aimw_main_refined_1x_00001_.png"
            large = source / "aimw_main_upscaled_4x_00001_.png"
            small.write_bytes(_png_bytes((8, 8)))
            large.write_bytes(_png_bytes((64, 64)))
            session.add(
                JobStep(
                    job_id=job.id,
                    block_name="media_producer",
                    order=0,
                    status=JobStatus.COMPLETED,
                    output=json.dumps(
                        {
                            "generated_images": [str(small), str(large)],
                            "generation_metadata": [
                                {
                                    "variant_name": "main",
                                    "image_paths": [str(small), str(large)],
                                }
                            ],
                        }
                    ),
                )
            )
            session.add(
                JobStep(
                    job_id=job.id,
                    block_name="social_media_specialist",
                    order=1,
                    status=JobStatus.COMPLETED,
                    output=json.dumps(
                        {
                            "social_media_posts": [
                                {
                                    "platform": "image feed",
                                    "title": "feed post",
                                    "description": "feed caption",
                                    "tags": ["feed"],
                                },
                                {
                                    "platform": "stock-marketplace",
                                    "title": "Neon bundle",
                                    "description": "A neon-drenched artwork pack.",
                                    "tags": ["neon", "cyber"],
                                },
                            ]
                        }
                    ),
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
            await session.flush()
            if with_upload:
                session.add(
                    CloudflareUpload(
                        job_id=job.id,
                        delivery_archive_id=archive.id,
                        attempt=1,
                        status=CloudflareUploadStatus.READY,
                        settings_json="{}",
                        archive_name=archive.archive_name,
                        archive_byte_size=archive.byte_size,
                        archive_sha256=archive.sha256,
                        bucket="ai-media",
                        object_key=f"fourthwall/{archive.sha256[:16]}-{archive.archive_name}",
                        object_url=(
                            f"https://pub-abc.r2.dev/"
                            f"fourthwall/{archive.sha256[:16]}-{archive.archive_name}"
                        ),
                    )
                )
        await session.commit()
        return job, source, archive


def _fake_api(log: list[httpx.Request], *, fail_path: str | None = None):
    """MockTransport handler implementing the FourthWall digital flow."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        path, method = request.url.path, request.method
        if fail_path == f"{method} {path}":
            return httpx.Response(400, json={"code": "BOOM", "msg": "nope"})
        if request.url.host == "storage.googleapis.com" and method == "PUT":
            return httpx.Response(200)
        if method == "POST" and path == "/products":
            return httpx.Response(
                201, json={"productId": "prod-1", "customizationId": None, "images": []}
            )
        if method == "POST" and path.endswith("/digital-files/upload-url"):
            return httpx.Response(
                200,
                json={
                    "uploadUrl": "https://storage.googleapis.com/fw-signed-zip",
                    "fileUrl": "fw-file-1",
                },
            )
        if method == "POST" and path.endswith("/digital-files"):
            return httpx.Response(200, json={"productId": "prod-1"})
        if method == "POST" and path == "/media/upload-url":
            return httpx.Response(
                200,
                json={
                    "uploadUrl": "https://storage.googleapis.com/fw-signed-media",
                    "fileUrl": "fw-media-1",
                },
            )
        if method == "POST" and path.endswith("/images"):
            return httpx.Response(200, json={"productId": "prod-1"})
        return httpx.Response(404, json={"code": "UNKNOWN"})

    return httpx.MockTransport(handler)


def _client(log: list[httpx.Request], **kwargs) -> fw.FourthwallClient:
    return fw.FourthwallClient(
        user="u",
        password="p",
        base_url="http://fw.test",
        transport=_fake_api(log, **kwargs),
    )


# ── Settings service ──────────────────────────────────────────────────────


async def test_storefront_settings_defaults(output_dir):
    async with app.database.async_session() as session:
        storefront = await ss.get_storefront_settings(session)
    assert storefront.price_usd is None
    assert storefront.publish_on_create is False
    assert storefront.link_document_format == "pdf"
    assert storefront.max_preview_images == 4
    assert "$description" in storefront.description_template


async def test_storefront_settings_roundtrip(output_dir):
    async with app.database.async_session() as session:
        await ss.save_storefront_settings(
            session,
            price_usd=9.99,
            publish_on_create=True,
            link_document_format="markdown",
            description_template="$title — $tags",
            max_preview_images=2,
        )
        await session.commit()
    async with app.database.async_session() as session:
        storefront = await ss.get_storefront_settings(session)
    assert storefront.price_usd == 9.99
    assert storefront.publish_on_create is True
    assert storefront.link_document_format == "markdown"
    assert storefront.description_template == "$title — $tags"
    assert storefront.max_preview_images == 2


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"price_usd": "0"}, "price_usd"),
        ({"price_usd": "-5"}, "price_usd"),
        ({"link_document_format": "epub"}, "link_document_format"),
        ({"link_document_format": 123}, "link_document_format"),
        ({"max_preview_images": "99"}, "max_preview_images"),
        ({"description_template": "   "}, "description_template"),
        ({"description_template": "x" * (ss.MAX_TEMPLATE_CHARS + 1)}, "description_template"),
    ],
)
async def test_storefront_settings_validation(output_dir, kwargs, match):
    async with app.database.async_session() as session:
        with pytest.raises(ss.StorefrontSettingsError, match=match):
            await ss.save_storefront_settings(session, **kwargs)


def _web_client():
    import app.main

    transport = httpx.ASGITransport(app=app.main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_settings_page_shows_storefronts(output_dir):
    async with _web_client() as client:
        page = await client.get("/settings/general")
    assert page.status_code == 200
    assert "Store Fronts" in page.text
    assert "FourthWall" in page.text
    assert "Cloudflare R2" in page.text
    assert "PayHip" in page.text
    assert "cannot be automated" in page.text
    assert 'name="description_template"' in page.text
    assert 'name="link_document_format"' in page.text


async def test_storefronts_save_via_web(output_dir):
    async with _web_client() as client:
        response = await client.post(
            "/settings/general/storefronts/save",
            data={
                "price_usd": "7.50",
                "link_document_format": "markdown",
                "description_template": "$description ($tags)",
                "max_preview_images": "3",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    async with app.database.async_session() as session:
        storefront = await ss.get_storefront_settings(session)
    assert storefront.price_usd == 7.5
    assert storefront.publish_on_create is False
    assert storefront.link_document_format == "markdown"
    assert storefront.max_preview_images == 3


async def test_storefronts_save_validation_replays_form(output_dir):
    async with _web_client() as client:
        response = await client.post(
            "/settings/general/storefronts/save",
            data={
                "price_usd": "abc",
                "description_template": "keep $title",
            },
        )
    assert response.status_code == 422
    assert "price_usd" in response.text
    assert "keep $title" in response.text


# ── Listing flow ──────────────────────────────────────────────────────────


async def test_listing_skipped_without_credentials(output_dir, monkeypatch):
    monkeypatch.setattr(settings, "fourthwall_api_user", "")
    monkeypatch.setattr(settings, "fourthwall_api_password", "")
    job, _, _ = await _seed_job(output_dir)
    async with app.database.async_session() as session:
        await ss.save_storefront_settings(session, price_usd=5.0)
        await session.commit()
    result = await fw.create_storefront_listing(job.id)
    assert result["storefront_listing_status"] == "skipped"
    assert "FOURTHWALL_API_USER" in result["_warning"]
    assert await _listing(job.id) is None


async def test_listing_skipped_without_archive(output_dir, listable):
    job, _, _ = await _seed_job(output_dir, with_archive=False)
    result = await fw.create_storefront_listing(job.id)
    assert result["storefront_listing_status"] == "skipped"
    assert "delivery archive" in result["_warning"]
    assert await _listing(job.id) is None


async def test_listing_skipped_without_price(output_dir, credentials):
    job, _, _ = await _seed_job(output_dir)
    async with app.database.async_session() as session:
        session.add(
            Setting(
                key=ss.STOREFRONT_SETTINGS_KEY,
                value=json.dumps({"price_usd": None}),
            )
        )
        await session.commit()
    result = await fw.create_storefront_listing(job.id)
    assert result["storefront_listing_status"] == "skipped"
    assert "price" in result["_warning"]
    assert await _listing(job.id) is None


async def test_listing_happy_path(output_dir, listable):
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, artist_attribution="© Studio Z")
        await session.commit()
    job, _, archive = await _seed_job(output_dir)
    log: list[httpx.Request] = []

    async with _client(log) as client:
        result = await fw.create_storefront_listing(job.id, job_step_id=7, client=client)

    assert result["storefront_listing_status"] == "ready"
    assert result["fourthwall_product_id"] == "prod-1"
    assert "prod-1" in result["brief"]

    listing = await _listing(job.id)
    assert listing is not None
    assert listing.status == StorefrontListingStatus.READY
    assert listing.attempt == 1
    assert listing.job_step_id == 7
    assert listing.delivery_archive_id == archive.id
    assert listing.product_id == "prod-1"
    assert listing.name == "Neon Shoot"
    assert listing.price_usd == 12.5
    assert listing.archive_name == archive.archive_name
    # Correlated to the seeded R2 upload + the generated link document
    assert listing.cloudflare_upload_id is not None
    assert listing.link_document_name == "neon-shoot-download.pdf"
    assert listing.link_document_sha256 is not None
    # Marketplace post + tags + attribution rendered through the template
    assert "A neon-drenched artwork pack." in listing.description
    assert "neon, cyber" in listing.description
    assert "© Studio Z" in listing.description
    detail = json.loads(listing.detail_json)
    assert detail["file_url"] == "fw-file-1"
    assert detail["preview_count"] == 1
    assert detail["cloudflare_upload_id"] == listing.cloudflare_upload_id
    assert detail["link_document"]["name"] == "neon-shoot-download.pdf"
    assert detail["link_document"]["url"].startswith("https://pub-abc.r2.dev/")

    by_key = {f"{r.method} {r.url.path}": r for r in log}
    create = json.loads(by_key["POST /products"].content)
    assert create["type"] == "digital"
    assert create["name"] == "Neon Shoot"
    assert create["price"] == 12.5
    assert create["publishOnCreate"] is False

    # The digital file is the PDF link document — NOT the delivery ZIP
    grant_key = "POST /products/prod-1/digital-files/upload-url"
    grant = json.loads(by_key[grant_key].content)
    assert grant["fileName"] == "neon-shoot-download.pdf"
    assert grant["contentType"] == "application/pdf"
    assert grant["size"] < 64 * 1024

    # Signed storage PUT carries the exact signed conditions, no auth
    puts = [r for r in log if r.method == "PUT"]
    doc_put = next(r for r in puts if "fw-signed-zip" in str(r.url))
    assert doc_put.headers["x-goog-content-length-range"] == f"0,{len(doc_put.content)}"
    assert doc_put.headers["Content-Type"] == "application/pdf"
    assert "authorization" not in {k.lower() for k in doc_put.headers}
    assert doc_put.content.startswith(b"%PDF")  # the generated link document

    confirm = json.loads(by_key["POST /products/prod-1/digital-files"].content)
    assert confirm == {"fileUrl": "fw-file-1", "fileName": "neon-shoot-download.pdf"}

    # The smallest variant image was converted to JPEG and attached
    media_grant = json.loads(by_key["POST /media/upload-url"].content)
    assert media_grant["contentType"] == "image/jpeg"
    assert media_grant["fileName"] == "aimw_main_refined_1x_00001.jpg"
    media_put = next(r for r in puts if "fw-signed-media" in str(r.url))
    assert media_put.content.startswith(b"\xff\xd8")  # JPEG magic
    attach = json.loads(by_key["POST /products/prod-1/images"].content)
    assert attach["images"] == [{"url": "fw-media-1", "width": 8, "height": 8}]


async def test_listing_markdown_link_document(output_dir, listable):
    job, _, _ = await _seed_job(output_dir)
    async with app.database.async_session() as session:
        await ss.save_storefront_settings(
            session, price_usd=5.0, link_document_format="markdown"
        )
        await session.commit()
    log: list[httpx.Request] = []
    async with _client(log) as client:
        result = await fw.create_storefront_listing(job.id, client=client)
    assert result["storefront_listing_status"] == "ready"
    grant = json.loads(
        next(
            r for r in log if r.url.path.endswith("/digital-files/upload-url")
        ).content
    )
    assert grant["fileName"] == "neon-shoot-download.md"
    assert grant["contentType"] == "text/markdown"
    listing = await _listing(job.id)
    assert listing.link_document_name == "neon-shoot-download.md"


async def test_listing_api_failure_is_best_effort(output_dir, listable):
    job, _, _ = await _seed_job(output_dir)
    log: list[httpx.Request] = []

    async with _client(log, fail_path="POST /products") as client:
        result = await fw.create_storefront_listing(job.id, client=client)

    assert result["storefront_listing_status"] == "failed"
    assert "400" in result["_warning"]
    listing = await _listing(job.id)
    assert listing.status == StorefrontListingStatus.FAILED
    assert listing.error_type == "FourthwallApiError"
    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
    assert job_row.status == JobStatus.COMPLETED


async def test_listing_skipped_without_upload(output_dir, listable):
    """A ready archive with no R2 upload → skipped, not failed."""
    job, _, _ = await _seed_job(output_dir, with_upload=False)
    async with _client([]) as client:
        result = await fw.create_storefront_listing(job.id, client=client)
    assert result["storefront_listing_status"] == "skipped"
    assert "cloudflare_r2_uploader" in result["_warning"]
    assert await _listing(job.id) is None


async def test_listing_is_idempotent(output_dir, listable):
    job, _, _ = await _seed_job(output_dir)
    async with _client([]) as client:
        first = await fw.create_storefront_listing(job.id, client=client)
    second = await fw.create_storefront_listing(job.id, client=_client([]))
    assert first["storefront_listing_status"] == "ready"
    assert second["storefront_listing_status"] == "skipped"
    assert second["fourthwall_product_id"] == "prod-1"
    async with app.database.async_session() as session:
        count = (
            await session.execute(
                select(StorefrontListing).where(StorefrontListing.job_id == job.id)
            )
        ).scalars().all()
    assert len(count) == 1


async def test_listing_no_images_still_attaches_document(output_dir, listable):
    job, _, _ = await _seed_job(output_dir, with_steps=False)
    log: list[httpx.Request] = []
    async with _client(log) as client:
        result = await fw.create_storefront_listing(job.id, client=client)
    assert result["storefront_listing_status"] == "ready"
    assert any(r.url.path.endswith("/digital-files") for r in log)
    assert not any(r.url.path == "/media/upload-url" for r in log)
    assert not any(r.url.path.endswith("/images") for r in log)


# ── Block ─────────────────────────────────────────────────────────────────


async def test_block_requires_job_id():
    from app.blocks.fourthwall_listing import FourthwallListing

    block = FourthwallListing()
    with pytest.raises(ValueError, match="_job_id"):
        await block.validate({})
    await block.validate({"_job_id": 1})


async def test_block_run_surfaces_warning(output_dir, monkeypatch):
    from app.blocks.fourthwall_listing import FourthwallListing

    async def fake_create(job_id, *, job_step_id=None, client=None):
        return {
            "brief": "nope",
            "storefront_listing_id": None,
            "fourthwall_product_id": None,
            "storefront_listing_status": "skipped",
            "fourthwall_listing_output": "nope",
            "_warning": "creds missing",
        }

    monkeypatch.setattr(fw, "create_storefront_listing", fake_create)
    context: dict = {"_job_id": 3}
    result = await FourthwallListing().run(context)
    assert "_warning" not in result
    assert context["_warnings"] == ["creds missing"]


# ── Retry ─────────────────────────────────────────────────────────────────


async def test_listing_retry_reserve_and_run(output_dir, listable, monkeypatch):
    job, _, _ = await _seed_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await fw.reserve_listing_retry(session, job.id)
        job_row = await session.get(Job, job.id)
    assert job_row.status == JobStatus.RUNNING

    async def fake_create(job_id, *, job_step_id=None, client=None):
        return {
            "brief": "ok",
            "storefront_listing_status": "ready",
            "fourthwall_product_id": "prod-9",
        }

    monkeypatch.setattr(fw, "create_storefront_listing", fake_create)
    await fw.run_listing_retry(job.id, step_id)

    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
        step = await session.get(JobStep, step_id)
    assert job_row.status == JobStatus.COMPLETED
    assert step.status == JobStatus.COMPLETED
    assert json.loads(step.output)["fourthwall_product_id"] == "prod-9"


async def test_listing_retry_restores_failed_job(output_dir, listable, monkeypatch):
    job, _, _ = await _seed_job(output_dir, status=JobStatus.FAILED)
    async with app.database.async_session() as session:
        step_id = await fw.reserve_listing_retry(session, job.id)

    async def fake_create(job_id, *, job_step_id=None, client=None):
        return {"storefront_listing_status": "failed", "fourthwall_listing_output": "boom"}

    monkeypatch.setattr(fw, "create_storefront_listing", fake_create)
    await fw.run_listing_retry(job.id, step_id)
    async with app.database.async_session() as session:
        job_row = await session.get(Job, job.id)
        step = await session.get(JobStep, step_id)
    assert job_row.status == JobStatus.FAILED
    assert step.status == JobStatus.FAILED


async def test_listing_retry_ineligible_cases(output_dir, listable):
    async with app.database.async_session() as session:
        job = Job(
            workflow_name="No Archive",
            status=JobStatus.COMPLETED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(job)
        await session.commit()
        with pytest.raises(fw.ListingRetryError, match="no ready delivery archive"):
            await fw.reserve_listing_retry(session, job.id)

    job2, _, archive2 = await _seed_job(output_dir, name="Listed")
    async with app.database.async_session() as session:
        session.add(
            StorefrontListing(
                job_id=job2.id,
                delivery_archive_id=archive2.id,
                attempt=1,
                status=StorefrontListingStatus.READY,
                name="Listed",
            )
        )
        await session.commit()
        with pytest.raises(fw.ListingRetryError, match="already has a listing"):
            await fw.reserve_listing_retry(session, job2.id)


async def test_listing_retry_api(output_dir, listable, monkeypatch):
    job, _, _ = await _seed_job(output_dir)
    scheduled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.api.workflows.schedule_listing_retry",
        lambda job_id, step_id: scheduled.append((job_id, step_id)),
    )
    async with _web_client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/listings/retry")
    assert response.status_code == 202
    assert response.json() == {"job_id": job.id, "status": "retrying"}
    assert len(scheduled) == 1 and scheduled[0][0] == job.id


async def test_listing_retry_api_ineligible(output_dir, listable):
    job, _, _ = await _seed_job(output_dir, with_archive=False)
    async with _web_client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/listings/retry")
    assert response.status_code == 409


async def test_listings_list_api(output_dir, listable):
    job, _, _ = await _seed_job(output_dir)
    async with _client([]) as client:
        await fw.create_storefront_listing(job.id, client=client)
    async with _web_client() as client:
        response = await client.get(f"/api/workflows/jobs/{job.id}/listings")
        job_response = await client.get(f"/api/workflows/jobs/{job.id}")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["product_id"] == "prod-1"
    assert rows[0]["status"] == "ready"
    assert rows[0]["cloudflare_upload_id"] is not None
    assert rows[0]["link_document_name"] == "neon-shoot-download.pdf"
    assert job_response.json()["listings"][0]["product_id"] == "prod-1"


async def test_job_detail_page_shows_listing(output_dir, listable):
    job, _, _ = await _seed_job(output_dir)
    async with _client([]) as client:
        await fw.create_storefront_listing(job.id, client=client)
    async with _web_client() as client:
        page = await client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert "Storefront listings" in page.text
    assert "Cloudflare R2 uploads" in page.text
    assert "prod-1" in page.text
    assert "$12.50" in page.text
