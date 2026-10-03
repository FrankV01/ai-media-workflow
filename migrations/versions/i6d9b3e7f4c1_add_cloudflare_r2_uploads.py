"""add cloudflare_r2_uploads; insert uploader into workflows; scrub size limit

Creates cloudflare_r2_uploads (per-attempt audit: immutable upload target,
archive identity, object key/URL, ETag, status, errors — binaries live in
R2) and adds cloudflare_upload_id / link_document_name /
link_document_sha256 to storefront_listings so every listing correlates
to the upload its link document points at.

Inserts cloudflare_r2_uploader immediately before the first top-level
fourthwall_listing step of every stored workflow definition — including
disabled ones — that contains the listing but not the uploader. Workflows
without a top-level fourthwall_listing (or with one already hosting the
uploader) are left untouched — nothing is normalized or reordered.

Also removes the now-obsolete "file_size_limit_mb" key from the stored
'storefronts.fourthwall' settings JSON — the digital file is now a small
link document, so the archive-size limit no longer applies. Rows without
the key are left untouched.

The exact pre-/post-migration steps_json of every changed workflow is
backed up under the Setting key
'cloudflare_r2_uploader.workflow_migration_backup', and the storefront
settings row under 'storefronts.fourthwall.r2_migration_backup', so the
downgrade can restore each row only when it still matches the
migration-written value — intervening user edits are never overwritten.

Revision ID: i6d9b3e7f4c1
Revises: h4f6a8b0c2d4
Create Date: 2026-10-05 04:00:00

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i6d9b3e7f4c1"
down_revision: str | Sequence[str] | None = "h4f6a8b0c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPLOAD_BLOCK = "cloudflare_r2_uploader"
LISTING_BLOCK = "fourthwall_listing"
_STOREFRONT_SETTINGS_KEY = "storefronts.fourthwall"
_WORKFLOW_BACKUP_KEY = "cloudflare_r2_uploader.workflow_migration_backup"
_SETTINGS_BACKUP_KEY = "storefronts.fourthwall.r2_migration_backup"
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


def _contains_block(steps: list, block: str) -> bool:
    for item in steps:
        if item == block:
            return True
        if isinstance(item, dict) and any(block in branch for branch in item.values()):
            return True
    return False


def _insert_uploader_to_workflows() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "workflows" not in tables:
        return
    rows = bind.execute(sa.text("SELECT id, steps_json FROM workflows ORDER BY id")).fetchall()

    updates: list[tuple[int, str, str]] = []
    for workflow_id, steps_json in rows:
        steps = _validate_steps(workflow_id, steps_json)
        if _contains_block(steps, UPLOAD_BLOCK):
            continue
        # Only insert before a top-level listing — a workflow that keeps the
        # listing inside a routing branch is left to manual editing.
        try:
            index = steps.index(LISTING_BLOCK)
        except ValueError:
            continue
        new_steps_json = json.dumps(steps[:index] + [UPLOAD_BLOCK] + steps[index:])
        updates.append((workflow_id, steps_json, new_steps_json))

    if not updates:
        return

    for workflow_id, _old, new in updates:
        bind.execute(
            sa.text("UPDATE workflows SET steps_json = :steps WHERE id = :id"),
            {"steps": new, "id": workflow_id},
        )

    if "settings" in tables:
        backup = json.dumps({str(wid): {"old": old, "new": new} for wid, old, new in updates})
        bind.execute(
            sa.text("DELETE FROM settings WHERE key = :key"), {"key": _WORKFLOW_BACKUP_KEY}
        )
        bind.execute(
            sa.text(
                "INSERT INTO settings (key, value, description) VALUES (:key, :value, :description)"
            ),
            {
                "key": _WORKFLOW_BACKUP_KEY,
                "value": backup,
                "description": "cloudflare_r2_uploader workflow migration backup",
            },
        )


def _scrub_storefront_size_limit() -> None:
    bind = op.get_bind()
    if "settings" not in set(sa.inspect(bind).get_table_names()):
        return
    row = bind.execute(
        sa.text("SELECT value FROM settings WHERE key = :key"),
        {"key": _STOREFRONT_SETTINGS_KEY},
    ).fetchone()
    if row is None:
        return
    try:
        data = json.loads(row[0])
    except ValueError:
        return
    if not isinstance(data, dict) or "file_size_limit_mb" not in data:
        return
    old_value = row[0]
    del data["file_size_limit_mb"]
    new_value = json.dumps(data, ensure_ascii=False)
    bind.execute(
        sa.text("UPDATE settings SET value = :value WHERE key = :key"),
        {"value": new_value, "key": _STOREFRONT_SETTINGS_KEY},
    )
    bind.execute(
        sa.text("DELETE FROM settings WHERE key = :key"), {"key": _SETTINGS_BACKUP_KEY}
    )
    bind.execute(
        sa.text(
            "INSERT INTO settings (key, value, description) VALUES (:key, :value, :description)"
        ),
        {
            "key": _SETTINGS_BACKUP_KEY,
            "value": json.dumps({"old": old_value, "new": new_value}),
            "description": "storefronts.fourthwall file-size-limit migration backup",
        },
    )


def _restore_workflows_from_backup() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "settings" not in tables or "workflows" not in tables:
        return
    row = bind.execute(
        sa.text("SELECT value FROM settings WHERE key = :key"), {"key": _WORKFLOW_BACKUP_KEY}
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
    bind.execute(
        sa.text("DELETE FROM settings WHERE key = :key"), {"key": _WORKFLOW_BACKUP_KEY}
    )


def _restore_storefront_size_limit() -> None:
    bind = op.get_bind()
    if "settings" not in set(sa.inspect(bind).get_table_names()):
        return
    row = bind.execute(
        sa.text("SELECT value FROM settings WHERE key = :key"), {"key": _SETTINGS_BACKUP_KEY}
    ).fetchone()
    if row is not None:
        backup = json.loads(row[0])
        current = bind.execute(
            sa.text("SELECT value FROM settings WHERE key = :key"),
            {"key": _STOREFRONT_SETTINGS_KEY},
        ).fetchone()
        if current is not None and current[0] == backup["new"]:
            bind.execute(
                sa.text("UPDATE settings SET value = :value WHERE key = :key"),
                {"value": backup["old"], "key": _STOREFRONT_SETTINGS_KEY},
            )
    bind.execute(
        sa.text("DELETE FROM settings WHERE key = :key"), {"key": _SETTINGS_BACKUP_KEY}
    )


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "cloudflare_r2_uploads" not in existing_tables:
        op.create_table(
            "cloudflare_r2_uploads",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=True),
            sa.Column("job_step_id", sa.Integer(), nullable=True),
            sa.Column("delivery_archive_id", sa.Integer(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.Enum("PENDING", "RUNNING", "READY", "FAILED", name="cloudflareuploadstatus"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("archive_name", sa.String(length=255), nullable=True),
            sa.Column("archive_byte_size", sa.Integer(), nullable=True),
            sa.Column("archive_sha256", sa.String(length=64), nullable=True),
            sa.Column("bucket", sa.String(length=255), nullable=True),
            sa.Column("object_key", sa.String(length=1024), nullable=True),
            sa.Column("object_url", sa.Text(), nullable=True),
            sa.Column("etag", sa.String(length=255), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("error_type", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
            sa.ForeignKeyConstraint(["job_step_id"], ["job_steps.id"]),
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"]),
            sa.ForeignKeyConstraint(["delivery_archive_id"], ["delivery_archives.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "attempt", name="uq_cloudflare_upload_job_attempt"),
        )
        with op.batch_alter_table("cloudflare_r2_uploads", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_cloudflare_r2_uploads_job_id"), ["job_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_cloudflare_r2_uploads_workflow_id"),
                ["workflow_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_cloudflare_r2_uploads_job_step_id"),
                ["job_step_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_cloudflare_r2_uploads_delivery_archive_id"),
                ["delivery_archive_id"],
                unique=False,
            )

    if "storefront_listings" in existing_tables:
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            existing_cols = {
                col["name"] for col in sa.inspect(op.get_bind()).get_columns("storefront_listings")
            }
            if "cloudflare_upload_id" not in existing_cols:
                batch_op.add_column(
                    sa.Column("cloudflare_upload_id", sa.Integer(), nullable=True)
                )
                batch_op.create_index(
                    batch_op.f("ix_storefront_listings_cloudflare_upload_id"),
                    ["cloudflare_upload_id"],
                    unique=False,
                )
            if "link_document_name" not in existing_cols:
                batch_op.add_column(
                    sa.Column("link_document_name", sa.String(length=255), nullable=True)
                )
            if "link_document_sha256" not in existing_cols:
                batch_op.add_column(
                    sa.Column("link_document_sha256", sa.String(length=64), nullable=True)
                )
            if "cloudflare_upload_id" not in existing_cols:
                batch_op.create_foreign_key(
                    "fk_storefront_listings_cloudflare_upload_id",
                    "cloudflare_r2_uploads",
                    ["cloudflare_upload_id"],
                    ["id"],
                )

    _insert_uploader_to_workflows()
    _scrub_storefront_size_limit()


def downgrade() -> None:
    """Downgrade schema."""
    _restore_workflows_from_backup()
    _restore_storefront_size_limit()

    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "storefront_listings" in existing_tables:
        existing_cols = {
            col["name"] for col in sa.inspect(op.get_bind()).get_columns("storefront_listings")
        }
        with op.batch_alter_table("storefront_listings", schema=None) as batch_op:
            if "cloudflare_upload_id" in existing_cols:
                batch_op.drop_constraint(
                    "fk_storefront_listings_cloudflare_upload_id", type_="foreignkey"
                )
                batch_op.drop_index(batch_op.f("ix_storefront_listings_cloudflare_upload_id"))
                batch_op.drop_column("cloudflare_upload_id")
            if "link_document_sha256" in existing_cols:
                batch_op.drop_column("link_document_sha256")
            if "link_document_name" in existing_cols:
                batch_op.drop_column("link_document_name")
    if "cloudflare_r2_uploads" in existing_tables:
        with op.batch_alter_table("cloudflare_r2_uploads", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_cloudflare_r2_uploads_delivery_archive_id"))
            batch_op.drop_index(batch_op.f("ix_cloudflare_r2_uploads_job_step_id"))
            batch_op.drop_index(batch_op.f("ix_cloudflare_r2_uploads_workflow_id"))
            batch_op.drop_index(batch_op.f("ix_cloudflare_r2_uploads_job_id"))
        op.drop_table("cloudflare_r2_uploads")
