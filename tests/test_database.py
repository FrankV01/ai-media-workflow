"""
tests.test_database — Startup schema validation and migration recovery tests

Regression coverage for the mixed-schema failure where the database sat at
revision e2f5a1c9d7b4 while the former startup create_all pre-created the
new configuration tables, so `alembic upgrade head` failed and requests hit
tables without jobs.warnings.

All Alembic upgrades run against throwaway SQLite files under tmp_path —
the real data/app.db is never touched.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database
import app.models.creative  # noqa: F401 — register models on Base.metadata
import app.models.job  # noqa: F401
import app.models.media  # noqa: F401
import app.models.setting  # noqa: F401
import app.pipeline.engine as engine_module
from app.config import settings
from app.database import Base
from app.models.creative import CreativeRole, LlmRoleConfiguration, Message, RoleExecution
from app.models.job import Job, JobStatus, JobStep
from app.models.media import MediaModelConfiguration

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "e2f5a1c9d7b4"
HEAD_REVISION = "c6e1f0a4b8d3"

SessionFactory = async_sessionmaker[AsyncSession]

LEGACY_SEED = """
INSERT INTO jobs (id, workflow_name, status, created_at)
VALUES (1, 'Migration test', 'COMPLETED', '2026-01-01 00:00:00');
INSERT INTO job_steps (id, job_id, block_name, "order", status)
VALUES (1, 1, 'art_director', 0, 'COMPLETED');
INSERT INTO creative_roles
(id, name, title, description, system_prompt, output_format, model_override,
 temperature, created_at, updated_at)
