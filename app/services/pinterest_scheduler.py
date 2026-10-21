"""app.services.pinterest_scheduler — In-process pin publishing loop

Pinterest's API publishes pins immediately, so scheduling lives here: an
asyncio task started by the FastAPI lifespan polls `scheduled_pins` every
PINTEREST_SCHEDULER_INTERVAL seconds, claims due rows (SCHEDULED →
PUBLISHING) inside a transaction, and publishes them under the workload
guard so external calls stay serialized with the rest of the app.

Pins due while the server was down publish on the next tick after startup.
The rolling daily cap (PinterestSettings.daily_pin_limit) defers — never
fails — pins that would exceed it; they publish in a later window.
"""

from __future__ import annotations

import asyncio
import logging

import app.database
from app.config import settings
from app.services.pinterest import (
    claim_pin,
    due_pins,
    publish_pin,
    published_last_24h,
)
from app.services.pinterest_settings import get_pinterest_settings
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)


async def publish_due_pins() -> int:
    """Publish every due pin allowed by the daily cap; returns count published."""
    published = 0
    async with app.database.async_session() as session:
        pins = await due_pins(session)
        if not pins:
            return 0
        pinterest_settings = await get_pinterest_settings(session)
        already = await published_last_24h(session)
        headroom = max(0, pinterest_settings.daily_pin_limit - already)
        due = [p.id for p in pins[:headroom]]
        if headroom < len(pins):
            logger.warning(
                "Pinterest daily pin limit (%s) reached — deferring %s due pin(s)",
                pinterest_settings.daily_pin_limit,
                len(pins) - headroom,
            )
        claimed: list[int] = []
        for pin_id in due:
            if await claim_pin(session, pin_id):
                claimed.append(pin_id)
        await session.commit()

    async with workload_guard.hold("pinterest-scheduler"):
        for pin_id in claimed:
            await publish_pin(pin_id)
            published += 1
    return published


async def _scheduler_loop() -> None:
    interval = max(5.0, settings.pinterest_scheduler_interval)
    logger.info("Pinterest scheduler started (every %.0fs)", interval)
    while True:
        try:
            await publish_due_pins()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pinterest scheduler tick failed")
        await asyncio.sleep(interval)


def start_scheduler() -> asyncio.Task:
    """Create the background task; call from the FastAPI lifespan."""
    return asyncio.create_task(_scheduler_loop(), name="pinterest-scheduler")


async def stop_scheduler(task: asyncio.Task) -> None:
    """Cancel the task and wait for it (lifespan shutdown)."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Pinterest scheduler stopped")
