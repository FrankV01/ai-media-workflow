"""add llm_configuration_changes audit table

Records every explicit LLM profile mutation (custom save / reset to code
defaults) with its origin and JSON before/after snapshots of the mutable
profile fields. No historical backfill — only mutations made after this
revision are audited.

Revision ID: c6e1f0a4b8d3
Revises: a1c4e7b9d2f6
Create Date: 2026-09-22 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e1f0a4b8d3"
down_revision: str | Sequence[str] | None = "a1c4e7b9d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Guard: databases that ran a create_all startup already have the table
    if "llm_configuration_changes" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "llm_configuration_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("configuration_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("origin", sa.String(length=50), nullable=False),
        sa.Column("before_snapshot", sa.Text(), nullable=False),
        sa.Column("after_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["configuration_id"], ["llm_role_configurations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_configuration_changes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_llm_configuration_changes_configuration_id"),
            ["configuration_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_llm_configuration_changes_action"), ["action"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_llm_configuration_changes_origin"), ["origin"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_llm_configuration_changes_created_at"), ["created_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    if "llm_configuration_changes" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    with op.batch_alter_table("llm_configuration_changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_configuration_changes_created_at"))
        batch_op.drop_index(batch_op.f("ix_llm_configuration_changes_origin"))
        batch_op.drop_index(batch_op.f("ix_llm_configuration_changes_action"))
        batch_op.drop_index(batch_op.f("ix_llm_configuration_changes_configuration_id"))
    op.drop_table("llm_configuration_changes")
