"""
app.services.generation.factory — Backend factory

Returns the active GenerationBackend based on settings.generation_backend.
"""

from __future__ import annotations

from app.config import settings
from app.services.generation.base import GenerationBackend


def get_backend() -> GenerationBackend:
    """Instantiate and return the configured generation backend."""
    name = settings.generation_backend.lower()

    if name == "comfyui":
        from app.services.generation.comfyui import ComfyUIBackend

        return ComfyUIBackend()

    if name == "placeholder":
        from app.services.generation.placeholder import PlaceholderBackend

        return PlaceholderBackend()

    raise ValueError(
        f"Unknown generation backend: '{name}'. "
        "Set GENERATION_BACKEND to 'comfyui' or 'placeholder'."
    )
