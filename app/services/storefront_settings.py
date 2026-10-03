"""app.services.storefront_settings — Global storefront listing settings

One global StorefrontSettings record (enable flag, USD price, publish flag,
file-size limit, description template, preview count) stored as JSON under
the namespaced Setting key "storefronts.fourthwall". These settings are
global-only — no workflow or per-run overrides — and each listing attempt
snapshots them immutably.

Storefront API credentials are env-only (FOURTHWALL_API_USER /
FOURTHWALL_API_PASSWORD) and never enter the database.

The description template uses string.Template placeholders: $title,
$description, $tags, $attribution.
"""

from __future__ import annotations

import json
import string
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import Setting

STOREFRONT_SETTINGS_KEY = "storefronts.fourthwall"

MAX_TEMPLATE_CHARS = 20_000
MAX_NAME_CHARS = 255

DEFAULT_DESCRIPTION_TEMPLATE = """\
$description

Tags: $tags

$attribution\
"""


class StorefrontSettingsError(ValueError):
    """Storefront settings failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class StorefrontSettings:
    """Global, DB-backed storefront (FourthWall) listing settings."""

    enabled: bool = False
    price_usd: float | None = None
    publish_on_create: bool = False
    file_size_limit_mb: int = 25
    description_template: str = DEFAULT_DESCRIPTION_TEMPLATE
    max_preview_images: int = 4

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StorefrontSettings:
        price = data.get("price_usd")
        return cls(
            enabled=bool(data.get("enabled", False)),
            price_usd=float(price) if price is not None else None,
            publish_on_create=bool(data.get("publish_on_create", False)),
            file_size_limit_mb=int(data.get("file_size_limit_mb") or 25),
            description_template=str(
                data.get("description_template") or DEFAULT_DESCRIPTION_TEMPLATE
            ),
            max_preview_images=int(data.get("max_preview_images") or 4),
        )


def _validate(
    enabled: Any,
    price_usd: Any,
    publish_on_create: Any,
    file_size_limit_mb: Any,
    description_template: Any,
    max_preview_images: Any,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(enabled, bool):
        errors.append("enabled must be a boolean")
    if not isinstance(publish_on_create, bool):
        errors.append("publish_on_create must be a boolean")
    if price_usd == "":
        price_usd = None  # empty form field == not provided
    if price_usd is not None:
        try:
            price = float(price_usd)
        except (TypeError, ValueError):
            errors.append("price_usd must be a number")
        else:
            if price < 0:
                errors.append("price_usd must be zero or positive")
            if enabled and price <= 0:
                errors.append("price_usd is required (must be positive) when enabled")
    elif enabled:
        errors.append("price_usd is required when the storefront is enabled")
    try:
        limit = int(file_size_limit_mb)
        if not (1 <= limit <= 1024):
            errors.append("file_size_limit_mb must be between 1 and 1024")
    except (TypeError, ValueError):
        errors.append("file_size_limit_mb must be an integer")
    try:
        count = int(max_preview_images)
        if not (0 <= count <= 20):
            errors.append("max_preview_images must be between 0 and 20")
    except (TypeError, ValueError):
        errors.append("max_preview_images must be an integer")
    if not isinstance(description_template, str):
        errors.append("description_template must be text")
    elif len(description_template) > MAX_TEMPLATE_CHARS:
        errors.append(
            f"description_template must be {MAX_TEMPLATE_CHARS} characters or fewer"
        )
    elif not description_template.strip():
        errors.append("description_template must not be empty")
    else:
        try:
            string.Template(description_template).safe_substitute(
                title="", description="", tags="", attribution=""
            )
        except ValueError:
            errors.append("description_template contains an invalid $placeholder")
    return errors


async def get_storefront_settings(session: AsyncSession) -> StorefrontSettings:
    """Return the saved storefront settings (code defaults when unset)."""
    row = (
        await session.execute(select(Setting).where(Setting.key == STOREFRONT_SETTINGS_KEY))
    ).scalar_one_or_none()
    if row is None:
        return StorefrontSettings()
    try:
        data = json.loads(row.value)
    except ValueError:
        return StorefrontSettings()
    if not isinstance(data, dict):
        return StorefrontSettings()
    try:
        return StorefrontSettings.from_dict(data)
    except (TypeError, ValueError):
        return StorefrontSettings()


async def save_storefront_settings(
    session: AsyncSession,
    *,
    enabled: Any = False,
    price_usd: Any = None,
    publish_on_create: Any = False,
    file_size_limit_mb: Any = 25,
    description_template: Any = DEFAULT_DESCRIPTION_TEMPLATE,
    max_preview_images: Any = 4,
) -> StorefrontSettings:
    """Validate and persist the global storefront settings."""
    errors = _validate(
        enabled,
        price_usd,
        publish_on_create,
        file_size_limit_mb,
        description_template,
        max_preview_images,
    )
    if errors:
        raise StorefrontSettingsError(errors)

    record = StorefrontSettings(
        enabled=bool(enabled),
        price_usd=float(price_usd) if price_usd is not None and price_usd != "" else None,
        publish_on_create=bool(publish_on_create),
        file_size_limit_mb=int(file_size_limit_mb),
        description_template=str(description_template),
        max_preview_images=int(max_preview_images),
    )
    row = (
        await session.execute(select(Setting).where(Setting.key == STOREFRONT_SETTINGS_KEY))
    ).scalar_one_or_none()
    payload = json.dumps(record.to_dict(), ensure_ascii=False)
    if row is None:
        session.add(
            Setting(
                key=STOREFRONT_SETTINGS_KEY,
                value=payload,
                description="Global storefront (FourthWall) listing settings",
            )
        )
    else:
        row.value = payload
    await session.flush()
    return record
