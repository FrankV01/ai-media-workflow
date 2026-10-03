"""app.models.delivery — Customer delivery archive audit models

- DeliveryArchive:      One archive-creation attempt for a job. Records the
                        immutable delivery settings snapshot, target artifact
                        identity (path/name/size/SHA-256/manifest), status,
                        and structured error fields. ZIP/image binaries stay
                        on disk — SQLite holds metadata only.
- DeliveryArchiveEntry: One inventoried file or directory inside the archive
                        (including generated `_delivery/` additions), with
                        size, SHA-256, and — for Markdown — the exact UTF-8
                        contents that were packaged.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DeliveryArchiveStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class DeliveryArchive(Base):
    """One attempted (or completed) customer delivery archive for a job."""

    __tablename__ = "delivery_archives"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_delivery_archive_job_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id"), nullable=True, index=True
    )
    job_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_steps.id"), nullable=True, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[DeliveryArchiveStatus] = mapped_column(
        Enum(DeliveryArchiveStatus), default=DeliveryArchiveStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source_dir: Mapped[str] = mapped_column(Text)
    settings_json: Mapped[str] = mapped_column(Text)

    archive_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    entries: Mapped[list["DeliveryArchiveEntry"]] = relationship(
        back_populates="archive", cascade="all, delete-orphan"
    )


class DeliveryArchiveEntry(Base):
    """One inventoried path inside a delivery archive."""

    __tablename__ = "delivery_archive_entries"
    __table_args__ = (
        UniqueConstraint("archive_id", "path", name="uq_delivery_archive_entry_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("delivery_archives.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    entry_type: Mapped[str] = mapped_column(String(10))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    markdown_contents: Mapped[str | None] = mapped_column(Text, nullable=True)

    archive: Mapped["DeliveryArchive"] = relationship(back_populates="entries")


from app.models.job import JobStep  # noqa: E402, F401
