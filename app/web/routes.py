"""
app.web.routes — HTML routes served via Jinja2 + HTMX

Full pages:
- /              — Dashboard (blocks, jobs, pipeline visual, quick run)
- /jobs/<id>     — Job detail with step-by-step input/output + runtime stats
- /settings/ai   — AI configuration: LLM role profiles + media model profiles

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
from dataclasses import replace
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.blocks.media_producer import code_media_defaults, selected_media_model
from app.blocks.registry import get_block, list_blocks
from app.config import settings
from app.database import get_session
from app.models.job import Job
from app.pipeline.engine import run_pipeline
from app.services.configuration.base import MediaProfileSettings
from app.services.configuration.catalog import is_media_block, registered_role_blocks
from app.services.configuration.database import DatabaseConfigurationProvider
from app.services.configuration.schemas import (
    LlmProfileInput,
    LlmResetInput,
    MediaProfileInput,
    MediaResetInput,
)

# Default workflow — the standard creative agency pipeline
DEFAULT_WORKFLOW: list[str | dict] = [
    "art_director",
    "prompt_architect",
    "media_producer",
    "art_critic",
    "art_critic_report",
    {"on_good": [], "on_bad": [], "always": ["social_media_specialist", "social_media_report"]},
]

# Human-friendly titles for blocks (used in the visual)
_BLOCK_TITLES = {
    "art_director": "Art Director",
    "prompt_architect": "Prompt Architect",
    "media_producer": "Media Producer",
    "art_critic": "Art Critic",
    "social_media_specialist": "Social Media",
    "art_critic_report": "Critic Report",
    "social_media_report": "Social Report",
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
        "warnings": json.loads(job_raw.warnings) if job_raw.warnings else [],
        "report_files": json.loads(job_raw.report_files) if job_raw.report_files else [],
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


# ── AI settings page ─────────────────────────────────────────────────────


@router.get("/settings/ai")
async def ai_settings_page(request: Request):
    """Render the AI configuration page.

    Lazily seeds profiles for the currently selected LLM model and media
    backend/model so every form is immediately editable; seeded rows keep
    uses_code_defaults=True and still warn at execution until customized.
    """
    provider = DatabaseConfigurationProvider()

    role_blocks = registered_role_blocks()
    for block in role_blocks.values():
        await provider.resolve_llm(block.code_defaults())

    backend_name = settings.generation_backend.lower()
    media_model = selected_media_model(backend_name)
    await provider.resolve_media(code_media_defaults("media_producer", backend_name, media_model))

    llm_rows = await provider.list_llm_configurations()
    media_rows = await provider.list_media_configurations()

    media_profiles = [
        {
            "block_name": config.block_name,
            "backend_name": config.backend_name,
            "model_name": config.model_name,
            "uses_code_defaults": config.uses_code_defaults,
            "settings_dict": MediaProfileSettings.from_json(config.settings_json),
        }
        for config in media_rows
    ]

    profiles_by_role: dict[str, list] = {}
    for role, config in llm_rows:
        profiles_by_role.setdefault(role.name, []).append(config)

    roles = [
        {
            "name": block.role_name,
            "title": block.role_title,
            "description": block.role_description,
            "profiles": profiles_by_role.get(block.role_name, []),
        }
        for block in role_blocks.values()
    ]

    return templates.TemplateResponse(
        request,
        "ai_settings.html",
        {
            "llm_model": settings.llm_model,
            "media_backend": backend_name,
            "media_model": media_model,
            "roles": roles,
            "media_profiles": media_profiles,
            "media_code_defaults": code_media_defaults(
                "media_producer", backend_name, media_model
            ).settings,
            "status": request.query_params.get("status"),
        },
    )


def _unprocessable(exc: ValidationError) -> HTTPException:
    """Convert a schema ValidationError into an HTTP 422."""
    return HTTPException(status_code=422, detail=exc.errors(include_context=False))


@router.post("/settings/ai/llm/save")
async def ai_settings_save_llm(
    role_name: str = Form(...),
    model_name: str = Form(...),
    system_prompt: str = Form(...),
    temperature: str = Form(...),
    max_tokens: str = Form(...),
    enable_thinking: str | None = Form(None),
):
    """Save a custom LLM role profile (marks it uses_code_defaults=False)."""
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    try:
        body = LlmProfileInput(
            model_name=model_name,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking == "on",
        )
    except ValidationError as exc:
        raise _unprocessable(exc) from exc
    defaults = replace(block.code_defaults(), model_name=body.model_name.strip())
    await DatabaseConfigurationProvider().upsert_llm_configuration(
        defaults,
        system_prompt=body.system_prompt,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        enable_thinking=body.enable_thinking,
    )
    return RedirectResponse("/settings/ai?status=saved", status_code=303)


@router.post("/settings/ai/llm/reset")
async def ai_settings_reset_llm(
    role_name: str = Form(...),
    model_name: str = Form(...),
):
    """Reset an LLM role profile back to code defaults."""
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    try:
        body = LlmResetInput(model_name=model_name)
    except ValidationError as exc:
        raise _unprocessable(exc) from exc
    defaults = replace(block.code_defaults(), model_name=body.model_name.strip())
    await DatabaseConfigurationProvider().reset_llm_configuration(defaults)
    return RedirectResponse("/settings/ai?status=reset", status_code=303)


@router.post("/settings/ai/media/save")
async def ai_settings_save_media(
    backend_name: str = Form(...),
    model_name: str = Form(...),
    width: str = Form(...),
    height: str = Form(...),
    cfg_scale: str = Form(...),
    steps: str = Form(...),
    sampler: str = Form(""),
    scheduler: str = Form(""),
    clip_skip: str = Form(...),
    refiner_checkpoint: str = Form(""),
    upscale_2x_model: str = Form(""),
    upscale_4x_model: str = Form(""),
    refiner_steps: str = Form(""),
    refiner_cfg_scale: str = Form(""),
    refiner_sampler: str = Form(""),
    refiner_scheduler: str = Form(""),
    refiner_denoise: str = Form(""),
):
    """Save a custom media model profile (marks it uses_code_defaults=False)."""
    if not is_media_block("media_producer"):
        raise HTTPException(404, "Media block not registered")
    try:
        body = MediaProfileInput(
            backend_name=backend_name,
            model_name=model_name,
            width=width,
            height=height,
            cfg_scale=cfg_scale,
            steps=steps,
            sampler=sampler,
            scheduler=scheduler,
            clip_skip=clip_skip,
            refiner_checkpoint=refiner_checkpoint or None,
            upscale_2x_model=upscale_2x_model or None,
            upscale_4x_model=upscale_4x_model or None,
            refiner_steps=int(refiner_steps) if refiner_steps else None,
            refiner_cfg_scale=float(refiner_cfg_scale) if refiner_cfg_scale else None,
            refiner_sampler=refiner_sampler or None,
            refiner_scheduler=refiner_scheduler or None,
            refiner_denoise=float(refiner_denoise) if refiner_denoise else None,
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise _unprocessable(exc) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await DatabaseConfigurationProvider().upsert_media_configuration(
        "media_producer",
        body.backend_name.strip().lower(),
        body.model_name.strip(),
        body.to_settings(),
    )
    return RedirectResponse("/settings/ai?status=saved", status_code=303)


@router.post("/settings/ai/media/reset")
async def ai_settings_reset_media(
    backend_name: str = Form(...),
    model_name: str = Form(...),
):
    """Reset a media model profile back to code defaults."""
    try:
        body = MediaResetInput(backend_name=backend_name, model_name=model_name)
    except ValidationError as exc:
        raise _unprocessable(exc) from exc
    await DatabaseConfigurationProvider().reset_media_configuration(
        code_media_defaults("media_producer", body.backend_name.strip(), body.model_name.strip())
    )
    return RedirectResponse("/settings/ai?status=reset", status_code=303)


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
