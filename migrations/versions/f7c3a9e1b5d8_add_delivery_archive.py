"""add delivery archive audit tables; append delivery_archive to workflows

Creates delivery_archives / delivery_archive_entries (attempt metadata,
inventory, checksums, Markdown snapshots — binaries stay on disk) and
appends the delivery_archive block as the final top-level step of every
stored workflow definition, including disabled ones. Workflows already
containing delivery_archive anywhere (top level or a routing branch) are
left untouched — nothing is normalized or reordered.

The exact pre-/post-migration steps_json of every changed workflow is
backed up under the Setting key
'delivery_archive.workflow_migration_backup' so the downgrade can restore
each row only when it still matches the migration-written value —
intervening user edits are never overwritten.

Revision ID: f7c3a9e1b5d8
Revises: d1e4f6a8b3c5
Create Date: 2026-10-03 02:30:00

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c3a9e1b5d8"
down_revision: str | Sequence[str] | None = "d1e4f6a8b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_BLOCK = "delivery_archive"
_BACKUP_KEY = "delivery_archive.workflow_migration_backup"
_ROUTING_KEYS = {"on_good", "on_bad", "always"}


def _validate_steps(workflow_id: int, raw: str) -> list:
    """Decode a stored steps_json; fail loudly with the workflow id."""
    try:
        steps = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"workflow {workflow_id}: steps_json is not valid JSON") from exc
    if not isinstance(steps, list):
        raise RuntimeError(f"workflow {workflow_id}: steps_json is not a list")
    for item in steps:
        if isinstance(item, str):
            continue
        if isinstance(item, dict):
            keys = set(item)
            if not keys or not keys <= _ROUTING_KEYS:
                raise RuntimeError(
                    f"workflow {workflow_id}: routing dict has invalid keys {sorted(keys)}"
                )
            for branch in keys:
                value = item[branch]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise RuntimeError(
                        f"workflow {workflow_id}: '{branch}' is not a list of block names"
                    )
            continue
        raise RuntimeError(f"workflow {workflow_id}: invalid step entry {item!r}")
    return steps


def _contains_archive(steps: list) -> bool:
    for item in steps:
        if item == DELIVERY_BLOCK:
            return True
        if isinstance(item, dict) and any(DELIVERY_BLOCK in branch for branch in item.values()):
            return True
    return False


def _append_archive_to_workflows() -> None:
    bind = op.get_bind()
    if "workflows" not in set(sa.inspect(bind).get_table_names()):
        return
    rows = bind.execute(sa.text("SELECT id, steps_json FROM workflows ORDER BY id")).fetchall()

    updates: list[tuple[int, str, str]] = []
    for workflow_id, steps_json in rows:
        steps = _validate_steps(workflow_id, steps_json)
        if _contains_archive(steps):
            continue
        new_steps_json = json.dumps(steps + [DELIVERY_BLOCK])
        updates.append((workflow_id, steps_json, new_steps_json))

    if not updates:
        return

    for workflow_id, _old, new in updates:
        bind.execute(
            sa.text("UPDATE workflows SET steps_json = :steps WHERE id = :id"),
            {"steps": new, "id": workflow_id},
        )

    if "settings" in set(sa.inspect(bind).get_table_names()):
        backup = json.dumps({str(wid): {"old": old, "new": new} for wid, old, new in updates})
        bind.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": _BACKUP_KEY})
        bind.execute(
            sa.text(
                "INSERT INTO settings (key, value, description) VALUES (:key, :value, :description)"
            ),
            {
                "key": _BACKUP_KEY,
                "value": backup,
                "description": "delivery_archive workflow migration backup",
            },
        )


def _restore_workflows_from_backup() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "settings" not in tables or "workflows" not in tables:
        return
    row = bind.execute(
        sa.text("SELECT value FROM settings WHERE key = :key"), {"key": _BACKUP_KEY}
    ).fetchone()
    if row is not None:
        backup = json.loads(row[0])
        current = {
            wid: steps_json
            for wid, steps_json in bind.execute(
                sa.text("SELECT id, steps_json FROM workflows ORDER BY id")
            ).fetchall()
        }
        for wid, pair in backup.items():
            if current.get(int(wid)) == pair["new"]:
                bind.execute(
                    sa.text("UPDATE workflows SET steps_json = :steps WHERE id = :id"),
                    {"steps": pair["old"], "id": int(wid)},
                )
    bind.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": _BACKUP_KEY})


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "delivery_archives" not in existing_tables:
        op.create_table(
            "delivery_archives",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=True),
            sa.Column("job_step_id", sa.Integer(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.Enum("PENDING", "RUNNING", "READY", "FAILED", name="deliveryarchivestatus"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_dir", sa.Text(), nullable=False),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("archive_path", sa.Text(), nullable=True),
            sa.Column("archive_name", sa.String(length=255), nullable=True),
            sa.Column("byte_size", sa.Integer(), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("manifest", sa.Text(), nullable=True),
            sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("error_type", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
            sa.ForeignKeyConstraint(["job_step_id"], ["job_steps.id"]),
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "attempt", name="uq_delivery_archive_job_attempt"),
        )
        with op.batch_alter_table("delivery_archives", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_delivery_archives_job_id"), ["job_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_delivery_archives_workflow_id"), ["workflow_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_delivery_archives_job_step_id"), ["job_step_id"], unique=False
            )

    if "delivery_archive_entries" not in existing_tables:
        op.create_table(
            "delivery_archive_entries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("archive_id", sa.Integer(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("entry_type", sa.String(length=10), nullable=False),
            sa.Column("size", sa.Integer(), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("markdown_contents", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["archive_id"], ["delivery_archives.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("archive_id", "path", name="uq_delivery_archive_entry_path"),
        )
        with op.batch_alter_table("delivery_archive_entries", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_delivery_archive_entries_archive_id"),
                ["archive_id"],
                unique=False,
            )

    _append_archive_to_workflows()


def downgrade() -> None:
    """Downgrade schema."""
    _restore_workflows_from_backup()

    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "delivery_archive_entries" in existing_tables:
        with op.batch_alter_table("delivery_archive_entries", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_delivery_archive_entries_archive_id"))
        op.drop_table("delivery_archive_entries")
    if "delivery_archives" in existing_tables:
        with op.batch_alter_table("delivery_archives", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_delivery_archives_job_step_id"))
            batch_op.drop_index(batch_op.f("ix_delivery_archives_workflow_id"))
            batch_op.drop_index(batch_op.f("ix_delivery_archives_job_id"))
        op.drop_table("delivery_archives")
