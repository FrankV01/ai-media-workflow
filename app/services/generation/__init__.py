"""
app.services.generation — Image generation backend abstraction

Provides a pluggable interface for text-to-image generation.
Backends:
- PlaceholderBackend  — returns a test image instantly (for dev/testing)
- ComfyUIBackend      — dispatches to a ComfyUI server, polls for completion

Use get_backend() to get the active backend based on settings.
"""

from app.services.generation.base import GenerationBackend, GenerationRequest, GenerationResult
from app.services.generation.factory import get_backend

__all__ = ["GenerationBackend", "GenerationRequest", "GenerationResult", "get_backend"]
