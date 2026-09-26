"""add block_configurations table for generic per-workflow block profiles

Stores mutable settings profiles for non-LLM, non-media blocks (e.g.
background_remover) keyed by (workflow_id, block_name) — the generic
counterpart to media_model_configurations.

Revision ID: c9d3e5f7a1b2
Revises: b7e2f4a1c9d6
Create Date: 2026-09-26 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d3e5f7a1b2"
down_revision: str | Sequence[str] | None = "b7e2f4a1c9d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "block_configurations" not in existing_tables:
        op.create_table(
            "block_configurations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=False),
            sa.Column("block_name", sa.String(length=255), nullable=False),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("uses_code_defaults", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workflow_id",
                "block_name",
                name="uq_block_configuration_component",
            ),
        )
        with op.batch_alter_table("block_configurations", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_block_configurations_workflow_id"),
                ["workflow_id"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("block_configurations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_block_configurations_workflow_id"))
    op.drop_table("block_configurations")
