"""app.services.fourthwall — FourthWall storefront listing service

Publishes a job's verified delivery archive as a FourthWall digital product.
The archive ZIP itself is too large for FourthWall's digital-file limit, so
it is hosted on Cloudflare R2 by the `cloudflare_r2_uploader` block; the
product's downloadable file is a small link document (PDF or Markdown) that
hands buyers the public R2 URL:

1. POST /products                       — type "digital", name/description/price
2. digital-files upload-url → PUT bytes → confirm   (the link document)
3. media upload-url → PUT bytes → /products/{id}/images  (preview thumbnails)

Requests authenticate with HTTP Basic auth using the Open API
username/password pair from FOURTHWALL_API_USER / FOURTHWALL_API_PASSWORD.
Signed uploads go straight to Google Cloud Storage — no auth header — and
must carry Content-Type plus `x-goog-content-length-range: 0,<size>` with
the exact byte count declared when the URL was requested.

The block is best-effort: this service never raises for listing outcomes.
It records a StorefrontListing row per attempt (RUNNING → READY | FAILED)
and returns a result dict whose `storefront_listing_status` is "ready",
"failed", or "skipped" (unconfigured, nothing to list, or
already listed). Only infrastructure-level surprises (missing job) raise.

Retry mirrors the archive-only retry flow: reserve → schedule → run under
the workload guard, appending a `fourthwall_listing` JobStep without
rerunning any creative steps. Because listing failures are best-effort,
the job's terminal status is preserved regardless of the outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import mistune
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.database
from app.config import settings
from app.models.delivery import DeliveryArchive
from app.models.job import Job, JobStatus, JobStep
from app.models.storefront import StorefrontListing, StorefrontListingStatus
from app.services import fourthwall_description as fw_description
from app.services.cloudflare_r2 import latest_ready_upload
from app.services.delivery_archive import latest_ready_archive
from app.services.general_settings import get_delivery_settings
from app.services.image_convert import ImageConversionError, convert_png_bytes
from app.services.link_document import build_link_document
from app.services.storefront_settings import (
    StorefrontSettings,
    get_storefront_settings,
)
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

LISTING_BLOCK_NAME = "fourthwall_listing"
_MARKETPLACE_PLATFORM = re.compile(r"market|stock|licens|store|shop", re.IGNORECASE)


class FourthwallError(RuntimeError):
    """A FourthWall listing could not be produced."""


class FourthwallApiError(FourthwallError):
    """The FourthWall API (or signed storage) rejected a request."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"FourthWall API {status_code}: {detail}")


class ListingRetryError(RuntimeError):
    """Listing-only retry was requested for an ineligible job."""

    def __init__(self, detail: str, status_code: int = 409) -> None:
        self.status_code = status_code
        super().__init__(detail)


