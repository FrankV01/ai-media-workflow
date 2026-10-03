"""
app.services.workflows — Workflow definition CRUD, validation, and cloning

A Workflow row is the persisted "container" for a pipeline run: it owns the
ordered step list (engine format — block names and on_good/on_bad/always
routing dicts) and scopes every AI configuration profile (LLM role +
media model) used by the run.

- get_or_create_default_workflow() lazily seeds the "Main" workflow
  (DEFAULT_WORKFLOW_STEPS — the pipeline formerly hardcoded in web routes).
  The configuration provider calls it when a caller supplies no
  workflow_id, so ad-hoc runs and fresh test databases still work.
- validate_steps() checks a candidate step list against the block
  registry (callers ensure discover_blocks() has run).
- clone_workflow() copies the definition AND every LLM/media profile row
  into the new workflow's scope, preserving uses_code_defaults — a clone
  of code-default profiles keeps warning until customized.
- set_enabled() refuses to disable the default workflow; set_default()
  transfers the flag atomically and force-enables the new default.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.block import BlockConfiguration
from app.models.creative import LlmRoleConfiguration
from app.models.media import MediaModelConfiguration
from app.models.workflow import Workflow
from app.pipeline.naming import slugify_photo_shoot_name

MAIN_WORKFLOW_NAME = "Main"
MAIN_WORKFLOW_SLUG = "main"
MAIN_WORKFLOW_DESCRIPTION = "Photorealistic AI-generated media pipeline"

# The standard creative agency pipeline — seeded as "Main"; also the
# default step list offered when creating a new workflow.
DEFAULT_WORKFLOW_STEPS: list[str | dict] = [
    "art_director",
    "prompt_architect",
    "media_producer",
    "media_producer_report",
    "art_critic",
    "art_critic_report",
    {
        "on_good": [],
        "on_bad": [],
        "always": ["social_media_specialist", "social_media_report", "llm_report"],
    },
    "delivery_archive",
]

_ROUTING_KEYS = ("on_good", "on_bad", "always")

# Sentinel for update_workflow: distinguishes "leave unchanged" from
# "set to NULL" on nullable fields like media_model_name.
_UNSET: Any = object()


class WorkflowError(Exception):
    """Base error for workflow operations."""


class WorkflowNotFoundError(WorkflowError):
    """The referenced workflow does not exist."""


class WorkflowDisabledError(WorkflowError):
    """The workflow exists but is disabled and cannot run."""


class WorkflowValidationError(WorkflowError):
    """Workflow fields failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class WorkflowConflictError(WorkflowError):
    """The operation conflicts with workflow invariants (e.g. disabling the default)."""


# ── helpers ──────────────────────────────────────────────────────────────


def slugify_workflow_name(name: str) -> str:
    """Derive the stable URL/API slug for a workflow name."""
    return slugify_photo_shoot_name(name)


def parse_steps_json(raw: str) -> list[str | dict]:
    """Decode a stored steps_json payload."""
    return json.loads(raw)


def validate_steps(steps: Any) -> list[str]:
    """Validate an engine step list against the block registry.

    Returns a list of human-readable error strings (empty = valid).
    """
    if not isinstance(steps, list) or not steps:
        return ["steps must be a non-empty list"]

    from app.blocks.registry import discover_blocks, list_block_classes

    discover_blocks()
    known = {cls.meta.name for cls in list_block_classes()}

    errors: list[str] = []

    def check_block_name(name: Any, where: str) -> None:
        if not isinstance(name, str):
            errors.append(f"{where}: block entries must be strings")
        elif name not in known:
            errors.append(f"{where}: unknown block '{name}'")

    for i, item in enumerate(steps):
        where = f"step {i + 1}"
        if isinstance(item, str):
            check_block_name(item, where)
        elif isinstance(item, dict):
            unexpected = set(item) - set(_ROUTING_KEYS)
            if unexpected:
                errors.append(f"{where}: unknown routing keys {sorted(unexpected)}")
            if not set(item) & set(_ROUTING_KEYS):
                errors.append(f"{where}: routing dict needs at least one of {_ROUTING_KEYS}")
            for branch in _ROUTING_KEYS:
                if branch not in item:
                    continue
                branch_steps = item[branch]
                if not isinstance(branch_steps, list):
                    errors.append(f"{where}.{branch}: must be a list of block names")
                    continue
                for bname in branch_steps:
                    check_block_name(bname, f"{where}.{branch}")
        else:
            errors.append(f"{where}: must be a block name or a routing dict")
    return errors


