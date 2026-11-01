"""
app.blocks.vision — shared vision-content helpers for RoleBlocks

Vision-capable roles attach downscaled, base64-encoded images to their
LLM call as OpenAI `image_url` content parts. encode_image_part handles
the Pillow downscale (LANCZOS, longest-edge cap), RGB normalization, and
JPEG/base64 encoding. Failures log a warning and return None so a single
unreadable image never fails the step.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)


def encode_image_part(path: str, max_edge: int) -> dict[str, Any] | None:
    """Downscale and base64-encode an image as an OpenAI image_url part."""
    try:
        with Image.open(path) as img:
            img.thumbnail((max_edge,) * 2, Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
        data_uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": data_uri}}
    except Exception as exc:
        logger.warning("Skipping image %s (%s)", path, exc)
        return None
