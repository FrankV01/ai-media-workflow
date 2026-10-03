"""add storefront listing audit table

Creates storefront_listings — one row per listing-creation attempt for a
job's delivery archive on an external storefront (FourthWall digital
products today). Records the immutable listing payload (name, description,
price, publish flag), the uploaded archive artifact identity, the
storefront product id, status, and structured error fields. Binaries live
on the storefront; the table holds metadata only.

The fourthwall_listing block is opt-in — no stored workflow definitions
are modified.

Revision ID: g8d5b2e4f6a1
Revises: f7c3a9e1b5d8
Create Date: 2026-10-03 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g8d5b2e4f6a1"
down_revision: str | Sequence[str] | None = "f7c3a9e1b5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "storefront_listings" not in existing_tables:
        op.create_table(
            "storefront_listings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=True),
            sa.Column("job_step_id", sa.Integer(), nullable=True),
            sa.Column("delivery_archive_id", sa.Integer(), nullable=True),
            sa.Column("storefront", sa.String(length=50), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.Enum("PENDING", "RUNNING", "READY", "FAILED", name="storefrontlistingstatus"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("price_usd", sa.Float(), nullable=True),
            sa.Column("publish_on_create", sa.Boolean(), nullable=False),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("archive_name", sa.String(length=255), nullable=True),
            sa.Column("archive_byte_size", sa.Integer(), nullable=True),
            sa.Column("archive_sha256", sa.String(length=64), nullable=True),
            sa.Column("product_id", sa.String(length=255), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("error_type", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
            sa.ForeignKeyConstraint(["job_step_id"], ["job_steps.id"]),
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"]),
            sa.ForeignKeyConstraint(["delivery_archive_id"], ["delivery_archives.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "attempt", name="uq_storefront_listing_job_attempt"),
        )
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_storefront_listings_job_id"), ["job_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_storefront_listings_workflow_id"), ["workflow_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_storefront_listings_job_step_id"), ["job_step_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_storefront_listings_delivery_archive_id"),
                ["delivery_archive_id"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "storefront_listings" in existing_tables:
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_storefront_listings_delivery_archive_id"))
            batch_op.drop_index(batch_op.f("ix_storefront_listings_job_step_id"))
            batch_op.drop_index(batch_op.f("ix_storefront_listings_workflow_id"))
            batch_op.drop_index(batch_op.f("ix_storefront_listings_job_id"))
        op.drop_table("storefront_listings")
