"""
app.pipeline.engine — Pipeline execution engine

Responsibilities:
- Accept a list of block names (or step dicts) + initial context
- Instantiate blocks from the registry
- Run them in order, threading the context dict through each
- Support conditional branching via verdict-based routing
- Record Job + JobStep rows in the DB
- Handle errors: mark failed step, stop or continue based on policy

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

When the engine encounters a routing dict, it reads context["_verdict"] (set by
the preceding block) and selects the appropriate branch:
- "good" → runs blocks listed under "on_good"
- "bad"  → runs blocks listed under "on_bad"
- blocks under "always" run regardless of verdict

If no routing dict follows a verdict-producing block, execution continues
linearly as before.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.blocks.registry import get_block
from app.models.job import Job, JobStatus, JobStep

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
    session: AsyncSession,
) -> Job:
    """
    Execute a sequence of blocks and persist results.

    block_names can be a simple list of strings or a mixed list containing
    routing dicts for conditional branching. See module docstring for format.

    Returns the completed (or failed) Job ORM instance.
    """
    job = Job(workflow_name=workflow_name, status=JobStatus.RUNNING)
    session.add(job)
    await session.flush()  # get job.id

    # Split the step list into pre-routing and routing portions
    # We process linearly until we hit a routing dict, then resolve the branch
    pending: list[str | dict] = list(block_names)
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
            # Prepend resolved blocks so they execute next
            pending = branch_blocks + pending
            continue

        # Normal block execution
        name = item
        step = JobStep(job_id=job.id, block_name=name, order=order, status=JobStatus.RUNNING)
        step.started_at = datetime.now(timezone.utc)
        session.add(step)
        await session.flush()

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
            break
        finally:
            step.finished_at = datetime.now(timezone.utc)
            order += 1

    else:
        # All blocks succeeded
        job.status = JobStatus.COMPLETED

    job.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return job
