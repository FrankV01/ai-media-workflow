"""
app.api.workflows — Workflow execution endpoints

POST /api/workflows/run       — queue a pipeline run, returns job_id immediately
GET  /api/workflows/jobs      — list recent jobs
GET  /api/workflows/jobs/{id} — job detail with per-step status, I/O, and timing
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models.job import Job
from app.pipeline.engine import run_pipeline

router = APIRouter()


class RunRequest(BaseModel):
    """Payload to trigger a workflow run."""
    workflow_name: str = "default"
    block_names: list[str | dict[str, Any]]
    context: dict = {}


@router.post("/run", status_code=202)
async def run_workflow(req: RunRequest):
    """Queue a pipeline run in the background. Returns the job_id immediately."""
    job_id = await run_pipeline(
        workflow_name=req.workflow_name,
        block_names=req.block_names,
        context=dict(req.context),
        start_in_background=True,
    )
    return {"job_id": job_id, "status": "running"}


@router.get("/jobs")
async def list_jobs(limit: int = 20, session: AsyncSession = Depends(get_session)):
    """Return recent jobs, newest first."""
    result = await session.execute(
        select(Job).order_by(Job.created_at.desc()).limit(limit)
    )
    jobs = result.scalars().all()
    return [
        {
            "id": j.id,
            "workflow_name": j.workflow_name,
            "status": j.status.value,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "error": j.error,
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
    return {
        "id": job.id,
        "workflow_name": job.workflow_name,
        "status": job.status.value,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "generated_assets": job.generated_assets,
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
            }
            for s in sorted(job.steps, key=lambda s: s.order)
        ],
    }
