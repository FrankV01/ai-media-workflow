"""add workflows table and workflow-scoped AI configuration

Workflows become first-class rows: a named pipeline definition (ordered
block steps in the engine's list[str | dict] format) that also scopes all
AI configuration.

- workflows: name/slug/description/steps_json/media_model_name/is_enabled/
  is_default; exactly one default row ("Main", slug "main") is seeded with
  the formerly hardcoded DEFAULT_WORKFLOW step list.
- llm_role_configurations gains workflow_id (NOT NULL): profiles are now
  keyed (role, model, workflow). Existing rows backfill to "Main" — they
  ran under the only workflow that existed.
- media_model_configurations gains workflow_id (NOT NULL): keyed
  (workflow, block, backend, model); backfilled to "Main".
- jobs gains workflow_id (nullable FK): backfilled to "Main"; the engine
  stamps it on every new run.

Revision ID: b7e2f4a1c9d6
Revises: c6e1f0a4b8d3
Create Date: 2026-09-24 18:00:00

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2f4a1c9d6"
down_revision: str | Sequence[str] | None = "c6e1f0a4b8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pipeline formerly hardcoded as DEFAULT_WORKFLOW in app.web.routes —
# inlined so this migration stays immutable if the code changes later.
_MAIN_WORKFLOW_STEPS = [
    "art_director",
    "prompt_architect",
    "media_producer",
    "art_critic",
    "art_critic_report",
    {"on_good": [], "on_bad": [], "always": ["social_media_specialist", "social_media_report"]},
]


def _seed_main_workflow() -> int:
    """Insert the default 'Main' workflow row if absent and return its id."""
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT id FROM workflows WHERE slug = 'main'")).scalar()
    if existing is not None:
        return int(existing)
    bind.execute(
        sa.text(
            "INSERT INTO workflows "
            "(name, slug, description, steps_json, media_model_name, "
            " is_enabled, is_default, created_at, updated_at) "
            "VALUES (:name, :slug, :description, :steps_json, NULL, 1, 1, "
            "        datetime('now'), datetime('now'))"
        ),
        {
            "name": "Main",
            "slug": "main",
            "description": "Photorealistic AI-generated media pipeline",
            "steps_json": json.dumps(_MAIN_WORKFLOW_STEPS),
        },
    )
    return int(bind.execute(sa.text("SELECT id FROM workflows WHERE slug = 'main'")).scalar_one())


def _add_llm_workflow_scope(main_id: int, columns: set[str]) -> None:
    """Add/backfill llm_role_configurations.workflow_id and re-key the unique constraint."""
    if "workflow_id" not in columns:
        with op.batch_alter_table("llm_role_configurations", schema=None) as batch_op:
            batch_op.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text("UPDATE llm_role_configurations SET workflow_id = :wid").bindparams(wid=main_id)
        )
        with op.batch_alter_table("llm_role_configurations", schema=None) as batch_op:
            batch_op.alter_column("workflow_id", existing_type=sa.Integer(), nullable=False)
            batch_op.create_foreign_key(
                "fk_llm_role_configurations_workflow_id",
                "workflows",
                ["workflow_id"],
                ["id"],
            )
            batch_op.create_index(
                batch_op.f("ix_llm_role_configurations_workflow_id"),
                ["workflow_id"],
                unique=False,
            )
            batch_op.drop_constraint("uq_llm_role_configuration_role_model", type_="unique")
            batch_op.create_unique_constraint(
                "uq_llm_role_configuration_role_model_workflow",
                ["role_id", "model_name", "workflow_id"],
            )


def _add_media_workflow_scope(main_id: int, columns: set[str]) -> None:
    """Add/backfill media_model_configurations.workflow_id and re-key the unique constraint."""
    if "workflow_id" not in columns:
        with op.batch_alter_table("media_model_configurations", schema=None) as batch_op:
            batch_op.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text("UPDATE media_model_configurations SET workflow_id = :wid").bindparams(
                wid=main_id
            )
        )
        with op.batch_alter_table("media_model_configurations", schema=None) as batch_op:
            batch_op.alter_column("workflow_id", existing_type=sa.Integer(), nullable=False)
            batch_op.create_foreign_key(
                "fk_media_model_configurations_workflow_id",
                "workflows",
                ["workflow_id"],
                ["id"],
            )
            batch_op.create_index(
                batch_op.f("ix_media_model_configurations_workflow_id"),
                ["workflow_id"],
                unique=False,
            )
            batch_op.drop_constraint("uq_media_model_configuration_component", type_="unique")
            batch_op.create_unique_constraint(
                "uq_media_model_configuration_component",
                ["workflow_id", "block_name", "backend_name", "model_name"],
            )


def _add_job_workflow(main_id: int, columns: set[str]) -> None:
    """Add/backfill jobs.workflow_id (nullable — stamped by the engine on new runs)."""
    if "workflow_id" not in columns:
        with op.batch_alter_table("jobs", schema=None) as batch_op:
            batch_op.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_jobs_workflow_id",
                "workflows",
                ["workflow_id"],
                ["id"],
            )
            batch_op.create_index(batch_op.f("ix_jobs_workflow_id"), ["workflow_id"], unique=False)
        op.execute(sa.text("UPDATE jobs SET workflow_id = :wid").bindparams(wid=main_id))


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())

    if "workflows" not in existing_tables:
        op.create_table(
            "workflows",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("steps_json", sa.Text(), nullable=False),
            sa.Column("media_model_name", sa.String(length=255), nullable=True),
            sa.Column("is_enabled", sa.Boolean(), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
            sa.UniqueConstraint("slug"),
        )
        with op.batch_alter_table("workflows", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_workflows_slug"), ["slug"], unique=False)

    main_id = _seed_main_workflow()

    llm_columns = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("llm_role_configurations")
    }
    _add_llm_workflow_scope(main_id, llm_columns)

    media_columns = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("media_model_configurations")
    }
    _add_media_workflow_scope(main_id, media_columns)

    job_columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    _add_job_workflow(main_id, job_columns)


def downgrade() -> None:
    """Drop workflow scoping and the workflows table (mirror of upgrade)."""
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())

    if "jobs" in existing_tables:
        job_columns = {c["name"] for c in inspector.get_columns("jobs")}
        if "workflow_id" in job_columns:
            with op.batch_alter_table("jobs", schema=None) as batch_op:
                batch_op.drop_constraint("fk_jobs_workflow_id", type_="foreignkey")
                batch_op.drop_index("ix_jobs_workflow_id")
                batch_op.drop_column("workflow_id")

    if "media_model_configurations" in existing_tables:
        media_columns = {c["name"] for c in inspector.get_columns("media_model_configurations")}
        if "workflow_id" in media_columns:
            with op.batch_alter_table("media_model_configurations", schema=None) as batch_op:
                batch_op.drop_constraint("uq_media_model_configuration_component", type_="unique")
                batch_op.drop_constraint(
                    "fk_media_model_configurations_workflow_id", type_="foreignkey"
                )
                batch_op.drop_index("ix_media_model_configurations_workflow_id")
                batch_op.drop_column("workflow_id")
                batch_op.create_unique_constraint(
                    "uq_media_model_configuration_component",
                    ["block_name", "backend_name", "model_name"],
                )

    if "llm_role_configurations" in existing_tables:
        llm_columns = {c["name"] for c in inspector.get_columns("llm_role_configurations")}
        if "workflow_id" in llm_columns:
            with op.batch_alter_table("llm_role_configurations", schema=None) as batch_op:
                batch_op.drop_constraint(
                    "uq_llm_role_configuration_role_model_workflow", type_="unique"
                )
                batch_op.drop_constraint(
                    "fk_llm_role_configurations_workflow_id", type_="foreignkey"
                )
                batch_op.drop_index("ix_llm_role_configurations_workflow_id")
                batch_op.drop_column("workflow_id")
                batch_op.create_unique_constraint(
                    "uq_llm_role_configuration_role_model",
                    ["role_id", "model_name"],
                )

    if "workflows" in existing_tables:
        with op.batch_alter_table("workflows", schema=None) as batch_op:
            batch_op.drop_index("ix_workflows_slug")
        op.drop_table("workflows")
