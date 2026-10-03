"""app.models.cloudflare — Cloudflare R2 upload audit model

- CloudflareUpload: One upload attempt for a job's delivery archive to
                    Cloudflare R2. Records the immutable upload target
                    (bucket, object key, public URL), the archive artifact
                    identity that was uploaded, the S3 ETag, status, and
                    structured error fields. Binaries live in R2 — SQLite
                    holds metadata only.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CloudflareUploadStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class CloudflareUpload(Base):
    """One attempted (or completed) R2 upload for a job's delivery archive."""

    __tablename__ = "cloudflare_r2_uploads"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_cloudflare_upload_job_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id"), nullable=True, index=True
    )
    job_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_steps.id"), nullable=True, index=True
    )
    # The delivery archive whose ZIP was uploaded to R2
    delivery_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_archives.id"), nullable=True, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[CloudflareUploadStatus] = mapped_column(
        Enum(CloudflareUploadStatus), default=CloudflareUploadStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Immutable snapshot of the settings used for this attempt
    settings_json: Mapped[str] = mapped_column(Text, default="")

    # Identity of the uploaded archive artifact
    archive_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # R2-assigned identity + public access coordinates
    bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    object_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)


from app.models.delivery import DeliveryArchive  # noqa: E402, F401
from app.models.job import JobStep  # noqa: E402, F401
from app.models.workflow import Workflow  # noqa: E402, F401
