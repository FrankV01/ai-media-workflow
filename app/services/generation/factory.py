"""
app.services.generation.factory — Backend factory

Returns a GenerationBackend for an explicit backend name (or the globally
configured settings.generation_backend when omitted). An optional
SdxlWorkflowConfig injects resolved AI-model/workflow settings into the
ComfyUI backend; without it the backend derives code/env defaults so
no-arg calls keep working for startup checks and tests.
"""

from __future__ import annotations

from app.config import settings
from app.services.generation.base import GenerationBackend
from app.services.generation.sdxl_workflow import SdxlWorkflowConfig


def get_backend(
    name: str | None = None,
    workflow_config: SdxlWorkflowConfig | None = None,
) -> GenerationBackend:
    """Instantiate the generation backend for `name` (default: configured one)."""
    backend_name = (name or settings.generation_backend).lower()

    if backend_name == "comfyui":
        from app.services.generation.comfyui import ComfyUIBackend

        return ComfyUIBackend(workflow_config=workflow_config)

    if backend_name == "placeholder":
        from app.services.generation.placeholder import PlaceholderBackend

        return PlaceholderBackend()

    raise ValueError(
        f"Unknown generation backend: '{backend_name}'. "
        "Set GENERATION_BACKEND to 'comfyui' or 'placeholder'."
    )
