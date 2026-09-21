"""
app.database — SQLAlchemy async engine & session factory

Responsibilities:
- Create the async engine from settings.DATABASE_URL
- Provide an async session maker for use in route dependencies
- init_db() validates that Alembic migrations have been applied

Usage in routes:
    from app.database import get_session
    async def my_route(session: AsyncSession = Depends(get_session)):
        ...
"""

from collections.abc import AsyncGenerator
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


def _check_schema(connection: Connection) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    current_heads = set(MigrationContext.configure(connection).get_current_heads())
    if current_heads != expected_heads:
        current = ", ".join(sorted(current_heads)) or "unversioned"
        expected = ", ".join(sorted(expected_heads))
        raise RuntimeError(
            f"Database migrations are required (current: {current}; expected: {expected}). "
            "Stop the server and run `alembic upgrade head` from the project root, "
            "then restart the server."
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


async def init_db() -> None:
    """Validate the migrated schema before the server accepts requests."""
    # Import models so they register with Base.metadata
    import app.models.creative  # noqa: F401
    import app.models.job  # noqa: F401
    import app.models.media  # noqa: F401
    import app.models.setting  # noqa: F401

    async with engine.connect() as conn:
        await conn.run_sync(_check_schema)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session() as session:
        yield session
