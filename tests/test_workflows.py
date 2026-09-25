"""
tests.test_workflows — Persistent workflow definitions

Covers the workflow service (CRUD, validation, clone, enable/disable,
default handling, lazy "Main" seeding), workflow-scoped configuration
profiles, engine integration (steps from the workflow row, job stamping,
disabled/unknown rejection), and the /api/workflow-definitions router.

The conftest `temp_config_db` fixture points app.database.async_session at
a throwaway SQLite file; engine tests additionally patch
engine_module.async_session so run_pipeline writes to the same test DB.
"""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.pipeline.engine as engine_module
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.database import Base
from app.models.job import Job, JobStatus, JobStep
from app.models.workflow import Workflow
from app.services.configuration.database import DatabaseConfigurationProvider
from app.services.workflows import (
    DEFAULT_WORKFLOW_STEPS,
    WorkflowConflictError,
    WorkflowDisabledError,
    WorkflowNotFoundError,
    WorkflowValidationError,
    clone_workflow,
    create_workflow,
    get_or_create_default_workflow,
    list_workflows,
    parse_steps_json,
    set_default,
    set_enabled,
    update_workflow,
    validate_steps,
)


@register
class WorkflowProbeBlock(Block):
    """Records the workflow context keys the engine injects."""

    meta = BlockMeta(
        name="test_workflow_probe",
        description="Echoes workflow scope — workflow tests only",
        category="test",
        inputs=["brief"],
        outputs=["brief", "seen_workflow_slug", "seen_workflow_media_model"],
    )

    async def run(self, context):
        return {
            "brief": "x",
            "seen_workflow_slug": context.get("_workflow_slug"),
            "seen_workflow_media_model": context.get("_workflow_media_model"),
        }


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    """Point the engine AND app session factories at a throwaway SQLite file."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/wf_test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "async_session", factory)
    monkeypatch.setattr(app.database, "async_session", factory)
    yield factory
    await engine.dispose()


# ── Step validation ──────────────────────────────────────────────────────


def test_validate_steps_accepts_default_pipeline():
    assert validate_steps(DEFAULT_WORKFLOW_STEPS) == []


def test_validate_steps_rejects_non_list():
    assert validate_steps("art_director") == ["steps must be a non-empty list"]
    assert validate_steps([]) == ["steps must be a non-empty list"]


def test_validate_steps_rejects_unknown_block():
    errors = validate_steps(["art_director", "no_such_block"])
    assert errors == ["step 2: unknown block 'no_such_block'"]


def test_validate_steps_rejects_bad_routing():
    errors = validate_steps(
        [
            "art_director",
            {"on_good": "media_producer"},  # branch must be a list
            {"bogus_key": []},  # unknown routing key
            {"on_bad": ["ghost_block"]},  # unknown block in a branch
            42,  # neither str nor dict
        ]
    )
    assert any("must be a list" in e for e in errors)
    assert any("unknown routing keys" in e for e in errors)
    assert any("unknown block 'ghost_block'" in e for e in errors)
    assert any("block name or a routing dict" in e for e in errors)


# ── Service: defaults, CRUD, clone, flags ────────────────────────────────


async def test_default_workflow_seeded_lazily(temp_config_db):
    async with temp_config_db() as session:
        workflow = await get_or_create_default_workflow(session)
        await session.commit()

    assert workflow.slug == "main"
    assert workflow.is_default is True
    assert workflow.is_enabled is True
    assert parse_steps_json(workflow.steps_json) == DEFAULT_WORKFLOW_STEPS


async def test_get_or_create_default_is_idempotent(temp_config_db):
    async with temp_config_db() as session:
        first = await get_or_create_default_workflow(session)
        await session.commit()
        second = await get_or_create_default_workflow(session)
        assert second.id == first.id


async def test_create_and_list_workflows(temp_config_db):
    async with temp_config_db() as session:
        main = await get_or_create_default_workflow(session)
        created = await create_workflow(
            session,
            name="Adobe Stock Illustration",
            description="Illustration assets",
            media_model_name="illustration.safetensors",
        )
        await session.commit()

        workflows = await list_workflows(session)

    assert created.slug == "adobe-stock-illustration"
    assert created.is_enabled is True
    assert created.is_default is False
    assert created.media_model_name == "illustration.safetensors"
    # Default first, then alphabetical
    assert [w.id for w in workflows] == [main.id, created.id]


async def test_create_defaults_steps_to_main_pipeline(temp_config_db):
    async with temp_config_db() as session:
        created = await create_workflow(session, name="Copy Flow")
        await session.commit()
    assert parse_steps_json(created.steps_json) == DEFAULT_WORKFLOW_STEPS


async def test_create_rejects_blank_name_and_bad_steps(temp_config_db):
    async with temp_config_db() as session:
        with pytest.raises(WorkflowValidationError):
            await create_workflow(session, name="   ")
        with pytest.raises(WorkflowValidationError) as exc:
            await create_workflow(session, name="Bad", steps=["nope"])
        assert "unknown block 'nope'" in str(exc.value)


async def test_create_rejects_duplicate_name(temp_config_db):
    async with temp_config_db() as session:
        await get_or_create_default_workflow(session)
        with pytest.raises(WorkflowValidationError):
            await create_workflow(session, name="Main")


async def test_update_workflow_fields_and_validation(temp_config_db):
    async with temp_config_db() as session:
        wf = await create_workflow(session, name="Editable")
        await update_workflow(
            session,
            wf.id,
            name="Renamed",
            description="d",
            steps=["echo"],
            media_model_name="other.safetensors",
        )
        await session.commit()
        assert wf.name == "Renamed"
        assert wf.slug == "editable"  # slug stays stable
        assert parse_steps_json(wf.steps_json) == ["echo"]
        assert wf.media_model_name == "other.safetensors"

        # Clearing the override: explicit None
        await update_workflow(session, wf.id, media_model_name=None)
        assert wf.media_model_name is None

        with pytest.raises(WorkflowValidationError):
            await update_workflow(session, wf.id, steps=[{"nope": []}])


async def test_update_missing_workflow_raises(temp_config_db):
    async with temp_config_db() as session:
        with pytest.raises(WorkflowNotFoundError):
            await update_workflow(session, 999, name="x")


async def test_clone_copies_definition_and_profiles(temp_config_db):
    provider = DatabaseConfigurationProvider()
    async with temp_config_db() as session:
        source = await create_workflow(session, name="Source", steps=["echo"])
        await session.commit()
        source_id = source.id

    # Custom LLM profile scoped to the source workflow
    from app.blocks.art_director import ArtDirector

    await provider.upsert_llm_configuration(
        ArtDirector().code_defaults(),
        system_prompt="Custom source prompt.",
        temperature=0.9,
        max_tokens=512,
        enable_thinking=False,
        workflow_id=source_id,
    )
    # Custom media profile scoped to the source workflow
    from app.blocks.media_producer import code_media_defaults

    await provider.upsert_media_configuration(
        "media_producer",
        "placeholder",
        "placeholder",
        code_media_defaults("media_producer", "placeholder", "placeholder").settings,
        workflow_id=source_id,
    )

    async with temp_config_db() as session:
        clone = await clone_workflow(session, source_id, "Cloned Flow")
        await session.commit()
        clone_id = clone.id

    assert clone.slug == "cloned-flow"
    assert parse_steps_json(clone.steps_json) == ["echo"]
    assert clone.id != source_id

    # Clone carries independent copies of the profiles
    clone_llm = await provider.resolve_llm(ArtDirector().code_defaults(), workflow_id=clone_id)
    assert clone_llm.system_prompt == "Custom source prompt."
    assert clone_llm.source == "custom"

    clone_media = await provider.resolve_media(
        code_media_defaults("media_producer", "placeholder", "placeholder"),
        workflow_id=clone_id,
    )
    assert clone_media.source == "custom"

    # Editing the clone does not leak back into the source
    await provider.upsert_llm_configuration(
        ArtDirector().code_defaults(),
        system_prompt="Clone-only prompt.",
        temperature=0.9,
        max_tokens=512,
        enable_thinking=False,
        workflow_id=clone_id,
    )
    source_llm = await provider.resolve_llm(ArtDirector().code_defaults(), workflow_id=source_id)
    assert source_llm.system_prompt == "Custom source prompt."


async def test_clone_rejects_duplicate_name_and_missing_source(temp_config_db):
    async with temp_config_db() as session:
        source = await create_workflow(session, name="Orig")
        await session.commit()
        with pytest.raises(WorkflowValidationError):
            await clone_workflow(session, source.id, "Orig")
        with pytest.raises(WorkflowNotFoundError):
            await clone_workflow(session, 999, "Whatever")


async def test_disable_and_enable(temp_config_db):
    async with temp_config_db() as session:
        wf = await create_workflow(session, name="Toggle")
        await session.commit()
        wf = await set_enabled(session, wf.id, False)
        assert wf.is_enabled is False
        wf = await set_enabled(session, wf.id, True)
        assert wf.is_enabled is True


async def test_cannot_disable_default_workflow(temp_config_db):
    async with temp_config_db() as session:
        main = await get_or_create_default_workflow(session)
        await session.commit()
        with pytest.raises(WorkflowConflictError):
            await set_enabled(session, main.id, False)


async def test_set_default_swaps_flag_and_enables(temp_config_db):
    async with temp_config_db() as session:
        main = await get_or_create_default_workflow(session)
        other = await create_workflow(session, name="Other")
        await session.commit()
        await set_enabled(session, other.id, False)

        promoted = await set_default(session, other.id)
        await session.commit()

        assert promoted.is_default is True
        assert promoted.is_enabled is True
        await session.refresh(main)
        assert main.is_default is False


# ── Provider scoping ─────────────────────────────────────────────────────


async def test_llm_profiles_do_not_leak_across_workflows(temp_config_db):
    from app.blocks.art_director import ArtDirector

    provider = DatabaseConfigurationProvider()
    async with temp_config_db() as session:
        a = await create_workflow(session, name="Flow A")
        b = await create_workflow(session, name="Flow B")
        await session.commit()
        a_id, b_id = a.id, b.id

    defaults = ArtDirector().code_defaults()
    await provider.upsert_llm_configuration(
        defaults,
        system_prompt="Prompt for A.",
        temperature=0.7,
        max_tokens=100,
        enable_thinking=False,
        workflow_id=a_id,
    )

    resolved_a = await provider.resolve_llm(defaults, workflow_id=a_id)
    resolved_b = await provider.resolve_llm(defaults, workflow_id=b_id)
    assert resolved_a.system_prompt == "Prompt for A."
    assert resolved_a.workflow_id == a_id
    # B has no custom profile — seeded from code defaults
    assert resolved_b.system_prompt == defaults.system_prompt
    assert resolved_b.workflow_id == b_id
    assert resolved_b.source == "code_default"


async def test_unknown_workflow_id_rejected(temp_config_db):
    from app.blocks.art_director import ArtDirector

    provider = DatabaseConfigurationProvider()
    with pytest.raises(WorkflowNotFoundError):
        await provider.resolve_llm(ArtDirector().code_defaults(), workflow_id=999)


# ── Engine integration ───────────────────────────────────────────────────


async def test_engine_stamps_job_workflow_and_scopes_context(session_factory):
    async with session_factory() as session:
        wf = await create_workflow(
            session,
            name="Probe Flow",
            steps=["test_workflow_probe"],
            media_model_name="illustration.safetensors",
        )
        await session.commit()
        wf_id = wf.id

    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        workflow_id=wf_id,
        context={"brief": "go"},
    )

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        step = (await session.execute(select(JobStep).where(JobStep.job_id == job_id))).scalar_one()

    assert job.status == JobStatus.COMPLETED
    assert job.workflow_id == wf_id
    output = json.loads(step.output)
    assert output["seen_workflow_slug"] == "probe-flow"
    assert output["seen_workflow_media_model"] == "illustration.safetensors"


async def test_engine_uses_workflow_steps_when_block_names_omitted(session_factory):
    async with session_factory() as session:
        wf = await create_workflow(session, name="Steps Flow", steps=["test_workflow_probe"])
        await session.commit()
        wf_id = wf.id

    job_id = await engine_module.run_pipeline(
        workflow_name=None, workflow_id=wf_id, context={"brief": "go"}
    )

    async with session_factory() as session:
        steps = (
            (
                await session.execute(
                    select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
                )
            )
            .scalars()
            .all()
        )
    assert [s.block_name for s in steps] == ["test_workflow_probe"]


async def test_engine_falls_back_to_default_workflow(session_factory):
    """No workflow_id → the lazily-seeded default workflow scopes the run."""
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["test_workflow_probe"],
        context={"brief": "go"},
    )

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        workflows = (await session.execute(select(Workflow))).scalars().all()

    assert len(workflows) == 1
    assert workflows[0].slug == "main"
    assert job.workflow_id == workflows[0].id


async def test_engine_rejects_disabled_workflow(session_factory):
    async with session_factory() as session:
        wf = await create_workflow(session, name="Off Flow", steps=["test_workflow_probe"])
        await session.commit()
        await set_enabled(session, wf.id, False)
        await session.commit()
        wf_id = wf.id

    with pytest.raises(WorkflowDisabledError):
        await engine_module.run_pipeline(
            workflow_name=None, workflow_id=wf_id, context={"brief": "go"}
        )

    # No orphaned PENDING job row
    async with session_factory() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
    assert jobs == []


async def test_engine_rejects_unknown_workflow(session_factory):
    with pytest.raises(WorkflowNotFoundError):
        await engine_module.run_pipeline(
            workflow_name=None, workflow_id=999, context={"brief": "go"}
        )


# ── REST API ─────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(monkeypatch, session_factory):
    """TestClient with the engine's execution stubbed and DBs redirected."""
    from fastapi.testclient import TestClient

    import app.main

    spy = AsyncMock()
    monkeypatch.setattr(engine_module, "_guarded_execute", spy)
    client = TestClient(app.main.app, raise_server_exceptions=False)
    yield client, spy


