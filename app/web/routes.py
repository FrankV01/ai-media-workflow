"""
app.web.routes — HTML routes served via Jinja2 + HTMX

Full pages:
- /              — Dashboard (blocks, jobs, pipeline visual, quick run)
- /jobs/<id>     — Job detail with step-by-step input/output + runtime stats

HTMX partials (return HTML fragments, not full pages):
- /partials/blocks              — styled block list
- /partials/jobs                — styled recent jobs list
- /partials/jobs/<id>/preview   — hover preview popover for a job
- /partials/workflow            — pipeline visual diagram
- /partials/run-pipeline        — queue the full creative pipeline (background)
- /partials/run-test-pipeline   — same, but with placeholder image backend

Also defines DEFAULT_WORKFLOW — the standard creative agency pipeline
used by the dashboard visual and available for API submissions.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.blocks.registry import get_block, list_blocks
from app.database import get_session
from app.models.job import Job
from app.pipeline.engine import run_pipeline

# Default workflow — the standard creative agency pipeline
DEFAULT_WORKFLOW: list[str | dict] = [
    "art_director",
    "prompt_architect",
    "media_producer",
    "art_critic",
    {"on_good": [], "on_bad": [], "always": ["social_media_specialist"]},
]

# Human-friendly titles for blocks (used in the visual)
_BLOCK_TITLES = {
    "art_director": "Art Director",
    "prompt_architect": "Prompt Architect",
    "media_producer": "Media Producer",
    "art_critic": "Art Critic",
    "social_media_specialist": "Social Media",
    "echo": "Echo",
}

templates = Jinja2Templates(directory="app/web/templates")

router = APIRouter()


def _fmt_duration(start: datetime | None, end: datetime | None) -> str:
    """Format a human-readable duration between two datetimes."""
    if not start or not end:
        return "—"
    delta = end - start
    total_secs = int(delta.total_seconds())
    if total_secs < 1:
        return f"{int(delta.total_seconds() * 1000)}ms"
    if total_secs < 60:
        return f"{total_secs}s"
    mins, secs = divmod(total_secs, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


def _extract_brief(raw_json: str | None) -> str:
    """Pull the main text value out of a JSON-serialized context dict.

    Looks for common keys in priority order; falls back to the first
    string value, then the raw JSON truncated.
    """
    if not raw_json:
        return ""
    try:
        data = json.loads(raw_json)
        for key in ("brief", "message", "input_brief", "output_deliverable"):
            if key in data and data[key]:
                return str(data[key])
        # Fallback: first string value in the dict
        for v in data.values():
            if isinstance(v, str) and v:
                return v
        return json.dumps(data, ensure_ascii=False)[:500]
    except (json.JSONDecodeError, AttributeError):
        return str(raw_json)[:500]


def _preview(text: str, max_len: int = 120) -> str:
    """Truncate text for preview display."""
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


# ── Full pages ───────────────────────────────────────────────────────────


@router.get("/")
async def dashboard(request: Request):
    """Render the main dashboard page."""
    return templates.TemplateResponse(request, "dashboard.html")


@router.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: int, session: AsyncSession = Depends(get_session)):
    """Render the full job detail page with step-by-step input/output."""
    result = await session.execute(
        select(Job).where(Job.id == job_id).options(selectinload(Job.steps))
    )
    job_raw = result.scalar_one_or_none()
    if not job_raw:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    sorted_steps = sorted(job_raw.steps, key=lambda s: s.order)
    job = {
        "id": job_raw.id,
        "workflow_name": job_raw.workflow_name,
        "status": job_raw.status.value,
        "created_at": (
            job_raw.created_at.strftime("%b %d, %Y %H:%M:%S") if job_raw.created_at else "—"
        ),
        "finished_at": (
            job_raw.finished_at.strftime("%b %d, %Y %H:%M:%S") if job_raw.finished_at else None
        ),
        "duration": _fmt_duration(job_raw.created_at, job_raw.finished_at),
        "error": job_raw.error,
        "steps": [
            {
                "block_name": s.block_name,
                "order": s.order,
                "status": s.status.value,
                "duration": _fmt_duration(s.started_at, s.finished_at),
                "input_brief": _extract_brief(s.input_context),
                "output_brief": _extract_brief(s.output),
                "error": s.error,
            }
            for s in sorted_steps
        ],
    }
    return templates.TemplateResponse(request, "job_detail.html", {"job": job})


# ── HTMX partials ────────────────────────────────────────────────────────


def _build_workflow_nodes(workflow: list[str | dict]) -> list[dict]:
    """Convert a workflow step list into visual node dicts for the template."""
    nodes = []
    for item in workflow:
        if isinstance(item, str):
            # Block node
            try:
                block_cls = get_block(item)
                desc = block_cls.meta.description
                category = block_cls.meta.category
            except KeyError:
                desc = ""
                category = "utility"
            nodes.append(
                {
                    "type": "block",
                    "name": item,
                    "title": _BLOCK_TITLES.get(item, item.replace("_", " ").title()),
                    "description": desc,
                    "category": category,
                }
            )
        elif isinstance(item, dict):
            # Routing node
            on_good = [_BLOCK_TITLES.get(b, b) for b in item.get("on_good", [])]
            on_bad = [_BLOCK_TITLES.get(b, b) for b in item.get("on_bad", [])]
            always = [_BLOCK_TITLES.get(b, b) for b in item.get("always", [])]
            nodes.append(
                {
                    "type": "routing",
                    "on_good": on_good,
                    "on_bad": on_bad,
                    "always": always,
                }
            )
    return nodes


@router.get("/partials/workflow")
async def partial_workflow(request: Request):
    """Return the workflow pipeline visualization fragment."""
    nodes = _build_workflow_nodes(DEFAULT_WORKFLOW)
    return templates.TemplateResponse(request, "partials/workflow_visual.html", {"nodes": nodes})


@router.get("/partials/blocks")
async def partial_blocks(request: Request):
    """Return styled HTML fragment listing all registered blocks."""
    blocks = list_blocks()
    return templates.TemplateResponse(request, "partials/block_list.html", {"blocks": blocks})


@router.get("/partials/jobs")
async def partial_jobs(request: Request, session: AsyncSession = Depends(get_session)):
    """Return styled HTML fragment listing recent jobs."""
    result = await session.execute(
        select(Job).options(selectinload(Job.steps)).order_by(Job.created_at.desc()).limit(20)
    )
    jobs_raw = result.scalars().all()
    jobs = [
        {
            "id": j.id,
            "workflow_name": j.workflow_name,
            "status": j.status.value,
            "created_at": j.created_at.strftime("%b %d, %H:%M") if j.created_at else None,
            "duration": _fmt_duration(j.created_at, j.finished_at),
            "steps": list(j.steps),
        }
        for j in jobs_raw
    ]
    return templates.TemplateResponse(request, "partials/job_list.html", {"jobs": jobs})


@router.get("/partials/jobs/{job_id}/preview")
async def partial_job_preview(
    request: Request, job_id: int, session: AsyncSession = Depends(get_session)
):
    """Return a hover-preview HTML fragment for a job."""
    result = await session.execute(
        select(Job).where(Job.id == job_id).options(selectinload(Job.steps))
    )
    job_raw = result.scalar_one_or_none()
    if not job_raw:
        return templates.TemplateResponse(
            request,
            "partials/job_preview.html",
            {"job": None},
        )
    sorted_steps = sorted(job_raw.steps, key=lambda s: s.order)
    job = {
        "id": job_raw.id,
        "workflow_name": job_raw.workflow_name,
        "status": job_raw.status.value,
        "duration": _fmt_duration(job_raw.created_at, job_raw.finished_at),
        "error": job_raw.error,
        "steps": [
            {
                "block_name": s.block_name,
                "status": s.status.value,
                "duration": _fmt_duration(s.started_at, s.finished_at),
                "input_preview": _preview(_extract_brief(s.input_context)),
                "output_preview": _preview(_extract_brief(s.output)),
            }
            for s in sorted_steps
        ],
    }
    return templates.TemplateResponse(request, "partials/job_preview.html", {"job": job})


@router.post("/partials/run-pipeline")
async def partial_run_pipeline(
    request: Request,
    brief: str = Form(""),
):
    """Queue the default creative pipeline and return a queued-confirmation fragment."""
    brief = brief.strip()
    if not brief:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_queued.html",
            {"error": "Brief cannot be empty."},
        )
    job_id = await run_pipeline(
        workflow_name=None,
        block_names=list(DEFAULT_WORKFLOW),
        context={"brief": brief},
        start_in_background=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/pipeline_queued.html",
        {"job_id": job_id, "error": None},
    )


@router.post("/partials/run-test-pipeline")
async def partial_run_test_pipeline(
    request: Request,
    brief: str = Form(""),
):
    """Queue the default pipeline with the placeholder backend for testing."""
    brief = brief.strip()
    if not brief:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_queued.html",
            {"error": "Brief cannot be empty."},
        )
    job_id = await run_pipeline(
        workflow_name=None,
        block_names=list(DEFAULT_WORKFLOW),
        context={"brief": brief, "_generation_backend": "placeholder"},
        start_in_background=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/pipeline_queued.html",
        {"job_id": job_id, "error": None},
    )
