"""
app.services.image_convert — In-memory PNG→JPEG conversion service

Shared by the web drop-zone tool (app/api/convert.py) and the CLI script
(scripts/convert_pngs_to_jpegs.py). Handles EXIF orientation, embedded ICC →
sRGB conversion, white alpha compositing (JPEG has no alpha), and JPEG
encoding — entirely in memory, no temp files.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

JPEG_QUALITY = 85
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # per file
MAX_BATCH_FILES = 20
MAX_IMAGE_PIXELS = 100_000_000  # pre-decode guard, mirrors the script's MP cap

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SAFE_STEM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class ImageConversionError(ValueError):
    """Raised when an upload cannot be converted to JPEG."""


class NotAPngError(ImageConversionError):
    """Raised when the bytes are not a PNG file."""


class ImageTooLargeError(ImageConversionError):
    """Raised when an image exceeds the pixel limit."""


@dataclass
class ConvertedImage:
    """One converted file: JPEG bytes plus the metadata shown in the UI."""

    source_name: str
    output_name: str
    width: int
    height: int
    source_bytes: int
    jpeg_bytes: bytes = field(repr=False)
    flattened: bool = False  # transparency composited onto white

    @property
    def output_bytes(self) -> int:
        return len(self.jpeg_bytes)

    @property
    def saved_pct(self) -> float:
        if not self.source_bytes:
            return 0.0
        return (1 - self.output_bytes / self.source_bytes) * 100


def srgb_profile_bytes() -> bytes:
    profile = ImageCms.createProfile("sRGB")
    return ImageCms.ImageCmsProfile(profile).tobytes()


def convert_to_srgb(image: Image.Image) -> Image.Image:
    embedded_profile = image.info.get("icc_profile")
    if embedded_profile:
        try:
            source_profile = ImageCms.ImageCmsProfile(embedded_profile)
            target_profile = ImageCms.createProfile("sRGB")
            output_mode = "RGBA" if image.mode in {"RGBA", "LA"} else "RGB"
            image = ImageCms.profileToProfile(
                image,
                source_profile,
                target_profile,
                outputMode=output_mode,
            )
        except (OSError, TypeError, ValueError):
            pass

    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")

    return image.convert("RGB")


def _safe_stem(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    stem = Path(base).stem or "image"
    cleaned = "".join(c if c in _SAFE_STEM_CHARS else "_" for c in stem).strip("._")
    return cleaned or "image"


def convert_png_bytes(data: bytes, source_name: str) -> ConvertedImage:
    """Decode PNG bytes and re-encode as an sRGB JPEG, fully in memory."""
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
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise ImageTooLargeError(f"image exceeds the {MAX_IMAGE_PIXELS // 1_000_000} MP limit")
        try:
            image = ImageOps.exif_transpose(opened)
            image.load()
        except Image.DecompressionBombError as exc:
            raise ImageTooLargeError("image exceeds the pixel limit") from exc
        except Exception as exc:
            raise ImageConversionError("corrupt or unsupported PNG") from exc

    flattened = image.mode in {"RGBA", "LA"} or "transparency" in image.info
    image = convert_to_srgb(image)

    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
        progressive=True,
        icc_profile=srgb_profile_bytes(),
    )

    return ConvertedImage(
        source_name=source_name.replace("\\", "/").split("/")[-1] or "image.png",
        output_name=f"{_safe_stem(source_name)}.jpg",
        width=image.width,
        height=image.height,
        source_bytes=len(data),
        jpeg_bytes=buffer.getvalue(),
        flattened=flattened,
    )


def build_zip(images: Sequence[ConvertedImage]) -> bytes:
    """Pack converted JPEGs into an in-memory ZIP, deduping entry names."""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for image in images:
            image.output_name = _unique_name(image.output_name, used)
            archive.writestr(image.output_name, image.jpeg_bytes)
    return buffer.getvalue()


def _unique_name(name: str, used: set[str]) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = name
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate
