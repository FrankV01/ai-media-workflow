"""add scheduled pinterest pins table

Creates scheduled_pins — one row per scheduled (or published) Pinterest pin
promoting a FourthWall product listing. Records the pin payload (board,
title, description, link, source image path), the scheduled publish time,
the Pinterest pin identity, status, and structured error fields. Images are
uploaded to Pinterest base64-encoded at publish time; the table holds
metadata only.

Scheduling rules (one pending pin per listing, post-publish cooldown) are
enforced in app/services/pinterest.py — not by table constraints.

Revision ID: l9g2h4i6j8k5
Revises: k8f1d4h6i7j3
Create Date: 2026-10-20 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l9g2h4i6j8k5"
down_revision: str | Sequence[str] | None = "k8f1d4h6i7j3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "scheduled_pins" not in existing_tables:
        op.create_table(
            "scheduled_pins",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("storefront_listing_id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.Enum(
                    "SCHEDULED",
                    "PUBLISHING",
                    "PUBLISHED",
                    "FAILED",
                    "CANCELED",
                    name="scheduledpinstatus",
                ),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("board_id", sa.String(length=255), nullable=False),
            sa.Column("board_name", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=100), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=False),
            sa.Column("link", sa.Text(), nullable=False),
            sa.Column("image_path", sa.Text(), nullable=False),
            sa.Column("pinterest_pin_id", sa.String(length=255), nullable=True),
            sa.Column("pinterest_pin_url", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("error_type", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["storefront_listing_id"], ["storefront_listings.id"]),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("scheduled_pins", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_scheduled_pins_storefront_listing_id"),
                ["storefront_listing_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_scheduled_pins_job_id"), ["job_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_scheduled_pins_scheduled_for"),
                ["scheduled_for"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "scheduled_pins" in existing_tables:
        with op.batch_alter_table("scheduled_pins", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_scheduled_pins_scheduled_for"))
            batch_op.drop_index(batch_op.f("ix_scheduled_pins_job_id"))
            batch_op.drop_index(batch_op.f("ix_scheduled_pins_storefront_listing_id"))
        op.drop_table("scheduled_pins")
