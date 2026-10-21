"""app.services.pinterest — Pinterest pin scheduling & publishing

Schedules pins that promote FourthWall products (StorefrontListing rows) and
publishes them through the official Pinterest API v5 at the user's chosen
time. The API has no native scheduled publishing, so this service holds the
pin until `scheduled_for`, then `publish_pin` sends `POST /v5/pins` with the
image base64-encoded.

Anti-spam rules (Pinterest's duplicate-content guidance, applied app-side —
pins never expire on Pinterest):

- No second pin while one for the same listing is SCHEDULED or PUBLISHING
- No new pin within `repin_cooldown_days` of the last PUBLISHED pin
- A rolling daily publish cap (`daily_pin_limit`) across all listings

`pin_ineligibility()` returns a human-readable reason or None; the schedule
route enforces it and the scheduler re-checks it before publishing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.database
from app.config import settings
from app.models.job import Job, JobStatus, JobStep
from app.models.pinterest import ScheduledPin, ScheduledPinStatus
from app.models.storefront import StorefrontListing, StorefrontListingStatus
from app.services.fourthwall import FourthwallClient, configured_credentials
from app.services.pinterest_auth import (
    PinterestApiError,
    PinterestError,
    PinterestNotConfiguredError,
    PinterestNotConnectedError,
    get_valid_token,
)
from app.services.pinterest_images import PinterestImageError, prepare_pin_image
from app.services.pinterest_settings import (
    PinterestSettings,
    get_pinterest_settings,
)

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_SCHEDULE_HORIZON_DAYS = 365


class PinScheduleError(ValueError):
    """A pin could not be scheduled; safe to show to the user."""


class PinterestClient:
    """Minimal async client for the Pinterest API v5 endpoints we use."""

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=(base_url or settings.pinterest_base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout if timeout is not None else settings.pinterest_timeout,
            transport=transport,
        )

    async def __aenter__(self) -> PinterestClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        """JSON request; one retry on 429 honoring Retry-After."""
        for attempt in range(2):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise PinterestError(f"Pinterest request failed: {exc}") from exc
            if response.status_code == 429 and attempt == 0:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 5.0
                await asyncio.sleep(min(delay, 60.0))
                continue
            if response.status_code >= 400:
                raise PinterestApiError(response.status_code, response.text[:500])
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise PinterestError(
                    f"Pinterest returned a non-JSON response ({response.status_code})"
                ) from exc
        return None  # unreachable

    async def list_boards(self) -> list[dict[str, Any]]:
        """All boards the token can pin to: [{id, name}, ...]."""
        boards: list[dict[str, Any]] = []
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if bookmark:
                params["bookmark"] = bookmark
            payload = await self._api("GET", "/boards", params=params)
            boards.extend(
                {"id": str(b.get("id")), "name": str(b.get("name") or "")}
                for b in (payload or {}).get("items") or []
                if isinstance(b, dict) and b.get("id")
            )
            bookmark = (payload or {}).get("bookmark")
            if not bookmark:
                return boards

    async def create_pin(
        self,
        *,
        board_id: str,
        title: str,
        description: str,
        link: str,
        image_b64: str,
        content_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        """POST /pins; returns the pin payload (id, link, ...)."""
        return await self._api(
            "POST",
            "/pins",
            json={
                "board_id": board_id,
                "title": title[:MAX_TITLE_CHARS],
                "description": description[:MAX_DESCRIPTION_CHARS],
                "link": link,
                "media_source": {
                    "source_type": "image_base64",
                    "content_type": content_type,
                    "data": image_b64,
                },
            },
        )


# ── Listing / pin queries ────────────────────────────────────────────────


async def list_pinnable_listings(session: AsyncSession) -> list[StorefrontListing]:
    """READY storefront listings — the products that can be pinned."""
    result = await session.execute(
        select(StorefrontListing)
        .where(StorefrontListing.status == StorefrontListingStatus.READY)
        .order_by(StorefrontListing.id.desc())
    )
    return list(result.scalars().all())


async def list_pins(
    session: AsyncSession, storefront_listing_id: int | None = None
) -> list[ScheduledPin]:
    """Scheduled/published pin rows, newest first; optionally per listing."""
    stmt = select(ScheduledPin).order_by(ScheduledPin.id.desc())
    if storefront_listing_id is not None:
        stmt = stmt.where(ScheduledPin.storefront_listing_id == storefront_listing_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def pins_by_listing(session: AsyncSession) -> dict[int, list[ScheduledPin]]:
    """All pins grouped by storefront_listing_id for the products table."""
    grouped: dict[int, list[ScheduledPin]] = {}
    for pin in await list_pins(session):
        grouped.setdefault(pin.storefront_listing_id, []).append(pin)
    return grouped


# ── Eligibility rules ────────────────────────────────────────────────────


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%b %d, %Y %H:%M")


async def pin_ineligibility(
    session: AsyncSession,
    storefront_listing_id: int,
    *,
    pinterest_settings: PinterestSettings | None = None,
    now: datetime | None = None,
    resolved_link: str | None = None,
    _link_resolved: bool = False,
) -> str | None:
    """A human-readable reason a new pin can't be scheduled, or None.

    ``resolved_link``/``_link_resolved`` let callers that already computed
    the product link (e.g. the products table) avoid a second resolution.
    """
    pinterest_settings = pinterest_settings or await get_pinterest_settings(session)
    now = now or datetime.now(UTC)

    listing = await session.get(StorefrontListing, storefront_listing_id)
    if listing is None:
        return "This product is not a published storefront listing."
    link = resolved_link if _link_resolved else await resolve_product_link(session, listing)
    if not link.strip():
        return "No product link for this product — add it with Edit link."

    pins = await list_pins(session, storefront_listing_id)

    pending = [
        p
        for p in pins
        if p.status in (ScheduledPinStatus.SCHEDULED, ScheduledPinStatus.PUBLISHING)
    ]
    if pending:
        when = _fmt_dt(pending[0].scheduled_for)
        return (
            f"A pin is already scheduled for this product ({when}). "
            "Cancel it or wait for it to publish."
        )

    published = [p for p in pins if p.status == ScheduledPinStatus.PUBLISHED]
    if published and pinterest_settings.repin_cooldown_days > 0:
        latest = max(
            (p.published_at for p in published if p.published_at),
            default=None,
        )
        if latest is not None:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            eligible_at = latest + timedelta(days=pinterest_settings.repin_cooldown_days)
            if now < eligible_at:
                return (
                    f"Last pinned {_fmt_dt(latest)} — next pin allowed "
                    f"{_fmt_dt(eligible_at)} "
                    f"({pinterest_settings.repin_cooldown_days}-day cooldown)."
                )
    return None


def _validate_pin_payload(
    *,
    title: str,
    description: str,
    link: str,
    board_id: str,
    image_path: str,
    scheduled_for: datetime,
    now: datetime,
) -> list[str]:
    errors: list[str] = []
    if not board_id.strip():
        errors.append("A Pinterest board is required.")
    if not title.strip():
        errors.append("Title is required.")
    elif len(title.strip()) > MAX_TITLE_CHARS:
        errors.append(f"Title must be {MAX_TITLE_CHARS} characters or fewer.")
    if len(description.strip()) > MAX_DESCRIPTION_CHARS:
        errors.append(f"Description must be {MAX_DESCRIPTION_CHARS} characters or fewer.")
    if not link.strip() or not link.strip().lower().startswith(("http://", "https://")):
        errors.append("Link must be an absolute http(s) URL.")
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    if scheduled_for <= now:
        errors.append("Scheduled time must be in the future.")
    elif scheduled_for > now + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS):
        errors.append(
            f"Scheduled time can't be more than {MAX_SCHEDULE_HORIZON_DAYS} days out."
        )
    if not image_path.strip():
        errors.append("A pin image is required.")
    return errors


async def schedule_pin(
    session: AsyncSession,
    *,
    storefront_listing_id: int,
    board_id: str,
    board_name: str,
    title: str,
    description: str,
    link: str,
    image_path: str,
    scheduled_for: datetime,
    now: datetime | None = None,
) -> ScheduledPin:
    """Validate and insert a SCHEDULED pin row. Raises PinScheduleError."""
    now = now or datetime.now(UTC)
    listing = await session.get(StorefrontListing, storefront_listing_id)
    if listing is None or listing.status != StorefrontListingStatus.READY:
        raise PinScheduleError("This product is not a published storefront listing.")

    pinterest_settings = await get_pinterest_settings(session)
    reason = await pin_ineligibility(
        session,
        storefront_listing_id,
        pinterest_settings=pinterest_settings,
        now=now,
    )
    if reason:
        raise PinScheduleError(reason)

    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    errors = _validate_pin_payload(
        title=title,
        description=description,
        link=link,
        board_id=board_id,
        image_path=image_path,
        scheduled_for=scheduled_for,
        now=now,
    )
    if errors:
        raise PinScheduleError(" ".join(errors))

    pin = ScheduledPin(
        storefront_listing_id=storefront_listing_id,
        job_id=listing.job_id,
        status=ScheduledPinStatus.SCHEDULED,
        scheduled_for=scheduled_for,
        board_id=board_id.strip(),
        board_name=board_name.strip(),
        title=title.strip()[:MAX_TITLE_CHARS],
        description=description.strip()[:MAX_DESCRIPTION_CHARS],
        link=link.strip(),
        image_path=image_path,
    )
    session.add(pin)
    await session.flush()
    return pin


async def cancel_pin(session: AsyncSession, pin_id: int) -> ScheduledPin:
    """Cancel a SCHEDULED pin. Raises PinScheduleError when not cancelable."""
    pin = await session.get(ScheduledPin, pin_id)
    if pin is None:
        raise PinScheduleError("Pin not found.")
    if pin.status != ScheduledPinStatus.SCHEDULED:
        raise PinScheduleError(f"Pin {pin_id} is {pin.status.value}, not scheduled.")
    pin.status = ScheduledPinStatus.CANCELED
    pin.finished_at = datetime.now(UTC)
    await session.flush()
    return pin


async def reschedule_failed_pin(
    session: AsyncSession, pin_id: int, *, now: datetime | None = None
) -> ScheduledPin:
    """Re-queue a FAILED pin for immediate publishing (new attempt on the row)."""
    pin = await session.get(ScheduledPin, pin_id)
    if pin is None:
        raise PinScheduleError("Pin not found.")
    if pin.status != ScheduledPinStatus.FAILED:
        raise PinScheduleError(f"Pin {pin_id} is {pin.status.value}, not failed.")
    now = now or datetime.now(UTC)
    reason = await pin_ineligibility(
        session, pin.storefront_listing_id, now=now
    )
    # The failed row itself is neither pending nor published, so only a
    # different row could block — surfacing that reason is correct.
    if reason:
        raise PinScheduleError(reason)
    pin.status = ScheduledPinStatus.SCHEDULED
    pin.scheduled_for = now
    pin.attempt += 1
    pin.error = None
    pin.error_type = None
    pin.finished_at = None
    await session.flush()
    return pin


# ── Due-pin publishing (called by the scheduler) ────────────────────────


async def due_pins(session: AsyncSession, *, now: datetime | None = None) -> list[ScheduledPin]:
    """SCHEDULED pins whose time has come (includes pins missed while down)."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(ScheduledPin)
        .where(
            ScheduledPin.status == ScheduledPinStatus.SCHEDULED,
            ScheduledPin.scheduled_for <= now,
        )
        .order_by(ScheduledPin.scheduled_for)
    )
    return list(result.scalars().all())


