"""
app.models.media — Media generation configuration and audit models

- MediaModelConfiguration:   Mutable per-(block, backend, model) behavior
                             profile. settings_json stores typed request
                             defaults plus backend-specific workflow fields.
                             Rows still on code defaults (uses_code_defaults)
                             warn on every use until customized.

- MediaGenerationExecution:  One attempted image-generation request within a
                             job step. The copied prompts/settings snapshot
                             (not the mutable configuration FK) are the audit
                             authority.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MediaModelConfiguration(Base):
    """
    Mutable behavior profile for one (block, backend, model) triple.

    Keyed by (block_name, backend_name, model_name); model_name is the base
    checkpoint for ComfyUI and the stable string 'placeholder' for the
    placeholder backend. settings_json holds typed request defaults
    (width/height/cfg_scale/steps/sampler/scheduler/clip_skip) plus
    backend-specific workflow fields (refiner/upscale models and sampling).
    """

    __tablename__ = "media_model_configurations"
    __table_args__ = (
        UniqueConstraint(
            "block_name",
            "backend_name",
            "model_name",
            name="uq_media_model_configuration_component",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_name: Mapped[str] = mapped_column(String(255))
    backend_name: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(255))
    settings_json: Mapped[str] = mapped_column(Text)
    uses_code_defaults: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    executions: Mapped[list["MediaGenerationExecution"]] = relationship(
        back_populates="configuration"
    )


class MediaGenerationExecution(Base):
    """
    One attempted image-generation request within a job step, including failures.

    settings_snapshot is the exact resolved profile JSON used for the request;
    it, not configuration_id, is the audit authority for what was dispatched.
    """

    __tablename__ = "media_generation_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_step_id: Mapped[int] = mapped_column(ForeignKey("job_steps.id"), index=True)
    configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_model_configurations.id"), nullable=True, index=True
    )
    configuration_source: Mapped[str] = mapped_column(String(50), default="code_default")
    block_name: Mapped[str] = mapped_column(String(255))
    backend_name: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(255))
    variant_name: Mapped[str] = mapped_column(String(255), default="main")

    positive_prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    positive_refiner_prompt: Mapped[str] = mapped_column(Text, default="")
    negative_refiner_prompt: Mapped[str] = mapped_column(Text, default="")
    settings_snapshot: Mapped[str] = mapped_column(Text)

    seed_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backend_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    image_paths: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    status: Mapped[str] = mapped_column(String(50), default="running")
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    configuration: Mapped["MediaModelConfiguration | None"] = relationship(
        back_populates="executions"
    )
    job_step: Mapped["JobStep"] = relationship()


# Avoid circular import — import at module level for relationship resolution
from app.models.job import JobStep  # noqa: E402, F401
