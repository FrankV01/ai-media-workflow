"""app.services.cloudflare_r2_settings — Global Cloudflare R2 settings

One global R2Settings record (S3 endpoint, bucket, key prefix, public base
URL) stored as JSON under the namespaced Setting key "cloudflare.r2". These
settings are global-only — no workflow or per-run overrides — and each
upload attempt snapshots them immutably.

R2 API credentials are env-only (CLOUDFLARE_R2_ACCESS_KEY_ID /
CLOUDFLARE_R2_SECRET_ACCESS_KEY) and never enter the database.

The public base URL is the bucket's public access endpoint — the r2.dev
development URL or a custom domain — and is what buyers' links are built
from. The S3 API endpoint URL is only used by the upload client and is
never part of a link.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import Setting

R2_SETTINGS_KEY = "cloudflare.r2"

MAX_NAME_CHARS = 255

DEFAULT_ENDPOINT_URL = (
    "https://d168825896f30364c102d714479c5f1f.r2.cloudflarestorage.com"
)
DEFAULT_BUCKET = "ai-media"
DEFAULT_KEY_PREFIX = "fourthwall/"


class R2SettingsError(ValueError):
    """R2 settings failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class R2Settings:
    """Global, DB-backed Cloudflare R2 upload settings."""

    endpoint_url: str = DEFAULT_ENDPOINT_URL
    bucket: str = DEFAULT_BUCKET
    key_prefix: str = DEFAULT_KEY_PREFIX
    public_base_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> R2Settings:
        return cls(
            endpoint_url=str(data.get("endpoint_url") or DEFAULT_ENDPOINT_URL),
            bucket=str(data.get("bucket") or DEFAULT_BUCKET),
            key_prefix=str(data.get("key_prefix") or DEFAULT_KEY_PREFIX),
            public_base_url=str(data.get("public_base_url") or ""),
        )


def _validate(
    endpoint_url: Any,
    bucket: Any,
    key_prefix: Any,
    public_base_url: Any,
) -> list[str]:
    errors: list[str] = []
    for name, value, required in (
        ("endpoint_url", endpoint_url, True),
        ("bucket", bucket, True),
        ("public_base_url", public_base_url, False),
    ):
        if not isinstance(value, str):
            errors.append(f"{name} must be text")
            continue
        if required and not value.strip():
            errors.append(f"{name} must not be empty")
        if len(value) > 1024:
            errors.append(f"{name} must be 1024 characters or fewer")
    for name, value in (("endpoint_url", endpoint_url), ("public_base_url", public_base_url)):
        if (
            isinstance(value, str)
            and value.strip()
            and not (value.startswith("https://") or value.startswith("http://"))
        ):
            errors.append(f"{name} must be an http(s) URL")
    if isinstance(bucket, str) and "/" in bucket:
        errors.append("bucket must be a bucket name only (no slashes)")
    if isinstance(key_prefix, str) and len(key_prefix) > 512:
        errors.append("key_prefix must be 512 characters or fewer")
    return errors


async def get_r2_settings(session: AsyncSession) -> R2Settings:
    """Return the saved R2 settings (code defaults when unset)."""
    row = (
        await session.execute(select(Setting).where(Setting.key == R2_SETTINGS_KEY))
    ).scalar_one_or_none()
    if row is None:
        return R2Settings()
    try:
        data = json.loads(row.value)
    except ValueError:
        return R2Settings()
    if not isinstance(data, dict):
        return R2Settings()
    try:
        return R2Settings.from_dict(data)
    except (TypeError, ValueError):
        return R2Settings()


async def save_r2_settings(
    session: AsyncSession,
    *,
    endpoint_url: Any = DEFAULT_ENDPOINT_URL,
    bucket: Any = DEFAULT_BUCKET,
    key_prefix: Any = DEFAULT_KEY_PREFIX,
    public_base_url: Any = "",
) -> R2Settings:
    """Validate and persist the global R2 settings."""
    errors = _validate(endpoint_url, bucket, key_prefix, public_base_url)
    if errors:
        raise R2SettingsError(errors)

    record = R2Settings(
        endpoint_url=str(endpoint_url).strip(),
        bucket=str(bucket).strip(),
        key_prefix=str(key_prefix).strip(),
        public_base_url=str(public_base_url).strip(),
    )
    row = (
        await session.execute(select(Setting).where(Setting.key == R2_SETTINGS_KEY))
    ).scalar_one_or_none()
    payload = json.dumps(record.to_dict(), ensure_ascii=False)
    if row is None:
        session.add(
            Setting(
                key=R2_SETTINGS_KEY,
                value=payload,
                description="Global Cloudflare R2 upload settings",
            )
        )
    else:
        row.value = payload
    await session.flush()
    return record
