"""Serialize GPU-heavy workflow, LLM, and generation work across tasks and processes."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)


class ExclusiveWorkloadGuard:
    """Provide a reentrant async lock backed by an operating-system file lock."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._task_lock = asyncio.Lock()
        self._ownership: ContextVar[tuple[asyncio.Task[object], int] | None] = ContextVar(
            "workload_guard_ownership", default=None
        )

    @asynccontextmanager
    async def hold(self, owner: str) -> AsyncIterator[None]:
        """Wait for exclusive ownership and retain it for the full guarded operation."""
        ownership = self._ownership.get()
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("ExclusiveWorkloadGuard requires an asyncio task")
        if ownership is not None and ownership[0] is current_task:
            token = self._ownership.set((current_task, ownership[1] + 1))
            try:
                yield
            finally:
                self._ownership.reset(token)
            return

        async with self._task_lock:
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                await self._acquire_file_lock(descriptor, owner)
                token = self._ownership.set((current_task, 1))
                logger.info("Exclusive workload started: %s", owner)
                try:
                    yield
                finally:
                    logger.info("Exclusive workload finished: %s", owner)
                    self._ownership.reset(token)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    async def _acquire_file_lock(self, descriptor: int, owner: str) -> None:
        waiting_logged = False
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if not waiting_logged:
                    logger.warning("Exclusive workload queued: %s", owner)
                    waiting_logged = True
                await asyncio.sleep(0.25)


workload_guard = ExclusiveWorkloadGuard(
    Path(tempfile.gettempdir()) / "ai-media-workflow.workload.lock"
)
