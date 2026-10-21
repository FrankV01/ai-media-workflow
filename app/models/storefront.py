"""app.models.storefront — Storefront listing audit model

- StorefrontListing:  One listing-creation attempt for a job's delivery
                      archive on an external storefront (FourthWall today).
                      Records the immutable listing payload (name,
                      description, price, publish flag), the archive artifact
                      identity that was uploaded, the storefront product id,
                      status, and structured error fields. Binaries live on
                      the storefront — SQLite holds metadata only.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StorefrontListingStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class StorefrontListing(Base):
    """One attempted (or completed) storefront listing for a job."""

    __tablename__ = "storefront_listings"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_storefront_listing_job_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id"), nullable=True, index=True
    )
    job_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_steps.id"), nullable=True, index=True
    )
    # The delivery archive whose ZIP was uploaded as the digital file
    delivery_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_archives.id"), nullable=True, index=True
    )
    # The R2 upload hosting the archive the link document points at
    cloudflare_upload_id: Mapped[int | None] = mapped_column(
        ForeignKey("cloudflare_r2_uploads.id"), nullable=True, index=True
    )
    storefront: Mapped[str] = mapped_column(String(50), default="fourthwall")
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[StorefrontListingStatus] = mapped_column(
        Enum(StorefrontListingStatus), default=StorefrontListingStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Immutable snapshot of the listing payload sent to the storefront
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    publish_on_create: Mapped[bool] = mapped_column(Boolean, default=False)
    settings_json: Mapped[str] = mapped_column(Text, default="")

    # Identity of the uploaded archive artifact
    archive_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Storefront-assigned identity + misc response details (JSON)
    product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Public product page on the selling site — set manually from the
    # Pinterest page when the API can't resolve it; preferred link for pins.
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Identity of the link document uploaded as the digital file
    link_document_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    link_document_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)


from app.models.cloudflare import CloudflareUpload  # noqa: E402, F401
from app.models.delivery import DeliveryArchive  # noqa: E402, F401
from app.models.job import JobStep  # noqa: E402, F401
from app.models.workflow import Workflow  # noqa: E402, F401
