"""app.services.job_recovery — restart recovery for interrupted jobs.

A pipeline run lives in an in-process asyncio task; a restart abandons
it, leaving the Job row PENDING (queued, never started) or RUNNING
(killed mid-flight). At startup this module hands every such job back to
the engine:

- RUNNING jobs resume at the step that was executing — the engine
  rebuilds the context from persisted step snapshots and retries the
  interrupted step (noted in job warnings). If resume state is missing,
  the job is marked FAILED and requeued from its original brief, exactly
  as if the user had just submitted it.
- PENDING jobs (and RUNNING jobs that died before their step rows were
  committed) restart from the steps/context persisted at submission.
  Jobs submitted before that persistence existed have no recoverable
  brief and are marked FAILED.

All recovered jobs are launched as background tasks; the workload guard
serializes them one at a time, oldest first, exactly like the original
submission queue.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.database import async_session
from app.models.job import Job, JobStatus, JobStep
from app.models.workflow import Workflow
from app.pipeline.engine import (
    ResumeError,
    _apply_workflow_context,
    _guarded_execute,
    resume_interrupted_job,
    run_pipeline,
)
from app.services.workflows import parse_steps_json

logger = logging.getLogger(__name__)

_INTERRUPTED_ERROR = "Job abandoned by an application restart"


async def recover_interrupted_jobs() -> list[asyncio.Task]:
    """Find jobs left PENDING/RUNNING by a previous process and relaunch them.

    Returns the launched asyncio tasks (queued on the workload guard, so
    they still run one at a time) so callers/tests can await completion.
    """
    launches: list[tuple[str, int, list[str | dict] | None, dict[str, Any] | None]] = []

    async with async_session() as session:
        jobs = list(
            (
                await session.execute(
                    select(Job)
                    .where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
                    .order_by(Job.created_at, Job.id)
                )
            )
            .scalars()
            .all()
        )
        if not jobs:
            return []

        step_counts = dict(
            (
                await session.execute(select(JobStep.job_id, func.count()).group_by(JobStep.job_id))
            ).all()
        )

        for job in jobs:
            if step_counts.get(job.id):
                # Step rows exist — resume where it stopped (or fail+requeue)
                launches.append(("resume", job.id, None, None))
                continue

            plan = await _queued_plan(session, job)
            if plan is None:
                _fail_abandoned(
                    job,
                    "submission context was not persisted — cannot requeue",
                )
            else:
                launches.append(("execute", job.id, plan[0], plan[1]))

        await session.commit()

    tasks: list[asyncio.Task] = []
    for kind, job_id, steps, context in launches:
        if kind == "resume":
            logger.info("Recovery: resuming interrupted job %s", job_id)
            tasks.append(
                asyncio.create_task(
                    _resume_or_requeue(job_id),
                    name=f"pipeline-job-{job_id}-recover",
                )
            )
        else:
            logger.info("Recovery: requeueing pending job %s", job_id)
            tasks.append(
                asyncio.create_task(
                    _guarded_execute(job_id, steps or [], context or {}),
                    name=f"pipeline-job-{job_id}-recover",
                )
            )
    return tasks


async def _queued_plan(session: Any, job: Job) -> tuple[list[str | dict], dict[str, Any]] | None:
    """Return (steps, context) to re-run a job that never produced step rows."""
    if not job.context_json:
        return None
    context: dict[str, Any] = json.loads(job.context_json)

    if job.steps_json:
        steps: list[str | dict] = json.loads(job.steps_json)
    else:
        workflow = (
            await session.get(Workflow, job.workflow_id) if job.workflow_id is not None else None
        )
        if workflow is None:
            return None
        steps = parse_steps_json(workflow.steps_json)

    # Restamp workflow keys — profiles resolve at run time, and a disabled
    # workflow does not cancel an already-queued job.
    try:
        await _apply_workflow_context(session, job.workflow_id, context, require_enabled=False)
    except Exception as exc:
        logger.warning("Recovery: job %s workflow unusable: %s", job.id, exc)
        return None
    return steps, context


async def _resume_or_requeue(job_id: int) -> None:
    """Resume an interrupted job; on failure, fail it and requeue its brief."""
    try:
        await resume_interrupted_job(job_id)
        return
    except ResumeError as exc:
        logger.warning("Recovery: cannot resume job %s: %s", job_id, exc)
    except Exception:
        logger.exception("Recovery: resume of job %s crashed", job_id)

    brief, context, workflow_id = await _extract_original_brief(job_id)
    new_job_id: int | None = None
    if brief:
        try:
            new_job_id = await run_pipeline(
                workflow_name=None,
                context=context,
                workflow_id=workflow_id,
                start_in_background=True,
            )
        except Exception as exc:
            logger.warning("Recovery: requeue of job %s failed: %s", job_id, exc)

    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        note = f"; requeued as job {new_job_id}" if new_job_id else ""
        _fail_abandoned(job, f"could not resume after restart{note}")
        await session.commit()


def _fail_abandoned(job: Job, detail: str) -> None:
    job.status = JobStatus.FAILED
    job.error = f"{_INTERRUPTED_ERROR}: {detail}"
    job.finished_at = datetime.now(UTC)


async def _extract_original_brief(
    job_id: int,
) -> tuple[str | None, dict[str, Any], int | None]:
    """Recover the original submission context for a failed job's requeue."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return None, {}, None

        context: dict[str, Any] = {}
        if job.context_json:
            context = json.loads(job.context_json)
        else:
            # Legacy rows: the brief lives in the earliest step snapshot
            row = (
                await session.execute(
                    select(JobStep.input_context)
                    .where(
                        JobStep.job_id == job_id,
                        JobStep.input_context.is_not(None),
                    )
                    .order_by(JobStep.order, JobStep.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                context = json.loads(row)

        brief = str(context.get("brief") or "").strip()
        if not brief:
            return None, {}, None
        # Requeue as a fresh submission: keep user-facing context only
        requeue_context = {
            k: v for k, v in context.items() if not k.startswith("_") or k == "_generation_backend"
        }
        return brief, requeue_context, job.workflow_id
