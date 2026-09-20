"""
app.services.generation.placeholder — Fast test backend

Generates a simple labeled placeholder image using Pillow so the full
pipeline can be tested end-to-end without a real model or ComfyUI.

The image is a solid gradient with the prompt text overlaid, saved as PNG.
Falls back to a tiny 1×1 PNG if Pillow is not installed.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from app.config import settings
from app.services.generation.base import GenerationBackend, GenerationRequest, GenerationResult

logger = logging.getLogger(__name__)


def _create_placeholder_image(request: GenerationRequest, output_path: Path) -> None:
    """Create a labeled placeholder PNG. Uses Pillow if available, else a tiny PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (request.width, request.height))
        draw = ImageDraw.Draw(img)

        # Gradient background
        for y in range(request.height):
            r = int(30 + (y / request.height) * 40)
            g = int(20 + (y / request.height) * 30)
            b = int(50 + (y / request.height) * 60)
            draw.line([(0, y), (request.width, y)], fill=(r, g, b))

        # Overlay text
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
        except (OSError, AttributeError):
            font = ImageFont.load_default()

        lines = [
            f"PLACEHOLDER — {request.variant_name}",
            f"{request.width}×{request.height} | steps={request.steps} | cfg={request.cfg_scale}",
            "",
            "Positive prompt:",
            request.positive_prompt[:200] + ("…" if len(request.positive_prompt) > 200 else ""),
            "",
            "Negative prompt:",
            request.negative_prompt[:120] + ("…" if len(request.negative_prompt) > 120 else ""),
        ]

        y_offset = 40
        for line in lines:
            draw.text((30, y_offset), line, fill=(200, 200, 200), font=font)
            y_offset += 20

        img.save(output_path, "PNG")

    except ImportError:
        # Minimal 1×1 PNG fallback (no Pillow)
        import struct
        import zlib

        def _minimal_png(w: int = 1, h: int = 1) -> bytes:
            raw = b"\x00" + b"\x80\x80\x80" * w
            raw_data = raw * h
            compressed = zlib.compress(raw_data)

            def chunk(ctype: bytes, data: bytes) -> bytes:
                c = ctype + data
                return (
                    struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
                )

            return (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", compressed)
                + chunk(b"IEND", b"")
            )

        output_path.write_bytes(_minimal_png())


class PlaceholderBackend(GenerationBackend):
    """Instant placeholder image generator for testing."""

    name = "placeholder"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        start = time.monotonic()

        output_dir = Path(settings.image_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Deterministic filename from prompt content
        prompt_hash = hashlib.sha256(request.positive_prompt.encode()).hexdigest()[:12]
        filename = f"placeholder_{request.variant_name}_{prompt_hash}.png"
        output_path = output_dir / filename

        logger.info("PlaceholderBackend: generating %s", output_path)
        _create_placeholder_image(request, output_path)

        elapsed = time.monotonic() - start
        logger.info("PlaceholderBackend: done in %.2fs", elapsed)

        return GenerationResult(
            image_paths=[output_path],
            seed_used=42,
            backend_name=self.name,
            generation_time_seconds=elapsed,
            metadata={
                "width": request.width,
                "height": request.height,
                "steps": request.steps,
                "cfg_scale": request.cfg_scale,
            },
        )

    async def is_available(self) -> bool:
        return True
