"""
app.api.workflow_definitions — Workflow definition CRUD endpoints

Mounted at /api/workflow-definitions.

- GET  /                  — list all workflow definitions
- POST /                  — create {name, description?, steps?, media_model_name?}
- GET  /{slug}            — workflow detail (steps decoded)
- PUT  /{slug}            — update name/description/steps/media_model_name
- POST /{slug}/clone      — clone as {name}: copies steps + ALL scoped AI
                            configuration profiles (uses_code_defaults preserved)
- POST /{slug}/enable     — re-enable a disabled workflow
- POST /{slug}/disable    — disable (409 for the default workflow)
- POST /{slug}/set-default — make it the default (force-enables it)

`steps` uses the engine's format: a list of block names plus optional
verdict-routing dicts ({"on_good": [...], "on_bad": [...], "always": [...]}).
A workflow's configuration profiles are managed under /api/configurations
with ?workflow=<slug>.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.workflow import Workflow
from app.services.workflows import (
    WorkflowConflictError,
    WorkflowDisabledError,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowValidationError,
    clone_workflow,
    create_workflow,
    get_workflow_by_slug,
    list_workflows,
    parse_steps_json,
    set_default,
    set_enabled,
    update_workflow,
)

router = APIRouter()


class WorkflowCreateInput(BaseModel):
    """Fields accepted when defining a new workflow."""

    name: str
    description: str = ""
    steps: list[Any] | None = None  # None → DEFAULT_WORKFLOW_STEPS
    media_model_name: str | None = None


class WorkflowUpdateInput(BaseModel):
    """Partial update — only provided fields change; slug stays immutable."""

    name: str | None = None
    description: str | None = None
    steps: list[Any] | None = None
    media_model_name: str | None = None


class WorkflowCloneInput(BaseModel):
    """Name for the cloned workflow."""

    name: str


def _workflow_json(workflow: Workflow) -> dict[str, Any]:
    return {
        "id": workflow.id,
        "name": workflow.name,
        "slug": workflow.slug,
        "description": workflow.description,
        "steps": parse_steps_json(workflow.steps_json),
        "media_model_name": workflow.media_model_name,
        "is_enabled": workflow.is_enabled,
        "is_default": workflow.is_default,
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
        "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
    }


def _raise_http(exc: WorkflowError) -> None:
    """Map workflow service errors to HTTP status codes."""
    if isinstance(exc, WorkflowNotFoundError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, WorkflowValidationError):
        raise HTTPException(422, detail=exc.errors) from exc
    if isinstance(exc, (WorkflowConflictError, WorkflowDisabledError)):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


async def _get_or_404(session: AsyncSession, slug: str) -> Workflow:
    workflow = await get_workflow_by_slug(session, slug)
    if workflow is None:
        raise HTTPException(404, f"Unknown workflow '{slug}'")
    return workflow


@router.get("/")
async def list_definitions(session: AsyncSession = Depends(get_session)):
    """List all workflow definitions (default first, then alphabetical)."""
    workflows = await list_workflows(session)
    return {"workflows": [_workflow_json(w) for w in workflows]}


@router.post("/", status_code=201)
async def create_definition(
    body: WorkflowCreateInput, session: AsyncSession = Depends(get_session)
):
    """Create a new enabled workflow. `steps` defaults to the main pipeline."""
    try:
        workflow = await create_workflow(
            session,
            name=body.name,
            description=body.description,
            steps=body.steps,
            media_model_name=body.media_model_name,
        )
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(workflow)


@router.get("/{slug}")
async def get_definition(slug: str, session: AsyncSession = Depends(get_session)):
    """Return a single workflow definition with decoded steps."""
    return _workflow_json(await _get_or_404(session, slug))


@router.put("/{slug}")
async def update_definition(
    slug: str, body: WorkflowUpdateInput, session: AsyncSession = Depends(get_session)
):
    """Partially update a workflow; only provided fields change."""
    workflow = await _get_or_404(session, slug)
    try:
        workflow = await update_workflow(
            session, workflow.id, **body.model_dump(exclude_unset=True)
        )
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(workflow)


@router.post("/{slug}/clone", status_code=201)
async def clone_definition(
    slug: str, body: WorkflowCloneInput, session: AsyncSession = Depends(get_session)
):
    """Clone a workflow: steps, description, media model, and every profile."""
    workflow = await _get_or_404(session, slug)
    try:
        clone = await clone_workflow(session, workflow.id, body.name)
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(clone)


@router.post("/{slug}/enable")
async def enable_definition(slug: str, session: AsyncSession = Depends(get_session)):
    """Re-enable a disabled workflow."""
    workflow = await _get_or_404(session, slug)
    try:
        workflow = await set_enabled(session, workflow.id, True)
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(workflow)


@router.post("/{slug}/disable")
async def disable_definition(slug: str, session: AsyncSession = Depends(get_session)):
    """Disable a workflow (409 while it is the default)."""
    workflow = await _get_or_404(session, slug)
    try:
        workflow = await set_enabled(session, workflow.id, False)
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(workflow)


@router.post("/{slug}/set-default")
async def set_default_definition(slug: str, session: AsyncSession = Depends(get_session)):
    """Make this workflow the default (clears the flag elsewhere, enables it)."""
    workflow = await _get_or_404(session, slug)
    try:
        workflow = await set_default(session, workflow.id)
    except WorkflowError as exc:
        _raise_http(exc)
    await session.commit()
    return _workflow_json(workflow)
