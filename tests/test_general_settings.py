"""
tests.test_general_settings — Global delivery settings + signature assets

Covers the DB-backed DeliverySettings service (persistence, validation,
signature upload/replace/clear, filename safety) and the /settings/general
web page (render, save feedback, error replay, HTML escaping, preview).
All files land under tmp_path — nothing touches the real output volume.
"""

import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

import app.database
from app.config import settings
from app.models.setting import Setting
from app.services import general_settings as gs


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Redirect IMAGE_OUTPUT_DIR (and the signatures dir under it) to tmp."""
    monkeypatch.setattr(settings, "image_output_dir", tmp_path / "output")
    return tmp_path / "output"


def _png_bytes(fmt: str = "PNG", size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


async def test_settings_default_to_empty(output_dir):
    async with app.database.async_session() as session:
        delivery = await gs.get_delivery_settings(session)
    assert delivery.customer_note == ""
    assert delivery.artist_attribution == ""
    assert delivery.license_markdown == ""
    assert delivery.signature_asset is None


async def test_settings_save_and_reload(output_dir):
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(
            session,
            customer_note="Thanks!",
            artist_attribution="Studio X",
            license_markdown="# Terms\nAll rights reserved.",
        )
        await session.commit()
    async with app.database.async_session() as session:
        delivery = await gs.get_delivery_settings(session)
        row = (
            await session.execute(select(Setting).where(Setting.key == gs.DELIVERY_SETTINGS_KEY))
        ).scalar_one()
    assert delivery.customer_note == "Thanks!"
    assert delivery.artist_attribution == "Studio X"
    assert "All rights reserved" in delivery.license_markdown
    assert row.value


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("customer_note", gs.MAX_NOTE_CHARS),
        ("artist_attribution", gs.MAX_ATTRIBUTION_CHARS),
        ("license_markdown", gs.MAX_LICENSE_CHARS),
    ],
)
async def test_settings_field_limits(output_dir, field, limit):
    kwargs = {field: "x" * (limit + 1)}
    async with app.database.async_session() as session:
        with pytest.raises(gs.DeliverySettingsError, match=field):
            await gs.save_delivery_settings(session, **kwargs)


async def test_signature_upload_lifecycle(output_dir):
    filename = gs.save_signature_file(_png_bytes(), "sig.png")
    assert filename.startswith("signature-") and filename.endswith(".png")
    path = gs.signature_path(filename)
    assert path is not None
    assert path.is_relative_to(gs.signatures_dir().resolve())

    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, signature_asset=filename)
        delivery = await gs.get_delivery_settings(session)
    assert delivery.signature_asset == filename

    second = gs.save_signature_file(_png_bytes(fmt="JPEG"), "sig.jpg")
    assert second != filename and second.endswith(".jpg")
    assert gs.signature_path(filename) is not None
    async with app.database.async_session() as session:
        current = await gs.get_delivery_settings(session)
        await gs.save_delivery_settings(
            session,
            customer_note=current.customer_note,
            artist_attribution=current.artist_attribution,
            license_markdown=current.license_markdown,
            signature_asset=second,
        )
        delivery = await gs.get_delivery_settings(session)
    assert delivery.signature_asset == second

    async with app.database.async_session() as session:
        current = await gs.get_delivery_settings(session)
        await gs.save_delivery_settings(
            session,
            customer_note=current.customer_note,
            artist_attribution=current.artist_attribution,
            license_markdown=current.license_markdown,
            signature_asset=None,
        )
        delivery = await gs.get_delivery_settings(session)
    assert delivery.signature_asset is None
    assert gs.signature_path(second) is not None


@pytest.mark.parametrize("name", ["sig.svg", "sig.gif", "sig.webp", "sig.txt", "sig"])
def test_signature_rejects_wrong_extension(output_dir, name):
    with pytest.raises(gs.SignatureValidationError, match="PNG or JPEG"):
        gs.save_signature_file(_png_bytes(), name)


def test_signature_rejects_decoded_format_mismatch(output_dir):
    """JPEG bytes named .png fail — the decoded format is authoritative."""
    with pytest.raises(gs.SignatureValidationError, match="PNG or JPEG"):
        gs.save_signature_file(_png_bytes(fmt="JPEG"), "sig.png")


def test_signature_rejects_malformed(output_dir):
    with pytest.raises(gs.SignatureValidationError):
        gs.save_signature_file(b"not an image at all", "sig.png")


def test_signature_rejects_oversized(output_dir):
    too_big = _png_bytes() + b"x" * (gs.MAX_SIGNATURE_BYTES)
    with pytest.raises(gs.SignatureValidationError, match="MB limit"):
        gs.save_signature_file(too_big, "sig.png")


def test_signature_rejects_decompression_bomb(output_dir, monkeypatch):
    monkeypatch.setattr(gs, "MAX_SIGNATURE_PIXELS", 64)
    with pytest.raises(gs.SignatureValidationError, match="MP limit"):
        gs.save_signature_file(_png_bytes(size=(16, 16)), "sig.png")


def test_signature_never_trusts_client_names(output_dir, tmp_path):
    assert gs.signature_path("../escape.png") is None
    assert gs.signature_path("a/b.png") is None
    assert gs.signature_path("missing.png") is None
    evil = tmp_path / "output" / "_delivery_assets" / "signatures"
    evil.mkdir(parents=True)
    (evil / "sig-1.png").write_bytes(b"x")
    outside = tmp_path / "output" / "outside.png"
    outside.write_bytes(b"x")
    assert gs.signature_path("../outside.png") is None


async def test_signature_asset_must_be_managed_filename(output_dir):
    async with app.database.async_session() as session:
        with pytest.raises(gs.DeliverySettingsError):
            await gs.save_delivery_settings(session, signature_asset="../x.png")
        with pytest.raises(gs.DeliverySettingsError):
            await gs.save_delivery_settings(session, signature_asset="missing.png")


def _client():
    import app.main

    transport = httpx.ASGITransport(app=app.main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_general_settings_page(output_dir):
    async with _client() as client:
        response = await client.get("/settings/general")
    assert response.status_code == 200
    assert "General Settings" in response.text
    assert "Customer note" in response.text


async def test_general_settings_save_roundtrip(output_dir):
    async with _client() as client:
        response = await client.post(
            "/settings/general/save",
            data={
                "customer_note": "web note",
                "artist_attribution": "web artist",
                "license_markdown": "web license",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = await client.get("/settings/general")
    assert 'name="customer_note"' in page.text
    assert "web note" in page.text
    assert "web artist" in page.text
    assert "web license" in page.text
    async with app.database.async_session() as session:
        delivery = await gs.get_delivery_settings(session)
    assert delivery.customer_note == "web note"


async def test_general_settings_get_does_not_overwrite_saved(output_dir):
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, customer_note="saved note")
        await session.commit()
    async with _client() as client:
        await client.get("/settings/general")
    async with app.database.async_session() as session:
        delivery = await gs.get_delivery_settings(session)
    assert delivery.customer_note == "saved note"


async def test_general_settings_error_replays_form(output_dir):
    async with _client() as client:
        response = await client.post(
            "/settings/general/save",
            data={
                "customer_note": "y" * (gs.MAX_NOTE_CHARS + 1),
                "artist_attribution": "keep me",
                "license_markdown": "and me",
            },
        )
    assert response.status_code == 422
    assert "customer_note" in response.text
    assert "keep me" in response.text
    assert "and me" in response.text


async def test_general_settings_escapes_html(output_dir):
    async with _client() as client:
        await client.post(
            "/settings/general/save",
            data={
                "customer_note": "<script>alert(1)</script>",
                "artist_attribution": "",
                "license_markdown": "",
            },
        )
        page = await client.get("/settings/general")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


async def test_general_settings_signature_flow(output_dir):
    async with _client() as client:
        response = await client.post(
            "/settings/general/signature",
            files={"signature": ("sig.png", _png_bytes(), "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        preview = await client.get("/settings/general/signature")
        assert preview.status_code == 200
        assert preview.content == _png_bytes()

        bad = await client.post(
            "/settings/general/signature",
            files={"signature": ("sig.svg", b"<svg/>", "image/svg+xml")},
        )
        assert bad.status_code == 422
        assert "PNG or JPEG" in bad.text

        cleared = await client.post("/settings/general/signature/clear", follow_redirects=False)
        assert cleared.status_code == 303
        assert (await client.get("/settings/general/signature")).status_code == 404


async def test_nav_links_general_settings(output_dir):
    async with _client() as client:
        page = await client.get("/settings/general")
    assert 'href="/settings/general"' in page.text


def test_signature_rejects_truncated_jpeg(output_dir):
    data = _png_bytes(fmt="JPEG")
    truncated = data[: len(data) // 3]
    assert truncated.startswith(b"\xff\xd8")
    with pytest.raises(gs.SignatureValidationError, match="corrupt"):
        gs.save_signature_file(truncated, "sig.jpg")


def test_signature_rejects_redirected_storage(output_dir):
    outside = output_dir.parent / "elsewhere"
    outside.mkdir()
    output_dir.mkdir()
    (output_dir / "_delivery_assets").symlink_to(outside)
    with pytest.raises(gs.SignatureValidationError, match="unavailable"):
        gs.save_signature_file(_png_bytes(), "sig.png")
    assert gs.signature_path("signature-anything.png") is None


async def test_signature_upload_failure_discards_new_file(output_dir, monkeypatch):
    import app.web.routes as routes

    async def boom(*args, **kwargs):
        raise gs.DeliverySettingsError(["save exploded"])

    monkeypatch.setattr(routes, "save_delivery_settings", boom)
    before = set(gs.signatures_dir().glob("*")) if gs.signatures_dir().exists() else set()
    import app.main

    transport = httpx.ASGITransport(app=app.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/settings/general/signature",
            files={"signature": ("sig.png", _png_bytes(), "image/png")},
        )
    assert response.status_code == 422
    after = set(gs.signatures_dir().glob("*")) if gs.signatures_dir().exists() else set()
    assert after == before


def test_signature_rejects_symlinked_asset(output_dir, tmp_path):
    real = tmp_path / "real-signature.png"
    real.write_bytes(_png_bytes())
    signatures = gs.signatures_dir()
    signatures.mkdir(parents=True)
    (signatures / "signature-1.png").symlink_to(real)
    assert gs.signature_path("signature-1.png") is None
