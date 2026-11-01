import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event
from starlette.requests import Request

from app.models.pinterest import ScheduledPin, ScheduledPinStatus
from app.models.storefront import StorefrontListing, StorefrontListingStatus
from app.services import pinterest as service
from app.services import pinterest_auth as auth
from app.services import pinterest_scheduler as scheduler
from app.web import pinterest_routes as routes
from tests.test_pinterest import _make_listing_sync


@pytest.fixture(autouse=True)
def isolated_guard(tmp_path, monkeypatch):
    from app.services.workload_guard import ExclusiveWorkloadGuard

    monkeypatch.setattr(scheduler, "workload_guard", ExclusiveWorkloadGuard(tmp_path / "work.lock"))


async def authorize(session):
    await auth._store_tokens(
        session,
        {
            "access_token": "test",
            "refresh_token": "test-refresh",
            "expires_in": 172800,
            "scope": auth.PINTEREST_SCOPES,
        },
        "original",
    )
    await session.commit()


async def add_pin(session, listing_id, status=ScheduledPinStatus.SCHEDULED, days=-1):
    listing = await session.get(StorefrontListing, listing_id)
    pin = ScheduledPin(
        storefront_listing_id=listing_id,
        job_id=listing.job_id,
        status=status,
        scheduled_for=datetime.now(UTC) + timedelta(days=days),
        title="Test",
        board_id="123",
        link="https://syntheticmuse.ai/products/test",
        image_path="unused.png",
    )
    session.add(pin)
    await session.commit()
    return pin.id