class FourthwallClient:
    """Minimal async client for the FourthWall Open API product flow."""

    def __init__(
        self,
        *,
        user: str,
        password: str,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=(base_url or settings.fourthwall_base_url).rstrip("/"),
            auth=(user, password),
            timeout=timeout if timeout is not None else settings.fourthwall_timeout,
            transport=transport,
        )

    async def __aenter__(self) -> FourthwallClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        """JSON request against the Open API; raises FourthwallApiError."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise FourthwallError(f"FourthWall request failed: {exc}") from exc
        if response.status_code >= 400:
            raise FourthwallApiError(response.status_code, response.text[:500])
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise FourthwallError(
                f"FourthWall returned a non-JSON response ({response.status_code})"
            ) from exc

    async def _storage_put(self, upload_url: str, data: bytes, content_type: str) -> None:
        """PUT to signed storage with NO credentials on the wire."""
        try:
            async with httpx.AsyncClient(
                timeout=self._client.timeout, transport=self._transport
            ) as bare:
                response = await bare.put(
                    upload_url,
                    content=data,
                    headers={
                        "Content-Type": content_type,
                        "x-goog-content-length-range": f"0,{len(data)}",
                    },
                )
        except httpx.HTTPError as exc:
            raise FourthwallError(f"storage upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise FourthwallApiError(response.status_code, response.text[:500])

    async def create_product(
        self, *, name: str, description: str, price: float, publish_on_create: bool
    ) -> str:
        """Create the hidden/published digital product; return its product id."""
        payload = await self._api(
            "POST",
            "/products",
            json={
                "type": "digital",
                "name": name,
                "description": description,
                "price": price,
                "publishOnCreate": publish_on_create,
            },
        )
        product_id = (payload or {}).get("productId")
        if not product_id:
            raise FourthwallError("FourthWall create-product returned no productId")
        return str(product_id)

    async def attach_digital_file(
        self, product_id: str, *, file_name: str, data: bytes, content_type: str
    ) -> dict[str, Any]:
        """Upload the downloadable file buyers receive; returns confirm payload."""
        grant = await self._api(
            "POST",
            f"/products/{product_id}/digital-files/upload-url",
            json={
                "fileName": file_name,
                "contentType": content_type,
                "size": len(data),
            },
        )
        upload_url = (grant or {}).get("uploadUrl")
        file_url = (grant or {}).get("fileUrl")
        if not upload_url or not file_url:
            raise FourthwallError("FourthWall did not grant a digital-file upload URL")
        await self._storage_put(upload_url, data, content_type)
        confirmed = await self._api(
            "POST",
            f"/products/{product_id}/digital-files",
            json={"fileUrl": file_url, "fileName": file_name},
        )
        return {"file_url": file_url, "confirmed": confirmed}

    async def upload_media(self, data: bytes, *, content_type: str, file_name: str) -> str:
        """Upload bytes to the shop media library; returns the file URL."""
        grant = await self._api(
            "POST",
            "/media/upload-url",
            json={
                "fileName": file_name,
                "contentType": content_type,
                "size": len(data),
            },
        )
        upload_url = (grant or {}).get("uploadUrl")
        file_url = (grant or {}).get("fileUrl")
        if not upload_url or not file_url:
            raise FourthwallError("FourthWall did not grant a media upload URL")
        await self._storage_put(upload_url, data, content_type)
        return str(file_url)

    async def attach_images(self, product_id: str, images: list[dict[str, Any]]) -> Any:
        """Attach uploaded media URLs as storefront preview images."""
        return await self._api("POST", f"/products/{product_id}/images", json={"images": images})


def configured_credentials() -> tuple[str, str] | None:
    """The env-configured Open API credential pair, or None when unset."""
    user = settings.fourthwall_api_user.strip()
    password = settings.fourthwall_api_password.strip()
    if not user or not password:
        return None
    return user, password


def _step_outputs(steps: list[JobStep]) -> dict[str, Any]:
    """Merge completed steps' output snapshots; later steps win per key."""
    merged: dict[str, Any] = {}
    for step in sorted(steps, key=lambda s: s.order):
        if step.status != JobStatus.COMPLETED or not step.output:
            continue
        try:
            data = json.loads(step.output)
        except ValueError:
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _pick_marketplace_post(posts: Any) -> dict[str, Any] | None:
    """Prefer the licensing-marketplace post; fall back to the first."""
    if not isinstance(posts, list) or not posts:
        return None
    posts = [p for p in posts if isinstance(p, dict)]
    if not posts:
        return None
    for post in posts:
        if _MARKETPLACE_PLATFORM.search(str(post.get("platform") or "")):
            return post
    return posts[0]


def _render_description(
    template: str,
    *,
    title: str,
    description: str,
    tags: list[str],
    attribution: str,
) -> str:
    """Render the DB-stored template; unknown placeholders pass through."""
    rendered = string.Template(template).safe_substitute(
        title=title,
        description=description,
        tags=", ".join(tags),
        attribution=attribution,
    )
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()


