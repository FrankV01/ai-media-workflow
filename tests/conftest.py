"""
tests.conftest — Shared fixtures

Autouse fixture points app.database.async_session at a throwaway SQLite file
so configuration providers (constructed lazily inside blocks) never touch
the real data/app.db during tests.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.models.creative  # noqa: F401 — register models on Base.metadata
import app.models.job  # noqa: F401
import app.models.media  # noqa: F401
import app.models.setting  # noqa: F401
from app.database import Base


@pytest.fixture(autouse=True)
def temp_config_db(tmp_path, monkeypatch):
    """Redirect lazily-built configuration providers to a temp database."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/config_test.db")

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app.database, "async_session", factory)
    yield factory
    asyncio.run(engine.dispose())
