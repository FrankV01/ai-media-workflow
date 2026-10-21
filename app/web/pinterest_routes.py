"""app.web.pinterest_routes — Pinterest scheduling UI + OAuth flow (Jinja2 + HTMX)

Full pages:
- /pinterest                        — connection card, pinnable products,
                                      scheduled/published pins
- /pinterest/schedule/<listing_id>  — pin composition + scheduling form

OAuth:
- GET /pinterest/connect            → Pinterest authorize URL
- GET /pinterest/callback           → exchange code, store tokens
- POST /pinterest/disconnect        → drop stored tokens

Form POSTs (redirect with a status query):
- /pinterest/schedule/<listing_id>          — create a scheduled pin
- /pinterest/pins/<id>/cancel               — cancel a scheduled pin
- /pinterest/pins/<id>/retry                — re-queue a failed pin
- /settings/general/pinterest/save          — global Pinterest settings

HTMX partials:
- /pinterest/schedule/<id>/check    — eligibility message + submit enablement
- /pinterest/pins                   — pin table fragment (auto-refresh)
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.job import Job
from app.models.pinterest import ScheduledPinStatus
from app.models.storefront import StorefrontListing
from app.services.pinterest import (
    PinScheduleError,
    PinterestClient,
    cancel_pin,
    list_pinnable_listings,
    list_pins,
    pin_ineligibility,
    pins_by_listing,
    reschedule_failed_pin,
    resolve_product_link,
    save_product_url,
    schedule_pin,
    suggest_pin_content,
    suggest_product_link,
)
from app.services.pinterest_auth import (
    PinterestError,
    build_authorize_url,
    configured_credentials,
    disconnect,
    exchange_code,
    get_tokens,
)
from app.services.pinterest_settings import (
    PinterestSettingsError,
    get_pinterest_settings,
    save_pinterest_settings,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/web/templates")
router = APIRouter()

# Pending OAuth CSRF states: state → expiry epoch. Module-level is fine for
# this single-user local app; entries expire after 10 minutes.
_pending_states: dict[str, float] = {}
_STATE_TTL_SECONDS = 600


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%b %d, %Y %H:%M")


def _pin_row(pin) -> dict:
    return {
        "id": pin.id,
        "status": pin.status.value,
        "title": pin.title,
        "board_name": pin.board_name,
        "link": pin.link,
        "scheduled_for": _fmt_dt(pin.scheduled_for),
        "published_at": _fmt_dt(pin.published_at),
        "pinterest_pin_url": pin.pinterest_pin_url,
        "error": pin.error,
        "cancelable": pin.status == ScheduledPinStatus.SCHEDULED,
        "retryable": pin.status == ScheduledPinStatus.FAILED,
    }


async def _boards_for(session: AsyncSession) -> list[dict]:
    """Boards for the picker — empty when Pinterest isn't reachable."""
    tokens = await get_tokens(session)
    if tokens is None:
        return []
    try:
        from app.services.pinterest_auth import get_valid_token

        token = await get_valid_token(session)
        await session.commit()
        async with PinterestClient(token) as client:
            return await client.list_boards()
    except Exception:
        logger.warning("Pinterest: could not list boards", exc_info=True)
        return []


# ── Pages ────────────────────────────────────────────────────────────────


@router.get("/pinterest", response_class=HTMLResponse)
async def pinterest_page(request: Request, session: AsyncSession = Depends(get_session)):
    pinterest_settings = await get_pinterest_settings(session)
    tokens = await get_tokens(session)
    credentials = configured_credentials() is not None

    listings = await list_pinnable_listings(session)
    grouped = await pins_by_listing(session)
    job_ids = {listing.job_id for listing in listings}
    job_names: dict[int, str] = {}
    if job_ids:
        result = await session.execute(select(Job).where(Job.id.in_(job_ids)))
        job_names = {j.id: j.workflow_name or f"Job {j.id}" for j in result.scalars().all()}

    now = datetime.now(UTC)
    products = []
    for listing in listings:
        pins = grouped.get(listing.id, [])
        link = await resolve_product_link(session, listing)
        reason = await pin_ineligibility(
            session,
            listing.id,
            pinterest_settings=pinterest_settings,
            now=now,
            resolved_link=link,
            _link_resolved=True,
        )
        pending = next(
            (p for p in pins if p.status in (
                ScheduledPinStatus.SCHEDULED, ScheduledPinStatus.PUBLISHING)),
            None,
        )
        published = [p for p in pins if p.status == ScheduledPinStatus.PUBLISHED]
        last_published = max(
            (p.published_at for p in published if p.published_at), default=None
        )
        products.append(
            {
                "id": listing.id,
                "name": listing.name or f"Product {listing.product_id or listing.id}",
                "job_id": listing.job_id,
                "job_name": job_names.get(listing.job_id, f"Job {listing.job_id}"),
                "product_id": listing.product_id,
                "product_url": listing.product_url or "",
                "product_link": link,
                "link_is_stored": bool(listing.product_url and listing.product_url.strip()),
                "pin_status": (
                    f"Scheduled for {_fmt_dt(pending.scheduled_for)}"
                    if pending
                    else (
                        f"Last pinned {_fmt_dt(last_published)}"
                        if last_published
                        else "Never pinned"
                    )
                ),
                "ineligibility": reason,
                "can_schedule": reason is None,
            }
        )

    pins = [_pin_row(p) for p in await list_pins(session)]

    return templates.TemplateResponse(
        request,
        "pinterest.html",
        {
            "connected": tokens is not None,
            "pinterest_username": tokens.username if tokens else "",
            "credentials_configured": credentials,
            "products": products,
            "pins": pins,
            "status": request.query_params.get("status"),
            "error": request.query_params.get("error"),
            "cooldown_days": pinterest_settings.repin_cooldown_days,
        },
    )