async def published_last_24h(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Count of pins published in the last 24h — the daily-cap input."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(func.count(ScheduledPin.id)).where(
            ScheduledPin.status == ScheduledPinStatus.PUBLISHED,
            ScheduledPin.published_at >= now - timedelta(hours=24),
        )
    )
    return int(result.scalar() or 0)


async def claim_pin(session: AsyncSession, pin_id: int) -> bool:
    """Transition SCHEDULED → PUBLISHING; False when already claimed."""
    pin = await session.get(ScheduledPin, pin_id)
    if pin is None or pin.status != ScheduledPinStatus.SCHEDULED:
        return False
    pin.status = ScheduledPinStatus.PUBLISHING
    pin.started_at = datetime.now(UTC)
    await session.flush()
    return True


async def publish_pin(pin_id: int) -> None:
    """Publish a claimed pin; outcome written to the row in a fresh session."""
    try:
        async with app.database.async_session() as session:
            pin = await session.get(ScheduledPin, pin_id)
            if pin is None:
                return
            token = await get_valid_token(session)
            image_b64, content_type = prepare_pin_image(pin.image_path)
            async with PinterestClient(token) as client:
                payload = await client.create_pin(
                    board_id=pin.board_id,
                    title=pin.title,
                    description=pin.description,
                    link=pin.link,
                    image_b64=image_b64,
                    content_type=content_type,
                )
            pin.pinterest_pin_id = str(payload.get("id")) if payload.get("id") else None
            pin.pinterest_pin_url = (
                str(payload.get("link") or payload.get("url")) or None
            )
            pin.status = ScheduledPinStatus.PUBLISHED
            pin.published_at = datetime.now(UTC)
            pin.finished_at = pin.published_at
            pin.error = None
            pin.error_type = None
            await session.commit()
            logger.info("Published scheduled pin %s → Pinterest %s", pin_id, pin.pinterest_pin_id)
    except Exception as exc:
        logger.exception("Scheduled pin %s failed to publish", pin_id)
        async with app.database.async_session() as session:
            pin = await session.get(ScheduledPin, pin_id)
            if pin is not None:
                pin.status = ScheduledPinStatus.FAILED
                pin.error = str(exc)
                pin.error_type = type(exc).__name__
                pin.finished_at = datetime.now(UTC)
                await session.commit()


