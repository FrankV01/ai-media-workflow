"""
app.services.background_removal — In-memory solid-background removal service

Pure-logic chroma keying for art assets: assumes a uniform background color
(auto-detected from the image corners or supplied explicitly) and converts it
to transparency. Shared by the /remove-background drop-zone tool
(app/api/background_removal.py) and the background_remover pipeline block.

Pipeline: PNG decode → key-color selection → Euclidean distance match mask →
optional contiguous fill (only background connected to the image edges) →
alpha post-processing (erode, feather, despill) → RGBA PNG out.

All processing is in memory via NumPy + Pillow — no temp files, no ML model.
"""

from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, get_type_hints

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.services import image_convert
from app.services.image_convert import (
    _PNG_SIGNATURE,
    ImageConversionError,
    ImageTooLargeError,
    NotAPngError,
    _safe_stem,
)

# Tolerance 0–100 maps onto this fraction of the maximum RGB Euclidean distance
_MAX_RGB_DISTANCE = math.sqrt(3 * 255 * 255)

# Corner patch size (px) sampled for auto key-color detection
_CORNER_PATCH = 8

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class BackgroundRemovalSettings:
    """Typed settings for solid-background removal.

    `key_color` is a hex color like "#ffffff"; None means auto-detect from
    the four image corners (the background is assumed to reach them).
    """

    key_color: str | None = None
    tolerance: int = 15  # 0–100, percent of max RGB distance
    contiguous: bool = True  # True = edge-connected background only
    erode: int = 0  # px to shrink the kept matte inward
    feather: float = 1.0  # px gaussian blur on the alpha edge
    despill: bool = False  # recover foreground color on edge pixels

    def __post_init__(self) -> None:
        if self.key_color is not None and not _HEX_COLOR_RE.match(self.key_color.strip()):
            raise ValueError("key_color must be a hex color like '#ffffff'")
        if not 0 <= self.tolerance <= 100:
            raise ValueError("tolerance must be between 0 and 100")
        if not 0 <= self.erode <= 50:
            raise ValueError("erode must be between 0 and 50")
        if not 0.0 <= self.feather <= 25.0:
            raise ValueError("feather must be between 0.0 and 25.0")

    def normalized_key_color(self) -> str | None:
        """Canonical '#rrggbb' form of key_color, or None for auto-detect."""
        if self.key_color is None:
            return None
        value = self.key_color.strip()
        return value if value.startswith("#") else f"#{value}"

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize all fields to a plain dict (stored in the profile row)."""
        return {
            "key_color": self.key_color,
            "tolerance": self.tolerance,
            "contiguous": self.contiguous,
            "erode": self.erode,
            "feather": self.feather,
            "despill": self.despill,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict())

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> BackgroundRemovalSettings:
        """Build typed settings from stored JSON or HTML-form values.

        Unknown keys are dropped; values are coerced to each field's
        declared type so form posts ("32", "on", "") work the same as JSON.
        """
        if not isinstance(data, dict):
            raise ValueError("settings must be an object")
        hints = get_type_hints(cls)
        known = set(cls.__dataclass_fields__)  # noqa: SLF001
        kwargs = {
            key: _coerce_field(key, value, hints.get(key))
            for key, value in data.items()
            if key in known
        }
        return cls(**kwargs)

    @classmethod
    def from_json(cls, raw: str) -> BackgroundRemovalSettings:
        return cls.from_json_dict(json.loads(raw))


@dataclass
class RemovedImage:
    """One processed file: RGBA PNG bytes plus the metadata shown in the UI."""

    source_name: str
    output_name: str
    width: int
    height: int
    source_bytes: int
    key_color: str  # the key color actually used ('#rrggbb')
    removed_pct: float  # share of pixels made fully transparent
    png_bytes: bytes = field(repr=False)

    @property
    def data(self) -> bytes:
        """Payload bytes — the generic build_zip accessor."""
        return self.png_bytes

    @property
    def output_bytes(self) -> int:
        return len(self.png_bytes)


_TRUE_VALUES = {"true", "on", "1", "yes"}
_FALSE_VALUES = {"false", "off", "0", "no", ""}


def _is_optional(hint: Any) -> bool:
    return type(None) in get_args(hint)


def _coerce_field(name: str, value: Any, hint: Any) -> Any:
    """Coerce one stored/form value to its declared field type."""
    if value is None:
        return None
    base = hint
    if get_origin(hint) is not None:
        non_none = [a for a in get_args(hint) if a is not type(None)]
        base = non_none[0] if non_none else str
    try:
        if base is bool:
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in _TRUE_VALUES:
                    return True
                if lowered in _FALSE_VALUES:
                    return False
                raise ValueError(f"{name} must be a boolean")
            return bool(value)
        if base is int:
            return int(value)
        if base is float:
            return float(value)
        if base is str:
            text = str(value)
            return None if _is_optional(hint) and not text.strip() else text
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    """Parse '#rrggbb'/'rrggbb' into an (r, g, b) tuple."""
    v = value.strip().lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _decode_png(data: bytes) -> Image.Image:
    """Decode PNG bytes into a loaded image, mirroring image_convert's guards."""
    if not data.startswith(_PNG_SIGNATURE):
        raise NotAPngError("not a PNG file")
    try:
        opened = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError("image exceeds the pixel limit") from exc
    except Exception as exc:
        raise ImageConversionError("corrupt or unsupported PNG") from exc

    with opened:
        if opened.format != "PNG":
            raise NotAPngError("not a PNG file")
        if opened.width * opened.height > image_convert.MAX_IMAGE_PIXELS:
            raise ImageTooLargeError(
                f"image exceeds the {image_convert.MAX_IMAGE_PIXELS // 1_000_000} MP limit"
            )
        try:
            image = ImageOps.exif_transpose(opened)
            image.load()
        except Image.DecompressionBombError as exc:
            raise ImageTooLargeError("image exceeds the pixel limit") from exc
        except Exception as exc:
            raise ImageConversionError("corrupt or unsupported PNG") from exc
    return image


