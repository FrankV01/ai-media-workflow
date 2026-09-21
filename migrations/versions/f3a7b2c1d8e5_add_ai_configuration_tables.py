"""add AI configuration tables and immutable execution snapshots

Splits mutable AI behavior configuration out of CreativeRole into
LlmRoleConfiguration (keyed role + model) and adds media model profiles
plus per-request media generation audit records.

- RoleExecution gains configuration_id / configuration_source / system_prompt.
  system_prompt is backfilled from each execution's SYSTEM message so the
  copied column becomes the audit authority, then made non-null; existing
  executions are marked configuration_source='legacy'.
- CreativeRole drops system_prompt/model_override/temperature (last-run
  cache, not user configuration) — only after the backfill.
- Job gains warnings (JSON list[str]); generated_assets is added when absent —
  older databases obtained it via the former startup create_all, not a migration.

Revision ID: f3a7b2c1d8e5
Revises: e2f5a1c9d7b4
Create Date: 2026-09-21 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7b2c1d8e5"
down_revision: str | Sequence[str] | None = "e2f5a1c9d7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "llm_role_configurations" not in existing_tables:
        op.create_table(
            "llm_role_configurations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("model_name", sa.String(length=255), nullable=False),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column("temperature", sa.Float(), nullable=False),
            sa.Column("max_tokens", sa.Integer(), nullable=False),
            sa.Column("enable_thinking", sa.Boolean(), nullable=False),
            sa.Column("uses_code_defaults", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["role_id"], ["creative_roles.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "role_id", "model_name", name="uq_llm_role_configuration_role_model"
            ),
        )
        with op.batch_alter_table("llm_role_configurations", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_llm_role_configurations_role_id"), ["role_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_llm_role_configurations_model_name"), ["model_name"], unique=False
            )

    if "media_model_configurations" not in existing_tables:
        op.create_table(
            "media_model_configurations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("block_name", sa.String(length=255), nullable=False),
            sa.Column("backend_name", sa.String(length=100), nullable=False),
            sa.Column("model_name", sa.String(length=255), nullable=False),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("uses_code_defaults", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "block_name",
                "backend_name",
                "model_name",
                name="uq_media_model_configuration_component",
            ),
        )

    if "media_generation_executions" not in existing_tables:
        op.create_table(
            "media_generation_executions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_step_id", sa.Integer(), nullable=False),
            sa.Column("configuration_id", sa.Integer(), nullable=True),
            sa.Column("configuration_source", sa.String(length=50), nullable=False),
            sa.Column("block_name", sa.String(length=255), nullable=False),
            sa.Column("backend_name", sa.String(length=100), nullable=False),
            sa.Column("model_name", sa.String(length=255), nullable=False),
            sa.Column("variant_name", sa.String(length=255), nullable=False),
            sa.Column("positive_prompt", sa.Text(), nullable=False),
            sa.Column("negative_prompt", sa.Text(), nullable=False),
            sa.Column("positive_refiner_prompt", sa.Text(), nullable=False),
            sa.Column("negative_refiner_prompt", sa.Text(), nullable=False),
            sa.Column("settings_snapshot", sa.Text(), nullable=False),
            sa.Column("seed_used", sa.Integer(), nullable=True),
            sa.Column("backend_metadata", sa.Text(), nullable=True),
            sa.Column("image_paths", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("error_type", sa.String(length=255), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["job_step_id"], ["job_steps.id"]),
            sa.ForeignKeyConstraint(["configuration_id"], ["media_model_configurations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("media_generation_executions", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_media_generation_executions_job_step_id"),
                ["job_step_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_media_generation_executions_configuration_id"),
                ["configuration_id"],
                unique=False,
            )

    jobs_columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("warnings", sa.Text(), nullable=True))
        if "generated_assets" not in jobs_columns:
            batch_op.add_column(sa.Column("generated_assets", sa.Text(), nullable=True))

    with op.batch_alter_table("role_executions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("configuration_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "configuration_source",
                sa.String(length=50),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(sa.Column("system_prompt", sa.Text(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_role_executions_configuration_id"),
            ["configuration_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_role_executions_configuration_id",
            "llm_role_configurations",
            ["configuration_id"],
            ["id"],
        )

    # Backfill the immutable prompt snapshot from each execution's SYSTEM message
    op.execute(
        """
        UPDATE role_executions
        SET system_prompt = (
            SELECT content FROM messages
            WHERE messages.execution_id = role_executions.id
              AND messages.role = 'SYSTEM'
            ORDER BY ordinal
            LIMIT 1
        )
        """
    )
    op.execute("UPDATE role_executions SET system_prompt = '' WHERE system_prompt IS NULL")

    with op.batch_alter_table("role_executions", schema=None) as batch_op:
        batch_op.alter_column("system_prompt", existing_type=sa.Text(), nullable=False)

    # Drop the last-run cache columns from CreativeRole only after backfill
    with op.batch_alter_table("creative_roles", schema=None) as batch_op:
        batch_op.drop_column("system_prompt")
        batch_op.drop_column("model_override")
        batch_op.drop_column("temperature")


def downgrade() -> None:
    """Downgrade schema."""
    # Restore CreativeRole's former columns (prompt backfilled from latest execution)
    with op.batch_alter_table("creative_roles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("system_prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("model_override", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("temperature", sa.Float(), nullable=True))

    op.execute(
        """
        UPDATE creative_roles
        SET system_prompt = COALESCE(
            (
                SELECT role_executions.system_prompt FROM role_executions
                WHERE role_executions.role_id = creative_roles.id
                ORDER BY role_executions.id DESC
                LIMIT 1
            ),
            ''
        ),
        model_override = (
            SELECT role_executions.model_used FROM role_executions
            WHERE role_executions.role_id = creative_roles.id
            ORDER BY role_executions.id DESC
            LIMIT 1
        ),
        temperature = (
            SELECT role_executions.temperature FROM role_executions
            WHERE role_executions.role_id = creative_roles.id
            ORDER BY role_executions.id DESC
            LIMIT 1
        )
        """
    )
    with op.batch_alter_table("creative_roles", schema=None) as batch_op:
        batch_op.alter_column("system_prompt", existing_type=sa.Text(), nullable=False)

    with op.batch_alter_table("role_executions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_role_executions_configuration_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_role_executions_configuration_id"))
        batch_op.drop_column("system_prompt")
        batch_op.drop_column("configuration_source")
        batch_op.drop_column("configuration_id")

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("warnings")
        batch_op.drop_column("generated_assets")

    with op.batch_alter_table("media_generation_executions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_generation_executions_configuration_id"))
        batch_op.drop_index(batch_op.f("ix_media_generation_executions_job_step_id"))
    op.drop_table("media_generation_executions")

    op.drop_table("media_model_configurations")

    with op.batch_alter_table("llm_role_configurations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_role_configurations_model_name"))
        batch_op.drop_index(batch_op.f("ix_llm_role_configurations_role_id"))
    op.drop_table("llm_role_configurations")
