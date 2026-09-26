"""
app.models.workflow — Workflow definition model

A workflow is a named, persisted pipeline definition: an ordered list of
block steps in the engine's format (block names and verdict-routing dicts)
plus the flags that control how it can be used.

- Exactly one row is the default (is_default=True): it supplies the
  configuration scope for ad-hoc runs that don't name a workflow and the
  fallback steps for runs that supply neither.
- is_enabled=False blocks new runs (routes reject them) while keeping the
  definition, its configuration profiles, and its job history intact.
- media_model_name optionally overrides which (block, backend, model) media
  profile the Media Producer resolves — e.g. an illustration workflow can
  target a different checkpoint than the env-selected default.

Every pipeline run (Job) belongs to a workflow, and all AI configuration
profiles (LlmRoleConfiguration, MediaModelConfiguration) are scoped to one.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Workflow(Base):
    """A named, persisted pipeline definition and configuration scope."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON list[str | dict] — the engine's step format (block names +
    # verdict-routing dicts with on_good/on_bad/always branches)
    steps_json: Mapped[str] = mapped_column(Text)
    # Optional per-workflow media model key (e.g. a different ComfyUI
    # checkpoint); None → the env-selected model for the active backend
    media_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="workflow")
    llm_configurations: Mapped[list["LlmRoleConfiguration"]] = relationship(
        back_populates="workflow"
    )
    media_configurations: Mapped[list["MediaModelConfiguration"]] = relationship(
        back_populates="workflow"
    )
    block_configurations: Mapped[list["BlockConfiguration"]] = relationship(
        back_populates="workflow"
    )


# Avoid circular import — import at module level for relationship resolution
from app.models.block import BlockConfiguration  # noqa: E402, F401
from app.models.creative import LlmRoleConfiguration  # noqa: E402, F401
from app.models.job import Job  # noqa: E402, F401
from app.models.media import MediaModelConfiguration  # noqa: E402, F401
