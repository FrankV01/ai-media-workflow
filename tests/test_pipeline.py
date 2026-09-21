"""
tests.test_pipeline — Pipeline execution engine tests

Uses a per-test throwaway SQLite file (tmp_path) so run_pipeline never
touches the real database. Covers: mid-run job retitling from
context["photo_shoot_name"], preservation of an explicit workflow name,
and the _job_id context key exposed to blocks.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.creative  # noqa: F401 — register models on Base.metadata
import app.models.job  # noqa: F401
import app.models.setting  # noqa: F401
import app.pipeline.engine as engine_module
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.database import Base
from app.models.job import Job, JobStep


@register
class NamerBlock(Block):
    """Test block that names the shoot (if unset) and echoes the job id."""

    meta = BlockMeta(
        name="test_namer_block",
        description="Names the shoot and echoes _job_id — engine tests only",
        category="test",
        inputs=["brief"],
        outputs=["brief", "photo_shoot_name", "seen_job_id"],
    )

    async def run(self, context):
        result = {"brief": "x", "seen_job_id": context.get("_job_id")}
        if "photo_shoot_name" not in context:
            result["photo_shoot_name"] = "Golden Hour Editorial"
        return result


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    """Point the engine's session factory at a throwaway SQLite file."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "async_session", factory)
    yield factory
    await engine.dispose()


async def _get_job(factory, job_id: int) -> Job:
    async with factory() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one()


@pytest.mark.asyncio
async def test_pipeline_retitles_job_from_block_output(session_factory):
    """A block that sets photo_shoot_name retitles an untitled job mid-run."""
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_namer_block"],
        context={"brief": "c"},
    )

    job = await _get_job(session_factory, job_id)
    assert job.workflow_name == "Golden Hour Editorial"


@pytest.mark.asyncio
async def test_pipeline_explicit_name_not_overridden(session_factory):
    """An explicit workflow_name wins — the block may not retitle the job."""
    job_id = await engine_module.run_pipeline(
        workflow_name="Preset",
        block_names=["test_namer_block"],
        context={"brief": "c"},
    )

    job = await _get_job(session_factory, job_id)
    assert job.workflow_name == "Preset"


@pytest.mark.asyncio
async def test_pipeline_exposes_job_id_in_context(session_factory):
    """Blocks can read context['_job_id']; it matches the created job."""
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_namer_block"],
        context={"brief": "c"},
    )

    async with session_factory() as session:
        result = await session.execute(select(JobStep).where(JobStep.job_id == job_id))
        step = result.scalar_one()
    assert json.loads(step.output)["seen_job_id"] == job_id
