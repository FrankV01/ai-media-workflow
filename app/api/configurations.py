"""
app.api.configurations — AI configuration endpoints

Mounted at /api/configurations.

- GET  /llm                          — list LLM role profiles
- PUT  /llm/{role_name}              — save a custom LLM profile (model in body)
- POST /llm/{role_name}/reset        — restore code defaults (model in body)
- GET  /llm/{role_name}/history      — audited save/reset events (model in query)
- GET  /media                        — list media model profiles
- PUT  /media/{block_name}           — save a custom media profile (backend/model in body)
- POST /media/{block_name}/reset     — restore code defaults (backend/model in body)
- GET  /block                        — list generic block profiles (has_db_settings blocks)
- PUT  /block/{block_name}           — save a custom block profile (settings in body)
- POST /block/{block_name}/reset     — restore code defaults (block_name in body)

Every profile is scoped to a workflow: an optional ?workflow=<slug> query
parameter selects the scope on all endpoints; omitted → the default
workflow (backward compatible with pre-workflow callers).

LLM saves/resets are audited: each call records an LlmConfigurationChange
row (action, origin='rest_api', JSON before/after snapshots) in the same
transaction as the profile update.

Model names may contain '/', so model and backend are always request-body or
query fields, never path components. Responses include source/default flags,
timestamps, and `is_selected` against the effective selection (env defaults
or the workflow's media_model_name override). No secrets, URLs, timeouts,
poll intervals, or filesystem paths are exposed.

Validation: role must be a registered RoleBlock; block must be a registered
media block; field rules come from the shared schemas in
app/services/configuration/schemas.py (canonical checks in
app/services/configuration/base.py).
"""

import json
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.blocks.media_producer import code_media_defaults, selected_media_model
from app.config import settings
from app.database import get_session
from app.models.workflow import Workflow
from app.services.configuration.base import (
    LLM_CHANGE_ORIGIN_REST_API,
    MediaProfileSettings,
)
from app.services.configuration.catalog import (
    configurable_blocks,
    is_media_block,
    registered_role_blocks,
)
from app.services.configuration.database import DatabaseConfigurationProvider
from app.services.configuration.schemas import (
    BlockProfileInput,
    BlockResetInput,
    LlmProfileInput,
    LlmResetInput,
    MediaProfileInput,
    MediaResetInput,
)
from app.services.workflows import get_or_create_default_workflow, get_workflow_by_slug

router = APIRouter()


def _provider() -> DatabaseConfigurationProvider:
    return DatabaseConfigurationProvider()


async def _workflow_scope(session: AsyncSession, slug: str | None) -> Workflow:
    """Resolve ?workflow=<slug> to the workflow row (default when omitted).

    Commits so a lazily-seeded default workflow is visible to the provider's
    own sessions before it is referenced as a foreign key.
    """
    if slug is None:
        workflow = await get_or_create_default_workflow(session)
    else:
        workflow = await get_workflow_by_slug(session, slug)
        if workflow is None:
            raise HTTPException(404, f"Unknown workflow '{slug}'")
    await session.commit()
    return workflow


def _effective_media_model(workflow: Workflow, backend_name: str) -> str:
    """The model key a media run resolves: workflow override or env selection."""
    return workflow.media_model_name or selected_media_model(backend_name.lower())