def _auto_key_color(rgb: np.ndarray) -> np.ndarray:
    """Median of the four corner patches — the assumed background color."""
    h, w = rgb.shape[:2]
    p = max(1, min(_CORNER_PATCH, h // 4 or 1, w // 4 or 1))
    patches = np.concatenate(
        [
            rgb[:p, :p].reshape(-1, 3),
            rgb[:p, w - p :].reshape(-1, 3),
            rgb[h - p :, :p].reshape(-1, 3),
            rgb[h - p :, w - p :].reshape(-1, 3),
        ]
    )
    return np.median(patches, axis=0)


def _contiguous_background(match: np.ndarray) -> np.ndarray:
    """Keep only the match-mask regions connected to the image border.

    Flood-fills from every border pixel inside the mask (PIL does the
    connected-component work in C); components fully enclosed by the
    subject are kept opaque.
    """
    h, w = match.shape
    # frombytes (not fromarray) — floodfill needs a writable pixel buffer
    mask_img = Image.frombytes("L", (w, h), np.where(match, 255, 0).astype(np.uint8).tobytes())

    def fill_from(x: int, y: int) -> None:
        if mask_img.getpixel((x, y)) == 255:
            ImageDraw.floodfill(mask_img, (x, y), 128, thresh=0)

    for x in range(w):
        fill_from(x, 0)
        fill_from(x, h - 1)
    for y in range(1, h - 1):
        fill_from(0, y)
        fill_from(w - 1, y)
    return np.asarray(mask_img) == 128


def _despill(
    rgb: np.ndarray,
    alpha: np.ndarray,
    background: np.ndarray,
    dist: np.ndarray,
    threshold: float,
    key: np.ndarray,
) -> np.ndarray:
    """Recover foreground color under the solid-background composite model.

    Observed edge color ≈ a·fg + (1-a)·k, so fg ≈ (p − (1−a)·k)/a. Applied to
    partial-alpha pixels (a from the matte) and to opaque pixels in a 2 px
    band around the background whose color is near-key (a estimated from the
    distance to the key color, tapering to no correction at 1.6× tolerance).
    """
    a = alpha.astype(np.float32) / 255.0
    partial = (a > 0.0) & (a < 1.0)

    bg_img = Image.fromarray(np.where(background, 255, 0).astype(np.uint8), mode="L")
    dilated = np.asarray(bg_img.filter(ImageFilter.MaxFilter(5)))
    band = (dilated > 0) & (a == 1.0) & (dist <= threshold * 1.6)
    band_a = 1.0 - np.clip(1.6 - dist / max(threshold, 1e-6), 0.0, 1.0)

    eff_a = np.where(partial, a, band_a)
    safe_a = np.clip(eff_a, 0.05, 1.0)
    unmixed = (rgb.astype(np.float32) - (1.0 - safe_a)[..., None] * key) / safe_a[..., None]

    out = rgb.astype(np.float32)
    out[partial | band] = np.clip(unmixed, 0, 255)[partial | band]
    return np.clip(out, 0, 255).astype(np.uint8)


def remove_background_png(
    data: bytes,
    source_name: str,
    settings: BackgroundRemovalSettings | None = None,
) -> RemovedImage:
    """Cut a solid-color background out of PNG bytes; returns RGBA PNG bytes."""
    settings = settings or BackgroundRemovalSettings()
    image = _decode_png(data)

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    rgb = rgba[..., :3]
    source_alpha = rgba[..., 3]

    key = (
        np.array(_parse_hex_color(settings.key_color), dtype=np.float32)
        if settings.key_color is not None
        else _auto_key_color(rgb).astype(np.float32)
    )

    diff = rgb.astype(np.float32) - key
    dist = np.sqrt((diff * diff).sum(axis=2))
    threshold = (settings.tolerance / 100.0) * _MAX_RGB_DISTANCE
    match = dist <= threshold

    background = _contiguous_background(match) if settings.contiguous else match

    alpha = np.where(background, 0, 255).astype(np.uint8)
    if settings.erode > 0:
        alpha = np.asarray(
            Image.fromarray(alpha, mode="L").filter(ImageFilter.MinFilter(2 * settings.erode + 1))
        )
    if settings.feather > 0:
        alpha = np.asarray(
            Image.fromarray(alpha, mode="L").filter(ImageFilter.GaussianBlur(settings.feather))
        )

    out_rgb = _despill(rgb, alpha, background, dist, threshold, key) if settings.despill else rgb

    # Preserve pre-existing transparency from the source PNG
    alpha = np.minimum(alpha, source_alpha).astype(np.uint8)

    removed_pct = round(float((alpha == 0).mean()) * 100, 1)
    r, g, b = (int(round(float(c))) for c in key)
    key_hex = f"#{r:02x}{g:02x}{b:02x}"

    output = Image.fromarray(np.dstack([out_rgb, alpha]), mode="RGBA")
    buffer = io.BytesIO()
    output.save(buffer, format="PNG", optimize=True)

    stem = _safe_stem(source_name)
    return RemovedImage(
        source_name=source_name.replace("\\", "/").split("/")[-1] or "image.png",
        output_name=f"{stem}_cutout.png",
        width=output.width,
        height=output.height,
        source_bytes=len(data),
        key_color=key_hex,
        removed_pct=removed_pct,
        png_bytes=buffer.getvalue(),
    )
