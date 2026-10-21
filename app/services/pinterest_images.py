"""app.services.pinterest_images — Pin image preparation

Pinterest recommends a 2:3 aspect ratio (1000x1500). Source renders are
cover-cropped to 2:3, resized to 1000x1500, encoded as JPEG, and returned
base64 for the pin-create request's media_source. No public hosting is
needed — bytes travel inside the API call.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

PIN_WIDTH = 1000
PIN_HEIGHT = 1500  # 2:3 — Pinterest's recommended pin ratio


class PinterestImageError(RuntimeError):
    """The chosen source image could not be prepared for upload."""


def prepare_pin_image(path: str | Path) -> tuple[str, str]:
    """Return (base64_data, content_type) for a 2:3 Pinterest-ready JPEG."""
    source = Path(path)
    if not source.is_file():
        raise PinterestImageError(f"image not found: {source}")
    try:
        with Image.open(source) as img:
            img = img.convert("RGB")
            target_ratio = PIN_WIDTH / PIN_HEIGHT
            ratio = img.width / img.height
            if ratio > target_ratio:  # too wide — crop width
                new_w = int(img.height * target_ratio)
                left = (img.width - new_w) // 2
                img = img.crop((left, 0, left + new_w, img.height))
            elif ratio < target_ratio:  # too tall — crop height
                new_h = int(img.width / target_ratio)
                top = (img.height - new_h) // 2
                img = img.crop((0, top, img.width, top + new_h))
            img = img.resize((PIN_WIDTH, PIN_HEIGHT), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=90)
    except PinterestImageError:
        raise
    except Exception as exc:
        raise PinterestImageError(f"could not prepare {source.name}: {exc}") from exc
    return base64.b64encode(buffer.getvalue()).decode("ascii"), "image/jpeg"