VALUES (1, 'art_director', 'Art Director', 'legacy role', 'cached prompt', 'text',
 'old-model', 0.7, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
INSERT INTO role_executions (id, job_step_id, role_id, input_brief, model_used, started_at)
VALUES (1, 1, 1, 'test brief', 'old-model', '2026-01-01 00:00:00');
INSERT INTO messages (id, execution_id, role, content, ordinal, created_at)
VALUES (1, 1, 'SYSTEM', 'historical system prompt', 0, '2026-01-01 00:00:00');
"""


def _alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _upgrade(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(_alembic_config(), revision)


def _stamp(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{path}")
    command.stamp(_alembic_config(), revision)


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _column_names(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _current_revisions(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT version_num FROM alembic_version")}


@pytest.fixture
def bind_db(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Path], SessionFactory]]:
    """Point the app engine and both session factories at a temp database."""
    engines = []

    def bind(path: Path) -> SessionFactory:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        factory: SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(app.database, "engine", engine)
        monkeypatch.setattr(app.database, "async_session", factory)
        monkeypatch.setattr(engine_module, "async_session", factory)
        engines.append(engine)
        return factory

    yield bind
    for engine in engines:
        asyncio.run(engine.dispose())


async def _create_missing_tables() -> None:
    async with app.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _simulate_buggy_startup(factory: SessionFactory) -> None:
    """Reproduce the old create_all startup on an e2f5a1c9d7b4 database."""
    await _create_missing_tables()
    async with factory() as session:
        session.add(
            LlmRoleConfiguration(
                role_id=1,
                model_name="test-model",
                system_prompt="saved profile",
                temperature=0.2,
                max_tokens=100,
                enable_thinking=False,
                uses_code_defaults=False,
            )
        )
        session.add(
            MediaModelConfiguration(
                block_name="media_producer",
                backend_name="placeholder",
                model_name="placeholder",
                settings_json="{}",
                uses_code_defaults=False,
            )
        )
        await session.commit()


def test_startup_rejects_unmigrated_database_without_creating_tables(
    tmp_path: Path, bind_db: Callable[[Path], SessionFactory]
) -> None:
    path = tmp_path / "app.db"
    path.touch()
    bind_db(path)

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        asyncio.run(app.database.init_db())
    assert _table_names(path) == set()


def test_startup_rejects_old_schema_without_creating_new_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    tables_before = _table_names(path)
    bind_db(path)

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        asyncio.run(app.database.init_db())

    assert _table_names(path) == tables_before
    assert "llm_role_configurations" not in _table_names(path)
    assert "warnings" not in _column_names(path, "jobs")


@pytest.mark.parametrize("precreated", [False, True])
def test_upgrade_recovers_create_all_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bind_db: Callable[[Path], SessionFactory],
    precreated: bool,
) -> None:
    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SEED)

    factory = bind_db(path)
    if precreated:
        asyncio.run(_simulate_buggy_startup(factory))

    _upgrade(path, "head", monkeypatch)
    asyncio.run(app.database.init_db())

    assert _current_revisions(path) == {HEAD_REVISION}
    assert "warnings" in _column_names(path, "jobs")
    assert "llm_configuration_changes" in _table_names(path)

    async def _read() -> tuple[Job, JobStep, CreativeRole, RoleExecution, Message]:
        async with factory() as session:
            job = (await session.execute(select(Job))).scalar_one()
            step = (await session.execute(select(JobStep))).scalar_one()
            role = (await session.execute(select(CreativeRole))).scalar_one()
            execution = (await session.execute(select(RoleExecution))).scalar_one()
            message = (await session.execute(select(Message))).scalar_one()
            return job, step, role, execution, message

    job, step, role, execution, message = asyncio.run(_read())
    assert job.workflow_name == "Migration test"
    assert job.status == JobStatus.COMPLETED
    assert step.block_name == "art_director"
    assert role.name == "art_director"
    assert role.title == "Art Director"
    assert execution.system_prompt == "historical system prompt"
    assert execution.configuration_source == "legacy"
    assert execution.model_used == "old-model"
    assert message.content == "historical system prompt"

    if precreated:

        async def _configs() -> tuple[LlmRoleConfiguration, MediaModelConfiguration]:
            async with factory() as session:
                llm = (await session.execute(select(LlmRoleConfiguration))).scalar_one()
                media = (await session.execute(select(MediaModelConfiguration))).scalar_one()
                return llm, media

        llm_config, media_config = asyncio.run(_configs())
        assert llm_config.id == 1
        assert llm_config.role_id == 1
        assert llm_config.model_name == "test-model"
        assert llm_config.system_prompt == "saved profile"
        assert llm_config.uses_code_defaults is False
        assert media_config.id == 1
        assert media_config.block_name == "media_producer"
        assert media_config.backend_name == "placeholder"
        assert media_config.model_name == "placeholder"


def test_startup_accepts_fresh_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    path = tmp_path / "app.db"
    _upgrade(path, "head", monkeypatch)
    bind_db(path)
    asyncio.run(app.database.init_db())
    assert "llm_configuration_changes" in _table_names(path)


def test_startup_rejects_stamped_but_incomplete_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    _stamp(path, "head", monkeypatch)
    tables_before = _table_names(path)
    bind_db(path)

    with pytest.raises(RuntimeError, match="jobs.warnings"):
        asyncio.run(app.database.init_db())
    assert _table_names(path) == tables_before


def test_lifespan_rejects_old_schema_before_dependency_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    import app.main

    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    bind_db(path)
    dependencies = AsyncMock()
    monkeypatch.setattr(app.main, "_check_dependencies", dependencies)

    async def _enter() -> None:
        async with app.main.lifespan(app.main.app):
            pass

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        asyncio.run(_enter())
    dependencies.assert_not_awaited()


def test_migrated_web_job_listing_and_test_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    import app.main

    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SEED)
    factory = bind_db(path)
    asyncio.run(_create_missing_tables())
    _upgrade(path, "head", monkeypatch)

    spy = AsyncMock()
    monkeypatch.setattr(engine_module, "_guarded_execute", spy)

    async def _exercise() -> None:
        await app.database.init_db()
        transport = httpx.ASGITransport(app=app.main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listing = await client.get("/partials/jobs")
            assert listing.status_code == 200

            queued = await client.post(
                "/partials/run-test-pipeline", data={"brief": "schema regression"}
            )
            assert queued.status_code == 200

            await asyncio.sleep(0)
            spy.assert_awaited_once()
            job_id, _blocks, context = spy.await_args.args
            assert context["_generation_backend"] == "placeholder"
            assert context["brief"] == "schema regression"

            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                assert job.workflow_name == "Untitled shoot"
                assert job.status == JobStatus.PENDING
                assert job.warnings is None

            listing = await client.get("/partials/jobs")
            assert listing.status_code == 200
            assert "Untitled shoot" in listing.text

    asyncio.run(_exercise())


def test_upgrade_database_brings_old_schema_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    """upgrade_database() applies pending migrations and init_db() then passes."""
    path = tmp_path / "app.db"
    _upgrade(path, PREVIOUS_REVISION, monkeypatch)
    bind_db(path)

    app.database.upgrade_database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(app.database.init_db())

    assert _current_revisions(path) == {HEAD_REVISION}
    assert "llm_configuration_changes" in _table_names(path)


def test_upgrade_database_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_db: Callable[[Path], SessionFactory]
) -> None:
    """upgrade_database() is safe to call on an already-head database."""
    path = tmp_path / "app.db"
    _upgrade(path, "head", monkeypatch)
    bind_db(path)

    app.database.upgrade_database(f"sqlite+aiosqlite:///{path}")
    asyncio.run(app.database.init_db())

    assert _current_revisions(path) == {HEAD_REVISION}