# ── Pin-copy suggestion & product link resolution ───────────────────────


def _merged_step_outputs(steps: list[JobStep]) -> dict[str, Any]:
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


async def suggest_pin_content(
    session: AsyncSession, listing: StorefrontListing
) -> dict[str, Any]:
    """Seed title/description/image options for the schedule form.

    Copy comes from the Social Media Specialist's marketplace post (same
    source as the storefront listing); image options are the smallest
    per-variant renders, matching the listing's preview selection.
    """
    from app.services.fourthwall import _pick_marketplace_post, _select_preview_paths

    result = await session.execute(
        select(Job).where(Job.id == listing.job_id).options(selectinload(Job.steps))
    )
    job = result.scalar_one_or_none()
    outputs = _merged_step_outputs(list(job.steps)) if job is not None else {}
    post = _pick_marketplace_post(outputs.get("social_media_posts")) or {}
    title = str(post.get("title") or listing.name or "")
    description = str(post.get("description") or "")
    previews = _select_preview_paths(outputs, 8)
    return {
        "title": title[:MAX_TITLE_CHARS],
        "description": description[:MAX_DESCRIPTION_CHARS],
        "image_paths": [str(p) for p in previews],
        "default_image": str(previews[0]) if previews else "",
    }


async def resolve_product_link(
    session: AsyncSession, listing: StorefrontListing
) -> str:
    """Best-effort public product URL for the pin's link field.

    Order: stored product_url (manual value wins) → detail_json URL/slug →
    FourthWall GET /products/{id}. Returns "" when nothing concrete
    resolves — the template slug is only a schedule-form guess
    (`suggest_product_link`), never a resolved answer.
    """
    url_keys = ("url", "productUrl", "product_url", "storefrontUrl", "permalink")

    if listing.product_url and listing.product_url.strip():
        return listing.product_url.strip()

    if listing.detail_json:
        try:
            detail = json.loads(listing.detail_json)
        except ValueError:
            detail = None
        if isinstance(detail, dict):
            for key in url_keys:
                if detail.get(key):
                    return str(detail[key])
            slug = detail.get("slug")
            if slug:
                template = (await get_pinterest_settings(session)).product_url_template
                return template.format(slug=slug, product_id=listing.product_id or "")

    credentials = configured_credentials()
    if credentials and listing.product_id:
        try:
            async with FourthwallClient(user=credentials[0], password=credentials[1]) as client:
                product = await client.get_product(listing.product_id)
            if isinstance(product, dict):
                for key in url_keys:
                    if product.get(key):
                        return str(product[key])
                slug = product.get("slug")
                if slug:
                    template = (await get_pinterest_settings(session)).product_url_template
                    return template.format(slug=slug, product_id=listing.product_id)
        except Exception:
            logger.warning(
                "Pinterest: could not resolve product URL for %s via FourthWall",
                listing.product_id,
                exc_info=True,
            )

    return ""