async def _compose_description(
    outputs: dict[str, Any],
    *,
    job_name: str,
    storefront: StorefrontSettings,
    attribution: str,
) -> str:
    """Render the product description: summarized Art Director brief + tags.

    $description is produced by an LLM summarization of `art_director_output`
    so the final storefront copy fits FourthWall's character limit. The prompt
    asks for the overall mood and direction rather than a literal recitation
    of the brief's technical details. If the LLM call fails or returns empty
    content, the original brief is truncated instead.

    $tags still come from the Social Media Specialist's licensing-marketplace
    post; its tags are written as search keywords rather than social hashtags.

    The composed copy is Markdown — FourthWall renders descriptions as HTML,
    so it is converted once here. The final HTML is truncated to
    `MAX_PRODUCT_DESCRIPTION_CHARS` as a last-resort guard.
    """
    post = _pick_marketplace_post(outputs.get("social_media_posts"))
    tags = [str(t) for t in (post or {}).get("tags") or [] if str(t).strip()]
    brief = str(outputs.get("art_director_output") or "")

    # Size the template wrapper (title/tags/attribution/disclaimer) so we can
    # ask the summarizer for a description seed that leaves room for it.
    wrapper = _render_description(
        storefront.description_template,
        title=job_name,
        description="",
        tags=tags,
        attribution=attribution,
    )
    wrapper_html_len = len(mistune.html(wrapper).strip())
    disclaimer = storefront.disclaimer.strip()
    if disclaimer:
        wrapper_html_len += len("<br><br>") + len(disclaimer)
    budget = fw_description.budget_for_wrapper(wrapper_html_len)

    if brief.strip():
        try:
            seed = await fw_description.summarize_description(brief, max_chars=budget)
        except Exception:
            logger.exception(
                "Storefront description summarizer raised; falling back to truncation"
            )
            seed = brief[:budget].strip()
    else:
        seed = f"Digital artwork collection: {job_name}"

    if not seed:
        seed = f"Digital artwork collection: {job_name}"

    description = _render_description(
        storefront.description_template,
        title=job_name,
        description=seed,
        tags=tags,
        attribution=attribution,
    )
    if not description:
        description = f"Digital artwork collection: {job_name}"
    html = mistune.html(description).strip()
    if disclaimer:
        html = f"{html}<br><br>{disclaimer}"
    if len(html) > fw_description.MAX_PRODUCT_DESCRIPTION_CHARS:
        logger.warning(
            "Rendered storefront description exceeds %s characters; truncating",
            fw_description.MAX_PRODUCT_DESCRIPTION_CHARS,
        )
        html = html[: fw_description.MAX_PRODUCT_DESCRIPTION_CHARS].strip()
    return html


def _select_preview_paths(outputs: dict[str, Any], limit: int) -> list[Path]:
    """Smallest-pixel image per variant, in variant order, capped at `limit`.

    `generation_metadata` groups paths by variant; a bare `generated_images`
    list degrades to one group. Pixel dimensions come from the image header
    (no full decode), so 4x masters are only measured, never rasterized.
    """
    if limit <= 0:
        return []
    groups: list[list[str]] = []
    metadata = outputs.get("generation_metadata")
    if isinstance(metadata, list):
        for entry in metadata:
            if isinstance(entry, dict) and isinstance(entry.get("image_paths"), list):
                groups.append([str(p) for p in entry["image_paths"]])
    if not groups and isinstance(outputs.get("generated_images"), list):
        groups.append([str(p) for p in outputs["generated_images"]])

    selected: list[Path] = []
    for group in groups:
        best: Path | None = None
        best_pixels: int | None = None
        for raw in group:
            path = Path(raw)
            if path.suffix.lower() != ".png" or not path.is_file():
                continue
            try:
                with Image.open(path) as probe:  # header only — cheap
                    pixels = probe.width * probe.height
            except Exception:
                continue
            if best_pixels is None or pixels < best_pixels:
                best, best_pixels = path, pixels
        if best is not None:
            selected.append(best)
        if len(selected) >= limit:
            break
    return selected[:limit]


