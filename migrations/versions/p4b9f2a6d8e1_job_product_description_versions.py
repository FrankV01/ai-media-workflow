"""Version product descriptions by source job."""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "p4b9f2a6d8e1"
down_revision: str | Sequence[str] | None = "o3a8e6f1c4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from app.config import settings
    from app.pipeline.naming import output_subdir

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "product_description_requests" not in tables:
        op.create_table(
            "product_description_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
            sa.Column("generation_job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("generation_job_id"),
        )
        op.create_index(
            "ix_product_description_requests_source_job_id",
            "product_description_requests",
            ["source_job_id"],
        )
    columns = {c["name"] for c in sa.inspect(bind).get_columns("product_descriptions")}
    with op.batch_alter_table("product_descriptions") as batch:
        for name, kind in (
            ("source_job_id", sa.Integer()),
            ("request_id", sa.Integer()),
            ("version", sa.Integer()),
            ("system_prompt", sa.Text()),
            ("configuration_snapshot", sa.Text()),
            ("export_error", sa.Text()),
        ):
            if name not in columns:
                batch.add_column(sa.Column(name, kind, nullable=True))
        if "source_job_id" not in columns:
            batch.create_foreign_key("fk_description_source_job", "jobs", ["source_job_id"], ["id"])
            batch.create_foreign_key(
                "fk_description_request", "product_description_requests", ["request_id"], ["id"]
            )
            batch.create_index("ix_product_descriptions_source_job_id", ["source_job_id"])
            batch.create_unique_constraint(
                "uq_description_source_version", ["source_job_id", "version"]
            )
            batch.create_unique_constraint("uq_description_request", ["request_id"])
    roots: dict[str, list[int]] = {}
    jobs = bind.execute(sa.text("SELECT id, workflow_name, created_at FROM jobs")).mappings()
    for job in jobs:
        date = str(job["created_at"])[:10] if job["created_at"] else None
        root = (
            Path(settings.image_output_dir) / output_subdir(job["workflow_name"], job["id"], date)
        ).resolve()
        roots.setdefault(str(root), []).append(job["id"])
    versions: dict[int, int] = {}
    rows = (
        bind.execute(sa.text("SELECT * FROM product_descriptions ORDER BY created_at, id"))
        .mappings()
        .all()
    )
    for row in rows:
        candidates = roots.get(str(Path(row["directory_path"]).resolve()), [])
        source_id = candidates[0] if len(candidates) == 1 else None
        version = None
        if source_id and row["status"] == "completed":
            versions[source_id] = versions.get(source_id, 0) + 1
            version = versions[source_id]
        audits = (
            bind.execute(
                sa.text(
                    "SELECT e.id, e.system_prompt FROM role_executions e "
                    "JOIN creative_roles r ON r.id=e.role_id "
                    "WHERE e.job_step_id=:step AND r.name='product_describer' "
                    "AND e.output_deliverable=:content AND e.status='completed'"
                ),
                {"step": row["job_step_id"], "content": row["content"]},
            )
            .mappings()
            .all()
        )
        audit = audits[0] if len(audits) == 1 else None
        bind.execute(
            sa.text(
                "UPDATE product_descriptions SET source_job_id=:source, version=:version, "
                "system_prompt=:prompt, role_execution_id=:execution WHERE id=:id"
            ),
            {
                "source": source_id,
                "version": version,
                "prompt": audit["system_prompt"] if audit else row.get("system_prompt"),
                "execution": audit["id"] if audit else row["role_execution_id"],
                "id": row["id"],
            },
        )


def downgrade() -> None:
    with op.batch_alter_table("product_descriptions") as batch:
        batch.drop_constraint("uq_description_request", type_="unique")
        batch.drop_constraint("uq_description_source_version", type_="unique")
        batch.drop_index("ix_product_descriptions_source_job_id")
        batch.drop_constraint("fk_description_request", type_="foreignkey")
        batch.drop_constraint("fk_description_source_job", type_="foreignkey")
        for name in (
            "export_error",
            "configuration_snapshot",
            "system_prompt",
            "version",
            "request_id",
            "source_job_id",
        ):
            batch.drop_column(name)
    op.drop_index("ix_product_description_requests_source_job_id", "product_description_requests")
    op.drop_table("product_description_requests")
