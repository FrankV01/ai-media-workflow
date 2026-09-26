"""tests.test_background_removal — Solid-background removal tests.

Covers the chroma-key service (auto/override key color, tolerance,
contiguous vs global fill, erode/feather/despill, RGBA output), the
/api/remove-background endpoint (single PNG vs batch ZIP, manifest header,
per-file statuses, form settings), the background_remover block
(registration, DB-backed settings resolution, cutout outputs), and the
generic BlockConfiguration provider paths (seed/upsert/reset).
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.services import image_convert
from app.services.background_removal import (
    BackgroundRemovalSettings,
    RemovedImage,
    remove_background_png,
)
from app.services.image_convert import (
    ImageConversionError,
    ImageTooLargeError,
    NotAPngError,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GREEN = (0, 200, 0)
RED = (220, 20, 20)


def _png_bytes(mode="RGB", size=(64, 48), color=GREEN, **save_kw) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG", **save_kw)
    return buf.getvalue()


def _green_screen_png(size=(100, 80), hole=False) -> bytes:
    """Green backdrop with a red circle subject; `hole` adds an enclosed
    same-green disc inside the subject."""
    img = Image.new("RGB", size, GREEN)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse((w // 4, h // 4, 3 * w // 4, 3 * h // 4), fill=RED)
    if hole:
        draw.ellipse((w // 2 - 5, h // 2 - 5, w // 2 + 5, h // 2 + 5), fill=GREEN)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _rgba(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


# ── Settings ─────────────────────────────────────────────────────────────


def test_settings_defaults_match_requested_state():
    s = BackgroundRemovalSettings()
    assert s.key_color is None  # auto corner detection
    assert s.tolerance == 15
    assert s.contiguous is True
    assert s.erode == 0
    assert s.feather == 1.0
    assert s.despill is False


def test_settings_validation():
    with pytest.raises(ValueError, match="key_color"):
        BackgroundRemovalSettings(key_color="not-a-hex")
    with pytest.raises(ValueError, match="tolerance"):
        BackgroundRemovalSettings(tolerance=101)
    with pytest.raises(ValueError, match="erode"):
        BackgroundRemovalSettings(erode=-1)
    with pytest.raises(ValueError, match="feather"):
        BackgroundRemovalSettings(feather=99.0)


def test_settings_json_round_trip_and_coercion():
    s = BackgroundRemovalSettings(
        key_color="#00ff00", tolerance=45, contiguous=False, erode=2, feather=1.5, despill=True
    )
    clone = BackgroundRemovalSettings.from_json(s.to_json())
    assert clone == s

    # HTML-form style string values coerce to the declared types
    parsed = BackgroundRemovalSettings.from_json_dict(
        {
            "key_color": "ff00ff",
            "tolerance": "55",
            "contiguous": "false",
            "erode": "3",
            "feather": "2.5",
            "despill": "on",
            "unknown_extra": "dropped",
        }
    )
    assert parsed.key_color == "ff00ff"
    assert parsed.tolerance == 55
    assert parsed.contiguous is False
    assert parsed.erode == 3
    assert parsed.feather == 2.5
    assert parsed.despill is True
    assert parsed.normalized_key_color() == "#ff00ff"


def test_settings_blank_key_color_is_auto():
    parsed = BackgroundRemovalSettings.from_json_dict({"key_color": ""})
    assert parsed.key_color is None


# ── Service ──────────────────────────────────────────────────────────────


def test_auto_detect_removes_solid_background():
    data = _green_screen_png()
    result = remove_background_png(data, "art.png")

    assert result.output_name == "art_cutout.png"
    assert result.key_color == "#00c800"
    assert result.removed_pct > 70
    assert (result.width, result.height) == (100, 80)

    with _rgba(result.png_bytes) as img:
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0  # corner → transparent
        assert img.getpixel((50, 40))[3] == 255  # red circle stays
        assert img.getpixel((50, 40))[:3] == RED


def test_explicit_key_color_override():
    # White background art; corners are white but we key out red instead.
    # Global mode — an enclosed keyed region isn't edge-connected.
    img = Image.new("RGB", (40, 40), (255, 255, 255))
    ImageDraw.Draw(img).ellipse((10, 10, 30, 30), fill=RED)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = remove_background_png(
        buf.getvalue(),
        "x.png",
        BackgroundRemovalSettings(key_color="dc1414", tolerance=10, contiguous=False),
    )
    assert result.key_color == "#dc1414"
    with _rgba(result.png_bytes) as out:
        assert out.getpixel((20, 20))[3] == 0  # keyed-out circle
        assert out.getpixel((0, 0))[3] == 255  # white bg kept


def test_tolerance_controls_match_strictness():
    # Pure-green backdrop with an off-green noise band along the top edge
    # (inset from the corners so auto-detect still samples pure green).
    img = Image.new("RGB", (50, 50), GREEN)
    px = img.load()
    for y in range(10):
        for x in range(10, 40):
            px[x, y] = (0, 235, 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    strict = remove_background_png(data, "a.png", BackgroundRemovalSettings(tolerance=5))
    loose = remove_background_png(data, "a.png", BackgroundRemovalSettings(tolerance=60))
    assert 0.0 < strict.removed_pct < loose.removed_pct
    assert loose.removed_pct == 100.0


def test_contiguous_preserves_enclosed_color_but_global_removes_it():
    data = _green_screen_png(hole=True)
    center = (50, 40)

    cont = remove_background_png(data, "a.png", BackgroundRemovalSettings(contiguous=True))
    with _rgba(cont.png_bytes) as img:
        assert img.getpixel(center)[3] == 255  # enclosed green hole survives

    glob = remove_background_png(data, "a.png", BackgroundRemovalSettings(contiguous=False))
    with _rgba(glob.png_bytes) as img:
        assert img.getpixel(center)[3] == 0  # global match removes it too


def test_erode_shrinks_kept_area():
    data = _green_screen_png()
    plain = remove_background_png(data, "a.png", BackgroundRemovalSettings())
    eroded = remove_background_png(data, "a.png", BackgroundRemovalSettings(erode=4))
    assert eroded.removed_pct > plain.removed_pct

    with _rgba(eroded.png_bytes) as img:
        # A pixel just inside the circle edge is cut by the erosion
        assert img.getpixel((26, 40))[3] == 0


def test_feather_produces_partial_alpha():
    data = _green_screen_png()
    result = remove_background_png(data, "a.png", BackgroundRemovalSettings(feather=2.0))
    with _rgba(result.png_bytes) as img:
        alphas = {img.getpixel((x, y))[3] for x in range(100) for y in range(80)}
    assert 0 in alphas and 255 in alphas
    assert any(0 < a < 255 for a in alphas)  # soft edge


def test_despill_recolors_edge_pixels():
    data = _green_screen_png()
    plain = remove_background_png(data, "a.png", BackgroundRemovalSettings())
    spilled = remove_background_png(
        data, "a.png", BackgroundRemovalSettings(feather=1.5, despill=True)
    )
    with _rgba(plain.png_bytes) as a, _rgba(spilled.png_bytes) as b:
        pa, pb = a.load(), b.load()
        changed = any(pa[x, y][:3] != pb[x, y][:3] for x in range(100) for y in range(80))
    assert changed


def test_rgba_and_palette_inputs_work():
    rgba = _png_bytes(mode="RGBA", color=GREEN + (255,))
    result = remove_background_png(rgba, "rgba.png")
    assert result.removed_pct == 100.0

    pal = _png_bytes(mode="P", size=(16, 16), color=0)
    result = remove_background_png(pal, "pal.png")
    assert result.output_name == "pal_cutout.png"


def test_preexisting_transparency_preserved():
    img = Image.new("RGBA", (30, 30), GREEN + (255,))
    px = img.load()
    for x in range(10):  # already-transparent strip along the left
        for y in range(30):
            px[x, y] = (200, 0, 0, 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = remove_background_png(buf.getvalue(), "a.png")
    with _rgba(result.png_bytes) as out:
        assert out.getpixel((5, 15))[3] == 0


def test_non_png_and_corrupt_inputs():
    with pytest.raises(NotAPngError):
        remove_background_png(b"junk", "fake.png")
    with pytest.raises(ImageConversionError, match="corrupt"):
        remove_background_png(PNG_SIGNATURE + b"\x00" * 32, "broken.png")


def test_pixel_limit_enforced(monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageTooLargeError):
        remove_background_png(_png_bytes(), "huge.png")


def test_removed_image_zip_compatible():
    result = remove_background_png(_png_bytes(), "z.png")
    assert isinstance(result, RemovedImage)
    assert result.data == result.png_bytes
    assert result.output_bytes == len(result.png_bytes)


# ── Endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _upload(name: str, data: bytes, content_type: str = "image/png"):
    return ("files", (name, data, content_type))


def test_remove_background_page_renders(client):
    resp = client.get("/remove-background")
    assert resp.status_code == 200
    assert "Background Remover" in resp.text
    assert "Contiguous" in resp.text


def test_single_png_returns_rgba(client):
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("art.png", _green_screen_png())],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert 'filename="art_cutout.png"' in resp.headers["content-disposition"]

    manifest = json.loads(resp.headers["x-removal-results"])
    assert manifest["skipped"] == []
    [item] = manifest["converted"]
    assert item["output_name"] == "art_cutout.png"
    assert item["key_color"] == "#00c800"
    assert item["removed_pct"] > 70

    with Image.open(io.BytesIO(resp.content)) as img:
        assert img.format == "PNG" and img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0


def test_endpoint_accepts_settings_form_fields(client):
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("art.png", _green_screen_png(hole=True))],
        data={"contiguous": "false", "tolerance": "40", "feather": "1.5", "despill": "true"},
    )
    assert resp.status_code == 200
    with Image.open(io.BytesIO(resp.content)) as img:
        assert img.getpixel((50, 40))[3] != 255  # global mode cut the hole


def test_endpoint_rejects_bad_settings(client):
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("art.png", _png_bytes())],
        data={"tolerance": "500"},
    )
    assert resp.status_code == 422


def test_endpoint_single_non_png_415(client):
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("notes.txt", b"hello", "text/plain")],
    )
    assert resp.status_code == 415

    resp = client.post(
        "/api/remove-background/",
        files=[_upload("sneaky.png", b"not a png")],
    )
    assert resp.status_code == 415


def test_endpoint_oversized_413(client, monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_UPLOAD_BYTES", 16)
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("big.png", _png_bytes())],
    )
    assert resp.status_code == 413


def test_endpoint_corrupt_422(client):
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("broken.png", PNG_SIGNATURE + b"\x00" * 64)],
    )
    assert resp.status_code == 422


def test_endpoint_batch_returns_zip(client):
    resp = client.post(
        "/api/remove-background/",
        files=[
            _upload("one.png", _png_bytes()),
            _upload("two.png", _green_screen_png(size=(16, 16))),
        ],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "cutout-images.zip" in resp.headers["content-disposition"]

    manifest = json.loads(resp.headers["x-removal-results"])
    assert len(manifest["converted"]) == 2

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert archive.namelist() == ["one_cutout.png", "two_cutout.png"]


def test_endpoint_batch_skips_invalid(client):
    resp = client.post(
        "/api/remove-background/",
        files=[
            _upload("ok.png", _png_bytes()),
            _upload("notes.txt", b"hi", "text/plain"),
        ],
    )
    assert resp.status_code == 200
    manifest = json.loads(resp.headers["x-removal-results"])
    assert len(manifest["converted"]) == 1
    assert manifest["skipped"][0]["name"] == "notes.txt"


def test_endpoint_too_many_files_413(client, monkeypatch):
    monkeypatch.setattr(image_convert, "MAX_BATCH_FILES", 1)
    resp = client.post(
        "/api/remove-background/",
        files=[_upload("a.png", _png_bytes()), _upload("b.png", _png_bytes())],
    )
    assert resp.status_code == 413


# ── Block ────────────────────────────────────────────────────────────────


@pytest.fixture
def remover_block():
    from app.blocks.background_remover import BackgroundRemover

    return BackgroundRemover()


def test_block_registered_and_configurable():
    from app.blocks.registry import discover_blocks, get_block
    from app.services.configuration.catalog import configurable_blocks, is_configurable_block

    discover_blocks()
    assert get_block("background_remover") is not None
    assert "background_remover" in configurable_blocks()
    assert is_configurable_block("background_remover")
    assert not is_configurable_block("echo")


def test_block_code_defaults(remover_block):
    defaults = remover_block.code_block_defaults()
    assert defaults.block_name == "background_remover"
    assert defaults.settings == BackgroundRemovalSettings().to_json_dict()


def test_block_parse_settings_validation(remover_block):
    normalized = remover_block.parse_settings({"tolerance": "50", "contiguous": "false"})
    assert normalized["tolerance"] == 50
    assert normalized["contiguous"] is False
    with pytest.raises(ValueError):
        remover_block.parse_settings({"tolerance": "999"})


def test_block_validate_requires_images(remover_block):
    import asyncio

    with pytest.raises(ValueError, match="generated_images"):
        asyncio.run(remover_block.validate({}))
    asyncio.run(remover_block.validate({"generated_images": ["/tmp/x.png"]}))


def test_block_run_writes_cutouts(remover_block, tmp_path):
    import asyncio

    src = tmp_path / "aimw_shot_00001_.png"
    src.write_bytes(_green_screen_png(size=(40, 40)))
    context = {"generated_images": [str(src)]}

    result = asyncio.run(remover_block.run(context))

    [cutout] = result["cutout_images"]
    out_path = Path(cutout)
    assert out_path.name == "aimw_shot_00001_cutout.png"
    assert out_path.exists()
    with Image.open(out_path) as img:
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0

    assert result["cutout_metadata"][0]["key_color"] == "#00c800"
    assert result["background_remover_output"]
    assert result["brief"] == result["background_remover_output"]
    # First run on an uncustomized profile → code-default warning recorded
    assert any("code-default" in w for w in context["_warnings"])


def test_block_run_uses_saved_profile(remover_block, tmp_path):
    import asyncio

    from app.services.configuration.database import DatabaseConfigurationProvider

    provider = DatabaseConfigurationProvider()
    asyncio.run(
        provider.upsert_block_configuration(
            "background_remover",
            {"key_color": "#00c800", "tolerance": 60, "contiguous": False},
        )
    )

    src = tmp_path / "x.png"
    src.write_bytes(_green_screen_png(size=(40, 40), hole=True))
    context = {"generated_images": [str(src)]}
    result = asyncio.run(remover_block.run(context))

    with Image.open(result["cutout_images"][0]) as img:
        assert img.getpixel((20, 20))[3] == 0  # saved global mode removed the hole
    assert "_warnings" not in context  # custom profile → no warning


# ── Configuration provider ───────────────────────────────────────────────


def test_block_profile_seed_update_reset():
    import asyncio

    from app.blocks.background_remover import BackgroundRemover
    from app.services.configuration.database import DatabaseConfigurationProvider

    provider = DatabaseConfigurationProvider()
    defaults = BackgroundRemover.code_block_defaults()

    resolved = asyncio.run(provider.resolve_block(defaults))
    assert resolved.source == "code_default"
    assert resolved.warning is not None
    assert "background_remover" in resolved.warning
    assert resolved.settings == BackgroundRemovalSettings().to_json_dict()

    config = asyncio.run(
        provider.upsert_block_configuration(
            "background_remover", {"tolerance": 70, "despill": True}
        )
    )
    assert config.uses_code_defaults is False
    saved = json.loads(config.settings_json)
    assert saved["tolerance"] == 70 and saved["despill"] is True

    resolved = asyncio.run(provider.resolve_block(defaults))
    assert resolved.source == "custom"
    assert resolved.warning is None
    assert resolved.settings["tolerance"] == 70

    config = asyncio.run(provider.reset_block_configuration(defaults))
    assert config.uses_code_defaults is True
    assert json.loads(config.settings_json) == BackgroundRemovalSettings().to_json_dict()


# ── Block configuration API ──────────────────────────────────────────────


def test_block_config_api_put_get_reset(client):
    from app.services.configuration.database import DatabaseConfigurationProvider

    provider = DatabaseConfigurationProvider()
    import asyncio

    asyncio.run(provider.resolve_block(_remover_defaults()))

    resp = client.put(
        "/api/configurations/block/background_remover",
        json={"settings": {"tolerance": "55", "despill": "on"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["block_name"] == "background_remover"
    assert body["source"] == "custom"
    assert body["settings"]["tolerance"] == 55
    assert body["settings"]["despill"] is True

    listed = client.get("/api/configurations/block")
    assert listed.status_code == 200
    profiles = {p["block_name"]: p for p in listed.json()["profiles"]}
    assert profiles["background_remover"]["settings"]["tolerance"] == 55

    resp = client.post(
        "/api/configurations/block/background_remover/reset",
        json={"block_name": "background_remover"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["uses_code_defaults"] is True
    assert body["settings"] == BackgroundRemovalSettings().to_json_dict()


def test_block_config_api_unknown_block_404(client):
    resp = client.put("/api/configurations/block/nope", json={"settings": {}})
    assert resp.status_code == 404
    resp = client.post("/api/configurations/block/nope/reset", json={"block_name": "nope"})
    assert resp.status_code == 404


def test_block_config_api_invalid_settings_422(client):
    resp = client.put(
        "/api/configurations/block/background_remover",
        json={"settings": {"tolerance": 500}},
    )
    assert resp.status_code == 422


def _remover_defaults():
    from app.blocks.background_remover import BackgroundRemover

    return BackgroundRemover.code_block_defaults()


def test_block_profiles_scoped_per_workflow():
    import asyncio

    from app.models.workflow import Workflow
    from app.services.configuration.database import DatabaseConfigurationProvider

    provider = DatabaseConfigurationProvider()

    async def make_workflow() -> int:
        import app.database

        async with app.database.async_session() as session:
            wf = Workflow(
                slug="alt",
                name="Alt",
                description="",
                steps_json="[]",
                is_enabled=True,
                is_default=False,
            )
            session.add(wf)
            await session.commit()
            return wf.id

    wf_id = asyncio.run(make_workflow())
    asyncio.run(
        provider.upsert_block_configuration(
            "background_remover", {"tolerance": 5}, workflow_id=wf_id
        )
    )

    async def resolve(wf_id=None):
        import app.database

        async with app.database.async_session() as session:
            from app.services.workflows import get_or_create_default_workflow

            wf = (
                await session.get(Workflow, wf_id)
                if wf_id
                else await get_or_create_default_workflow(session)
            )
            return wf.id

    default_wf = asyncio.run(resolve())
    rows = asyncio.run(provider.list_block_configurations(default_wf))
    assert all(r.settings_json != '{"tolerance": 5}' for r in rows)

    alt_rows = asyncio.run(provider.list_block_configurations(wf_id))
    assert any(json.loads(r.settings_json)["tolerance"] == 5 for r in alt_rows)