async def _ready_listing_for(session: AsyncSession, archive_id: int) -> StorefrontListing | None:
    """An already-live listing covering this archive, or None."""
    result = await session.execute(
        select(StorefrontListing)
        .where(
            StorefrontListing.delivery_archive_id == archive_id,
            StorefrontListing.status == StorefrontListingStatus.READY,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _result(
    status: str,
    *,
    listing_id: int | None = None,
    product_id: str | None = None,
    note: str = "",
    warning: str | None = None,
) -> dict[str, Any]:
    """Uniform block-result payload; `brief` keeps the chain readable."""
    return {
        "brief": note,
        "storefront_listing_id": listing_id,
        "fourthwall_product_id": product_id,
        "storefront_listing_status": status,
        "fourthwall_listing_output": note,
        "_warning": warning,
    }


async def _fail_listing(listing_id: int, exc: BaseException, product_id: str | None = None) -> None:
    """Best-effort: record the failure on the attempt row in a fresh session."""
    try:
        async with app.database.async_session() as session:
            listing = await session.get(StorefrontListing, listing_id)
            if listing is not None:
                listing.status = StorefrontListingStatus.FAILED
                listing.error = str(exc)
                listing.error_type = type(exc).__name__
                if product_id:
                    listing.product_id = product_id
                listing.finished_at = datetime.now(UTC)
                await session.commit()
    except Exception:
        logger.exception("Could not record failure for storefront listing %s", listing_id)


async def create_storefront_listing(
    job_id: int,
    *,
    job_step_id: int | None = None,
    client: FourthwallClient | None = None,
) -> dict[str, Any]:
    """Publish the job's latest ready delivery archive as a digital product.

    Returns a result dict for the pipeline context; `storefront_listing_status`
    is "ready", "failed", or "skipped". Never raises for listing outcomes —
    failures land on the attempt row and in `_warning` for the block to
    surface as a job warning.
    """
    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise FourthwallError(f"Job {job_id} not found")
        job_name = job.workflow_name
        workflow_id = job.workflow_id
        steps = list(
            (
                await session.execute(
                    select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
                )
            ).scalars()
        )
        storefront = await get_storefront_settings(session)
        delivery = await get_delivery_settings(session)
        archive = await latest_ready_archive(session, job_id)
        existing = await _ready_listing_for(session, archive.id) if archive is not None else None
        upload = await latest_ready_upload(session, archive.id) if archive is not None else None

    credentials = configured_credentials()
    if credentials is None:
        note = (
            "FourthWall listing skipped: FOURTHWALL_API_USER / FOURTHWALL_API_PASSWORD "
            "are not configured"
        )
        return _result("skipped", note=note, warning=note)
    if archive is None:
        note = "FourthWall listing skipped: no ready delivery archive — run delivery_archive first"
        return _result("skipped", note=note, warning=note)
    if existing is not None:
        note = f"FourthWall listing skipped: archive already listed (product {existing.product_id})"
        return _result("skipped", listing_id=existing.id, product_id=existing.product_id, note=note)
    if upload is None or not upload.object_url:
        note = (
            "FourthWall listing skipped: no ready Cloudflare R2 upload for the "
            "latest archive — run cloudflare_r2_uploader first"
        )
        return _result("skipped", note=note, warning=note)
    price = storefront.price_usd
    if price is None or price <= 0:
        note = "FourthWall listing skipped: no price configured (settings → Store Fronts)"
        return _result("skipped", note=note, warning=note)

    outputs = _step_outputs(steps)
    description = await _compose_description(
        outputs,
        job_name=job_name,
        storefront=storefront,
        attribution=delivery.artist_attribution,
    )

    # Commit the attempt row before any network/filesystem work so failures
    # always have an audit record (same contract as create_delivery_archive).
    async with app.database.async_session() as session:
        attempt = (
            await session.execute(
                select(func.max(StorefrontListing.attempt)).where(
                    StorefrontListing.job_id == job_id
                )
            )
        ).scalar() or 0
        attempt += 1
        record = StorefrontListing(
            job_id=job_id,
            workflow_id=workflow_id,
            job_step_id=job_step_id,
            delivery_archive_id=archive.id,
            cloudflare_upload_id=upload.id,
            attempt=attempt,
            status=StorefrontListingStatus.RUNNING,
            started_at=datetime.now(UTC),
            name=job_name[:255],
            description=description,
            price_usd=price,
            publish_on_create=storefront.publish_on_create,
            settings_json=json.dumps(storefront.to_dict(), ensure_ascii=False),
            archive_name=archive.archive_name,
            archive_byte_size=archive.byte_size,
            archive_sha256=archive.sha256,
        )
        session.add(record)
        await session.commit()
        listing_id = record.id

    product_id: str | None = None
    try:
        document = build_link_document(
            storefront.link_document_format,
            title=job_name,
            url=upload.object_url,
            archive_name=archive.archive_name or "delivery.zip",
            byte_size=archive.byte_size,
            sha256=archive.sha256,
        )
        document_sha256 = hashlib.sha256(document.data).hexdigest()
        async with app.database.async_session() as session:
            record = await session.get(StorefrontListing, listing_id)
            if record is not None:
                record.link_document_name = document.filename
                record.link_document_sha256 = document_sha256
                await session.commit()

        previews: list[dict[str, Any]] = []
        preview_notes: list[str] = []
        owns_client = client is None
        if owns_client:
            client = FourthwallClient(user=credentials[0], password=credentials[1])
        assert client is not None
        try:
            product_id = await client.create_product(
                name=job_name[:255],
                description=description,
                price=price,
                publish_on_create=storefront.publish_on_create,
            )
            file_detail = await client.attach_digital_file(
                product_id,
                file_name=document.filename,
                data=document.data,
                content_type=document.content_type,
            )
            for path in _select_preview_paths(outputs, storefront.max_preview_images):
                try:
                    converted = await asyncio.to_thread(
                        convert_png_bytes, path.read_bytes(), path.name
                    )
                except (ImageConversionError, OSError) as exc:
                    preview_notes.append(f"{path.name}: skipped ({exc})")
                    continue
                url = await client.upload_media(
                    converted.jpeg_bytes,
                    content_type="image/jpeg",
                    file_name=converted.output_name,
                )
                previews.append({"url": url, "width": converted.width, "height": converted.height})
            if previews:
                await client.attach_images(product_id, previews)
        finally:
            if owns_client:
                await client.aclose()

        detail = {
            "file_url": file_detail.get("file_url"),
            "cloudflare_upload_id": upload.id,
            "link_document": {
                "name": document.filename,
                "content_type": document.content_type,
                "size": len(document.data),
                "sha256": document_sha256,
                "url": upload.object_url,
            },
            "preview_count": len(previews),
            "preview_notes": preview_notes,
            "skipped_previews": len(preview_notes),
        }
        async with app.database.async_session() as session:
            record = await session.get(StorefrontListing, listing_id)
            record.status = StorefrontListingStatus.READY
            record.finished_at = datetime.now(UTC)
            record.product_id = product_id
            record.detail_json = json.dumps(detail, ensure_ascii=False)
            await session.commit()
        note = (
            f"Published FourthWall product {product_id} "
            f"({document.filename} → {upload.object_url}, "
            f"{len(previews)} preview image(s), "
            f"${price:.2f}, {'published' if storefront.publish_on_create else 'hidden'}) "
            f"— set the product category in the Fourthwall admin for correct sales tax"
        )
        return _result("ready", listing_id=listing_id, product_id=product_id, note=note)
    except Exception as exc:
        await _fail_listing(listing_id, exc, product_id=product_id)
        note = f"FourthWall listing failed: {exc}"
        return _result("failed", listing_id=listing_id, note=note, warning=note)


# ── Listing-only retry ────────────────────────────────────────────────────


async def list_listings(session: AsyncSession, job_id: int) -> list[StorefrontListing]:
    """All listing attempts for a job, newest first."""
    result = await session.execute(
        select(StorefrontListing)
        .where(StorefrontListing.job_id == job_id)
        .order_by(StorefrontListing.attempt.desc())
    )
    return list(result.scalars().all())


def listing_retry_ineligibility(
    job: Job,
    steps: list[JobStep],
    archive: DeliveryArchive | None,
    listings: list[StorefrontListing],
) -> str | None:
    """None when a listing-only retry may be reserved; otherwise the reason."""
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        return "job is still running"
    if any(s.status in (JobStatus.PENDING, JobStatus.RUNNING) for s in steps):
        return "job has unfinished steps"
    if archive is None:
        return "job has no ready delivery archive to list"
    if any(
        listing.status == StorefrontListingStatus.READY
        and listing.delivery_archive_id == archive.id
        for listing in listings
    ):
        return "the latest archive already has a listing"
    if any(listing.status == StorefrontListingStatus.RUNNING for listing in listings):
        return "a listing attempt is already in progress"
    return None


async def listing_retryable(session: AsyncSession, job_id: int) -> bool:
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
    listings = await list_listings(session, job_id)
    return listing_retry_ineligibility(job, steps, archive, listings) is None


async def reserve_listing_retry(session: AsyncSession, job_id: int) -> int:
    """Reserve a listing-only retry; returns the new pending JobStep id.

    Atomically claims the job back into RUNNING and appends a pending
    fourthwall_listing step in one transaction, so concurrent submissions
    cannot both enqueue. Raises ListingRetryError when ineligible.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ListingRetryError(f"Job {job_id} not found", status_code=404)
    steps = list(
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
            )
        ).scalars()
    )
    archive = await latest_ready_archive(session, job_id)
    listings = await list_listings(session, job_id)
    ineligible = listing_retry_ineligibility(job, steps, archive, listings)
    if ineligible is not None:
        raise ListingRetryError(ineligible)

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
        raise ListingRetryError("listing retry is already in progress")
    step = JobStep(
        job_id=job_id,
        block_name=LISTING_BLOCK_NAME,
        order=steps[-1].order + 1 if steps else 0,
        status=JobStatus.PENDING,
        input_context=json.dumps({"prior_job_status": prior_status.value}),
    )
    session.add(step)
    await session.commit()
    return step.id


async def run_listing_retry(job_id: int, job_step_id: int) -> None:
    """Background task: run only the listing block for a reserved retry.

    The job's prior terminal status is restored afterwards — a failed
    listing never re-fails an otherwise-completed job.
    """
    async with workload_guard.hold(f"listing-retry-job-{job_id}"):
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
                            "listing_retry": True,
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
                result = await create_storefront_listing(job_id, job_step_id=job_step_id)
            except Exception as exc:
                logger.exception("Listing retry failed for job %s", job_id)
                step.status = JobStatus.FAILED
                step.error = str(exc)
                step.error_type = type(exc).__name__
            else:
                step.output = json.dumps(result, default=str, ensure_ascii=False)
                if result.get("storefront_listing_status") == "failed":
                    step.status = JobStatus.FAILED
                    step.error = result.get("fourthwall_listing_output")
                else:
                    step.status = JobStatus.COMPLETED
            finally:
                step.finished_at = datetime.now(UTC)
                job.status = prior_status
                job.finished_at = datetime.now(UTC)
                await session.commit()


def schedule_listing_retry(job_id: int, job_step_id: int) -> None:
    """Queue the reserved retry as a background task."""
    asyncio.create_task(
        run_listing_retry(job_id, job_step_id),
        name=f"listing-retry-job-{job_id}",
    )
