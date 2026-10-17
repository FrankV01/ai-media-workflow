"""
app.database — SQLAlchemy async engine & session factory

Responsibilities:
- Create the async engine from settings.DATABASE_URL
- Provide an async session maker for use in route dependencies
- init_db() validates that Alembic migrations have been applied
- upgrade_database() applies pending Alembic migrations to head

Usage in routes:
    from app.database import get_session
    async def my_route(session: AsyncSession = Depends(get_session)):
        ...
"""

import logging
import os
import re
import sqlite3
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)
_STARTUP_BACKUP_MARKER = "AI_MEDIA_SQLITE_BACKUP_COMPLETE"
_BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser()


def _backup_path(database_path: Path, created_at: datetime) -> Path:
    timestamp = created_at.astimezone(UTC).strftime(_BACKUP_TIMESTAMP_FORMAT)
    return database_path.with_name(
        f"{database_path.stem}.backup-{timestamp}{database_path.suffix}"
    )


def _prune_sqlite_backups(
    database_path: Path,
    *,
    now: datetime,
    retention_days: int,
    retention_count: int,
    preserve_path: Path,
) -> None:
    pattern = re.compile(
        rf"^{re.escape(database_path.stem)}\.backup-(\d{{8}}T\d{{6}}Z)"
        rf"{re.escape(database_path.suffix)}$"
    )
    backups: list[tuple[datetime, Path]] = []
    for path in database_path.parent.iterdir():
        match = pattern.fullmatch(path.name)
        if match is not None and path.is_file():
            created_at = datetime.strptime(match.group(1), _BACKUP_TIMESTAMP_FORMAT).replace(
                tzinfo=UTC
            )
            backups.append((created_at, path))

    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    retained: list[tuple[datetime, Path]] = []
    for created_at, path in backups:
        if created_at < cutoff and path != preserve_path:
            path.unlink()
            logger.info("Pruned expired SQLite backup — %s", path)
        else:
            retained.append((created_at, path))

    retained.sort(key=lambda item: (item[0], item[1].name))
    excess = max(0, len(retained) - retention_count)
    for _created_at, path in retained:
        if excess == 0:
            break
        if path != preserve_path:
            path.unlink()
            excess -= 1
            logger.info("Pruned excess SQLite backup — %s", path)


def backup_sqlite_database(
    database_url: str | None = None,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
    retention_count: int | None = None,
) -> Path | None:
    """Create and retain a consistent adjacent backup for a file-backed SQLite database."""
    database_path = _sqlite_database_path(database_url or settings.database_url)
    if database_path is None or not database_path.is_file():
        return None

    created_at = now or datetime.now(UTC)
    destination_path = _backup_path(database_path, created_at)
    if destination_path.exists():
        raise FileExistsError(f"SQLite backup already exists: {destination_path}")

    source_uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(destination_path) as destination:
                source.backup(destination)
    except Exception as exc:
        destination_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to back up SQLite database {database_path}: {exc}") from exc

    _prune_sqlite_backups(
        database_path,
        now=created_at,
        retention_days=retention_days or settings.sqlite_backup_retention_days,
        retention_count=retention_count or settings.sqlite_backup_retention_count,
        preserve_path=destination_path,
    )
    logger.info("Created SQLite startup backup — %s", destination_path)
    return destination_path


def backup_sqlite_database_once() -> Path | None:
    """Create at most one SQLite backup across a startup supervisor and its worker."""
    if os.environ.get(_STARTUP_BACKUP_MARKER) == "1":
        return None
    backup_path = backup_sqlite_database()
    os.environ[_STARTUP_BACKUP_MARKER] = "1"
    return backup_path


def _check_schema(connection: Connection) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    current_heads = set(MigrationContext.configure(connection).get_current_heads())
    if current_heads != expected_heads:
        current = ", ".join(sorted(current_heads)) or "unversioned"
        expected = ", ".join(sorted(expected_heads))
        raise RuntimeError(
            f"Database migrations are required (current: {current}; expected: {expected}). "
            "Run `alembic upgrade head` from the project root. "
            "`./start.sh` and `python main.py` apply pending migrations automatically "
            "unless AI_MEDIA_AUTO_MIGRATE=0 is set."
        )
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            missing.append(table.name)
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(
            f"{table.name}.{column.name}" for column in table.columns if column.name not in columns
        )
    if missing:
        raise RuntimeError(
            "Database schema does not match the application: missing "
            + ", ".join(missing)
            + ". Check that DATABASE_URL points to the intended database and that migrations "
            "were applied correctly; do not stamp an outdated schema as current."
        )


def _alembic_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database(database_url: str | None = None) -> None:
    """Apply all pending Alembic migrations up to the current head."""
    command.upgrade(_alembic_config(database_url or settings.database_url), "head")


async def init_db() -> None:
    """Validate the migrated schema before the server accepts requests."""
    # Import models so they register with Base.metadata
    import app.models.block  # noqa: F401
    import app.models.creative  # noqa: F401
    import app.models.delivery  # noqa: F401
    import app.models.job  # noqa: F401
    import app.models.media  # noqa: F401
    import app.models.setting  # noqa: F401
    import app.models.storefront  # noqa: F401
    import app.models.workflow  # noqa: F401

    async with engine.connect() as conn:
        await conn.run_sync(_check_schema)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session() as session:
        yield session
