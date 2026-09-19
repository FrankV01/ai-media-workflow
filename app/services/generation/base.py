"""
app.services.generation.base — Abstract backend + shared data models

GenerationRequest holds the structured prompt data coming from the Prompt Architect.
GenerationResult holds paths to generated images and metadata.
GenerationBackend is the abstract interface every backend implements.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GenerationRequest:
    """Structured input for an image generation backend."""

    positive_prompt: str
    negative_prompt: str = ""
    positive_refiner_prompt: str = ""
    negative_refiner_prompt: str = ""

    # Generation parameters (sensible defaults for SDXL-class models)
    width: int = 1024
    height: int = 1024
    cfg_scale: float = 7.0
    steps: int = 69
    sampler: str = "dpmpp_2m"
    scheduler: str = "karras"
    clip_skip: int = 1
    seed: int = -1  # -1 = random

    # Optional variant label (for batch generation)
    variant_name: str = "main"

    # Extra backend-specific overrides
    extras: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Output from an image generation backend."""

    image_paths: list[Path] = field(default_factory=list)
    seed_used: int = -1
    backend_name: str = ""
    generation_time_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return len(self.image_paths) > 0


class GenerationBackend(abc.ABC):
    """Abstract interface for image generation backends."""

    name: str = "base"

    @abc.abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate image(s) from the given request. Returns result with paths."""
        ...

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Check whether this backend is reachable and ready."""
        ...