def _llm_json(role, config) -> dict[str, Any]:
    return {
        "role_name": role.name,
        "role_title": role.title,
        "model_name": config.model_name,
        "system_prompt": config.system_prompt,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "enable_thinking": config.enable_thinking,
        "uses_code_defaults": config.uses_code_defaults,
        "source": "code_default" if config.uses_code_defaults else "custom",
        "is_selected": config.model_name == settings.llm_model,
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def _media_json(config, workflow: Workflow) -> dict[str, Any]:
    return {
        "block_name": config.block_name,
        "backend_name": config.backend_name,
        "model_name": config.model_name,
        "settings": MediaProfileSettings.from_json(config.settings_json).to_json_dict(),
        "uses_code_defaults": config.uses_code_defaults,
        "source": "code_default" if config.uses_code_defaults else "custom",
        "is_selected": (
            config.backend_name == settings.generation_backend.lower()
            and config.model_name == _effective_media_model(workflow, config.backend_name)
        ),
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def _block_json(config) -> dict[str, Any]:
    return {
        "block_name": config.block_name,
        "settings": json.loads(config.settings_json),
        "uses_code_defaults": config.uses_code_defaults,
        "source": "code_default" if config.uses_code_defaults else "custom",
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


async def _role_for_config(role_name: str, workflow_id: int | None = None):
    """Look up the registered role; also returns its CreativeRole row."""
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    rows = await _provider().list_llm_configurations(workflow_id)
    role = next((role for role, _ in rows if role.name == role_name), None)
    return block, role


# ── LLM endpoints ────────────────────────────────────────────────────────


@router.get("/llm")
async def list_llm_configurations(
    workflow: str | None = None, session: AsyncSession = Depends(get_session)
):
    """List saved LLM role profiles for one workflow scope (default: the default)."""
    scope = await _workflow_scope(session, workflow)
    rows = await _provider().list_llm_configurations(scope.id)
    return {
        "selected_model": settings.llm_model,
        "workflow": {"slug": scope.slug, "name": scope.name},
        "profiles": [_llm_json(role, config) for role, config in rows],
    }


@router.put("/llm/{role_name}")
async def put_llm_configuration(
    role_name: str,
    body: LlmProfileInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Save a custom LLM profile for (workflow, role_name, body.model_name)."""
    scope = await _workflow_scope(session, workflow)
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    defaults = replace(block.code_defaults(), model_name=body.model_name)
    config = await _provider().upsert_llm_configuration(
        defaults,
        system_prompt=body.system_prompt,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        enable_thinking=body.enable_thinking,
        origin=LLM_CHANGE_ORIGIN_REST_API,
        workflow_id=scope.id,
    )
    _, role = await _role_for_config(role_name, scope.id)
    return _llm_json(role, config)


@router.post("/llm/{role_name}/reset")
async def reset_llm_configuration(
    role_name: str,
    body: LlmResetInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Reset (workflow, role_name, body.model_name) to code defaults."""
    scope = await _workflow_scope(session, workflow)
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    defaults = replace(block.code_defaults(), model_name=body.model_name)
    config = await _provider().reset_llm_configuration(
        defaults, origin=LLM_CHANGE_ORIGIN_REST_API, workflow_id=scope.id
    )
    _, role = await _role_for_config(role_name, scope.id)
    return _llm_json(role, config)


@router.get("/llm/{role_name}/history")
async def llm_configuration_history(
    role_name: str,
    model_name: str,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Audited save/reset events for (workflow, role_name, model_name), newest first.

    `model_name` and `workflow` stay in the query because model names can
    contain '/'. 404 for an unknown role/workflow or when no profile exists
    under the exact model key in that workflow's scope.
    """
    scope = await _workflow_scope(session, workflow)
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    provider = _provider()
    rows = await provider.list_llm_configurations(scope.id)
    config = next(
        (c for r, c in rows if r.name == role_name and c.model_name == model_name),
        None,
    )
    if config is None:
        raise HTTPException(
            404,
            f"No saved profile for role '{role_name}' and model '{model_name}' "
            f"in workflow '{scope.slug}'",
        )
    changes = await provider.list_llm_configuration_changes([config.id])
    return {
        "role_name": role_name,
        "model_name": model_name,
        "workflow": scope.slug,
        "changes": [
            {
                "id": change.id,
                "action": change.action,
                "origin": change.origin,
                "before": json.loads(change.before_snapshot),
                "after": json.loads(change.after_snapshot),
                "created_at": (change.created_at.isoformat() if change.created_at else None),
            }
            for change in changes
        ],
    }


# ── Media endpoints ──────────────────────────────────────────────────────


@router.get("/media")
async def list_media_configurations(
    workflow: str | None = None, session: AsyncSession = Depends(get_session)
):
    """List saved media model profiles for one workflow scope."""
    scope = await _workflow_scope(session, workflow)
    configs = await _provider().list_media_configurations(scope.id)
    backend = settings.generation_backend.lower()
    return {
        "selected_backend": settings.generation_backend,
        "selected_model": _effective_media_model(scope, backend),
        "workflow": {"slug": scope.slug, "name": scope.name},
        "profiles": [_media_json(config, scope) for config in configs],
    }


@router.put("/media/{block_name}")
async def put_media_configuration(
    block_name: str,
    body: MediaProfileInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Save a custom media profile for (workflow, block_name, backend, model)."""
    scope = await _workflow_scope(session, workflow)
    if not is_media_block(block_name):
        raise HTTPException(404, f"Unknown media block '{block_name}'")
    config = await _provider().upsert_media_configuration(
        block_name,
        body.backend_name.lower(),
        body.model_name,
        body.to_settings(),
        workflow_id=scope.id,
    )
    return _media_json(config, scope)


@router.post("/media/{block_name}/reset")
async def reset_media_configuration(
    block_name: str,
    body: MediaResetInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Reset (workflow, block_name, backend, model) to code defaults."""
    scope = await _workflow_scope(session, workflow)
    if not is_media_block(block_name):
        raise HTTPException(404, f"Unknown media block '{block_name}'")
    defaults = code_media_defaults(block_name, body.backend_name, body.model_name)
    config = await _provider().reset_media_configuration(defaults, workflow_id=scope.id)
    return _media_json(config, scope)


# ── Block endpoints ──────────────────────────────────────────────────────


@router.get("/block")
async def list_block_configurations(
    workflow: str | None = None, session: AsyncSession = Depends(get_session)
):
    """List saved generic block profiles for one workflow scope."""
    scope = await _workflow_scope(session, workflow)
    configs = await _provider().list_block_configurations(scope.id)
    return {
        "workflow": {"slug": scope.slug, "name": scope.name},
        "profiles": [_block_json(config) for config in configs],
    }


@router.put("/block/{block_name}")
async def put_block_configuration(
    block_name: str,
    body: BlockProfileInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Save a custom block profile for (workflow, block_name).

    `settings` is normalized+validated by the block class itself; a
    ValueError from `parse_settings` maps to 422.
    """
    scope = await _workflow_scope(session, workflow)
    block_cls = configurable_blocks().get(block_name)
    if block_cls is None:
        raise HTTPException(404, f"Unknown configurable block '{block_name}'")
    try:
        normalized = block_cls.parse_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    config = await _provider().upsert_block_configuration(
        block_name, normalized, workflow_id=scope.id
    )
    return _block_json(config)


@router.post("/block/{block_name}/reset")
async def reset_block_configuration(
    block_name: str,
    body: BlockResetInput,
    workflow: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Reset (workflow, block_name) to the block's code defaults."""
    scope = await _workflow_scope(session, workflow)
    block_cls = configurable_blocks().get(block_name)
    if block_cls is None or block_cls.meta.name != body.block_name.strip():
        raise HTTPException(404, f"Unknown configurable block '{block_name}'")
    config = await _provider().reset_block_configuration(
        block_cls.code_block_defaults(), workflow_id=scope.id
    )
    return _block_json(config)
