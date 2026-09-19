"""
app.models.creative — Creative agency data models (3NF)

Normalized schema for the role-based creative workflow:

- CreativeRole:     Defines a reusable role (Art Director, Prompt Architect, etc.)
                    with its system prompt, suggested next role, and metadata.
                    One role can be used across many job executions.

- RoleExecution:    One invocation of a role within a specific job step.
                    Links a CreativeRole to a JobStep.  Captures the input brief,
                    output deliverable, LLM model used, and token counts.

- Message:          Individual messages in the LLM conversation for a given
                    role execution.  Provides a full audit trail of the
                    system prompt, user input, and assistant response.

Relationships:
    CreativeRole  1──M  RoleExecution  M──1  JobStep
    RoleExecution 1──M  Message

Future tables (planned):
- Artifact:         Binary/file outputs (images, audio) linked to a RoleExecution
- ReviewFeedback:   Human-in-the-loop approval/rejection per execution
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ── CreativeRole ─────────────────────────────────────────────────────────


class CreativeRole(Base):
    """
    A reusable role definition in the creative pipeline.

    Each role has a system prompt that shapes LLM behavior and an optional
    pointer to the suggested next role in the chain.
    """

    __tablename__ = "creative_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    output_format: Mapped[str] = mapped_column(
        String(50), default="text"
    )  # text, json, markdown
    suggested_next_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    executions: Mapped[list["RoleExecution"]] = relationship(
        back_populates="role", cascade="all, delete"
    )


# ── RoleExecution ────────────────────────────────────────────────────────


class RoleExecution(Base):
    """
    A single invocation of a creative role within a job step.

    Captures the input brief, the LLM-generated output, model metadata,
    and token usage for cost tracking.
    """

    __tablename__ = "role_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_step_id: Mapped[int] = mapped_column(ForeignKey("job_steps.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("creative_roles.id"), index=True)

    input_brief: Mapped[str] = mapped_column(Text)
    output_deliverable: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_next_role: Mapped[str | None] = mapped_column(String(255), nullable=True)

    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped["CreativeRole"] = relationship(back_populates="executions")
    job_step: Mapped["JobStep"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="execution", cascade="all, delete"
    )


# ── Message ──────────────────────────────────────────────────────────────


class MessageRole(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(Base):
    """
    An individual message in the LLM conversation for a role execution.

    Provides full audit trail: the system prompt sent, the user brief,
    and the assistant's response are each stored as separate rows.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("role_executions.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    execution: Mapped["RoleExecution"] = relationship(back_populates="messages")


# Avoid circular import — import at module level for relationship resolution
from app.models.job import JobStep  # noqa: E402, F401
