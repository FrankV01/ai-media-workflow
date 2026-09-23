"""
app.services.configuration.base — Typed configuration values + provider Protocols

Frozen dataclasses carry code defaults in and resolved profiles out, so
blocks never deal with ORM rows or raw JSON:

- LlmRoleDefaults / ResolvedLlmConfiguration — per-(role, model) LLM profiles
- MediaDefaults / MediaProfileSettings / ResolvedMediaConfiguration —
  per-(block, backend, model) media profiles

`configuration_source` is 'code_default' when the row still ships the values
the code seeded it with, 'custom' once a user has edited it, and 'legacy' on
executions recorded before configuration tracking existed.

The LLM_CHANGE_* constants name the audited mutation actions
(save_custom / reset_to_defaults) and origins (service / web_settings /
rest_api) written to llm_configuration_changes on every explicit save/reset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

LLM_DEFAULT_WARNING = (
    "Using code-default AI configuration for role '{role}' and model '{model}'. "
    "Customize it at /settings/ai."
)
MEDIA_DEFAULT_WARNING = (
    "Using code-default AI configuration for block '{block}' "
    "(backend '{backend}', model '{model}'). Customize it at /settings/ai."
)

SOURCE_CODE_DEFAULT = "code_default"
SOURCE_CUSTOM = "custom"
SOURCE_LEGACY = "legacy"

# LlmConfigurationChange.action values — which explicit mutation was made
LLM_CHANGE_ACTION_SAVE_CUSTOM = "save_custom"
LLM_CHANGE_ACTION_RESET_TO_DEFAULTS = "reset_to_defaults"

# LlmConfigurationChange.origin values — where the mutation was triggered
LLM_CHANGE_ORIGIN_SERVICE = "service"
LLM_CHANGE_ORIGIN_WEB_SETTINGS = "web_settings"
LLM_CHANGE_ORIGIN_REST_API = "rest_api"

# Backends that media profiles may target
SUPPORTED_MEDIA_BACKENDS = ("comfyui", "placeholder")


def validate_llm_profile_fields(
    model_name: str,
    system_prompt: str,
    temperature: float,
    max_tokens: int,
) -> None:
    """Canonical invariant checks for an LLM role profile. Raises ValueError."""
    if not model_name or not model_name.strip():
        raise ValueError("model_name must be non-blank")
    if not system_prompt or not system_prompt.strip():
        raise ValueError("system_prompt must be non-blank")
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be between 0.0 and 2.0")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")


def validate_media_profile_fields(
    block_name: str,
    backend_name: str,
    model_name: str,
    settings: MediaProfileSettings,
) -> None:
    """Canonical invariant checks for a media model profile. Raises ValueError."""
    if not block_name or not block_name.strip():
        raise ValueError("block_name must be non-blank")
    if not backend_name or backend_name.strip().lower() not in SUPPORTED_MEDIA_BACKENDS:
        raise ValueError(f"backend_name must be one of {', '.join(SUPPORTED_MEDIA_BACKENDS)}")
    if not model_name or not model_name.strip():
        raise ValueError("model_name must be non-blank")
    for name in ("sampler", "scheduler"):
        value = getattr(settings, name)
        if not value or not value.strip():
            raise ValueError(f"{name} must be non-blank")
    if backend_name.strip().lower() == "comfyui":
        missing = [
            name
            for name in (
                "refiner_checkpoint",
                "upscale_2x_model",
                "upscale_4x_model",
                "refiner_sampler",
                "refiner_scheduler",
            )
            if not (getattr(settings, name) or "").strip()
        ] + [
            name
            for name in ("refiner_steps", "refiner_cfg_scale", "refiner_denoise")
            if getattr(settings, name) is None
        ]
        if missing:
            raise ValueError(f"ComfyUI profiles require non-blank: {', '.join(missing)}")


# ── LLM role configuration ───────────────────────────────────────────────


@dataclass(frozen=True)
class LlmRoleDefaults:
    """Code defaults + stable role metadata for one (role, model) pair."""

    role_name: str
    role_title: str
    role_description: str
    output_format: str
    suggested_next_role: str | None
    model_name: str
    system_prompt: str
    temperature: float
    max_tokens: int
    enable_thinking: bool


@dataclass(frozen=True)
class ResolvedLlmConfiguration:
    """Immutable effective LLM configuration for one role invocation."""

    configuration_id: int | None
    role_id: int | None
    source: str
    warning: str | None
    system_prompt: str
    model_name: str
    temperature: float
    max_tokens: int
    enable_thinking: bool


# ── Media model configuration ────────────────────────────────────────────


@dataclass(frozen=True)
class MediaProfileSettings:
    """
    Typed media profile values.

    Request defaults apply to every GenerationRequest the block builds
    (Prompt Architect parameters still win per request). The workflow fields
    are backend-specific: required for 'comfyui', optional/None otherwise.
    Seed stays per-request (-1 = random) and is intentionally absent.
    """

    # Request defaults
    width: int
    height: int
    cfg_scale: float
    steps: int
    sampler: str
    scheduler: str
    clip_skip: int

    # Backend workflow/model fields (required for ComfyUI)
    refiner_checkpoint: str | None = None
    upscale_2x_model: str | None = None
    upscale_4x_model: str | None = None
    refiner_steps: int | None = None
    refiner_cfg_scale: float | None = None
    refiner_sampler: str | None = None
    refiner_scheduler: str | None = None
    refiner_denoise: float | None = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive")
        if self.steps <= 0:
            raise ValueError("steps must be positive")
        if self.cfg_scale <= 0:
            raise ValueError("cfg_scale must be positive")
        if self.clip_skip <= 0:
            raise ValueError("clip_skip must be positive")
        if self.refiner_steps is not None and self.refiner_steps <= 0:
            raise ValueError("refiner_steps must be positive")
        if self.refiner_cfg_scale is not None and self.refiner_cfg_scale <= 0:
            raise ValueError("refiner_cfg_scale must be positive")
        if self.refiner_denoise is not None and not 0.0 <= self.refiner_denoise <= 1.0:
            raise ValueError("refiner_denoise must be between 0.0 and 1.0")

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize all fields (including None workflow fields) to a dict."""
        return {
            "width": self.width,
            "height": self.height,
            "cfg_scale": self.cfg_scale,
            "steps": self.steps,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "clip_skip": self.clip_skip,
            "refiner_checkpoint": self.refiner_checkpoint,
            "upscale_2x_model": self.upscale_2x_model,
            "upscale_4x_model": self.upscale_4x_model,
            "refiner_steps": self.refiner_steps,
            "refiner_cfg_scale": self.refiner_cfg_scale,
            "refiner_sampler": self.refiner_sampler,
            "refiner_scheduler": self.refiner_scheduler,
            "refiner_denoise": self.refiner_denoise,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict())

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> MediaProfileSettings:
        """Build typed settings from stored JSON, dropping unknown keys."""
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_json(cls, raw: str) -> MediaProfileSettings:
        return cls.from_json_dict(json.loads(raw))


@dataclass(frozen=True)
class MediaDefaults:
    """Code defaults for one (block, backend, model) triple."""

    block_name: str
    backend_name: str
    model_name: str
    settings: MediaProfileSettings


@dataclass(frozen=True)
class ResolvedMediaConfiguration:
    """Immutable effective media configuration for one generation run."""

    configuration_id: int | None
    source: str
    warning: str | None
    block_name: str
    backend_name: str
    model_name: str
    settings: MediaProfileSettings


# ── Provider protocols ───────────────────────────────────────────────────


class LlmConfigurationProvider(Protocol):
    """Resolves effective LLM role configuration for a run."""

    async def resolve_llm(self, defaults: LlmRoleDefaults) -> ResolvedLlmConfiguration:
        """Get-or-create the (role, model) profile and return resolved values."""
        ...


class MediaConfigurationProvider(Protocol):
    """Resolves effective media model configuration for a run."""

    async def resolve_media(self, defaults: MediaDefaults) -> ResolvedMediaConfiguration:
        """Get-or-create the (block, backend, model) profile and return values."""
        ...


class ConfigurationProvider(LlmConfigurationProvider, MediaConfigurationProvider, Protocol):
    """Combined provider implemented by DatabaseConfigurationProvider."""

    ...