def validate_workflow_fields(
    name: Any,
    steps: Any,
    media_model_name: Any = None,
) -> list[str]:
    """Validate name/steps/media_model_name for create/update. Error list, empty = valid."""
    errors: list[str] = []
    if not isinstance(name, str) or not name.strip():
        errors.append("name must be non-blank")
    elif len(name.strip()) > 255:
        errors.append("name must be 255 characters or fewer")
    errors.extend(validate_steps(steps))
    if media_model_name is not None and (
        not isinstance(media_model_name, str) or not media_model_name.strip()
    ):
        errors.append("media_model_name must be non-blank when provided")
    return errors


# ── queries ──────────────────────────────────────────────────────────────


async def get_or_create_default_workflow(session) -> Workflow:
    """Return the default workflow, lazily seeding 'Main' if none exists."""
    workflow = (
        await session.execute(select(Workflow).where(Workflow.is_default.is_(True)))
    ).scalar_one_or_none()
    if workflow is not None:
        return workflow

    workflow = (
        await session.execute(select(Workflow).where(Workflow.slug == MAIN_WORKFLOW_SLUG))
    ).scalar_one_or_none()
    if workflow is not None:
        workflow.is_default = True
        await session.flush()
        return workflow

    try:
        async with session.begin_nested():
            workflow = Workflow(
                name=MAIN_WORKFLOW_NAME,
                slug=MAIN_WORKFLOW_SLUG,
                description=MAIN_WORKFLOW_DESCRIPTION,
                steps_json=json.dumps(DEFAULT_WORKFLOW_STEPS),
                is_enabled=True,
                is_default=True,
            )
            session.add(workflow)
            await session.flush()
    except IntegrityError:
        # Concurrent creator — re-select under the unique constraints
        workflow = (
            await session.execute(select(Workflow).where(Workflow.slug == MAIN_WORKFLOW_SLUG))
        ).scalar_one()
        workflow.is_default = True
        await session.flush()
    return workflow


async def list_workflows(session) -> list[Workflow]:
    """All workflows, default first then alphabetically."""
    result = await session.execute(
        select(Workflow).order_by(Workflow.is_default.desc(), Workflow.name)
    )
    return list(result.scalars().all())


async def get_workflow(session, workflow_id: int) -> Workflow | None:
    """Fetch a workflow by primary key."""
    return await session.get(Workflow, workflow_id)


async def get_workflow_by_slug(session, slug: str) -> Workflow | None:
    """Fetch a workflow by its stable slug."""
    result = await session.execute(select(Workflow).where(Workflow.slug == slug))
    return result.scalar_one_or_none()


async def require_workflow_by_slug(session, slug: str) -> Workflow:
    """Fetch by slug, raising WorkflowNotFoundError / WorkflowDisabledError."""
    workflow = await get_workflow_by_slug(session, slug)
    if workflow is None:
        raise WorkflowNotFoundError(f"Unknown workflow '{slug}'")
    if not workflow.is_enabled:
        raise WorkflowDisabledError(f"Workflow '{slug}' is disabled")
    return workflow


# ── mutations ────────────────────────────────────────────────────────────


async def _ensure_unique_name(session, name: str, slug: str, exclude_id: int | None = None) -> None:
    """Raise WorkflowValidationError when name or slug is already taken."""
    stmt = select(Workflow).where((Workflow.name == name) | (Workflow.slug == slug))
    if exclude_id is not None:
        stmt = stmt.where(Workflow.id != exclude_id)
    clash = (await session.execute(stmt)).scalar_one_or_none()
    if clash is not None:
        field = "name" if clash.name == name else "slug"
        raise WorkflowValidationError([f"{field} '{name if field == 'name' else slug}' is taken"])


async def create_workflow(
    session,
    *,
    name: str,
    description: str = "",
    steps: list[str | dict] | None = None,
    media_model_name: str | None = None,
) -> Workflow:
    """Create a new enabled, non-default workflow.

    `steps` defaults to DEFAULT_WORKFLOW_STEPS (a useful starting point —
    most workflows begin as a variation on the main pipeline).
    """
    name = name.strip() if isinstance(name, str) else name
    steps = DEFAULT_WORKFLOW_STEPS if steps is None else steps
    media_model_name = media_model_name.strip() if isinstance(media_model_name, str) else None
    errors = validate_workflow_fields(name, steps, media_model_name)
    if errors:
        raise WorkflowValidationError(errors)
    slug = slugify_workflow_name(name)
    await _ensure_unique_name(session, name, slug)

    workflow = Workflow(
        name=name,
        slug=slug,
        description=(description or "").strip(),
        steps_json=json.dumps(steps),
        media_model_name=media_model_name,
        is_enabled=True,
        is_default=False,
    )
    session.add(workflow)
    await session.flush()
    return workflow


