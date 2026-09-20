"""Verify exclusive workload serialization and reentrant pipeline ownership."""

import asyncio
from pathlib import Path

import pytest

from app.services.workload_guard import ExclusiveWorkloadGuard


@pytest.mark.asyncio
async def test_guard_prevents_task_overlap(tmp_path: Path) -> None:
    guard = ExclusiveWorkloadGuard(tmp_path / "workload.lock")
    active = 0
    maximum_active = 0
    events: list[str] = []

    async def worker(name: str) -> None:
        nonlocal active, maximum_active
        async with guard.hold(name):
            active += 1
            maximum_active = max(maximum_active, active)
            events.append(f"{name}:start")
            await asyncio.sleep(0.02)
            events.append(f"{name}:end")
            active -= 1

    await asyncio.gather(worker("prompt"), worker("media"))

    assert maximum_active == 1
    assert events == ["prompt:start", "prompt:end", "media:start", "media:end"]


@pytest.mark.asyncio
async def test_child_task_cannot_bypass_parent_ownership(tmp_path: Path) -> None:
    guard = ExclusiveWorkloadGuard(tmp_path / "workload.lock")
    child_entered = asyncio.Event()

    async def child() -> None:
        async with guard.hold("child"):
            child_entered.set()

    async with guard.hold("parent"):
        child_task = asyncio.create_task(child())
        await asyncio.sleep(0.02)
        assert not child_entered.is_set()

    await asyncio.wait_for(child_task, timeout=1.0)
    assert child_entered.is_set()


@pytest.mark.asyncio
async def test_guard_is_reentrant_for_pipeline_blocks(tmp_path: Path) -> None:
    guard = ExclusiveWorkloadGuard(tmp_path / "workload.lock")

    async def nested_work() -> None:
        async with guard.hold("pipeline"):
            async with guard.hold("prompt"):
                async with guard.hold("media"):
                    return

    await asyncio.wait_for(nested_work(), timeout=1.0)
