"""
app.services.configuration.schemas — Shared request validation

Pydantic schemas for AI configuration writes, used by both the REST API
(app/api/configurations.py) and the server-rendered settings forms
(app/web/routes.py). Field checks delegate to the canonical validators in
app/services/configuration/base.py so the API, UI, and provider-level
invariants cannot drift apart.

Role/block "known to the registry" checks happen in the callers
(catalog helpers), since the service layer must not import blocks.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.configuration.base import (
    SUPPORTED_MEDIA_BACKENDS,
    MediaProfileSettings,
    validate_llm_profile_fields,
    validate_media_profile_fields,
)


class LlmProfileInput(BaseModel):
    """Editable fields of an LLM role profile."""

    model_name: str
    system_prompt: str
    temperature: float
    max_tokens: int
    enable_thinking: bool = False

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _validate(self) -> "LlmProfileInput":
        validate_llm_profile_fields(
            self.model_name, self.system_prompt, self.temperature, self.max_tokens
        )
        return self


class LlmResetInput(BaseModel):
    """Model selector for an LLM profile reset."""

    model_name: str

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _validate(self) -> "LlmResetInput":
        if not self.model_name:
            raise ValueError("model_name must be non-blank")
        return self


class MediaProfileInput(BaseModel):
    """Editable fields of a media model profile."""

    backend_name: str
    model_name: str

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    cfg_scale: float = Field(gt=0)
    steps: int = Field(gt=0)
    sampler: str
    scheduler: str
    clip_skip: int = Field(gt=0)

    refiner_checkpoint: str | None = None
    upscale_2x_model: str | None = None
    upscale_4x_model: str | None = None
    refiner_steps: int | None = Field(default=None, gt=0)
    refiner_cfg_scale: float | None = Field(default=None, gt=0)
    refiner_sampler: str | None = None
    refiner_scheduler: str | None = None
    refiner_denoise: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("model_name", "sampler", "scheduler")
    @classmethod
    def _strip_str(cls, v: str) -> str:
        return v.strip()

    @field_validator(
        "refiner_checkpoint",
        "upscale_2x_model",
        "upscale_4x_model",
        "refiner_sampler",
        "refiner_scheduler",
    )
    @classmethod
    def _strip_optional(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None

    @field_validator("backend_name")
    @classmethod
    def _norm_backend(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def _validate(self) -> "MediaProfileInput":
        validate_media_profile_fields(
            "media_producer",  # block checked separately by callers
            self.backend_name,
            self.model_name,
            self.to_settings(),
        )
        return self

    def to_settings(self) -> MediaProfileSettings:
        """Build the typed settings object stored in the profile row."""
        return MediaProfileSettings(
            width=self.width,
            height=self.height,
            cfg_scale=self.cfg_scale,
            steps=self.steps,
            sampler=self.sampler,
            scheduler=self.scheduler,
            clip_skip=self.clip_skip,
            refiner_checkpoint=self.refiner_checkpoint,
            upscale_2x_model=self.upscale_2x_model,
            upscale_4x_model=self.upscale_4x_model,
            refiner_steps=self.refiner_steps,
            refiner_cfg_scale=self.refiner_cfg_scale,
            refiner_sampler=self.refiner_sampler,
            refiner_scheduler=self.refiner_scheduler,
            refiner_denoise=self.refiner_denoise,
        )


class MediaResetInput(BaseModel):
    """Backend/model selector for a media profile reset."""

    backend_name: str
    model_name: str

    @field_validator("model_name")
    @classmethod
    def _strip_model(cls, v: str) -> str:
        return v.strip()

    @field_validator("backend_name")
    @classmethod
    def _norm_backend(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def _validate(self) -> "MediaResetInput":
        if not self.model_name:
            raise ValueError("model_name must be non-blank")
        if self.backend_name not in SUPPORTED_MEDIA_BACKENDS:
            raise ValueError(f"backend_name must be one of {', '.join(SUPPORTED_MEDIA_BACKENDS)}")
        return self
