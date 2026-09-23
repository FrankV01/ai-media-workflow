"""
app.models.creative — Creative agency data models (3NF)

Normalized schema for the role-based creative workflow:

- CreativeRole:         Stable identity metadata for a reusable role
                        (Art Director, Prompt Architect, etc.). Behavior
                        configuration lives in LlmRoleConfiguration.

- LlmRoleConfiguration: Mutable per-(role, model) behavior profile —
                        system prompt, temperature, max_tokens, thinking.
                        Rows seeded from code defaults carry
                        uses_code_defaults=True and trigger a warning on
                        every use until customized via UI/API. Existing rows
                        are never overwritten by resolution — only explicit
                        save/reset mutations change them.

- LlmConfigurationChange:
                        Audit trail for explicit profile mutations. Each
                        save/reset records action, origin (service /
                        web_settings / rest_api), and JSON before/after
                        snapshots of the mutable profile fields.

- RoleExecution:        One attempted role invocation within a specific job
                        step. Copies the exact prompt/model/parameters used —
                        those copied fields (not the mutable configuration FK)
                        are the audit authority.

- Message:              Individual messages in the LLM conversation for a
                        given role execution.

Relationships:
    CreativeRole 1──M LlmRoleConfiguration
    CreativeRole 1──M RoleExecution  M──1 JobStep
    LlmRoleConfiguration 1──M RoleExecution
    LlmRoleConfiguration 1──M LlmConfigurationChange
    RoleExecution 1──M Message
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# ── CreativeRole ─────────────────────────────────────────────────────────


class CreativeRole(Base):
    """
    Stable identity metadata for a reusable role in the creative pipeline.

    Behavior configuration (system prompt, temperature, …) lives in
    LlmRoleConfiguration rows keyed by (role_id, model_name).
    """

    __tablename__ = "creative_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    output_format: Mapped[str] = mapped_column(String(50), default="text")  # text, json, markdown
    suggested_next_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    configurations: Mapped[list["LlmRoleConfiguration"]] = relationship(
        back_populates="role", cascade="all, delete"
    )
    executions: Mapped[list["RoleExecution"]] = relationship(
        back_populates="role", cascade="all, delete"
    )


# ── LlmRoleConfiguration ────────────────────────────────────────────────


class LlmRoleConfiguration(Base):
    """
    Mutable behavior profile for one (role, model) pair.

    Profiles are keyed by role + model name; changing LLM_MODEL selects a
    different profile. Rows still on code defaults (uses_code_defaults=True)
    produce a persisted warning on every execution until customized.
    """

    __tablename__ = "llm_role_configurations"
    __table_args__ = (
        UniqueConstraint("role_id", "model_name", name="uq_llm_role_configuration_role_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("creative_roles.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(255), index=True)
    system_prompt: Mapped[str] = mapped_column(Text)
    temperature: Mapped[float] = mapped_column(Float)
    max_tokens: Mapped[int] = mapped_column(Integer)
    enable_thinking: Mapped[bool] = mapped_column(Boolean)
    uses_code_defaults: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    role: Mapped["CreativeRole"] = relationship(back_populates="configurations")
    executions: Mapped[list["RoleExecution"]] = relationship(back_populates="configuration")
    changes: Mapped[list["LlmConfigurationChange"]] = relationship(
        back_populates="configuration", cascade="all, delete-orphan"
    )


# ── LlmConfigurationChange ───────────────────────────────────────────────


class LlmConfigurationChange(Base):
    """
    Audit record for one explicit LLM profile mutation.

    Written only by save/reset calls (never by resolve or page loads), in the
    same transaction as the profile update. before_snapshot/after_snapshot
    hold JSON (serialized as Text for SQLite portability) of the mutable
    profile fields; an event is recorded even when values are unchanged
    because the explicit user action itself matters.
    """

    __tablename__ = "llm_configuration_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("llm_role_configurations.id"), index=True
    )
    action: Mapped[str] = mapped_column(String(50), index=True)
    origin: Mapped[str] = mapped_column(String(50), index=True)
    before_snapshot: Mapped[str] = mapped_column(Text)
    after_snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    configuration: Mapped["LlmRoleConfiguration"] = relationship(back_populates="changes")


# ── RoleExecution ────────────────────────────────────────────────────────


class RoleExecution(Base):
    """
    A single attempted creative-role invocation within a job step.

    The copied system_prompt/model/parameter fields are the immutable audit
    authority; configuration_id only points back at the (mutable) profile
    that produced them.
    """

    __tablename__ = "role_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_step_id: Mapped[int] = mapped_column(ForeignKey("job_steps.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("creative_roles.id"), index=True)
    configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_role_configurations.id"), nullable=True, index=True
    )
    configuration_source: Mapped[str] = mapped_column(
        String(50), default="legacy"
    )  # legacy | code_default | custom
    system_prompt: Mapped[str] = mapped_column(Text, default="")

    input_brief: Mapped[str] = mapped_column(Text)
    output_deliverable: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_next_role: Mapped[str | None] = mapped_column(String(255), nullable=True)

    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(50), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="completed")
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped["CreativeRole"] = relationship(back_populates="executions")
    configuration: Mapped["LlmRoleConfiguration | None"] = relationship(back_populates="executions")
    job_step: Mapped["JobStep"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="execution", cascade="all, delete"
    )


# ── Message ──────────────────────────────────────────────────────────────


class MessageRole(enum.StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(Base):
    """
    An individual message sent or received during a role execution.

    Stores the effective system prompt and user input for every attempted call,
    plus the assistant response when one was returned.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("role_executions.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    execution: Mapped["RoleExecution"] = relationship(back_populates="messages")


# Avoid circular import — import at module level for relationship resolution
from app.models.job import JobStep  # noqa: E402, F401
