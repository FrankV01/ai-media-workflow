"""conservative background_removal code defaults (tolerance 15, feather 1px)

Refresh block_configurations rows that still track code defaults
(uses_code_defaults=1) so they pick up the new shipped values
(tolerance 32→15, feather 0→1.0). Customized profiles are untouched —
Reset remains the explicit path to adopt the new defaults there.

Revision ID: d1e4f6a8b3c5
Revises: c9d3e5f7a1b2
Create Date: 2026-09-26 16:00:00

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e4f6a8b3c5"
down_revision: str | Sequence[str] | None = "c9d3e5f7a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Field order matches BackgroundRemovalSettings.to_json_dict()
_NEW_DEFAULTS = json.dumps(
    {
        "key_color": None,
        "tolerance": 15,
        "contiguous": True,
        "erode": 0,
        "feather": 1.0,
        "despill": False,
    }
)
_OLD_DEFAULTS = json.dumps(
    {
        "key_color": None,
        "tolerance": 32,
        "contiguous": True,
        "erode": 0,
        "feather": 0.0,
        "despill": False,
    }
)


def _refresh_defaults(settings_json: str) -> None:
    """Point still-on-code-defaults background_remover rows at `settings_json`."""
    if "block_configurations" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.get_bind().execute(
        sa.text(
            "UPDATE block_configurations SET settings_json = :settings "
            "WHERE block_name = :block_name AND uses_code_defaults = 1"
        ),
        {"settings": settings_json, "block_name": "background_remover"},
    )


def upgrade() -> None:
    """Upgrade schema."""
    _refresh_defaults(_NEW_DEFAULTS)


def downgrade() -> None:
    """Downgrade schema."""
    _refresh_defaults(_OLD_DEFAULTS)
