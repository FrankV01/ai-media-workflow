"""
app.pipeline.engine — Pipeline execution engine

Runs blocks sequentially, threading a shared context dict through each.
Supports conditional branching via verdict-based routing dicts.

Designed for background execution: creates its own DB session and commits
after each step so progress is visible in real-time via the status API.

A module-level semaphore ensures only one pipeline runs at a time.
Additional submissions queue (PENDING) until the running pipeline finishes.
This prevents concurrent LLM / ComfyUI calls that would overwhelm the host.

Step format (simple):
    ["art_director", "prompt_architect", "media_producer"]

Step format (with routing):
    [
        "art_director",
        "prompt_architect",
        "media_producer",
        "art_critic",
        {"on_good": ["publisher"], "on_bad": ["art_director"], "always": ["archiver"]},
    ]

Routing reads context["_verdict"] set by the preceding block.
See agents.md § "Pipeline routing" for details.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.blocks.registry import get_block
from app.database import async_session
from app.models.job import Job, JobStatus, JobStep
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

# Verdicts that blocks can set via context["_verdict"]
VERDICT_GOOD = "good"
VERDICT_BAD = "bad"


def _resolve_steps(steps: list[str | dict], verdict: str | None) -> list[str]:
    """Flatten a mixed step list into an ordered list of block names.

    When a routing dict is encountered, the appropriate branch is selected
    based on the current verdict. The "always" branch runs regardless.
    """
    resolved: list[str] = []
    for item in steps:
        if isinstance(item, str):
            resolved.append(item)
        elif isinstance(item, dict):
            # Routing dict — pick branch based on verdict
            always_blocks = item.get("always", [])
            if verdict == VERDICT_GOOD:
                resolved.extend(item.get("on_good", []))
            elif verdict == VERDICT_BAD:
                resolved.extend(item.get("on_bad", []))
            else:
                logger.warning("Routing dict with no verdict set — running 'always' only")
            resolved.extend(always_blocks)
    return resolved


async def run_pipeline(
    workflow_name: str,
    block_names: list[str | dict],
    context: dict[str, Any],
    *,
    start_in_background: bool = False,
) -> int:
    """
    Create a Job and execute (or schedule) a pipeline run.

    If start_in_background is True, the job row is created and committed,
    then execution is launched as an asyncio task. The function returns the
    job_id immediately.

    If False, execution runs inline and the function returns when the
    pipeline finishes.

    Returns the job ID (int).
    """
    # Create the job row as PENDING — it transitions to RUNNING once the lock is acquired
    async with async_session() as session:
        job = Job(workflow_name=workflow_name, status=JobStatus.PENDING)
        session.add(job)
        await session.commit()
        job_id = job.id

    if start_in_background:
        asyncio.create_task(
            _guarded_execute(job_id, block_names, context),
            name=f"pipeline-job-{job_id}",
        )
    else:
        await _guarded_execute(job_id, block_names, context)

    return job_id


async def _guarded_execute(
    job_id: int,
    block_names: list[str | dict],
    context: dict[str, Any],
) -> None:
    """Acquire exclusive workload ownership, then execute the complete pipeline."""
    async with workload_guard.hold(f"pipeline-job-{job_id}"):
        await _execute_pipeline(job_id, block_names, context)


async def _execute_pipeline(
    job_id: int,
    block_names: list[str | dict],
    context: dict[str, Any],
) -> None:
    """Run blocks sequentially, committing status after each step."""
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()

        # Transition from PENDING to RUNNING now that we hold the lock
        job.status = JobStatus.RUNNING

        # Pre-create PENDING steps for all known blocks so they appear in status
        # queries immediately.  Routing dicts are skipped — those blocks are
        # created when the branch is resolved at runtime.
        order = 0
        pending: list[str | dict] = list(block_names)
        step_lookup: dict[int, JobStep] = {}  # order → step (for pre-created steps)

        for item in pending:
            if isinstance(item, str):
                step = JobStep(
                    job_id=job_id,
                    block_name=item,
                    order=order,
                    status=JobStatus.PENDING,
                )
                session.add(step)
                step_lookup[order] = step
                order += 1

        next_order = order  # track next available order for dynamically-added steps
        await session.commit()

        # Preserve the original user brief so re-routed blocks can access it
        if "brief" in context:
            context["_original_brief"] = context["brief"]

        # Process blocks: linearly until a routing dict, then resolve the branch
        order = 0

        while pending:
            item = pending.pop(0)

            # Routing dict — resolve based on current verdict and prepend to pending
            if isinstance(item, dict):
                verdict = context.get("_verdict")
                branch_blocks = _resolve_steps([item], verdict)
                logger.info(
                    "Routing: verdict=%s → %s",
                    verdict,
                    branch_blocks if branch_blocks else "(no blocks)",
                )
                # Restore original brief for re-routed blocks
                if "_original_brief" in context:
                    context["brief"] = context["_original_brief"]
                # Create PENDING steps for newly-resolved blocks
                for bname in branch_blocks:
                    step = JobStep(
                        job_id=job_id,
                        block_name=bname,
                        order=next_order,
                        status=JobStatus.PENDING,
                    )
                    session.add(step)
                    step_lookup[next_order] = step
                    next_order += 1
                if branch_blocks:
                    await session.commit()
                pending = branch_blocks + pending
                continue

            # Normal block execution — use existing pre-created step or the one
            # just created by routing resolution above
            name = item
            step = step_lookup.get(order)
            if step is None:
                # Shouldn't happen, but guard against it
                step = JobStep(
                    job_id=job_id,
                    block_name=name,
                    order=order,
                    status=JobStatus.PENDING,
                )
                session.add(step)

            step.status = JobStatus.RUNNING
            step.started_at = datetime.now(UTC)
            await session.commit()

            try:
                block_cls = get_block(name)
                block = block_cls()

                # Snapshot the input context (exclude internal keys for readability)
                input_snap = {k: v for k, v in context.items() if not k.startswith("_")}
                step.input_context = json.dumps(input_snap, default=str, ensure_ascii=False)

                await block.validate(context)
                result = await block.run(context)
                context.update(result)

                # Store clean output (exclude internal keys)
                output_snap = {k: v for k, v in result.items() if not k.startswith("_")}
                step.status = JobStatus.COMPLETED
                step.output = json.dumps(output_snap, default=str, ensure_ascii=False)
            except Exception as exc:
                logger.exception("Block %s failed", name)
                step.status = JobStatus.FAILED
                step.error = str(exc)
                job.status = JobStatus.FAILED
                job.error = f"Block '{name}' failed: {exc}"
            finally:
                step.finished_at = datetime.now(UTC)
                await session.commit()
                order += 1

            if job.status == JobStatus.FAILED:
                break

        else:
            # All blocks succeeded
            job.status = JobStatus.COMPLETED

        # Persist generated asset paths at the Job level for easy querying
        generated_images = context.get("generated_images")
        if generated_images:
            job.generated_assets = json.dumps(
                generated_images,
                default=str,
                ensure_ascii=False,
            )

        job.finished_at = datetime.now(UTC)
        await session.commit()
