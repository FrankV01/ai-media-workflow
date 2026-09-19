"""
app.pipeline.engine — Pipeline execution engine

Responsibilities:
- Accept a list of block names + initial context
- Instantiate blocks from the registry
- Run them in order, threading the context dict through each
- Record Job + JobStep rows in the DB
- Handle errors: mark failed step, stop or continue based on policy

Future enhancements:
- Parallel branches (DAG execution)
- Conditional / branching logic
- Retry with backoff
- Event hooks (on_step_start, on_step_complete, etc.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.blocks.registry import get_block
from app.models.job import Job, JobStatus, JobStep

logger = logging.getLogger(__name__)


async def run_pipeline(
    workflow_name: str,
    block_names: list[str],
    context: dict[str, Any],
    session: AsyncSession,
) -> Job:
    """
    Execute a sequence of blocks and persist results.

    Returns the completed (or failed) Job ORM instance.
    """
    job = Job(workflow_name=workflow_name, status=JobStatus.RUNNING)
    session.add(job)
    await session.flush()  # get job.id

    for order, name in enumerate(block_names):
        step = JobStep(job_id=job.id, block_name=name, order=order, status=JobStatus.RUNNING)
        step.started_at = datetime.now(timezone.utc)
        session.add(step)
        await session.flush()

        try:
            block_cls = get_block(name)
            block = block_cls()
            await block.validate(context)
            result = await block.run(context)
            context.update(result)

            step.status = JobStatus.COMPLETED
            step.output = str(result)
        except Exception as exc:
            logger.exception("Block %s failed", name)
            step.status = JobStatus.FAILED
            step.error = str(exc)
            job.status = JobStatus.FAILED
            job.error = f"Block '{name}' failed: {exc}"
            break
        finally:
            step.finished_at = datetime.now(timezone.utc)

    else:
        # All blocks succeeded
        job.status = JobStatus.COMPLETED

    job.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return job
