"""
app.database — SQLAlchemy async engine & session factory

Responsibilities:
- Create the async engine from settings.DATABASE_URL
- Provide an async session maker for use in route dependencies
- init_db() creates tables on first run (before Alembic is wired up)

Usage in routes:
    from app.database import get_session
    async def my_route(session: AsyncSession = Depends(get_session)):
        ...
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


async def init_db() -> None:
    """Create all tables. Replace with Alembic migrations for production."""
    # Import models so they register with Base.metadata
    import app.models.job  # noqa: F401
    import app.models.setting  # noqa: F401
    import app.models.creative  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session() as session:
        yield session
