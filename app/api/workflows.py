"""
app.api.workflows — Workflow execution endpoints

POST /api/workflows/run       — queue a pipeline run, returns job_id immediately
GET  /api/workflows/jobs      — list recent jobs (optional ?workflow=<slug> filter)
GET  /api/workflows/jobs/{id} — job detail with per-step status, I/O, and timing

Every run executes inside a workflow "container": `workflow` (a slug) picks
the persisted definition and the scope for all AI configuration profiles;
`block_names` may still override the step list. When neither is supplied,
the default workflow's steps and configuration scope apply.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models.creative import RoleExecution
from app.models.delivery import DeliveryArchive, DeliveryArchiveStatus
from app.models.job import Job
from app.models.workflow import Workflow
from app.pipeline.engine import run_pipeline
from app.pipeline.naming import PhotoShootNameError, resolve_photo_shoot_name
from app.services.delivery_archive import (
    ArchiveRetryError,
    DeliveryArchiveError,
    archive_download_path,
    get_archive,
    list_archives,
    reserve_archive_retry,
    schedule_archive_retry,
)
from app.services.workflows import get_workflow_by_slug, validate_steps

router = APIRouter()


def _workflow_json(workflow: Workflow | None) -> dict[str, Any] | None:
    if workflow is None:
        return None
    return {"slug": workflow.slug, "name": workflow.name}


class RunRequest(BaseModel):
    """Payload to trigger a workflow run."""

    photo_shoot_name: str | None = None
    workflow_name: str | None = Field(default=None, deprecated=True)
    # Slug of a stored workflow definition; selects its steps and its AI
    # configuration scope. None → the default workflow.
    workflow: str | None = None
    block_names: list[str | dict[str, Any]] | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_photo_shoot_name(self) -> "RunRequest":
        try:
            self.photo_shoot_name = resolve_photo_shoot_name(
                self.photo_shoot_name,
                self.__dict__.get("workflow_name"),
            )
        except PhotoShootNameError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @model_validator(mode="after")
    def validate_block_names(self) -> "RunRequest":
        if self.block_names is not None:
            errors = validate_steps(self.block_names)
            if errors:
                raise ValueError(f"invalid block_names: {'; '.join(errors)}")
        return self

    @property
    def job_name(self) -> str:
        if self.photo_shoot_name is None:
            raise RuntimeError("photo_shoot_name was not validated")
        return self.photo_shoot_name


@router.post("/run", status_code=202)
async def run_workflow(req: RunRequest, session: AsyncSession = Depends(get_session)):
    """Queue a pipeline run in the background. Returns the job_id immediately."""
    workflow = None
    if req.workflow is not None:
        workflow = await get_workflow_by_slug(session, req.workflow)
        if workflow is None:
            raise HTTPException(404, f"Unknown workflow '{req.workflow}'")
        if not workflow.is_enabled:
            raise HTTPException(409, f"Workflow '{req.workflow}' is disabled")
    job_id = await run_pipeline(
        workflow_name=req.job_name,
        block_names=req.block_names,
        context=dict(req.context),
        workflow_id=workflow.id if workflow else None,
        start_in_background=True,
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs")
async def list_jobs(
    limit: int = 20,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Return recent jobs, newest first; `workflow` filters by slug."""
    stmt = (
        select(Job).options(selectinload(Job.workflow)).order_by(Job.created_at.desc()).limit(limit)
    )
    if workflow is not None:
        stmt = stmt.join(Workflow, Job.workflow_id == Workflow.id).where(Workflow.slug == workflow)
    result = await session.execute(stmt)
    jobs = result.scalars().all()
    return [
        {
            "id": j.id,
            "photo_shoot_name": j.workflow_name,
            "workflow_name": j.workflow_name,
            "workflow": _workflow_json(j.workflow),
            "status": j.status.value,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "error": j.error,
            "warnings": json.loads(j.warnings) if j.warnings else [],
        }
        for j in jobs
    ]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    """Return a job with full per-step status, input, output, and timing."""
    result = await session.execute(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.steps), selectinload(Job.workflow))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    # Effective LLM audit rows per step — the copied fields record exactly
    # what was sent to the model (immutable; not the mutable profile row)
    steps_sorted = sorted(job.steps, key=lambda s: s.order)
    executions_by_step: dict[int, list[RoleExecution]] = {}
    step_ids = [s.id for s in steps_sorted]
    if step_ids:
        exec_result = await session.execute(
            select(RoleExecution)
            .where(RoleExecution.job_step_id.in_(step_ids))
            .order_by(RoleExecution.id)
        )
        for execution in exec_result.scalars().all():
            executions_by_step.setdefault(execution.job_step_id, []).append(execution)

    archives = [_archive_metadata(a) for a in await list_archives(session, job_id)]

    return {
        "id": job.id,
        "photo_shoot_name": job.workflow_name,
        "workflow_name": job.workflow_name,
        "workflow": _workflow_json(job.workflow),
        "status": job.status.value,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "warnings": json.loads(job.warnings) if job.warnings else [],
        "generated_assets": job.generated_assets,
        "report_files": json.loads(job.report_files) if job.report_files else [],
        "archives": archives,
        "steps": [
            {
                "block_name": s.block_name,
                "order": s.order,
                "status": s.status.value,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                "input_context": s.input_context,
                "output": s.output,
                "error": s.error,
                "llm_executions": [
                    {
                        "id": ex.id,
                        "configuration_id": ex.configuration_id,
                        "configuration_source": ex.configuration_source,
                        "system_prompt": ex.system_prompt,
                        "model_used": ex.model_used,
                        "temperature": ex.temperature,
                        "max_tokens": ex.max_tokens,
                        "reasoning_effort": ex.reasoning_effort,
                        "status": ex.status,
                        "started_at": ex.started_at.isoformat() if ex.started_at else None,
                        "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
                    }
                    for ex in executions_by_step.get(s.id, [])
                ],
            }
            for s in steps_sorted
        ],
    }


