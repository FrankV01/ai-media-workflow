"""add resume state to jobs

Adds jobs.steps_json — the effective step list captured at submission —
and jobs.context_json — the caller-supplied run context (brief, backend
overrides). Together they let startup recovery requeue PENDING jobs and
resume RUNNING jobs that were interrupted by an application restart.

Revision ID: n7f1c3d5e9a2
Revises: m0b5d8e2f4a6
Create Date: 2026-10-21 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n7f1c3d5e9a2"
down_revision: str | Sequence[str] | None = "m0b5d8e2f4a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        if "steps_json" not in columns:
            batch_op.add_column(sa.Column("steps_json", sa.Text(), nullable=True))
        if "context_json" not in columns:
            batch_op.add_column(sa.Column("context_json", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        if "context_json" in columns:
            batch_op.drop_column("context_json")
        if "steps_json" in columns:
            batch_op.drop_column("steps_json")
