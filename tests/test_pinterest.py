"""tests.test_pinterest — Pinterest scheduling rules, image prep, publish."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image

from app.models.job import Job, JobStatus
from app.models.pinterest import ScheduledPin, ScheduledPinStatus
from app.models.storefront import StorefrontListing, StorefrontListingStatus
from app.models.workflow import Workflow
from app.services import pinterest as pin_svc
from app.services.pinterest_auth import (
    _store_tokens,
    get_valid_token,
)
from app.services.pinterest_images import prepare_pin_image
from app.services.pinterest_settings import (
    PinterestSettingsError,
    get_pinterest_settings,
    save_pinterest_settings,
)


def _make_listing_sync(session_factory) -> int:
    """Insert a workflow + job + READY listing; return the listing id."""

    async def _insert() -> int:
        async with session_factory() as session:
            workflow = Workflow(
                name="Main", slug="main", steps_json="[]", is_default=True
            )
            session.add(workflow)
            await session.flush()
            job = Job(
                workflow_name="Test Shoot",
                workflow_id=workflow.id,
                status=JobStatus.COMPLETED,
            )
            session.add(job)
            await session.flush()
            listing = StorefrontListing(
                job_id=job.id,
                status=StorefrontListingStatus.READY,
                attempt=1,
                name="Test Product",
                product_id="fw-123",
                product_url="https://syntheticmuse.ai/products/test-product",
            )
            session.add(listing)
            await session.commit()
            return listing.id

    return asyncio.run(_insert())


def _future(days: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def test_settings_defaults_and_validation(temp_config_db):
    async def scenario():
        async with temp_config_db() as session:
            defaults = await get_pinterest_settings(session)
            assert defaults.repin_cooldown_days == 30
            assert defaults.daily_pin_limit == 10
            saved = await save_pinterest_settings(
                session, repin_cooldown_days="14", daily_pin_limit="5",
                product_url_template="https://shop.example.com/p/{slug}",
            )
            assert saved.repin_cooldown_days == 14
            loaded = await get_pinterest_settings(session)
            assert loaded.daily_pin_limit == 5
            with pytest.raises(PinterestSettingsError):
                await save_pinterest_settings(
                    session,
                    repin_cooldown_days="400",
                    daily_pin_limit="0",
                    product_url_template="http://x/{bad}",
                )

    asyncio.run(scenario())


def test_schedule_and_pending_block(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def scenario():
        async with temp_config_db() as session:
            pin = await pin_svc.schedule_pin(
                session,
                storefront_listing_id=listing_id,
                board_id="b1", board_name="Art", title="T", description="d",
                link="https://syntheticmuse.ai/products/x",
                image_path="/tmp/x.png", scheduled_for=_future(),
            )
            assert pin.status == ScheduledPinStatus.SCHEDULED
            await session.commit()

            # Second pin while one is pending → blocked
            reason = await pin_svc.pin_ineligibility(session, listing_id)
            assert reason and "already scheduled" in reason
            with pytest.raises(pin_svc.PinScheduleError):
                await pin_svc.schedule_pin(
                    session, storefront_listing_id=listing_id,
                    board_id="b1", board_name="Art", title="T2",
                    description="", link="https://x.co",
                    image_path="/tmp/x.png", scheduled_for=_future(2),
                )

    asyncio.run(scenario())


def test_cooldown_blocks_then_allows(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)
    now = datetime.now(UTC)

    async def scenario():
        async with temp_config_db() as session:
            pin = ScheduledPin(
                storefront_listing_id=listing_id, job_id=1, attempt=1,
                status=ScheduledPinStatus.PUBLISHED,
                scheduled_for=now - timedelta(days=10),
                published_at=now - timedelta(days=10),
                board_id="b", title="t", link="https://x.co", image_path="/x.png",
            )
            session.add(pin)
            await session.commit()

            reason = await pin_svc.pin_ineligibility(session, listing_id, now=now)
            assert reason and "cooldown" in reason

            # Past the 30-day cooldown → allowed
            pin.published_at = now - timedelta(days=31)
            await session.commit()
            assert await pin_svc.pin_ineligibility(session, listing_id, now=now) is None

    asyncio.run(scenario())


def test_payload_validation(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def scenario():
        async with temp_config_db() as session:
            with pytest.raises(pin_svc.PinScheduleError, match="future"):
                await pin_svc.schedule_pin(
                    session, storefront_listing_id=listing_id,
                    board_id="b", board_name="", title="t", description="",
                    link="https://x.co", image_path="/x.png",
                    scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
                )
            with pytest.raises(pin_svc.PinScheduleError, match="Title"):
                await pin_svc.schedule_pin(
                    session, storefront_listing_id=listing_id,
                    board_id="b", board_name="", title="", description="",
                    link="https://x.co", image_path="/x.png",
                    scheduled_for=_future(),
                )
            with pytest.raises(pin_svc.PinScheduleError, match="http"):
                await pin_svc.schedule_pin(
                    session, storefront_listing_id=listing_id,
                    board_id="b", board_name="", title="t", description="",
                    link="not-a-url", image_path="/x.png",
                    scheduled_for=_future(),
                )

    asyncio.run(scenario())


def test_cancel_and_retry(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def scenario():
        async with temp_config_db() as session:
            pin = await pin_svc.schedule_pin(
                session, storefront_listing_id=listing_id,
                board_id="b", board_name="B", title="t", description="",
                link="https://x.co", image_path="/x.png", scheduled_for=_future(),
            )
            await session.commit()
            await pin_svc.cancel_pin(session, pin.id)
            assert pin.status == ScheduledPinStatus.CANCELED
            with pytest.raises(pin_svc.PinScheduleError):
                await pin_svc.cancel_pin(session, pin.id)

            # A canceled pin doesn't block scheduling
            pin2 = await pin_svc.schedule_pin(
                session, storefront_listing_id=listing_id,
                board_id="b", board_name="B", title="t2", description="",
                link="https://x.co", image_path="/x.png", scheduled_for=_future(),
            )
            await session.commit()
            pin2.status = ScheduledPinStatus.FAILED
            await session.commit()
            await pin_svc.reschedule_failed_pin(session, pin2.id)
            assert pin2.status == ScheduledPinStatus.SCHEDULED
            assert pin2.attempt == 2

    asyncio.run(scenario())


def test_prepare_pin_image(tmp_path):
    src = tmp_path / "wide.png"
    Image.new("RGB", (2000, 500), (10, 20, 30)).save(src)
    data, content_type = prepare_pin_image(src)
    assert content_type == "image/jpeg"
    img = Image.open(io.BytesIO(base64.b64decode(data)))
    assert img.size == (1000, 1500)

    tall = tmp_path / "tall.png"
    Image.new("RGB", (500, 3000)).save(tall)
    data, _ = prepare_pin_image(tall)
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == (1000, 1500)

    with pytest.raises(Exception):
        prepare_pin_image(tmp_path / "missing.png")


def test_client_create_pin_payload():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "pin-9"})

    async def scenario():
        async with pin_svc.PinterestClient(
            "tok", transport=httpx.MockTransport(handler)
        ) as client:
            result = await client.create_pin(
                board_id="b1", title="t" * 200, description="d",
                link="https://x.co", image_b64="QUJD",
            )
        return result

    result = asyncio.run(scenario())
    assert result["id"] == "pin-9"
    body = json.loads(requests[0].content)
    assert body["board_id"] == "b1"
    assert len(body["title"]) == 100
    assert body["media_source"]["source_type"] == "image_base64"
    assert requests[0].headers["Authorization"] == "Bearer tok"


def test_missing_product_link_blocks_and_save(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def scenario():
        async with temp_config_db() as session:
            listing = await session.get(StorefrontListing, listing_id)
            listing.product_url = None
            await session.commit()

            # Nothing resolvable (no stored URL, no detail_json, no API creds)
            assert await pin_svc.resolve_product_link(session, listing) == ""
            reason = await pin_svc.pin_ineligibility(session, listing_id)
            assert reason and "product link" in reason
            with pytest.raises(pin_svc.PinScheduleError, match="product link"):
                await pin_svc.schedule_pin(
                    session, storefront_listing_id=listing_id,
                    board_id="b", board_name="B", title="t", description="",
                    link="https://x.co", image_path="/x.png",
                    scheduled_for=_future(),
                )

            # Bad URL rejected; good URL saved and preferred by resolution
            with pytest.raises(pin_svc.PinScheduleError, match="http"):
                await pin_svc.save_product_url(session, listing_id, "not-a-url")
            await pin_svc.save_product_url(
                session, listing_id, "https://syntheticmuse.ai/products/real-slug"
            )
            await session.commit()
            assert await pin_svc.resolve_product_link(session, listing) == (
                "https://syntheticmuse.ai/products/real-slug"
            )
            assert await pin_svc.pin_ineligibility(session, listing_id) is None

    asyncio.run(scenario())


def test_suggest_product_link_guess(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def scenario():
        async with temp_config_db() as session:
            listing = await session.get(StorefrontListing, listing_id)
            listing.product_url = None
            guess = await pin_svc.suggest_product_link(session, listing)
            assert guess.startswith("https://syntheticmuse.ai/products/")
            # A guess does not satisfy eligibility
            assert await pin_svc.pin_ineligibility(
                session, listing_id, resolved_link="", _link_resolved=True
            ) is not None

    asyncio.run(scenario())


def test_token_refresh_flow(temp_config_db, monkeypatch):
    monkeypatch.setenv("PINTEREST_CLIENT_ID", "cid")
    monkeypatch.setenv("PINTEREST_CLIENT_SECRET", "secret")

    from app.config import settings
    monkeypatch.setattr(settings, "pinterest_client_id", "cid")
    monkeypatch.setattr(settings, "pinterest_client_secret", "secret")

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.url.params)))
        return httpx.Response(200, json={
            "access_token": "new-access", "refresh_token": "new-refresh",
            "expires_in": 3600,
        })

    async def scenario():
        async with temp_config_db() as session:
            # Store an expiring token, then get_valid_token should refresh
            await _store_tokens(
                session,
                {"access_token": "old", "refresh_token": "r",
                 "expires_in": 60},
            )
            await session.commit()
            transport = httpx.MockTransport(handler)
            token = await get_valid_token(session, transport=transport)
            assert token == "new-access"

    asyncio.run(scenario())