async def suggest_product_link(
    session: AsyncSession, listing: StorefrontListing
) -> str:
    """The resolved link, or a best-guess from product_url_template.

    Used to prefill the schedule form's link field — the guess is
    explicitly a guess, so it never satisfies the eligibility check.
    """
    resolved = await resolve_product_link(session, listing)
    if resolved:
        return resolved
    template = (await get_pinterest_settings(session)).product_url_template
    slug = listing.name.lower().replace(" ", "-") if listing.name else ""
    try:
        return template.format(slug=slug, product_id=listing.product_id or "")
    except (KeyError, IndexError, ValueError):
        return f"https://syntheticmuse.ai/products/{slug}"


async def save_product_url(
    session: AsyncSession, storefront_listing_id: int, url: str
) -> StorefrontListing:
    """Persist a manually-entered public product link. Raises PinScheduleError."""
    listing = await session.get(StorefrontListing, storefront_listing_id)
    if listing is None:
        raise PinScheduleError("Product not found.")
    cleaned = (url or "").strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise PinScheduleError("Product link must be an absolute http(s) URL.")
    listing.product_url = cleaned
    await session.flush()
    return listing


__all__ = [
    "PinterestApiError",
    "PinterestClient",
    "PinterestError",
    "PinterestImageError",
    "PinterestNotConfiguredError",
    "PinterestNotConnectedError",
    "PinScheduleError",
    "cancel_pin",
    "claim_pin",
    "due_pins",
    "list_pinnable_listings",
    "list_pins",
    "pin_ineligibility",
    "pins_by_listing",
    "publish_pin",
    "published_last_24h",
    "reschedule_failed_pin",
    "resolve_product_link",
    "save_product_url",
    "schedule_pin",
    "suggest_pin_content",
    "suggest_product_link",
]
