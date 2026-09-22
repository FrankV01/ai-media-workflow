"""add report_files to jobs

Revision ID: a1c4e7b9d2f6
Revises: f3a7b2c1d8e5
Create Date: 2026-09-21 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b9d2f6"
down_revision: str | Sequence[str] | None = "f3a7b2c1d8e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("report_files", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("report_files")
