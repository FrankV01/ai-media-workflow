"""
app.pipeline.engine — Pipeline execution engine

Runs blocks sequentially, threading a shared context dict through each.
Supports conditional branching via verdict-based routing dicts.

Designed for background execution: creates its own DB session and commits
status, context snapshots, structured failures, and normalized LLM audit
records after each step so progress and model work are queryable in real time.

The workload guard (app.services.workload_guard — a reentrant async lock
backed by an OS file lock) ensures only one pipeline runs at a time.
Additional submissions queue (PENDING) until the running pipeline finishes.
This prevents concurrent LLM / ComfyUI calls that would overwhelm the host.

The engine exposes context["_job_id"] (the current job's id) and
context["_job_created_date"] (its UTC creation date) to blocks, and copies
context["photo_shoot_name"] onto Job.workflow_name mid-run so a block
(e.g. the Art Director) can title the job while it executes.

Step format (simple):
    ["art_director", "prompt_architect", "media_producer"]

Step format (with routing):
    [
        "art_director",
        "prompt_architect",
        "media_producer",
        "art_critic",
        {"on_good": ["publisher"], "on_bad": ["revise"], "always": ["archiver"]},
    ]

Routing reads context["_verdict"] set by the preceding block.
See agents.md § "Pipeline routing" for details.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.blocks.registry import get_block
from app.database import async_session
from app.models.creative import CreativeRole, Message, MessageRole, RoleExecution
from app.models.job import Job, JobStatus, JobStep
from app.models.media import MediaGenerationExecution
from app.pipeline.naming import MAX_PHOTO_SHOOT_NAME_LENGTH, resolve_photo_shoot_name
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

# Verdicts that blocks can set via context["_verdict"]
VERDICT_GOOD = "good"
VERDICT_BAD = "bad"

# Job title used until a block names the shoot (context["photo_shoot_name"])
UNTITLED_SHOOT = "Untitled shoot"


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


_SNAPSHOT_EXCLUDED_KEYS = {"_executions", "_media_executions", "_warnings"}


def _audit_snapshot(values: dict[str, Any]) -> dict[str, Any]:
    """Return persisted context data without normalized/private records."""
    return {k: v for k, v in values.items() if k not in _SNAPSHOT_EXCLUDED_KEYS}


async def _persist_role_executions(
    session: Any,
    step: JobStep,
    records: list[dict[str, Any]],
) -> None:
    """Persist LLM calls and ordered messages against their pipeline step.

    Never mutates CreativeRole or LlmRoleConfiguration — the execution's
    copied fields are the audit authority; role/config rows are only
    referenced (or minimally created) by stable lookup.
    """
    for record in records:
        role = None
        if record.get("role_id"):
            role = await session.get(CreativeRole, record["role_id"])
        if role is None:
            role_result = await session.execute(
                select(CreativeRole).where(CreativeRole.name == record["role_name"])
            )
            role = role_result.scalar_one_or_none()
        if role is None:
            role = CreativeRole(
                name=record["role_name"],
                title=record.get("role_title", ""),
                description=record.get("role_description", ""),
                output_format=record.get("output_format", "text"),
                suggested_next_role=record.get("suggested_next_role"),
            )
            session.add(role)
            await session.flush()

        execution = RoleExecution(
            job_step_id=step.id,
            role_id=role.id,
            configuration_id=record.get("configuration_id"),
            configuration_source=record.get("configuration_source", "legacy"),
            system_prompt=str(record.get("system_prompt", "")),
            input_brief=str(record.get("input_brief", "")),
            output_deliverable=record.get("output_deliverable"),
            suggested_next_role=record.get("suggested_next_role"),
            model_used=record.get("model_used"),
            temperature=record.get("temperature"),
            max_tokens=record.get("max_tokens"),
            reasoning_effort=record.get("reasoning_effort"),
            finish_reason=record.get("finish_reason"),
            status=record.get("status", "completed"),
            error_type=record.get("error_type"),
            error=record.get("error"),
            prompt_tokens=record.get("prompt_tokens"),
            completion_tokens=record.get("completion_tokens"),
            total_tokens=record.get("total_tokens"),
            started_at=record.get("started_at", step.started_at),
            finished_at=record.get("finished_at"),
        )
        session.add(execution)
        await session.flush()

        for message_record in record.get("messages", []):
            session.add(
                Message(
                    execution_id=execution.id,
                    role=MessageRole(message_record["role"]),
                    content=str(message_record.get("content", "")),
                    ordinal=int(message_record["ordinal"]),
                )
            )


async def _persist_media_executions(
    session: Any,
    step: JobStep,
    records: list[dict[str, Any]],
) -> None:
    """Persist one media-generation audit row per attempted request."""
    for record in records:
        session.add(
            MediaGenerationExecution(
                job_step_id=step.id,
                configuration_id=record.get("configuration_id"),
                configuration_source=record.get("configuration_source", "code_default"),
                block_name=record.get("block_name", step.block_name),
                backend_name=str(record.get("backend_name", "")),
                model_name=str(record.get("model_name", "")),
                variant_name=str(record.get("variant_name", "main")),
                positive_prompt=str(record.get("positive_prompt", "")),
                negative_prompt=str(record.get("negative_prompt", "")),
                positive_refiner_prompt=str(record.get("positive_refiner_prompt", "")),
                negative_refiner_prompt=str(record.get("negative_refiner_prompt", "")),
                settings_snapshot=str(record.get("settings_snapshot", "{}")),
                seed_used=record.get("seed_used"),
                backend_metadata=record.get("backend_metadata"),
                image_paths=record.get("image_paths"),
                status=record.get("status", "completed"),
                error_type=record.get("error_type"),
                error=record.get("error"),
                started_at=record.get("started_at", step.started_at),
                finished_at=record.get("finished_at"),
            )
        )


def _persist_job_warnings(job: Job, context: dict[str, Any]) -> None:
    """Write deduplicated context['_warnings'] onto the job as JSON."""
    warnings = context.get("_warnings") or []
    deduped = list(dict.fromkeys(warnings))
    job.warnings = json.dumps(deduped) if deduped else None


async def run_pipeline(
    workflow_name: str | None,
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
    if workflow_name is None:
        # Untitled — a block (e.g. Art Director) may name the shoot mid-run
        job_name = UNTITLED_SHOOT
    else:
        job_name = workflow_name
        # Prevents the Art Director from inventing a different name
        context.setdefault("photo_shoot_name", workflow_name)

    # Create the job row as PENDING — it transitions to RUNNING once the lock is acquired
    async with async_session() as session:
        job = Job(workflow_name=job_name, status=JobStatus.PENDING)
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
    """Run blocks sequentially and persist complete audit state after each step."""
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()

        # Transition from PENDING to RUNNING now that we hold the lock
        job.status = JobStatus.RUNNING
        context["_job_id"] = job_id
        context["_job_created_date"] = job.created_at.date().isoformat()

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

            # Snapshot the input context and commit before running so it's
            # visible in status queries while the block executes
            step.input_context = json.dumps(
                _audit_snapshot(context), default=str, ensure_ascii=False
            )
            execution_offset = len(context.setdefault("_executions", []))
            media_offset = len(context.setdefault("_media_executions", []))
            await session.commit()

            try:
                block_cls = get_block(name)
                block = block_cls()

                await block.validate(context)
                result = await block.run(context)
                context.update(result)

                # A block may have named the shoot — retitle the job mid-run
                name = context.get("photo_shoot_name")
                if isinstance(name, str) and name.strip() and name != job.workflow_name:
                    job.workflow_name = resolve_photo_shoot_name(name[:MAX_PHOTO_SHOOT_NAME_LENGTH])

                # Store the block result; LLM executions are normalized separately
                step.status = JobStatus.COMPLETED
                step.output = json.dumps(_audit_snapshot(result), default=str, ensure_ascii=False)
            except Exception as exc:
                logger.exception("Block %s failed", name)
                step.status = JobStatus.FAILED
                step.error = str(exc)
                step.error_type = type(exc).__name__
                step.error_traceback = traceback.format_exc()
                job.status = JobStatus.FAILED
                job.error = f"Block '{name}' failed: {exc}"
            finally:
                step.finished_at = datetime.now(UTC)
                await _persist_role_executions(
                    session,
                    step,
                    context.get("_executions", [])[execution_offset:],
                )
                await _persist_media_executions(
                    session,
                    step,
                    context.get("_media_executions", [])[media_offset:],
                )
                _persist_job_warnings(job, context)
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

        # Persist markdown report paths written by report blocks
        report_files = context.get("report_files")
        if report_files:
            job.report_files = json.dumps(report_files, default=str, ensure_ascii=False)

        job.finished_at = datetime.now(UTC)
        await session.commit()
