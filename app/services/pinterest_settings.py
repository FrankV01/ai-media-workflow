"""app.services.pinterest_settings — Global Pinterest scheduling settings

One global PinterestSettings record (default board, re-pin cooldown, daily
publish cap, product URL template) stored as JSON under the namespaced
Setting key "social.pinterest". These settings are global-only — no
workflow or per-run overrides.

Pinterest OAuth app credentials are env-only (PINTEREST_CLIENT_ID /
PINTEREST_CLIENT_SECRET) and never enter the database. The OAuth tokens
returned by /pinterest/connect are stored separately under
"social.pinterest.tokens" (app/services/pinterest_auth.py).

product_url_template uses str.format placeholders: {slug} and {product_id};
used to construct a product link when the FourthWall API does not return a
public URL.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import Setting

PINTEREST_SETTINGS_KEY = "social.pinterest"

MAX_URL_TEMPLATE_CHARS = 500
DEFAULT_PRODUCT_URL_TEMPLATE = "https://syntheticmuse.ai/products/{slug}"


class PinterestSettingsError(ValueError):
    """Pinterest settings failed validation; `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class PinterestSettings:
    """Global, DB-backed Pinterest scheduling settings."""

    default_board_id: str = ""
    default_board_name: str = ""
    repin_cooldown_days: int = 30
    daily_pin_limit: int = 10
    product_url_template: str = DEFAULT_PRODUCT_URL_TEMPLATE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinterestSettings:
        return cls(
            default_board_id=str(data.get("default_board_id") or ""),
            default_board_name=str(data.get("default_board_name") or ""),
            repin_cooldown_days=int(data.get("repin_cooldown_days") or 30),
            daily_pin_limit=int(data.get("daily_pin_limit") or 10),
            product_url_template=str(
                data.get("product_url_template") or DEFAULT_PRODUCT_URL_TEMPLATE
            ),
        )


def _validate(
    repin_cooldown_days: Any,
    daily_pin_limit: Any,
    product_url_template: Any,
) -> list[str]:
    errors: list[str] = []
    try:
        days = int(repin_cooldown_days)
        if not (0 <= days <= 365):
            errors.append("repin_cooldown_days must be between 0 and 365")
    except (TypeError, ValueError):
        errors.append("repin_cooldown_days must be an integer")
    try:
        limit = int(daily_pin_limit)
        if not (1 <= limit <= 100):
            errors.append("daily_pin_limit must be between 1 and 100")
    except (TypeError, ValueError):
        errors.append("daily_pin_limit must be an integer")
    if not isinstance(product_url_template, str):
        errors.append("product_url_template must be text")
    elif len(product_url_template) > MAX_URL_TEMPLATE_CHARS:
        errors.append(
            f"product_url_template must be {MAX_URL_TEMPLATE_CHARS} characters or fewer"
        )
    elif product_url_template.strip():
        try:
            product_url_template.format(slug="x", product_id="x")
        except (KeyError, IndexError, ValueError):
            errors.append(
                "product_url_template may only use {slug} and {product_id} placeholders"
            )
    else:
        errors.append("product_url_template must not be empty")
    return errors


async def get_pinterest_settings(session: AsyncSession) -> PinterestSettings:
    """Return the saved Pinterest settings (code defaults when unset)."""
    row = (
        await session.execute(select(Setting).where(Setting.key == PINTEREST_SETTINGS_KEY))
    ).scalar_one_or_none()
    if row is None:
        return PinterestSettings()
    try:
        data = json.loads(row.value)
    except ValueError:
        return PinterestSettings()
    if not isinstance(data, dict):
        return PinterestSettings()
    try:
        return PinterestSettings.from_dict(data)
    except (TypeError, ValueError):
        return PinterestSettings()


async def save_pinterest_settings(
    session: AsyncSession,
    *,
    default_board_id: Any = "",
    default_board_name: Any = "",
    repin_cooldown_days: Any = 30,
    daily_pin_limit: Any = 10,
    product_url_template: Any = DEFAULT_PRODUCT_URL_TEMPLATE,
) -> PinterestSettings:
    """Validate and persist the global Pinterest settings."""
    errors = _validate(repin_cooldown_days, daily_pin_limit, product_url_template)
    if errors:
        raise PinterestSettingsError(errors)

    record = PinterestSettings(
        default_board_id=str(default_board_id or "").strip(),
        default_board_name=str(default_board_name or "").strip(),
        repin_cooldown_days=int(repin_cooldown_days),
        daily_pin_limit=int(daily_pin_limit),
        product_url_template=str(product_url_template).strip(),
    )
    row = (
        await session.execute(select(Setting).where(Setting.key == PINTEREST_SETTINGS_KEY))
    ).scalar_one_or_none()
    payload = json.dumps(record.to_dict(), ensure_ascii=False)
    if row is None:
        session.add(
            Setting(
                key=PINTEREST_SETTINGS_KEY,
                value=payload,
                description="Global Pinterest scheduling settings",
            )
        )
    else:
        row.value = payload
    await session.flush()
    return record
