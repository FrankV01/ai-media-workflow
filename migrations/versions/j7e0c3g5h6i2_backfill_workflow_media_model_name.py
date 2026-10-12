"""backfill workflow media_model_name from env default

Existing workflows have media_model_name = NULL, which previously meant
"fall back to COMFYUI_CHECKPOINT / the generation backend default".
After this change the database owns the active model selection, so every
workflow must have an explicit value.

- Fills NULL media_model_name with the current env-selected default model.
- Does not touch already-populated overrides.
- The matching MediaModelConfiguration row is created lazily on the next
  settings-page load or pipeline run by the configuration provider.

Revision ID: j7e0c3g5h6i2
Revises: i6d9b3e7f4c1
Create Date: 2026-10-12 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "j7e0c3g5h6i2"
down_revision: str | Sequence[str] | None = "i6d9b3e7f4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Set every workflow's media_model_name to the env default if blank."""
    # Import at migration time so the fallback reflects the current env.
    from app.blocks.media_producer import selected_media_model
    from app.config import settings

    default_model = selected_media_model(settings.generation_backend)

    op.execute(
        sa.text(
            "UPDATE workflows SET media_model_name = :model WHERE media_model_name IS NULL"
        ).bindparams(model=default_model)
    )


def downgrade() -> None:
    """Leave media_model_name populated; downgrades cannot know which rows
    were originally NULL vs explicitly set by the user."""
    pass
