"""
app.models.block — Generic per-workflow block configuration profile

BlockConfiguration: Mutable settings profile for one (workflow, block)
pair — the non-AI analog of LlmRoleConfiguration / MediaModelConfiguration.
settings_json stores a typed settings dict owned by the block (e.g.
BackgroundRemovalSettings for background_remover). Rows still on code
defaults (uses_code_defaults) warn on every use until customized.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BlockConfiguration(Base):
    """
    Mutable behavior profile for one (workflow, block_name) pair.

    Unlike media profiles there is no backend/model selector — one row per
    block per workflow. The block owns the semantics of settings_json.
    """

    __tablename__ = "block_configurations"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "block_name",
            name="uq_block_configuration_component",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    block_name: Mapped[str] = mapped_column(String(255))
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

    workflow: Mapped["Workflow"] = relationship(back_populates="block_configurations")


# Avoid circular import — import at module level for relationship resolution
from app.models.workflow import Workflow  # noqa: E402, F401
