"""app.models.pinterest — Scheduled Pinterest pin audit model

- ScheduledPin:   One scheduled (or published) Pinterest pin promoting a
                  FourthWall product (StorefrontListing). Records the exact
                  pin payload (board, title, description, link, source image),
                  the scheduled publish time, the Pinterest-assigned pin
                  identity, status, and structured error fields. Pin images
                  are sent to Pinterest base64-encoded at publish time —
                  SQLite holds metadata only.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduledPinStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELED = "canceled"


class ScheduledPin(Base):
    """One scheduled/published pin attempt for a storefront product."""

    __tablename__ = "scheduled_pins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    storefront_listing_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_listings.id"), index=True
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[ScheduledPinStatus] = mapped_column(
        Enum(ScheduledPinStatus), default=ScheduledPinStatus.SCHEDULED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Immutable snapshot of the pin payload sent to Pinterest
    board_id: Mapped[str] = mapped_column(String(255), default="")
    board_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(String(500), default="")
    link: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str] = mapped_column(Text, default="")

    # Pinterest-assigned identity
    pinterest_pin_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinterest_pin_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)


from app.models.job import Job  # noqa: E402, F401
from app.models.storefront import StorefrontListing  # noqa: E402, F401
