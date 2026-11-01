"""add product descriptions table

Creates product_descriptions — one row per product_description.md artifact
produced by the `product_describer` block. Records the scanned directory,
the JSON list of source images analyzed (the collection), the Markdown
content itself, and the written file path. Linked JobStep (and its
RoleExecution rows) remain the audit authority for the LLM call.

Revision ID: o3a8e6f1c4b7
Revises: n7f1c3d5e9a2
Create Date: 2026-10-31 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o3a8e6f1c4b7"
down_revision: str | Sequence[str] | None = "n7f1c3d5e9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "product_descriptions" not in existing_tables:
        op.create_table(
            "product_descriptions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("job_step_id", sa.Integer(), nullable=False),
            sa.Column("role_execution_id", sa.Integer(), nullable=True),
            sa.Column("directory_path", sa.Text(), nullable=False),
            sa.Column("image_paths", sa.Text(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
            sa.ForeignKeyConstraint(["job_step_id"], ["job_steps.id"]),
            sa.ForeignKeyConstraint(["role_execution_id"], ["role_executions.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("product_descriptions", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_product_descriptions_job_id"), ["job_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_product_descriptions_job_step_id"),
                ["job_step_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_product_descriptions_role_execution_id"),
                ["role_execution_id"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "product_descriptions" in existing_tables:
        with op.batch_alter_table("product_descriptions", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_product_descriptions_role_execution_id"))
            batch_op.drop_index(batch_op.f("ix_product_descriptions_job_step_id"))
            batch_op.drop_index(batch_op.f("ix_product_descriptions_job_id"))
        op.drop_table("product_descriptions")
