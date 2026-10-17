"""add job quality control

Revision ID: k8f1d4h6i7j3
Revises: j7e0c3g5h6i2
Create Date: 2026-10-14 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k8f1d4h6i7j3"
down_revision: str | Sequence[str] | None = "j7e0c3g5h6i2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "job_quality_controls" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "job_quality_controls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column(
            "rating",
            sa.Enum("GOOD", "NEUTRAL", "BAD", name="jobqualityrating"),
            nullable=False,
        ),
        sa.Column("comment_markdown", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        op.f("ix_job_quality_controls_job_id"),
        "job_quality_controls",
        ["job_id"],
        unique=True,
    )


def downgrade() -> None:
    if "job_quality_controls" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.drop_index(op.f("ix_job_quality_controls_job_id"), table_name="job_quality_controls")
    op.drop_table("job_quality_controls")
