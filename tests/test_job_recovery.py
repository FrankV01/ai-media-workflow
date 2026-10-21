"""
tests.test_job_recovery — startup recovery of interrupted jobs

Covers recover_interrupted_jobs(): resuming RUNNING jobs at the step that
was executing, requeueing PENDING jobs, and failing jobs whose submission
context predates persistence.
"""

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.models.block  # noqa: F401 — register models on Base.metadata
import app.models.cloudflare  # noqa: F401
import app.models.creative  # noqa: F401
import app.models.delivery  # noqa: F401
import app.models.job  # noqa: F401
import app.models.media  # noqa: F401
import app.models.pinterest  # noqa: F401
import app.models.setting  # noqa: F401
import app.models.storefront  # noqa: F401
import app.models.workflow  # noqa: F401
import app.pipeline.engine as engine_module
import app.services.job_recovery as recovery_module
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import discover_blocks, register
from app.database import Base
from app.models.job import Job, JobStatus, JobStep
from app.services.job_recovery import recover_interrupted_jobs


@register
class RecoverMarkerBlock(Block):
    meta = BlockMeta(
        name="test_recover_marker",
        category="test",
        inputs=["brief"],
        outputs=["seen_brief", "seen_prior"],
    )

    async def run(self, context):
        return {
            "seen_brief": context.get("brief"),
            "seen_prior": context.get("prior_output"),
        }


@register
class RecoverProducerBlock(Block):
    meta = BlockMeta(
        name="test_recover_producer",
        category="test",
        inputs=["brief"],
        outputs=["prior_output"],
    )

    async def run(self, context):
        return {"prior_output": "from-producer"}


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    """Point engine + recovery at a throwaway SQLite file."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    discover_blocks()
    monkeypatch.setattr(engine_module, "async_session", factory)
    monkeypatch.setattr(recovery_module, "async_session", factory)
    monkeypatch.setattr(app.database, "async_session", factory)
    yield factory
    await engine.dispose()


async def _get_job(factory, job_id: int) -> Job:
    async with factory() as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _get_steps(factory, job_id: int) -> list[JobStep]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(JobStep)
                    .where(JobStep.job_id == job_id)
                    .order_by(JobStep.order, JobStep.id)
                )
            )
            .scalars()
            .all()
        )


async def _add_job(factory, **fields) -> int:
    async with factory() as session:
        job = Job(workflow_name="Recover Test", **fields)
        session.add(job)
        await session.commit()
        return job.id


async def _add_step(factory, job_id: int, block: str, order: int, status, **fields):
    async with factory() as session:
        session.add(JobStep(job_id=job_id, block_name=block, order=order, status=status, **fields))
        await session.commit()


@pytest.mark.asyncio
async def test_recovery_resumes_interrupted_step(session_factory):
    """A RUNNING job restarts the step that was executing when it died."""
    steps = ["test_recover_producer", "test_recover_marker"]
    job_id = await _add_job(
        session_factory,
        status=JobStatus.RUNNING,
        steps_json=json.dumps(steps),
        context_json=json.dumps({"brief": "resume me"}),
    )
    snapshot = json.dumps({"brief": "resume me", "prior_output": "from-producer"})
    await _add_step(
        session_factory,
        job_id,
        "test_recover_producer",
        0,
        JobStatus.COMPLETED,
        input_context=json.dumps({"brief": "resume me"}),
        output=json.dumps({"prior_output": "from-producer"}),
    )
    await _add_step(
        session_factory,
        job_id,
        "test_recover_marker",
        1,
        JobStatus.RUNNING,
        input_context=snapshot,
    )

    tasks = await recover_interrupted_jobs()
    await asyncio.gather(*tasks)

    job = await _get_job(session_factory, job_id)
    assert job.status == JobStatus.COMPLETED

    steps_out = await _get_steps(session_factory, job_id)
    assert [s.status for s in steps_out] == [JobStatus.COMPLETED, JobStatus.COMPLETED]
    resumed = json.loads(steps_out[1].output)
    assert resumed["seen_brief"] == "resume me"
    assert resumed["seen_prior"] == "from-producer"

    warnings = json.loads(job.warnings)
    assert any("interrupted" in w for w in warnings)


@pytest.mark.asyncio
async def test_recovery_resolves_routing_after_resume(session_factory):
    """Unresolved routing dicts still branch correctly after a resume."""
    steps = [
        "test_recover_producer",
        {"on_bad": [], "always": ["test_recover_marker"]},
    ]
    job_id = await _add_job(
        session_factory,
        status=JobStatus.RUNNING,
        steps_json=json.dumps(steps),
        context_json=json.dumps({"brief": "b"}),
    )
    await _add_step(
        session_factory,
        job_id,
        "test_recover_producer",
        0,
        JobStatus.RUNNING,
        input_context=json.dumps({"brief": "b"}),
    )

    tasks = await recover_interrupted_jobs()
    await asyncio.gather(*tasks)

    job = await _get_job(session_factory, job_id)
    assert job.status == JobStatus.COMPLETED
    steps_out = await _get_steps(session_factory, job_id)
    # Branch step was created on resume and ran
    assert [s.block_name for s in steps_out] == [
        "test_recover_producer",
        "test_recover_marker",
    ]
    assert all(s.status == JobStatus.COMPLETED for s in steps_out)


@pytest.mark.asyncio
async def test_recovery_requeues_pending_job(session_factory):
    """A PENDING job reruns from its stored submission state."""
    job_id = await _add_job(
        session_factory,
        status=JobStatus.PENDING,
        steps_json=json.dumps(["test_recover_marker"]),
        context_json=json.dumps({"brief": "queued brief"}),
    )

    tasks = await recover_interrupted_jobs()
    await asyncio.gather(*tasks)

    job = await _get_job(session_factory, job_id)
    assert job.status == JobStatus.COMPLETED
    steps_out = await _get_steps(session_factory, job_id)
    assert len(steps_out) == 1
    assert json.loads(steps_out[0].output)["seen_brief"] == "queued brief"


@pytest.mark.asyncio
async def test_recovery_fails_legacy_pending_job(session_factory):
    """A PENDING job with no persisted context cannot be recovered."""
    job_id = await _add_job(session_factory, status=JobStatus.PENDING)

    tasks = await recover_interrupted_jobs()
    assert tasks == []

    job = await _get_job(session_factory, job_id)
    assert job.status == JobStatus.FAILED
    assert "restart" in job.error


@pytest.mark.asyncio
async def test_recovery_fails_unresumable_running_job(session_factory):
    """A RUNNING job with no context or snapshots fails cleanly."""
    job_id = await _add_job(session_factory, status=JobStatus.RUNNING)
    await _add_step(session_factory, job_id, "test_recover_marker", 0, JobStatus.RUNNING)

    tasks = await recover_interrupted_jobs()
    await asyncio.gather(*tasks)

    job = await _get_job(session_factory, job_id)
    assert job.status == JobStatus.FAILED
    assert "restart" in job.error