async def update_workflow(
    session,
    workflow_id: int,
    *,
    name: Any = _UNSET,
    description: Any = _UNSET,
    steps: Any = _UNSET,
    media_model_name: Any = _UNSET,
) -> Workflow:
    """Update mutable workflow fields. Slug and flags are managed separately."""
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFoundError(f"Unknown workflow id {workflow_id}")

    errors: list[str] = []
    if name is not _UNSET:
        name = name.strip() if isinstance(name, str) else name
        if not isinstance(name, str) or not name or len(name) > 255:
            errors.append("name must be non-blank and 255 characters or fewer")
        else:
            await _ensure_unique_name(session, name, workflow.slug, exclude_id=workflow.id)
            workflow.name = name
    if description is not _UNSET:
        workflow.description = (description or "").strip()
    if steps is not _UNSET:
        step_errors = validate_steps(steps)
        if step_errors:
            errors.extend(step_errors)
        else:
            workflow.steps_json = json.dumps(steps)
    if media_model_name is not _UNSET:
        if media_model_name is None:
            workflow.media_model_name = None
        elif isinstance(media_model_name, str) and media_model_name.strip():
            workflow.media_model_name = media_model_name.strip()
        else:
            errors.append("media_model_name must be non-blank when provided")
    if errors:
        raise WorkflowValidationError(errors)
    await session.flush()
    return workflow


async def clone_workflow(session, source_id: int, new_name: str) -> Workflow:
    """Copy a workflow's definition and all of its AI configuration profiles.

    The clone starts enabled and non-default with identical steps,
    description, and media model. Every LlmRoleConfiguration,
    MediaModelConfiguration, and BlockConfiguration row scoped to the
    source is duplicated under the new workflow id, preserving values and
    uses_code_defaults.
    """
    source = await session.get(Workflow, source_id)
    if source is None:
        raise WorkflowNotFoundError(f"Unknown workflow id {source_id}")

    clone = await create_workflow(
        session,
        name=new_name,
        description=source.description,
        steps=parse_steps_json(source.steps_json),
        media_model_name=source.media_model_name,
    )

    llm_rows = (
        await session.execute(
            select(LlmRoleConfiguration).where(LlmRoleConfiguration.workflow_id == source.id)
        )
    ).scalars()
    for row in llm_rows:
        session.add(
            LlmRoleConfiguration(
                role_id=row.role_id,
                workflow_id=clone.id,
                model_name=row.model_name,
                system_prompt=row.system_prompt,
                temperature=row.temperature,
                max_tokens=row.max_tokens,
                enable_thinking=row.enable_thinking,
                uses_code_defaults=row.uses_code_defaults,
            )
        )

    media_rows = (
        await session.execute(
            select(MediaModelConfiguration).where(MediaModelConfiguration.workflow_id == source.id)
        )
    ).scalars()
    for row in media_rows:
        session.add(
            MediaModelConfiguration(
                workflow_id=clone.id,
                block_name=row.block_name,
                backend_name=row.backend_name,
                model_name=row.model_name,
                settings_json=row.settings_json,
                uses_code_defaults=row.uses_code_defaults,
            )
        )

    block_rows = (
        await session.execute(
            select(BlockConfiguration).where(BlockConfiguration.workflow_id == source.id)
        )
    ).scalars()
    for row in block_rows:
        session.add(
            BlockConfiguration(
                workflow_id=clone.id,
                block_name=row.block_name,
                settings_json=row.settings_json,
                uses_code_defaults=row.uses_code_defaults,
            )
        )
    await session.flush()
    return clone


async def set_enabled(session, workflow_id: int, enabled: bool) -> Workflow:
    """Enable/disable a workflow. The default workflow cannot be disabled."""
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFoundError(f"Unknown workflow id {workflow_id}")
    if not enabled and workflow.is_default:
        raise WorkflowConflictError(
            "The default workflow cannot be disabled — set another workflow as default first"
        )
    workflow.is_enabled = enabled
    await session.flush()
    return workflow


async def set_default(session, workflow_id: int) -> Workflow:
    """Make this workflow THE default: clears all others and force-enables it."""
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFoundError(f"Unknown workflow id {workflow_id}")
    await session.execute(update(Workflow).values(is_default=False))
    workflow.is_default = True
    workflow.is_enabled = True
    await session.flush()
    return workflow