def _archive_metadata(archive: DeliveryArchive) -> dict[str, Any]:
    """Attempt metadata for list/job responses (no Markdown audit contents)."""
    ready = archive.status == DeliveryArchiveStatus.READY
    return {
        "id": archive.id,
        "attempt": archive.attempt,
        "status": archive.status.value,
        "job_step_id": archive.job_step_id,
        "created_at": archive.created_at.isoformat() if archive.created_at else None,
        "started_at": archive.started_at.isoformat() if archive.started_at else None,
        "finished_at": archive.finished_at.isoformat() if archive.finished_at else None,
        "archive_name": archive.archive_name,
        "archive_path": archive.archive_path if ready else None,
        "byte_size": archive.byte_size,
        "sha256": archive.sha256,
        "manifest_sha256": archive.manifest_sha256,
        "error": archive.error,
        "error_type": archive.error_type,
        "download_url": (
            f"/api/workflows/jobs/{archive.job_id}/archives/{archive.id}/download"
            if ready
            else None
        ),
    }


@router.get("/jobs/{job_id}/archives")
async def list_job_archives(job_id: int, session: AsyncSession = Depends(get_session)):
    """Delivery archive attempt history for a job (metadata only)."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return [_archive_metadata(a) for a in await list_archives(session, job_id)]


@router.get("/jobs/{job_id}/archives/{archive_id}")
async def get_job_archive(
    job_id: int, archive_id: int, session: AsyncSession = Depends(get_session)
):
    """One attempt's full audit detail: inventory, checksums, Markdown contents."""
    archive = await get_archive(session, job_id, archive_id)
    if archive is None:
        raise HTTPException(404, "Archive not found")
    detail = _archive_metadata(archive)
    detail.update(
        {
            "source_dir": archive.source_dir,
            "settings": json.loads(archive.settings_json),
            "manifest": json.loads(archive.manifest) if archive.manifest else None,
            "entries": [
                {
                    "path": e.path,
                    "type": e.entry_type,
                    "size": e.size,
                    "sha256": e.sha256,
                    "markdown_contents": e.markdown_contents,
                }
                for e in sorted(archive.entries, key=lambda e: e.path)
            ],
        }
    )
    return detail


@router.get("/jobs/{job_id}/archives/{archive_id}/download")
async def download_job_archive(
    job_id: int, archive_id: int, session: AsyncSession = Depends(get_session)
):
    """Download a ready archive ZIP (server-derived path, delivery-root only)."""
    archive = await get_archive(session, job_id, archive_id)
    if archive is None:
        raise HTTPException(404, "Archive not found")
    try:
        path = archive_download_path(archive)
    except FileNotFoundError:
        raise HTTPException(404, "Archive file is unavailable") from None
    except DeliveryArchiveError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=archive.archive_name or path.name,
    )


@router.post("/jobs/{job_id}/archives/retry", status_code=202)
async def retry_job_archive(job_id: int, session: AsyncSession = Depends(get_session)):
    """Reserve and schedule an archive-only retry (no LLM/generation reruns)."""
    try:
        step_id = await reserve_archive_retry(session, job_id)
    except ArchiveRetryError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    schedule_archive_retry(job_id, step_id)
    return {"job_id": job_id, "status": "retrying"}
