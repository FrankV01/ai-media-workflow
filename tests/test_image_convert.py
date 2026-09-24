"""tests.test_image_convert — PNG→JPEG conversion service and endpoint tests.

Covers the shared conversion service (sRGB output, white alpha flattening,
palette/tRNS transparency, signature checks, pixel cap, ZIP dedupe) and the
/api/convert/png-to-jpeg endpoint (single JPEG vs batch ZIP responses,
manifest header, per-file validation statuses).
"""

import io
import json
import zipfile

import pytest
from PIL import Image

from app.services import image_convert
from app.services.image_convert import (
    ConvertedImage,
    ImageConversionError,
    ImageTooLargeError,
    NotAPngError,
    build_zip,
    convert_png_bytes,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_bytes(mode="RGB", size=(64, 48), color=(30, 60, 90), **save_kw) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG", **save_kw)
    return buf.getvalue()


def _converted(name: str = "a.png", **kwargs) -> ConvertedImage:
    return ConvertedImage(
        source_name=name,
        output_name=name.rsplit(".", 1)[0] + ".jpg",
        width=8,
        height=8,
        source_bytes=100,
        jpeg_bytes=b"jpeg-bytes",
        **kwargs,
    )


# ── Service ──────────────────────────────────────────────────────────────


def test_convert_rgb_png_to_srgb_jpeg():
    data = _png_bytes()
    result = convert_png_bytes(data, "shot.png")

    assert result.output_name == "shot.jpg"
    assert result.source_name == "shot.png"
    assert result.source_bytes == len(data)
    assert result.output_bytes == len(result.jpeg_bytes)
    assert (result.width, result.height) == (64, 48)
    assert not result.flattened
    assert result.saved_pct == pytest.approx((1 - result.output_bytes / result.source_bytes) * 100)

    with Image.open(io.BytesIO(result.jpeg_bytes)) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"
        assert img.size == (64, 48)
        assert img.info["progressive"] == 1
        assert img.info["icc_profile"]


def test_rgba_alpha_flattens_onto_white():
    img = Image.new("RGBA", (32, 32), (255, 0, 0, 0))
    img.paste((255, 0, 0, 255), (16, 0, 32, 32))  # opaque red right half
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = convert_png_bytes(buf.getvalue(), "transparent.png")

    assert result.flattened
    with Image.open(io.BytesIO(result.jpeg_bytes)) as converted:
        corner = converted.getpixel((0, 0))
        red_area = converted.getpixel((24, 16))
    assert all(channel >= 250 for channel in corner)
    assert red_area[0] >= 200
    assert red_area[1] <= 60


def test_palette_transparency_flattened():
    data = _png_bytes(mode="P", size=(16, 16), color=0, transparency=0)
    result = convert_png_bytes(data, "pal.png")
    assert result.flattened


def test_non_png_bytes_rejected():
    with pytest.raises(NotAPngError, match="not a PNG"):
        convert_png_bytes(b"definitely not an image", "fake.png")

    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8)).save(jpeg, format="JPEG")
    with pytest.raises(NotAPngError):
        convert_png_bytes(jpeg.getvalue(), "renamed.png")


def test_corrupt_png_rejected():
    with pytest.raises(ImageConversionError, match="corrupt"):
        convert_png_bytes(PNG_SIGNATURE + b"\x00" * 32 + b"garbage", "broken.png")
    # A truncated signature-bearing file is still a conversion error, not NotAPng
    assert not issubclass(ImageConversionError, NotAPngError)


def test_pixel_limit_enforced_before_decode(monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageTooLargeError, match="MP limit"):
        convert_png_bytes(_png_bytes(), "huge.png")


def test_output_name_sanitized():
    result = convert_png_bytes(_png_bytes(), "..\\..\\weird dir\\My File!.PNG")
    assert result.output_name == "My_File.jpg"


def test_build_zip_dedupes_names():
    images = [_converted("a.png"), _converted("a.png"), _converted("b.png")]
    payload = build_zip(images)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        assert names == ["a.jpg", "a-2.jpg", "b.jpg"]
        assert archive.read("a.jpg") == b"jpeg-bytes"
    assert [i.output_name for i in images] == ["a.jpg", "a-2.jpg", "b.jpg"]


# ── Endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _upload(name: str, data: bytes, content_type: str = "image/png"):
    return ("files", (name, data, content_type))


def test_convert_page_renders(client):
    resp = client.get("/convert")
    assert resp.status_code == 200
    assert "Drag and drop a PNG file here" in resp.text


def test_single_png_returns_jpeg(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("photo.png", _png_bytes())],
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert 'filename="photo.jpg"' in resp.headers["content-disposition"]

    manifest = json.loads(resp.headers["x-convert-results"])
    assert manifest["skipped"] == []
    [item] = manifest["converted"]
    assert item["output_name"] == "photo.jpg"
    assert item["width"] == 64 and item["height"] == 48
    assert item["output_bytes"] == len(resp.content)
    assert item["flattened"] is False

    with Image.open(io.BytesIO(resp.content)) as img:
        assert img.format == "JPEG" and img.mode == "RGB"


def test_single_non_png_415(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("notes.txt", b"hello", "text/plain")],
    )
    assert resp.status_code == 415

    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("sneaky.png", b"not a png at all")],
    )
    assert resp.status_code == 415


def test_oversized_file_413(client, monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_UPLOAD_BYTES", 16)
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("big.png", _png_bytes())],
    )
    assert resp.status_code == 413


def test_corrupt_png_422(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("broken.png", PNG_SIGNATURE + b"\x00" * 64)],
    )
    assert resp.status_code == 422


def test_batch_returns_zip(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[
            _upload("one.png", _png_bytes()),
            _upload("two.png", _png_bytes(size=(16, 16))),
        ],
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "converted-images.zip" in resp.headers["content-disposition"]

    manifest = json.loads(resp.headers["x-convert-results"])
    assert len(manifest["converted"]) == 2

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert archive.namelist() == ["one.jpg", "two.jpg"]
        with archive.open("one.jpg") as entry:
            with Image.open(entry) as img:
                assert img.format == "JPEG"


def test_batch_skips_invalid_files(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[
            _upload("ok.png", _png_bytes()),
            _upload("notes.txt", b"hi", "text/plain"),
            _upload("corrupt.png", PNG_SIGNATURE + b"\x00" * 64),
        ],
    )

    # Only one file converted → plain JPEG response, skips in the manifest
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    manifest = json.loads(resp.headers["x-convert-results"])
    assert len(manifest["converted"]) == 1
    reasons = {s["name"]: s["reason"] for s in manifest["skipped"]}
    assert reasons["notes.txt"] == "not a PNG file"
    assert "corrupt" in reasons["corrupt.png"]


def test_batch_all_invalid_422(client):
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[
            _upload("a.txt", b"hi", "text/plain"),
            _upload("b.png", PNG_SIGNATURE + b"\x00" * 64),
        ],
    )
    assert resp.status_code == 422


def test_too_many_files_413(client, monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_BATCH_FILES", 1)
    resp = client.post(
        "/api/convert/png-to-jpeg",
        files=[_upload("a.png", _png_bytes()), _upload("b.png", _png_bytes())],
    )
    assert resp.status_code == 413