def test_scopes_and_legacy_metadata(temp_config_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pinterest_client_id", "test")
    monkeypatch.setattr(settings, "pinterest_client_secret", "test")
    url, _ = auth.build_authorize_url()
    assert "boards:write" in parse_qs(urlparse(url).query)["scope"][0]

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            tokens = await auth.get_tokens(session)
            assert auth.permission_warning(tokens) is None
            await auth._store_tokens(
                session,
                {
                    "access_token": "rotated",
                    "refresh_token": "rotated-r",
                    "expires_in": 99999,
                },
                "original",
            )
            assert (await auth.get_tokens(session)).scopes == tokens.scopes
            tokens.scopes = None
            assert "unverified" in auth.permission_warning(tokens)
            tokens.scopes = ["pins:write"]
            assert "boards:write" in auth.permission_warning(tokens)

    asyncio.run(run())


@pytest.mark.parametrize(
    "response,expected,error",
    [
        ({"id": "123456", "link": "https://syntheticmuse.ai/products/test"}, True, None),
        ({}, False, "PublicationOutcomeUnknown"),
        (None, False, "PublicationOutcomeUnknown"),
        (auth.PinterestApiError(401, "Missing: ['boards:write']"), False, "PinterestApiError"),
        (auth.PinterestError("connection lost"), False, "PublicationOutcomeUnknown"),
    ],
)
def test_publish_outcomes(temp_config_db, monkeypatch, response, expected, error):
    listing_id = _make_listing_sync(temp_config_db)

    async def create(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(service.PinterestClient, "create_pin", create)
    monkeypatch.setattr(service, "prepare_pin_image", lambda path: ("AA", "image/jpeg"))

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            pin_id = await add_pin(session, listing_id, ScheduledPinStatus.PUBLISHING)
        assert await service.publish_pin(pin_id) is expected
        async with temp_config_db() as session:
            pin = await session.get(ScheduledPin, pin_id)
            assert pin.error_type == error
            if expected:
                assert pin.pinterest_pin_url == "https://www.pinterest.com/pin/123456/"
                assert pin.published_at is not None
            else:
                assert pin.status == ScheduledPinStatus.FAILED
                assert pin.published_at is None

    asyncio.run(run())


def test_scheduler_overdue_future_recovery_and_cap(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    monkeypatch.setattr(service, "prepare_pin_image", lambda path: ("AA", "image/jpeg"))
    create = AsyncMock(return_value={"id": "123456"})
    monkeypatch.setattr(service.PinterestClient, "create_pin", create)

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            due_id = await add_pin(session, listing_id)
            future_id = await add_pin(session, listing_id, days=1)
            interrupted_id = await add_pin(session, listing_id, ScheduledPinStatus.PUBLISHING)
        assert await scheduler.publish_due_pins() == 1
        assert await scheduler.publish_due_pins() == 0
        assert create.await_count == 1
        async with temp_config_db() as session:
            assert (await session.get(ScheduledPin, due_id)).status == ScheduledPinStatus.PUBLISHED
            future = await session.get(ScheduledPin, future_id)
            assert future.status == ScheduledPinStatus.SCHEDULED
            interrupted = await session.get(ScheduledPin, interrupted_id)
            assert interrupted.error_type == "PublicationOutcomeUnknown"
            assert interrupted.status == ScheduledPinStatus.FAILED

    asyncio.run(run())


def test_dashboard_constant_queries_no_network(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    monkeypatch.setattr(
        service.FourthwallClient,
        "get_product",
        AsyncMock(side_effect=AssertionError("Dashboard must not call FourthWall")),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/pinterest",
            "headers": [],
            "query_string": b"",
        }
    )

    async def run():
        async with temp_config_db() as session:
            await add_pin(session, listing_id, ScheduledPinStatus.FAILED)
        counts = []
        for total in (1, 30):
            async with temp_config_db() as session:
                if total > 1:
                    listing = await session.get(StorefrontListing, listing_id)
                    for index in range(total - 1):
                        session.add(
                            StorefrontListing(
                                job_id=listing.job_id,
                                attempt=index + 2,
                                status=StorefrontListingStatus.READY,
                                name=f"Product {index}",
                            )
                        )
                    await session.commit()
            statements = []
            engine = temp_config_db.kw["bind"].sync_engine

            def record(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            event.listen(engine, "before_cursor_execute", record)
            try:
                async with temp_config_db() as session:
                    context = await routes.dashboard_context(request, session)
                    assert any("Failed to publish" in p["pin_status"] for p in context["products"])
                    template = routes.templates.get_template("partials/pinterest_dashboard.html")
                    html = template.render(context, request=request)
                    assert "Failed to publish" in html
            finally:
                event.remove(engine, "before_cursor_execute", record)
            counts.append(len(statements))
        assert counts[0] == counts[1] == 5
        service.FourthwallClient.get_product.assert_not_awaited()

    asyncio.run(run())


def test_daily_cap_and_concurrent_workers(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    create = AsyncMock(return_value={"id": "123456"})
    monkeypatch.setattr(service.PinterestClient, "create_pin", create)
    monkeypatch.setattr(service, "prepare_pin_image", lambda path: ("AA", "image/jpeg"))

    async def run():
        from app.services.pinterest_settings import save_pinterest_settings

        async with temp_config_db() as session:
            await authorize(session)
            await save_pinterest_settings(session, daily_pin_limit=1)
            await session.commit()
            await add_pin(session, listing_id)
            await add_pin(session, listing_id)
        results = await asyncio.gather(scheduler.publish_due_pins(), scheduler.publish_due_pins())
        assert sum(results) == 1
        assert create.await_count == 1
        async with temp_config_db() as session:
            assert len(await service.due_pins(session)) == 1

    asyncio.run(run())


def test_cancel_waiting_scheduler_does_not_claim(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            pin_id = await add_pin(session, listing_id)
        async with scheduler.workload_guard.hold("test-owner"):
            task = asyncio.create_task(scheduler.publish_due_pins())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with temp_config_db() as session:
            pin = await session.get(ScheduledPin, pin_id)
            assert pin.status == ScheduledPinStatus.SCHEDULED
            assert pin.started_at is None

    asyncio.run(run())


def test_reauthorize_cannot_switch_pending_account(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    monkeypatch.setattr(
        auth,
        "_token_request",
        AsyncMock(
            return_value={
                "access_token": "other",
                "refresh_token": "other-r",
                "expires_in": 172800,
                "scope": auth.PINTEREST_SCOPES,
            }
        ),
    )
    monkeypatch.setattr(auth, "_fetch_username", AsyncMock(return_value="different"))

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            await add_pin(session, listing_id)
            with pytest.raises(auth.PinterestError, match="account"):
                await auth.exchange_code(session, "code")
            assert (await auth.get_tokens(session)).username == "original"
            with pytest.raises(auth.PinterestError, match="Cancel pending"):
                await auth.disconnect(session)
            monkeypatch.setattr(auth, "_fetch_username", AsyncMock(return_value="original"))
            await auth.exchange_code(session, "code")
            assert auth.permission_warning(await auth.get_tokens(session)) is None

    asyncio.run(run())


def test_unknown_outcome_retry_requires_confirmation(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def run():
        async with temp_config_db() as session:
            pin_id = await add_pin(session, listing_id, ScheduledPinStatus.FAILED)
            pin = await session.get(ScheduledPin, pin_id)
            pin.error_type = "PublicationOutcomeUnknown"
            await session.commit()
            with pytest.raises(service.PinScheduleError, match="confirm"):
                await service.reschedule_failed_pin(session, pin_id)
            await service.reschedule_failed_pin(session, pin_id, board_checked=True)
            assert pin.status == ScheduledPinStatus.SCHEDULED

    asyncio.run(run())


def test_refresh_survives_image_failure(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    monkeypatch.setattr(
        auth,
        "_token_request",
        AsyncMock(
            return_value={
                "access_token": "rotated",
                "refresh_token": "rotated-r",
                "expires_in": 172800,
            }
        ),
    )

    def fail_image(path):
        raise service.PinterestImageError("missing image")

    monkeypatch.setattr(service, "prepare_pin_image", fail_image)

    async def run():
        async with temp_config_db() as session:
            await authorize(session)
            await auth._store_tokens(
                session,
                {
                    "access_token": "old",
                    "refresh_token": "old-r",
                    "expires_in": 60,
                },
                "original",
            )
            await session.commit()
            pin_id = await add_pin(session, listing_id, ScheduledPinStatus.PUBLISHING)
        assert await service.publish_pin(pin_id) is False
        async with temp_config_db() as session:
            tokens = await auth.get_tokens(session)
            assert tokens.access_token == "rotated"
            assert "boards:write" in tokens.scopes
            assert (await session.get(ScheduledPin, pin_id)).error_type == "PinterestImageError"

    asyncio.run(run())


def test_dashboard_polling_and_delegated_editor():
    template = routes.templates.get_template("pinterest.html")
    html = template.render(
        request=Request({"type": "http", "headers": [], "query_string": b""}),
        products=[],
        pins=[],
        cooldown_days=30,
        connected=True,
        credentials_configured=True,
    )
    assert 'hx-swap="innerHTML"' in html
    assert 'hx-trigger="every 30s, linksResolved"' in html
    assert "resolveMissingLinks();" in html
    assert "lookupStates.has(el.dataset.listingId)" in html
    assert "Reauthorize Pinterest" in html
    assert "event.target.closest('.edit-link-btn')" in html


def test_remote_link_is_cached_and_reused(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    monkeypatch.setattr(service, "configured_credentials", lambda: ("test", "test"))
    remote = AsyncMock(return_value={"slug": "real-fourthwall-slug"})
    monkeypatch.setattr(service.FourthwallClient, "get_product", remote)

    async def run():
        async with temp_config_db() as session:
            listing = await session.get(StorefrontListing, listing_id)
            listing.product_url = None
            await session.commit()
            link = await service.resolve_and_cache_product_link(session, listing_id)
            assert link == "https://syntheticmuse.ai/products/real-fourthwall-slug"
        async with temp_config_db() as session:
            assert await service.resolve_and_cache_product_link(session, listing_id) == link
            listing = await session.get(StorefrontListing, listing_id)
            assert (
                service.local_product_link(listing, await service.get_pinterest_settings(session))
                == link
            )
        remote.assert_awaited_once_with("fw-123")

    asyncio.run(run())


def test_link_resolution_does_not_overwrite_manual_edit(temp_config_db, monkeypatch):
    listing_id = _make_listing_sync(temp_config_db)
    manual = "https://syntheticmuse.ai/products/manually-chosen"

    async def resolve(session, listing):
        async with temp_config_db() as other:
            await service.save_product_url(other, listing_id, manual)
            await other.commit()
        return "https://syntheticmuse.ai/products/remote-result"

    monkeypatch.setattr(service, "resolve_product_link", resolve)

    async def run():
        async with temp_config_db() as session:
            listing = await session.get(StorefrontListing, listing_id)
            listing.product_url = None
            await session.commit()
            assert await service.resolve_and_cache_product_link(session, listing_id) == manual

    asyncio.run(run())


@pytest.mark.parametrize("outcome,code", [("", 404), (TimeoutError(), 504)])
def test_link_lookup_failures_leave_database_empty(temp_config_db, monkeypatch, outcome, code):
    listing_id = _make_listing_sync(temp_config_db)
    lookup = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=outcome,
    )
    monkeypatch.setattr(service, "resolve_product_link", lookup)

    async def run():
        async with temp_config_db() as session:
            listing = await session.get(StorefrontListing, listing_id)
            listing.product_url = None
            await session.commit()
            response = await routes.pinterest_product_link_resolve(listing_id, session)
            assert response.status_code == code
        async with temp_config_db() as session:
            assert (await session.get(StorefrontListing, listing_id)).product_url is None

    asyncio.run(run())


def test_atomic_claim_and_permission_pause(temp_config_db):
    listing_id = _make_listing_sync(temp_config_db)

    async def run():
        async with temp_config_db() as session:
            pin_id = await add_pin(session, listing_id)
        assert await scheduler.publish_due_pins() == 0
        async with temp_config_db() as session:
            assert (await session.get(ScheduledPin, pin_id)).status == ScheduledPinStatus.SCHEDULED
            assert await service.claim_pin(session, pin_id)
            await session.commit()
        async with temp_config_db() as session:
            assert not await service.claim_pin(session, pin_id)

    asyncio.run(run())
