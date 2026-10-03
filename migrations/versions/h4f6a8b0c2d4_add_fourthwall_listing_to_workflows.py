"""append fourthwall_listing to workflows; drop storefront enabled flag

Appends the fourthwall_listing block as the final top-level step of every
stored workflow definition, including disabled ones. Workflows already
containing fourthwall_listing anywhere (top level or a routing branch)
are left untouched — nothing is normalized or reordered.

Also removes the now-redundant "enabled" key from the stored
'storefronts.fourthwall' settings JSON — workflow membership is the sole
enablement mechanism, so the global flag no longer exists in code. Rows
without the key are left untouched.

The exact pre-/post-migration steps_json of every changed workflow is
backed up under the Setting key
'fourthwall_listing.workflow_migration_backup', and the storefront
settings row under 'storefronts.fourthwall.enabled_migration_backup', so
the downgrade can restore each row only when it still matches the
migration-written value — intervening user edits are never overwritten.

Revision ID: h4f6a8b0c2d4
Revises: g8d5b2e4f6a1
Create Date: 2026-10-05 02:30:00

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h4f6a8b0c2d4"
down_revision: str | Sequence[str] | None = "g8d5b2e4f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LISTING_BLOCK = "fourthwall_listing"
_STOREFRONT_SETTINGS_KEY = "storefronts.fourthwall"
_WORKFLOW_BACKUP_KEY = "fourthwall_listing.workflow_migration_backup"
_SETTINGS_BACKUP_KEY = "storefronts.fourthwall.enabled_migration_backup"
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


def _contains_listing(steps: list) -> bool:
    for item in steps:
        if item == LISTING_BLOCK:
            return True
        if isinstance(item, dict) and any(LISTING_BLOCK in branch for branch in item.values()):
            return True
    return False


def _append_listing_to_workflows() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "workflows" not in tables:
        return
    rows = bind.execute(sa.text("SELECT id, steps_json FROM workflows ORDER BY id")).fetchall()

    updates: list[tuple[int, str, str]] = []
    for workflow_id, steps_json in rows:
        steps = _validate_steps(workflow_id, steps_json)
        if _contains_listing(steps):
            continue
        new_steps_json = json.dumps(steps + [LISTING_BLOCK])
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
                "description": "fourthwall_listing workflow migration backup",
            },
        )


def _scrub_storefront_enabled() -> None:
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
    if not isinstance(data, dict) or "enabled" not in data:
        return
    old_value = row[0]
    del data["enabled"]
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
            "description": "storefronts.fourthwall enabled-flag migration backup",
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


def _restore_storefront_enabled() -> None:
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
    _append_listing_to_workflows()
    _scrub_storefront_enabled()


def downgrade() -> None:
    """Downgrade schema."""
    _restore_workflows_from_backup()
    _restore_storefront_enabled()
