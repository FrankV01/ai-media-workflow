"""app.services.general_settings — Global delivery settings + signature assets

One global DeliverySettings record (customer note, artist attribution,
optional signature image reference, optional Markdown license terms) stored
as JSON under the namespaced Setting key "delivery.general_settings". These
settings are global-only — no workflow or per-run overrides — and each
delivery archive attempt snapshots them immutably.

Uploaded signature images live under
IMAGE_OUTPUT_DIR/_delivery_assets/signatures/ with server-generated
immutable filenames; replaced assets are preserved on disk so recorded
attempts keep valid references.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.setting import Setting

DELIVERY_SETTINGS_KEY = "delivery.general_settings"

MAX_NOTE_CHARS = 20_000
MAX_ATTRIBUTION_CHARS = 20_000
MAX_LICENSE_CHARS = 100_000
MAX_SIGNATURE_BYTES = 5 * 1024 * 1024
MAX_SIGNATURE_PIXELS = 20_000_000

_SIGNATURE_EXTENSIONS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}


class DeliverySettingsError(ValueError):
    """Delivery settings failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class SignatureValidationError(ValueError):
    """An uploaded signature image failed validation."""


@dataclass
class DeliverySettings:
    """Global, DB-backed customer delivery settings."""

    customer_note: str = ""
    artist_attribution: str = ""
    license_markdown: str = ""
    signature_asset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeliverySettings:
        signature = data.get("signature_asset")
        return cls(
            customer_note=str(data.get("customer_note") or ""),
            artist_attribution=str(data.get("artist_attribution") or ""),
            license_markdown=str(data.get("license_markdown") or ""),
            signature_asset=str(signature) if signature else None,
        )


def _validate_text_fields(
    customer_note: Any, artist_attribution: Any, license_markdown: Any
) -> list[str]:
    errors: list[str] = []
    for value, label, limit in (
        (customer_note, "customer_note", MAX_NOTE_CHARS),
        (artist_attribution, "artist_attribution", MAX_ATTRIBUTION_CHARS),
        (license_markdown, "license_markdown", MAX_LICENSE_CHARS),
    ):
        if not isinstance(value, str):
            errors.append(f"{label} must be text")
        elif len(value) > limit:
            errors.append(f"{label} must be {limit} characters or fewer")
    return errors


async def get_delivery_settings(session: AsyncSession) -> DeliverySettings:
    """Return the saved global delivery settings (empty fields when unset)."""
    row = (
        await session.execute(select(Setting).where(Setting.key == DELIVERY_SETTINGS_KEY))
    ).scalar_one_or_none()
    if row is None:
        return DeliverySettings()
    try:
        data = json.loads(row.value)
    except ValueError:
        return DeliverySettings()
    if not isinstance(data, dict):
        return DeliverySettings()
    return DeliverySettings.from_dict(data)


async def save_delivery_settings(
    session: AsyncSession,
    *,
    customer_note: Any = "",
    artist_attribution: Any = "",
    license_markdown: Any = "",
    signature_asset: Any = None,
) -> DeliverySettings:
    """Validate and persist the global delivery settings.

    `signature_asset` is a server-managed filename — callers pass the value
    produced by save_signature_file (or the current one); it is never taken
    from client paths. None clears the reference (the file stays on disk).
    """
    errors = _validate_text_fields(customer_note, artist_attribution, license_markdown)
    if signature_asset is not None:
        asset = str(signature_asset)
        if Path(asset).name != asset or not asset:
            errors.append("signature_asset must be a managed filename")
        elif signature_path(asset) is None:
            errors.append("signature_asset does not reference an uploaded signature")
    if errors:
        raise DeliverySettingsError(errors)

    record = DeliverySettings(
        customer_note=customer_note,
        artist_attribution=artist_attribution,
        license_markdown=license_markdown,
        signature_asset=str(signature_asset) if signature_asset else None,
    )
    row = (
        await session.execute(select(Setting).where(Setting.key == DELIVERY_SETTINGS_KEY))
    ).scalar_one_or_none()
    payload = json.dumps(record.to_dict(), ensure_ascii=False)
    if row is None:
        session.add(
            Setting(
                key=DELIVERY_SETTINGS_KEY,
                value=payload,
                description="Global customer delivery settings",
            )
        )
    else:
        row.value = payload
    await session.flush()
    return record


def signatures_dir() -> Path:
    """Directory holding uploaded signature assets (created on demand)."""
    return Path(settings.image_output_dir) / "_delivery_assets" / "signatures"


def _resolved_signatures_dir() -> Path | None:
    """signatures_dir resolved, or None when it escapes the output root."""
    output_root = Path(settings.image_output_dir).resolve()
    root = signatures_dir().resolve()
    if not root.is_relative_to(output_root):
        return None
    return root


def signature_path(asset: str | None) -> Path | None:
    """Resolve a managed signature filename to a path inside signatures_dir.

    Returns None for unsafe names, redirected storage, or missing files;
    never trusts client input.
    """
    if not asset or Path(asset).name != asset:
        return None
    root = _resolved_signatures_dir()
    if root is None:
        return None
    candidate = root / asset
    if candidate.is_symlink():
        return None
    path = candidate.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def discard_signature(asset: str | None) -> None:
    """Remove a freshly uploaded managed signature file (best-effort)."""
    path = signature_path(asset)
    if path is not None:
        path.unlink(missing_ok=True)


def save_signature_file(data: bytes, original_name: str | None) -> str:
    """Validate and store a signature upload; return its managed filename.

    PNG/JPEG only — the decoded Pillow format is authoritative (extensions
    and client filenames are not trusted). Previous assets are preserved.
    """
    if not data:
        raise SignatureValidationError("empty upload")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise SignatureValidationError(
            f"signature exceeds the {MAX_SIGNATURE_BYTES // (1024 * 1024)} MB limit"
        )
    extension = Path((original_name or "").replace("\\", "/")).suffix.lower()
    if extension not in _SIGNATURE_EXTENSIONS:
        raise SignatureValidationError("signature must be a PNG or JPEG file")
    try:
        opened = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise SignatureValidationError("signature exceeds the pixel limit") from exc
    except Exception as exc:
        raise SignatureValidationError("corrupt or unsupported image") from exc
    with opened:
        detected = opened.format
        if detected != _SIGNATURE_EXTENSIONS[extension]:
            raise SignatureValidationError("signature must be a PNG or JPEG file")
        if opened.width * opened.height > MAX_SIGNATURE_PIXELS:
            raise SignatureValidationError(
                f"signature exceeds the {MAX_SIGNATURE_PIXELS // 1_000_000} MP limit"
            )
        try:
            opened.verify()
        except Exception as exc:
            raise SignatureValidationError("corrupt or unsupported image") from exc
    try:
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
    except Exception as exc:
        raise SignatureValidationError("corrupt or unsupported image") from exc

    suffix = ".png" if detected == "PNG" else ".jpg"
    root = _resolved_signatures_dir()
    if root is None:
        raise SignatureValidationError("signature storage is unavailable")
    root.mkdir(parents=True, exist_ok=True)
    filename = f"signature-{uuid.uuid4().hex}{suffix}"
    (root / filename).write_bytes(data)
    return filename