@router.get("/pinterest/schedule/{listing_id}", response_class=HTMLResponse)
async def pinterest_schedule_page(
    request: Request, listing_id: int, session: AsyncSession = Depends(get_session)
):
    listing = await session.get(StorefrontListing, listing_id)
    if listing is None:
        return RedirectResponse("/pinterest?error=Product+not+found", status_code=303)

    pinterest_settings = await get_pinterest_settings(session)
    tokens = await get_tokens(session)
    boards = await _boards_for(session)
    suggestion = await suggest_pin_content(session, listing)
    link = await suggest_product_link(session, listing)
    reason = await pin_ineligibility(
        session, listing_id, pinterest_settings=pinterest_settings
    )

    now = datetime.now().astimezone()
    tomorrow_9am = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    saturday_11am = now + timedelta(days=(5 - now.weekday()) % 7 or 7)
    saturday_11am = saturday_11am.replace(hour=11, minute=0, second=0)
    in3days_8pm = (now + timedelta(days=3)).replace(hour=20, minute=0, second=0)
    quick_picks = [
        ("Tomorrow 9:00 AM", tomorrow_9am.strftime("%Y-%m-%dT%H:%M")),
        ("Saturday 11:00 AM", saturday_11am.strftime("%Y-%m-%dT%H:%M")),
        ("In 3 days, 8:00 PM", in3days_8pm.strftime("%Y-%m-%dT%H:%M")),
    ]

    return templates.TemplateResponse(
        request,
        "pinterest_schedule.html",
        {
            "listing": listing,
            "connected": tokens is not None,
            "boards": boards,
            "default_board_id": pinterest_settings.default_board_id,
            "suggested_title": suggestion["title"],
            "suggested_description": suggestion["description"],
            "image_paths": suggestion["image_paths"],
            "default_image": suggestion["default_image"],
            "link": link,
            "ineligibility": reason,
            "quick_picks": quick_picks,
            "min_time": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
            "errors": [],
            "values": None,
            "error": request.query_params.get("error"),
        },
    )


# ── OAuth ────────────────────────────────────────────────────────────────


@router.get("/pinterest/connect")
async def pinterest_connect():
    try:
        url, state = build_authorize_url()
    except PinterestError as exc:
        return RedirectResponse(
            f"/pinterest?{urlencode({'error': str(exc)})}", status_code=303
        )
    _pending_states[state] = time.time() + _STATE_TTL_SECONDS
    return RedirectResponse(url, status_code=303)


