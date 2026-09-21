"""
app.api.configurations — AI configuration endpoints

Mounted at /api/configurations.

- GET  /llm                          — list all LLM role profiles
- PUT  /llm/{role_name}              — save a custom LLM profile (model in body)
- POST /llm/{role_name}/reset        — restore code defaults (model in body)
- GET  /media                        — list all media model profiles
- PUT  /media/{block_name}           — save a custom media profile (backend/model in body)
- POST /media/{block_name}/reset     — restore code defaults (backend/model in body)

Model names may contain '/', so model and backend are always request-body or
query fields, never path components. Responses include source/default flags,
timestamps, and `is_selected` against the global env selection. No secrets,
URLs, timeouts, poll intervals, or filesystem paths are exposed.

Validation: role must be a registered RoleBlock; block must be a registered
media block; field rules come from the shared schemas in
app/services/configuration/schemas.py (canonical checks in
app/services/configuration/base.py).
"""

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException

from app.blocks.media_producer import code_media_defaults, selected_media_model
from app.config import settings
from app.services.configuration.base import MediaProfileSettings
from app.services.configuration.catalog import is_media_block, registered_role_blocks
from app.services.configuration.database import DatabaseConfigurationProvider
from app.services.configuration.schemas import (
    LlmProfileInput,
    LlmResetInput,
    MediaProfileInput,
    MediaResetInput,
)

router = APIRouter()


def _provider() -> DatabaseConfigurationProvider:
    return DatabaseConfigurationProvider()


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


def _media_json(config) -> dict[str, Any]:
    return {
        "block_name": config.block_name,
        "backend_name": config.backend_name,
        "model_name": config.model_name,
        "settings": MediaProfileSettings.from_json(config.settings_json).to_json_dict(),
        "uses_code_defaults": config.uses_code_defaults,
        "source": "code_default" if config.uses_code_defaults else "custom",
        "is_selected": (
            config.backend_name == settings.generation_backend.lower()
            and config.model_name == selected_media_model(config.backend_name)
        ),
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


async def _role_for_config(role_name: str):
    """Look up the registered role; also returns its CreativeRole row."""
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    rows = await _provider().list_llm_configurations()
    role = next((role for role, _ in rows if role.name == role_name), None)
    return block, role


# ── LLM endpoints ────────────────────────────────────────────────────────


@router.get("/llm")
async def list_llm_configurations():
    """List all saved LLM role profiles."""
    rows = await _provider().list_llm_configurations()
    return {
        "selected_model": settings.llm_model,
        "profiles": [_llm_json(role, config) for role, config in rows],
    }


@router.put("/llm/{role_name}")
async def put_llm_configuration(role_name: str, body: LlmProfileInput):
    """Save a custom LLM profile for (role_name, body.model_name)."""
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
    )
    _, role = await _role_for_config(role_name)
    return _llm_json(role, config)


@router.post("/llm/{role_name}/reset")
async def reset_llm_configuration(role_name: str, body: LlmResetInput):
    """Reset (role_name, body.model_name) to code defaults."""
    block = registered_role_blocks().get(role_name)
    if block is None:
        raise HTTPException(404, f"Unknown role '{role_name}'")
    defaults = replace(block.code_defaults(), model_name=body.model_name)
    config = await _provider().reset_llm_configuration(defaults)
    _, role = await _role_for_config(role_name)
    return _llm_json(role, config)


# ── Media endpoints ──────────────────────────────────────────────────────


@router.get("/media")
async def list_media_configurations():
    """List all saved media model profiles."""
    configs = await _provider().list_media_configurations()
    return {
        "selected_backend": settings.generation_backend,
        "selected_model": selected_media_model(settings.generation_backend.lower()),
        "profiles": [_media_json(config) for config in configs],
    }


@router.put("/media/{block_name}")
async def put_media_configuration(block_name: str, body: MediaProfileInput):
    """Save a custom media profile for (block_name, backend, model)."""
    if not is_media_block(block_name):
        raise HTTPException(404, f"Unknown media block '{block_name}'")
    config = await _provider().upsert_media_configuration(
        block_name, body.backend_name.lower(), body.model_name, body.to_settings()
    )
    return _media_json(config)


@router.post("/media/{block_name}/reset")
async def reset_media_configuration(block_name: str, body: MediaResetInput):
    """Reset (block_name, backend, model) to code defaults."""
    if not is_media_block(block_name):
        raise HTTPException(404, f"Unknown media block '{block_name}'")
    defaults = code_media_defaults(block_name, body.backend_name, body.model_name)
    config = await _provider().reset_media_configuration(defaults)
    return _media_json(config)
