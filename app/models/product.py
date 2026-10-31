"""
app.models.product — Product description records

- ProductDescription: One product-description artifact produced by the
  `product_describer` block within a job step. Stores the scanned
  directory, the image collection analyzed, the Markdown content itself,
  and the file path of the written product_description.md. The linked
  JobStep (and its RoleExecution rows) remain the audit authority for
  the LLM call that produced it.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProductDescription(Base):
    """
    A stored product description generated from a directory of images.

    image_paths (JSON list) is the collection of source images analyzed;
    content is the full Markdown product description; file_path points at
    the product_description.md written into the scanned directory.
    """

    __tablename__ = "product_descriptions"
    __table_args__ = (
        UniqueConstraint("source_job_id", "version", name="uq_description_source_version"),
        UniqueConstraint("request_id", name="uq_description_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", name="fk_description_source_job"), index=True
    )
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_description_requests.id", name="fk_description_request")
    )
    version: Mapped[int | None] = mapped_column(Integer)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    configuration_snapshot: Mapped[str | None] = mapped_column(Text)
    export_error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_step_id: Mapped[int] = mapped_column(ForeignKey("job_steps.id"), index=True)
    role_execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("role_executions.id"), nullable=True, index=True
    )
    directory_path: Mapped[str] = mapped_column(Text)
    image_paths: Mapped[str] = mapped_column(Text)  # JSON list[str]
    file_path: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="completed")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    job: Mapped["Job"] = relationship(foreign_keys=[job_id])
    source_job: Mapped["Job | None"] = relationship(foreign_keys=[source_job_id])
    job_step: Mapped["JobStep"] = relationship()
    role_execution: Mapped["RoleExecution | None"] = relationship()


class ProductDescriptionRequest(Base):
    __tablename__ = "product_description_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    generation_job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    source_job: Mapped["Job"] = relationship(foreign_keys=[source_job_id])
    generation_job: Mapped["Job"] = relationship(foreign_keys=[generation_job_id])


# Avoid circular import — import at module level for relationship resolution
from app.models.creative import RoleExecution  # noqa: E402, F401
from app.models.job import Job, JobStep  # noqa: E402, F401