@router.get("/pinterest/callback")
async def pinterest_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
):
    expiry = _pending_states.pop(state, None)
    if not code or expiry is None or expiry < time.time():
        return RedirectResponse(
            "/pinterest?error=Invalid+or+expired+OAuth+state", status_code=303
        )
    try:
        await exchange_code(session, code)
        await session.commit()
    except PinterestError as exc:
        return RedirectResponse(
            f"/pinterest?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse("/pinterest?status=connected", status_code=303)


@router.post("/pinterest/disconnect")
async def pinterest_disconnect(session: AsyncSession = Depends(get_session)):
    await disconnect(session)
    await session.commit()
    return RedirectResponse("/pinterest?status=disconnected", status_code=303)


# ── Pin actions ──────────────────────────────────────────────────────────


@router.post("/pinterest/schedule/{listing_id}")
async def pinterest_schedule_submit(
    request: Request,
    listing_id: int,
    board_id: str = Form(""),
    board_name: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    link: str = Form(""),
    image_path: str = Form(""),
    scheduled_for: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    try:
        when = datetime.fromisoformat(scheduled_for.strip())
        if when.tzinfo is None:
            when = when.astimezone()  # interpret naive input as server-local
        when = when.astimezone(UTC)
    except ValueError:
        when = None

    try:
        if when is None:
            raise PinScheduleError("Scheduled time is required (YYYY-MM-DD HH:MM).")
        await schedule_pin(
            session,
            storefront_listing_id=listing_id,
            board_id=board_id,
            board_name=board_name,
            title=title,
            description=description,
            link=link,
            image_path=image_path,
            scheduled_for=when,
        )
        await session.commit()
    except PinScheduleError as exc:
        return RedirectResponse(
            f"/pinterest/schedule/{listing_id}?{urlencode({'error': str(exc)})}",
            status_code=303,
        )
    return RedirectResponse("/pinterest?status=scheduled", status_code=303)


@router.post("/pinterest/products/{listing_id}/link")
async def pinterest_product_link_save(
    listing_id: int,
    product_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Save a manually-entered public product link for a listing."""
    try:
        await save_product_url(session, listing_id, product_url)
        await session.commit()
    except PinScheduleError as exc:
        return RedirectResponse(
            f"/pinterest?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse("/pinterest?status=link-saved", status_code=303)


@router.post("/pinterest/pins/{pin_id}/cancel")
async def pinterest_pin_cancel(pin_id: int, session: AsyncSession = Depends(get_session)):
    try:
        await cancel_pin(session, pin_id)
        await session.commit()
    except PinScheduleError as exc:
        return RedirectResponse(
            f"/pinterest?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse("/pinterest?status=canceled", status_code=303)


@router.post("/pinterest/pins/{pin_id}/retry")
async def pinterest_pin_retry(pin_id: int, session: AsyncSession = Depends(get_session)):
    try:
        await reschedule_failed_pin(session, pin_id)
        await session.commit()
    except PinScheduleError as exc:
        return RedirectResponse(
            f"/pinterest?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse("/pinterest?status=rescheduled", status_code=303)


@router.get("/pinterest/image")
async def pinterest_image(path: str = ""):
    """Serve a candidate pin image — only files under IMAGE_OUTPUT_DIR."""
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from app.config import settings as app_settings

    output_root = Path(app_settings.image_output_dir).resolve()
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "image not found") from exc
    if not resolved.is_file() or not resolved.is_relative_to(output_root):
        raise HTTPException(404, "image not found")
    return FileResponse(resolved)


# ── HTMX partials ────────────────────────────────────────────────────────


@router.get("/pinterest/pins", response_class=HTMLResponse)
async def partial_pins(request: Request, session: AsyncSession = Depends(get_session)):
    pins = [_pin_row(p) for p in await list_pins(session)]
    return templates.TemplateResponse(
        request, "partials/pinterest_pins.html", {"pins": pins}
    )


@router.get("/pinterest/schedule/{listing_id}/check", response_class=HTMLResponse)
async def partial_schedule_check(
    request: Request, listing_id: int, session: AsyncSession = Depends(get_session)
):
    reason = await pin_ineligibility(session, listing_id)
    return templates.TemplateResponse(
        request,
        "partials/pinterest_eligibility.html",
        {"ineligibility": reason},
    )


# ── Settings (shared /settings/general page) ─────────────────────────────


@router.post("/settings/general/pinterest/save")
async def pinterest_settings_save(
    request: Request,
    default_board_id: str = Form(""),
    default_board_name: str = Form(""),
    repin_cooldown_days: str = Form("30"),
    daily_pin_limit: str = Form("10"),
    product_url_template: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Save the global Pinterest scheduling settings."""
    try:
        await save_pinterest_settings(
            session,
            default_board_id=default_board_id,
            default_board_name=default_board_name,
            repin_cooldown_days=repin_cooldown_days.strip() or "30",
            daily_pin_limit=daily_pin_limit.strip() or "10",
            product_url_template=product_url_template.strip()
            or "https://syntheticmuse.ai/products/{slug}",
        )
    except PinterestSettingsError as exc:
        from app.web.routes import _general_settings_context
        from app.web.routes import templates as _tpl

        context = await _general_settings_context(session, errors=exc.errors)
        return _tpl.TemplateResponse(
            request, "general_settings.html", context, status_code=422
        )
    await session.commit()
    return RedirectResponse("/settings/general?status=pinterest", status_code=303)
