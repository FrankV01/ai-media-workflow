"""add product_url to storefront_listings

Adds storefront_listings.product_url — the product's public page on the
selling site (e.g. https://syntheticmuse.ai/products/<slug>). Set manually
from the Pinterest page when the FourthWall API can't resolve it; it is
the preferred destination link for scheduled pins.

Revision ID: m0b5d8e2f4a6
Revises: l9g2h4i6j8k5
Create Date: 2026-10-20 13:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m0b5d8e2f4a6"
down_revision: str | Sequence[str] | None = "l9g2h4i6j8k5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    columns = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("storefront_listings")
    }
    if "product_url" not in columns:
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            batch_op.add_column(sa.Column("product_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    columns = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("storefront_listings")
    }
    if "product_url" in columns:
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            batch_op.drop_column("product_url")
