"""
tests.test_pipeline — Pipeline execution engine tests

Uses a per-test throwaway SQLite file (tmp_path) so run_pipeline never
touches the real database. Covers: mid-run job retitling from
context["photo_shoot_name"], preservation of an explicit workflow name,
and the _job_id context key exposed to blocks.
"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.creative  # noqa: F401 — register models on Base.metadata
import app.models.job  # noqa: F401
import app.pipeline.engine as engine_module
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock
from app.database import Base
from app.models.creative import CreativeRole, Message, RoleExecution
from app.models.job import Job, JobStatus, JobStep


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


@register
class AuditRoleBlock(RoleBlock):
    meta = BlockMeta(
        name="test_audit_role",
        description="Persists an LLM audit trail",
        category="test",
        inputs=["brief"],
        outputs=["brief"],
    )
    role_name = "test_audit_role"
    role_title = "Audit Role"
    role_description = "Pipeline audit test role"
    system_prompt = "Return the audited response."


@register
class FailingAuditBlock(Block):
    meta = BlockMeta(
        name="test_failing_audit_block",
        description="Fails for structured error persistence",
        category="test",
        inputs=["brief"],
        outputs=[],
    )

    async def run(self, context):
        raise RuntimeError("audit failure")


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


async def test_pipeline_persists_role_execution_and_messages(session_factory, monkeypatch):
    import app.blocks.role_block as role_block_module

    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="audited response"),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )

    async def create(**kwargs):
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(role_block_module, "AsyncOpenAI", lambda **_: client)

    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_audit_role"],
        context={"brief": "audit me", "_verdict": "good"},
    )

    async with session_factory() as session:
        step = (
            await session.execute(select(JobStep).where(JobStep.job_id == job_id))
        ).scalar_one()
        role = (await session.execute(select(CreativeRole))).scalar_one()
        execution = (await session.execute(select(RoleExecution))).scalar_one()
        messages = (
            await session.execute(select(Message).order_by(Message.ordinal))
        ).scalars().all()

    assert json.loads(step.input_context)["_verdict"] == "good"
    assert role.name == "test_audit_role"
    assert execution.job_step_id == step.id
    assert execution.status == "completed"
    assert execution.output_deliverable == "audited response"
    assert execution.finish_reason == "stop"
    assert execution.total_tokens == 7
    assert [message.role.value for message in messages] == ["system", "user", "assistant"]
    assert messages[-1].content == "audited response"


async def test_pipeline_persists_structured_step_errors(session_factory):
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_failing_audit_block"],
        context={"brief": "fail"},
    )

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        step = (
            await session.execute(select(JobStep).where(JobStep.job_id == job_id))
        ).scalar_one()

    assert job.status == JobStatus.FAILED
    assert step.status == JobStatus.FAILED
    assert step.error == "audit failure"
    assert step.error_type == "RuntimeError"
    assert "RuntimeError: audit failure" in step.error_traceback


async def test_pipeline_persists_failed_llm_execution(session_factory, monkeypatch):
    import app.blocks.role_block as role_block_module

    async def create(**kwargs):
        raise ConnectionError("LLM unavailable")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(role_block_module, "AsyncOpenAI", lambda **_: client)

    await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_audit_role"],
        context={"brief": "audit failure"},
    )

    async with session_factory() as session:
        execution = (await session.execute(select(RoleExecution))).scalar_one()
        messages = (
            await session.execute(select(Message).order_by(Message.ordinal))
        ).scalars().all()

    assert execution.status == "failed"
    assert execution.error_type == "ConnectionError"
    assert execution.error == "LLM unavailable"
    assert execution.output_deliverable is None
    assert [message.role.value for message in messages] == ["system", "user"]
