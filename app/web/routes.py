"""
app.web.routes — HTML routes served via Jinja2 + HTMX

Full pages:
- /              — Dashboard: overview of recent jobs, available blocks

HTMX partials (return HTML fragments, not full pages):
- /partials/blocks     — styled block list
- /partials/jobs       — styled recent jobs list
- /partials/run        — execute a workflow and return result fragment

Planned pages:
- /workflows     — Build/edit workflows by arranging blocks
- /jobs          — Job history with drill-down
- /settings      — Manage persistent settings
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.blocks.registry import list_blocks
from app.database import get_session
from app.models.job import Job
from app.pipeline.engine import run_pipeline

templates = Jinja2Templates(directory="app/web/templates")

router = APIRouter()


# ── Full pages ───────────────────────────────────────────────────────────

@router.get("/")
async def dashboard(request: Request):
    """Render the main dashboard page."""
    return templates.TemplateResponse(request, "dashboard.html")


# ── HTMX partials ────────────────────────────────────────────────────────

@router.get("/partials/blocks")
async def partial_blocks(request: Request):
    """Return styled HTML fragment listing all registered blocks."""
    blocks = list_blocks()
    return templates.TemplateResponse(request, "partials/block_list.html", {"blocks": blocks})


@router.get("/partials/jobs")
async def partial_jobs(request: Request, session: AsyncSession = Depends(get_session)):
    """Return styled HTML fragment listing recent jobs."""
    result = await session.execute(
        select(Job)
        .options(selectinload(Job.steps))
        .order_by(Job.created_at.desc())
        .limit(20)
    )
    jobs_raw = result.scalars().all()
    jobs = [
        {
            "id": j.id,
            "workflow_name": j.workflow_name,
            "status": j.status.value,
            "created_at": j.created_at.strftime("%b %d, %H:%M") if j.created_at else None,
            "steps": list(j.steps),
        }
        for j in jobs_raw
    ]
    return templates.TemplateResponse(request, "partials/job_list.html", {"jobs": jobs})


@router.post("/partials/run")
async def partial_run(
    request: Request,
    message: str = Form("Hello, pipeline!"),
    session: AsyncSession = Depends(get_session),
):
    """Execute the echo block and return a styled result fragment."""
    job = await run_pipeline(
        workflow_name="quick_run",
        block_names=["echo"],
        context={"message": message},
        session=session,
    )
    # Re-fetch with steps loaded
    result = await session.execute(
        select(Job).where(Job.id == job.id).options(selectinload(Job.steps))
    )
    job = result.scalar_one()
    steps = [
        {"block_name": s.block_name, "order": s.order, "output": s.output, "error": s.error}
        for s in sorted(job.steps, key=lambda s: s.order)
    ]
    return templates.TemplateResponse(
        request,
        "partials/run_result.html",
        {
            "job_id": job.id,
            "status": job.status.value,
            "error": job.error,
            "steps": steps,
        },
    )
