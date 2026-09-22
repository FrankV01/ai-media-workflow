"""Convert PNG files to high-quality, Adobe Stock-oriented JPEG files."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from app.config import settings

MIN_MEGAPIXELS = 4.0
MAX_MEGAPIXELS = 100.0
JPEG_QUALITY = 95
JPEG_DPI = (300, 300)


def _srgb_profile_bytes() -> bytes:
    profile = ImageCms.createProfile("sRGB")
    return ImageCms.ImageCmsProfile(profile).tobytes()


def _convert_to_srgb(image: Image.Image) -> Image.Image:
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


def _resize_for_stock(
    image: Image.Image,
    min_megapixels: float,
    max_megapixels: float,
) -> Image.Image:
    pixels = image.width * image.height
    min_pixels = int(min_megapixels * 1_000_000)
    max_pixels = int(max_megapixels * 1_000_000)

    if min_pixels <= pixels <= max_pixels:
        return image

    target_pixels = min_pixels if pixels < min_pixels else max_pixels
    scale = math.sqrt(target_pixels / pixels)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def convert_pngs_to_jpegs(
    file_paths: Sequence[str | Path],
    output_dir: str | Path | None = None,
    *,
    min_megapixels: float = MIN_MEGAPIXELS,
    max_megapixels: float = MAX_MEGAPIXELS,
    quality: int = JPEG_QUALITY,
    upscale_small_images: bool = True,
) -> list[Path]:
    """Convert PNG paths to optimized sRGB JPEGs and return their output paths."""
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    if min_megapixels <= 0 or max_megapixels < min_megapixels:
        raise ValueError("megapixel limits must be positive and ordered")

    destination = (
        Path(output_dir).expanduser() if output_dir is not None else settings.image_output_dir
    )
    destination.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    srgb_profile = _srgb_profile_bytes()

    for raw_path in file_paths:
        source = Path(raw_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"PNG file not found: {source}")
        if source.suffix.lower() != ".png":
            raise ValueError(f"Expected a PNG file: {source}")

        target = destination / f"{source.stem}.jpg"
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing file: {target}")

        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            image = _convert_to_srgb(image)
            resize_minimum = min_megapixels if upscale_small_images else 0.000001
            image = _resize_for_stock(image, resize_minimum, max_megapixels)
            image.save(
                target,
                format="JPEG",
                quality=quality,
                subsampling=0,
                optimize=True,
                progressive=True,
                icc_profile=srgb_profile,
                dpi=JPEG_DPI,
            )

        outputs.append(target)

    return outputs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert PNG files to high-quality sRGB JPEGs for Adobe Stock.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="PNG files to convert")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for converted JPEG files (default: IMAGE_OUTPUT_DIR)",
    )
    parser.add_argument(
        "--no-upscale",
        action="store_true",
        help="Preserve images below Adobe Stock's 4 MP minimum instead of upscaling them",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    outputs = convert_pngs_to_jpegs(
        args.files,
        args.output_dir,
        upscale_small_images=not args.no_upscale,
    )
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
