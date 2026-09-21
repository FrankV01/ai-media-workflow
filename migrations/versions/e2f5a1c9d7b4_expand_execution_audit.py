"""expand execution audit details

Revision ID: e2f5a1c9d7b4
Revises: 8c117703e686
Create Date: 2026-09-20 21:28:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f5a1c9d7b4"
down_revision: str | Sequence[str] | None = "8c117703e686"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("job_steps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("error_traceback", sa.Text(), nullable=True))

    with op.batch_alter_table("role_executions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("temperature", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("max_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reasoning_effort", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("finish_reason", sa.String(length=50), nullable=True))
        batch_op.add_column(
            sa.Column("status", sa.String(length=50), nullable=False, server_default="completed")
        )
        batch_op.add_column(sa.Column("error_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("role_executions", schema=None) as batch_op:
        batch_op.drop_column("error")
        batch_op.drop_column("error_type")
        batch_op.drop_column("status")
        batch_op.drop_column("finish_reason")
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("max_tokens")
        batch_op.drop_column("temperature")

    with op.batch_alter_table("job_steps", schema=None) as batch_op:
        batch_op.drop_column("error_traceback")
        batch_op.drop_column("error_type")
