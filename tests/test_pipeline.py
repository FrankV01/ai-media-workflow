"""
tests.test_pipeline — Pipeline execution engine tests

Uses a per-test throwaway SQLite file (tmp_path) so run_pipeline never
touches the real database. Covers: mid-run job retitling from
context["photo_shoot_name"], preservation of an explicit workflow name,
and the _job_id / _job_created_date context keys exposed to blocks.
"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.models.creative  # noqa: F401 — register models on Base.metadata
import app.models.job  # noqa: F401
import app.models.media  # noqa: F401
import app.pipeline.engine as engine_module
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import discover_blocks, register
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
        result = {
            "brief": "x",
            "seen_job_id": context.get("_job_id"),
            "seen_job_created_date": context.get("_job_created_date"),
        }
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
    discover_blocks()
    monkeypatch.setattr(engine_module, "async_session", factory)
    # Configuration providers built lazily inside blocks share this DB
    monkeypatch.setattr(app.database, "async_session", factory)
    # Keep the engine's auto-prepended job_namer step hermetic
    import app.blocks.role_block as role_block_module

    async def _create(**_):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Test Shoot"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    monkeypatch.setattr(role_block_module, "AsyncOpenAI", lambda **_: client)
    yield factory
    await engine.dispose()


async def _get_job(factory, job_id: int) -> Job:
    async with factory() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one()


@pytest.mark.asyncio
async def test_pipeline_retitles_job_from_block_output(session_factory):
    """A block that sets photo_shoot_name retitles an untitled job mid-run."""
    # No brief → the engine's job_namer step is skipped, so the test block
    # still owns the naming
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_namer_block"],
        context={},
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
        # Pre-named — the namer step is skipped, leaving a single step
        context={"brief": "c", "photo_shoot_name": "Ctx Shoot"},
    )

    async with session_factory() as session:
        result = await session.execute(select(JobStep).where(JobStep.job_id == job_id))
        step = result.scalar_one()
    output = json.loads(step.output)
    job = await _get_job(session_factory, job_id)
    assert output["seen_job_id"] == job_id
    assert output["seen_job_created_date"] == job.created_at.date().isoformat()


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
        # Pre-named — the audit assertions stay on a single step/role/execution
        context={"brief": "audit me", "_verdict": "good", "photo_shoot_name": "Audit Shoot"},
    )

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        step = (await session.execute(select(JobStep).where(JobStep.job_id == job_id))).scalar_one()
        role = (await session.execute(select(CreativeRole))).scalar_one()
        execution = (await session.execute(select(RoleExecution))).scalar_one()
        messages = (
            (await session.execute(select(Message).order_by(Message.ordinal))).scalars().all()
        )

    assert json.loads(step.input_context)["_verdict"] == "good"
    assert role.name == "test_audit_role"
    assert execution.job_step_id == step.id
    assert execution.status == "completed"
    assert execution.system_prompt == "Return the audited response."
    assert execution.configuration_source == "code_default"
    assert execution.configuration_id is not None
    warnings = json.loads(job.warnings)
    assert any("code-default" in w for w in warnings)
    assert execution.output_deliverable == "audited response"
    assert execution.finish_reason == "stop"
    assert execution.total_tokens == 7
    assert [message.role.value for message in messages] == ["system", "user", "assistant"]
    assert messages[-1].content == "audited response"


async def test_pipeline_persists_structured_step_errors(session_factory):
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_failing_audit_block"],
        # Pre-named — job_namer is skipped; the failing block is the only step
        context={"brief": "fail", "photo_shoot_name": "Fail Shoot"},
    )

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        step = (await session.execute(select(JobStep).where(JobStep.job_id == job_id))).scalar_one()

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
        # Pre-named — the single RoleExecution is the role's failed call
        context={"brief": "audit failure", "photo_shoot_name": "Fail Shoot"},
    )

    async with session_factory() as session:
        execution = (await session.execute(select(RoleExecution))).scalar_one()
        messages = (
            (await session.execute(select(Message).order_by(Message.ordinal))).scalars().all()
        )

    assert execution.status == "failed"
    assert execution.error_type == "ConnectionError"
    assert execution.error == "LLM unavailable"
    assert execution.output_deliverable is None
    assert [message.role.value for message in messages] == ["system", "user"]


@register
class VerdictBlock(Block):
    """Sets context['_verdict'] from context['_test_verdict'] for routing tests."""

    meta = BlockMeta(
        name="test_verdict_block",
        description="Sets _verdict — engine routing tests only",
        category="test",
        outputs=["_verdict"],
    )

    async def run(self, context):
        return {"_verdict": context.get("_test_verdict")}


class _StepBoundMarker(Block):
    """Echoes which JobStep row the engine bound it to."""

    async def run(self, context):
        return {"seen_job_step_id": context.get("_job_step_id")}


@register
class BranchMarkerBlock(_StepBoundMarker):
    meta = BlockMeta(name="test_branch_marker", category="test", outputs=["seen_job_step_id"])


@register
class AlwaysMarkerBlock(_StepBoundMarker):
    meta = BlockMeta(name="test_always_marker", category="test", outputs=["seen_job_step_id"])


@register
class TailMarkerBlock(_StepBoundMarker):
    meta = BlockMeta(name="test_tail_marker", category="test", outputs=["seen_job_step_id"])


@register
class SecondBranchMarkerBlock(_StepBoundMarker):
    meta = BlockMeta(
        name="test_second_branch_marker", category="test", outputs=["seen_job_step_id"]
    )


async def _job_steps(factory, job_id: int) -> list[JobStep]:
    async with factory() as session:
        result = await session.execute(
            select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
        )
        return list(result.scalars().all())


@pytest.mark.parametrize(
    ("verdict", "expected_names"),
    [
        (
            "good",
            [
                "test_verdict_block",
                "test_branch_marker",
                "test_always_marker",
                "test_tail_marker",
            ],
        ),
        ("bad", ["test_verdict_block", "test_always_marker", "test_tail_marker"]),
        (None, ["test_verdict_block", "test_always_marker", "test_tail_marker"]),
    ],
)
async def test_routing_binds_branch_steps_to_own_rows(session_factory, verdict, expected_names):
    """Branch blocks expanded mid-run execute on their own JobStep rows,
    ahead of the pre-created trailing step — in execution order."""
    context = {"brief": "c", "photo_shoot_name": "Route Shoot"}
    if verdict is not None:
        context["_test_verdict"] = verdict
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=[
            "test_verdict_block",
            {
                "on_good": ["test_branch_marker"],
                "on_bad": [],
                "always": ["test_always_marker"],
            },
            "test_tail_marker",
        ],
        context=context,
    )

    steps = await _job_steps(session_factory, job_id)
    assert [s.block_name for s in steps] == expected_names
    assert [s.order for s in steps] == list(range(len(expected_names)))
    for step in steps:
        assert step.status == JobStatus.COMPLETED
        if step.block_name == "test_verdict_block":
            continue
        assert json.loads(step.output)["seen_job_step_id"] == step.id


async def test_routing_multi_block_branch_resequences_later_steps(session_factory):
    """A multi-block branch inserts in order; later pending rows shift behind it."""
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=[
            "test_verdict_block",
            {
                "on_good": ["test_branch_marker", "test_second_branch_marker"],
                "always": [],
            },
            "test_always_marker",
            "test_tail_marker",
        ],
        context={
            "brief": "c",
            "photo_shoot_name": "Route Shoot",
            "_test_verdict": "good",
        },
    )

    steps = await _job_steps(session_factory, job_id)
    assert [s.block_name for s in steps] == [
        "test_verdict_block",
        "test_branch_marker",
        "test_second_branch_marker",
        "test_always_marker",
        "test_tail_marker",
    ]
    assert [s.order for s in steps] == [0, 1, 2, 3, 4]
    for step in steps[1:]:
        assert json.loads(step.output)["seen_job_step_id"] == step.id


async def test_precreated_pending_steps_visible_before_branch_expansion(session_factory):
    """Top-level steps after a routing dict are pre-created PENDING and keep
    that visibility while earlier blocks run."""

    @register
    class CapturingBlock(_StepBoundMarker):
        meta = BlockMeta(
            name="test_capturing_marker", category="test", outputs=["seen_job_step_id"]
        )

        async def run(self, context):
            async with app.database.async_session() as session:
                rows = (
                    (await session.execute(select(JobStep).order_by(JobStep.order))).scalars().all()
                )
            statuses = {row.block_name: row.status for row in rows}
            assert statuses["test_capturing_marker"] == JobStatus.RUNNING
            assert statuses["test_tail_marker"] == JobStatus.PENDING
            return {"_verdict": "good", "seen_job_step_id": context.get("_job_step_id")}

    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=[
            "test_capturing_marker",
            {"always": ["test_always_marker"]},
            "test_tail_marker",
        ],
        context={"brief": "c", "photo_shoot_name": "Route Shoot"},
    )

    steps = await _job_steps(session_factory, job_id)
    assert [s.block_name for s in steps] == [
        "test_capturing_marker",
        "test_always_marker",
        "test_tail_marker",
    ]
    assert all(s.status == JobStatus.COMPLETED for s in steps)


@register
class ReportFilesBlock(Block):
    """Test block that emits report_files for the engine to persist."""

    meta = BlockMeta(
        name="test_report_files_block",
        description="Emits report_files — engine tests only",
        category="test",
        inputs=["brief"],
        outputs=["report_files"],
    )

    async def run(self, context):
        return {"report_files": ["/tmp/x.md"]}


async def test_pipeline_persists_report_files(session_factory):
    """context['report_files'] is persisted to Job.report_files as JSON."""
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_report_files_block"],
        context={"brief": "c"},
    )

    job = await _get_job(session_factory, job_id)
    assert json.loads(job.report_files) == ["/tmp/x.md"]
