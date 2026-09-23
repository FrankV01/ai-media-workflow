"""
app.api.workflows — Workflow execution endpoints

POST /api/workflows/run       — queue a pipeline run, returns job_id immediately
GET  /api/workflows/jobs      — list recent jobs
GET  /api/workflows/jobs/{id} — job detail with per-step status, I/O, and timing
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models.creative import RoleExecution
from app.models.job import Job
from app.pipeline.engine import run_pipeline
from app.pipeline.naming import PhotoShootNameError, resolve_photo_shoot_name

router = APIRouter()


class RunRequest(BaseModel):
    """Payload to trigger a workflow run."""

    photo_shoot_name: str | None = None
    workflow_name: str | None = Field(default=None, deprecated=True)
    block_names: list[str | dict[str, Any]]
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

    @property
    def job_name(self) -> str:
        if self.photo_shoot_name is None:
            raise RuntimeError("photo_shoot_name was not validated")
        return self.photo_shoot_name


@router.post("/run", status_code=202)
async def run_workflow(req: RunRequest):
    """Queue a pipeline run in the background. Returns the job_id immediately."""
    job_id = await run_pipeline(
        workflow_name=req.job_name,
        block_names=req.block_names,
        context=dict(req.context),
        start_in_background=True,
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs")
async def list_jobs(limit: int = 20, session: AsyncSession = Depends(get_session)):
    """Return recent jobs, newest first."""
    result = await session.execute(select(Job).order_by(Job.created_at.desc()).limit(limit))
    jobs = result.scalars().all()
    return [
        {
            "id": j.id,
            "photo_shoot_name": j.workflow_name,
            "workflow_name": j.workflow_name,
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
        select(Job).where(Job.id == job_id).options(selectinload(Job.steps))
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

    return {
        "id": job.id,
        "photo_shoot_name": job.workflow_name,
        "workflow_name": job.workflow_name,
        "status": job.status.value,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "warnings": json.loads(job.warnings) if job.warnings else [],
        "generated_assets": job.generated_assets,
        "report_files": json.loads(job.report_files) if job.report_files else [],
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