def test_api_workflow_definitions_crud(api_client):
    client, _ = api_client

    created = client.post(
        "/api/workflow-definitions/",
        json={"name": "API Flow", "steps": ["echo"], "description": "via api"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"] == "api-flow"
    assert body["steps"] == ["echo"]
    assert body["is_enabled"] is True

    listing = client.get("/api/workflow-definitions/")
    slugs = [w["slug"] for w in listing.json()["workflows"]]
    assert "api-flow" in slugs

    detail = client.get("/api/workflow-definitions/api-flow")
    assert detail.status_code == 200
    assert detail.json()["name"] == "API Flow"

    updated = client.put("/api/workflow-definitions/api-flow", json={"description": "new"})
    assert updated.status_code == 200
    assert updated.json()["description"] == "new"

    bad = client.post(
        "/api/workflow-definitions/",
        json={"name": "API Flow", "steps": ["echo"]},
    )
    assert bad.status_code == 422


def test_api_workflow_clone_enable_disable(api_client):
    client, _ = api_client
    client.post("/api/workflow-definitions/", json={"name": "Base", "steps": ["echo"]})

    clone = client.post("/api/workflow-definitions/base/clone", json={"name": "Copy"})
    assert clone.status_code == 201
    assert clone.json()["slug"] == "copy"
    assert clone.json()["steps"] == ["echo"]

    disabled = client.post("/api/workflow-definitions/copy/disable")
    assert disabled.status_code == 200
    assert disabled.json()["is_enabled"] is False

    enabled = client.post("/api/workflow-definitions/copy/enable")
    assert enabled.json()["is_enabled"] is True


def test_api_cannot_disable_default(api_client):
    client, _ = api_client
    # Seed the default through a run first
    client.post("/api/workflow-definitions/", json={"name": "X", "steps": ["echo"]})
    set_default = client.post("/api/workflow-definitions/x/set-default")
    assert set_default.status_code == 200
    assert set_default.json()["is_default"] is True

    resp = client.post("/api/workflow-definitions/x/disable")
    assert resp.status_code == 409


def test_api_run_with_workflow_slug(api_client):
    client, spy = api_client
    client.post("/api/workflow-definitions/", json={"name": "Run Me", "steps": ["echo"]})

    resp = client.post(
        "/api/workflows/run",
        json={"photo_shoot_name": "S", "workflow": "run-me"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["job_id"]


def test_api_run_rejects_disabled_and_unknown_workflow(api_client):
    client, _ = api_client
    client.post("/api/workflow-definitions/", json={"name": "Off", "steps": ["echo"]})
    client.post("/api/workflow-definitions/off/disable")

    resp = client.post("/api/workflows/run", json={"photo_shoot_name": "S", "workflow": "off"})
    assert resp.status_code == 409

    resp = client.post("/api/workflows/run", json={"photo_shoot_name": "S", "workflow": "ghost"})
    assert resp.status_code == 404


def test_api_run_rejects_invalid_block_names(api_client):
    client, _ = api_client
    resp = client.post(
        "/api/workflows/run",
        json={"photo_shoot_name": "S", "block_names": ["no_such_block"]},
    )
    assert resp.status_code == 422


def test_api_jobs_list_scoped_to_workflow(api_client):
    client, _ = api_client
    client.post("/api/workflow-definitions/", json={"name": "Scoped", "steps": ["echo"]})
    client.post("/api/workflows/run", json={"photo_shoot_name": "A", "workflow": "scoped"})
    client.post("/api/workflows/run", json={"photo_shoot_name": "B"})  # default wf

    scoped = client.get("/api/workflows/jobs?workflow=scoped")
    assert scoped.status_code == 200
    names = [j["workflow"]["slug"] for j in scoped.json()]
    assert names == ["scoped"]

    all_jobs = client.get("/api/workflows/jobs")
    assert len(all_jobs.json()) == 2
    by_slug = {j["workflow"]["slug"] for j in all_jobs.json()}
    assert by_slug == {"scoped", "main"}


# ── Web pages (template smoke tests) ─────────────────────────────────────


def test_workflows_pages_render(api_client):
    client, _ = api_client
    client.post(
        "/api/workflow-definitions/",
        json={"name": "Page Flow", "steps": ["echo"], "description": "d"},
    )

    listing = client.get("/workflows")
    assert listing.status_code == 200
    assert "Page Flow" in listing.text
    assert "page-flow" in listing.text

    detail = client.get("/api/workflow-definitions/page-flow")
    wf_id = detail.json()["id"]
    edit = client.get(f"/workflows/{wf_id}")
    assert edit.status_code == 200
    assert "echo" in edit.text
    assert "test_workflow_probe" in edit.text  # block palette

    dashboard = client.get("/?workflow=page-flow")
    assert dashboard.status_code == 200
    assert "Page Flow" in dashboard.text

    settings_page = client.get("/settings/ai?workflow=page-flow")
    assert settings_page.status_code == 200
    assert "Page Flow" in settings_page.text


def test_workflows_web_clone_and_disable(api_client):
    client, _ = api_client
    client.post("/api/workflow-definitions/", json={"name": "Src", "steps": ["echo"]})
    src_id = client.get("/api/workflow-definitions/src").json()["id"]

    clone = client.post(
        f"/workflows/{src_id}/clone",
        data={"name": "Web Clone"},
        follow_redirects=False,
    )
    assert clone.status_code == 303
    assert clone.headers["location"].startswith("/workflows/")

    clone_id = int(clone.headers["location"].split("/")[2].split("?")[0])
    disable = client.post(f"/workflows/{clone_id}/disable", follow_redirects=False)
    assert disable.status_code == 303

    # Disabled workflow can't run
    resp = client.post(
        "/api/workflows/run", json={"photo_shoot_name": "S", "workflow": "web-clone"}
    )
    assert resp.status_code == 409


def test_workflows_create_rejects_bad_steps(api_client):
    client, _ = api_client
    resp = client.post(
        "/workflows/create",
        data={"name": "Bad Steps", "steps": '["ghost_block"]'},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "status=error" in resp.headers["location"]
