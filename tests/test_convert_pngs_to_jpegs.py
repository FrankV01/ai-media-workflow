"""Tests for Adobe Stock-oriented PNG-to-JPEG conversion."""

from pathlib import Path

import pytest
from PIL import Image

from app.config import settings
from scripts.convert_pngs_to_jpegs import convert_pngs_to_jpegs


@pytest.fixture(autouse=True)
def configured_output_dir(monkeypatch, tmp_path: Path) -> Path:
    output_dir = tmp_path / "configured-output"
    output_dir.mkdir()
    monkeypatch.setattr(settings, "image_output_dir", output_dir)
    return output_dir


def test_converts_rgb_png_with_stock_jpeg_settings(tmp_path: Path, configured_output_dir: Path):
    source = tmp_path / "photo.png"
    Image.new("RGB", (2000, 2000), (20, 40, 60)).save(source)

    outputs = convert_pngs_to_jpegs([source])

    assert outputs == [configured_output_dir / "photo.jpg"]
    with Image.open(outputs[0]) as converted:
        assert converted.format == "JPEG"
        assert converted.mode == "RGB"
        assert converted.size == (2000, 2000)
        assert converted.info["progressive"] == 1
        assert converted.info["icc_profile"]
        assert converted.info["dpi"] == pytest.approx((300, 300), abs=0.1)


def test_explicit_output_dir_overrides_configured_default(
    tmp_path: Path, configured_output_dir: Path
):
    source = tmp_path / "photo.png"
    Image.new("RGB", (2000, 2000), (20, 40, 60)).save(source)
    explicit_dir = tmp_path / "explicit-output"

    outputs = convert_pngs_to_jpegs([source], output_dir=explicit_dir)

    assert outputs == [explicit_dir / "photo.jpg"]
    assert not (configured_output_dir / "photo.jpg").exists()


def test_flattens_transparency_onto_white(tmp_path: Path):
    source = tmp_path / "transparent.png"
    image = Image.new("RGBA", (2000, 2000), (255, 0, 0, 0))
    image.putpixel((1000, 1000), (255, 0, 0, 255))
    image.save(source)

    [output] = convert_pngs_to_jpegs([source], quality=100)

    with Image.open(output) as converted:
        corner = converted.getpixel((0, 0))
        center = converted.getpixel((1000, 1000))
    assert all(channel >= 250 for channel in corner)
    assert center[0] >= 240
    assert center[1] <= 20
    assert center[2] <= 20


def test_upscales_small_png_to_stock_minimum(tmp_path: Path):
    source = tmp_path / "small.png"
    Image.new("RGB", (1000, 1000), "navy").save(source)

    [output] = convert_pngs_to_jpegs([source])

    with Image.open(output) as converted:
        assert converted.width * converted.height >= 4_000_000
        assert converted.size == (2000, 2000)


def test_preserves_small_png_when_upscale_is_disabled(tmp_path: Path):
    source = tmp_path / "small.png"
    Image.new("RGB", (1000, 1000), "navy").save(source)

    [output] = convert_pngs_to_jpegs([source], upscale_small_images=False)

    with Image.open(output) as converted:
        assert converted.size == (1000, 1000)


def test_rejects_non_png_and_existing_output(tmp_path: Path, configured_output_dir: Path):
    not_png = tmp_path / "photo.webp"
    not_png.write_bytes(b"not an image")

    with pytest.raises(ValueError, match="Expected a PNG"):
        convert_pngs_to_jpegs([not_png])

    source = tmp_path / "photo.png"
    Image.new("RGB", (2000, 2000)).save(source)
    (configured_output_dir / "photo.jpg").write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        convert_pngs_to_jpegs([source])
