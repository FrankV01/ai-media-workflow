"""
app.api.workflows — Workflow execution endpoints

POST /api/workflows/run    — trigger a pipeline run
GET  /api/workflows/jobs   — list recent jobs
GET  /api/workflows/jobs/{id} — job detail with steps

Future:
- CRUD for saved workflow definitions (WorkflowDef model)
- WebSocket endpoint for live progress streaming
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models.job import Job, JobStep
from app.pipeline.engine import run_pipeline

router = APIRouter()


class RunRequest(BaseModel):
    """Payload to trigger a workflow run."""
    workflow_name: str = "default"
    block_names: list[str]
    context: dict = {}


@router.post("/run")
async def run_workflow(req: RunRequest, session: AsyncSession = Depends(get_session)):
    """Execute a list of blocks as a pipeline and return the Job."""
    job = await run_pipeline(
        workflow_name=req.workflow_name,
        block_names=req.block_names,
        context=req.context,
        session=session,
    )
    return {
        "job_id": job.id,
        "status": job.status.value,
        "error": job.error,
    }


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
        }
        for j in jobs
    ]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    """Return a job with all its steps."""
    result = await session.execute(
        select(Job).where(Job.id == job_id).options(selectinload(Job.steps))
    )
    job = result.scalar_one_or_none()
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    return {
        "id": job.id,
        "workflow_name": job.workflow_name,
        "status": job.status.value,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "steps": [
            {
                "block_name": s.block_name,
                "order": s.order,
                "status": s.status.value,
                "output": s.output,
                "error": s.error,
            }
            for s in sorted(job.steps, key=lambda s: s.order)
        ],
    }
