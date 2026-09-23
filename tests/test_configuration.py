"""
tests.test_configuration — Database-backed AI configuration tests

Covers: profile seeding on first use, idempotent re-resolution, custom
profiles silencing warnings, reset re-enabling warnings, (role, model)
isolation, 0.0 temperature, immutable execution snapshots across edits,
failed-LLM snapshots, media precedence/injection/audit, the REST API, the
audited LLM change history (actions, origins, before/after snapshots, the
history endpoint, and the web/API/non-overwrite invariants), effective
LLM execution metadata on job detail, and environment output
configuration (IMAGE_OUTPUT_DIR).
"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.pipeline.engine as engine_module
from app.blocks.base import BlockMeta
from app.blocks.media_producer import MediaProducer, _build_requests
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock
from app.config import Settings, settings
from app.database import Base
from app.models.creative import (
    CreativeRole,
    LlmConfigurationChange,
    LlmRoleConfiguration,
    RoleExecution,
)
from app.models.job import Job, JobStatus
from app.models.media import MediaGenerationExecution, MediaModelConfiguration
from app.services.configuration.base import (
    LlmRoleDefaults,
    MediaDefaults,
    MediaProfileSettings,
)
from app.services.configuration.database import DatabaseConfigurationProvider


def _fake_llm_client(content: str, captured: dict | None = None):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    response = SimpleNamespace(choices=[choice], usage=usage)

    class FakeCompletions:
        async def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return response

    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))


def _patch_llm(monkeypatch, content="ok", captured=None):
    import app.blocks.role_block as role_block_module

    monkeypatch.setattr(
        role_block_module, "AsyncOpenAI", lambda **_: _fake_llm_client(content, captured)
    )


@register
class ConfigTestRole(RoleBlock):
    meta = BlockMeta(
        name="test_config_role",
        description="Configuration test role",
        category="test",
        inputs=["brief"],
        outputs=["brief"],
    )
    role_name = "test_config_role"
    role_title = "Config Role"
    role_description = "Role for configuration tests"
    system_prompt = "Config test system prompt."


def _defaults(model="model-a", temperature=0.5) -> LlmRoleDefaults:
    return LlmRoleDefaults(
        role_name="test_config_role",
        role_title="Config Role",
        role_description="Role for configuration tests",
        output_format="text",
        suggested_next_role=None,
        model_name=model,
        system_prompt="Config test system prompt.",
        temperature=temperature,
        max_tokens=1024,
        enable_thinking=False,
    )


def test_image_output_dir_defaults_to_comfyui_output(monkeypatch):
    monkeypatch.delenv("IMAGE_OUTPUT_DIR", raising=False)

    configured = Settings(_env_file=None)

    assert configured.image_output_dir == Path("/Volumes/SanDisk Mac AI/ComfyUI/output")


def test_image_output_dir_environment_override(monkeypatch, tmp_path):
    output_dir = tmp_path / "generated"
    monkeypatch.setenv("IMAGE_OUTPUT_DIR", str(output_dir))

    configured = Settings(_env_file=None)

    assert configured.image_output_dir == output_dir


def test_image_output_dir_rejects_relative_path(monkeypatch):
    monkeypatch.setenv("IMAGE_OUTPUT_DIR", "./data/output")

    with pytest.raises(ValueError, match="IMAGE_OUTPUT_DIR must be an absolute path"):
        Settings(_env_file=None)


# ── Provider behavior ────────────────────────────────────────────────────


async def test_missing_profile_seeded_and_used_immediately(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    resolved = await provider.resolve_llm(_defaults())

    assert resolved.source == "code_default"
    assert resolved.warning and "/settings/ai" in resolved.warning
    assert resolved.system_prompt == "Config test system prompt."

    async with temp_config_db() as session:
        config = (await session.execute(select(LlmRoleConfiguration))).scalar_one()
    assert config.uses_code_defaults is True
    assert config.model_name == "model-a"


async def test_resolve_is_idempotent(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    first = await provider.resolve_llm(_defaults())
    second = await provider.resolve_llm(_defaults())

    assert first.configuration_id == second.configuration_id
    async with temp_config_db() as session:
        rows = (await session.execute(select(LlmRoleConfiguration))).scalars().all()
        roles = (await session.execute(select(CreativeRole))).scalars().all()
    assert len(rows) == 1
    assert len(roles) == 1


async def test_custom_profile_silences_warning(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="Custom prompt.",
        temperature=0.9,
        max_tokens=512,
        enable_thinking=True,
    )

    resolved = await provider.resolve_llm(_defaults())
    assert resolved.source == "custom"
    assert resolved.warning is None
    assert resolved.system_prompt == "Custom prompt."
    assert resolved.temperature == 0.9


async def test_reset_restores_warning(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="Custom prompt.",
        temperature=0.9,
        max_tokens=512,
        enable_thinking=False,
    )
    await provider.reset_llm_configuration(_defaults())

    resolved = await provider.resolve_llm(_defaults())
    assert resolved.source == "code_default"
    assert resolved.warning
    assert resolved.system_prompt == "Config test system prompt."


async def test_concurrent_resolves_create_single_profile(temp_config_db):
    """Two providers racing get-or-create must yield one role + one profile."""
    import asyncio

    provider_a = DatabaseConfigurationProvider(temp_config_db)
    provider_b = DatabaseConfigurationProvider(temp_config_db)

    first, second = await asyncio.gather(
        provider_a.resolve_llm(_defaults()),
        provider_b.resolve_llm(_defaults()),
    )
    assert first.configuration_id == second.configuration_id

    async with temp_config_db() as session:
        rows = (await session.execute(select(LlmRoleConfiguration))).scalars().all()
        roles = (await session.execute(select(CreativeRole))).scalars().all()
    assert len(rows) == 1
    assert len(roles) == 1


async def test_role_and_model_isolation(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults(model="model-a"))
    await provider.resolve_llm(_defaults(model="other/model-b"))

    other_defaults = replace(_defaults(), role_name="other_role")
    await provider.resolve_llm(other_defaults)

    async with temp_config_db() as session:
        rows = (await session.execute(select(LlmRoleConfiguration))).scalars().all()
    assert len(rows) == 3
    assert {r.model_name for r in rows} == {"model-a", "other/model-b", "model-a"}


async def test_zero_temperature_is_valid(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    resolved = await provider.resolve_llm(_defaults(temperature=0.0))
    assert resolved.temperature == 0.0

    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="p",
        temperature=0.0,
        max_tokens=10,
        enable_thinking=False,
    )
    resolved = await provider.resolve_llm(_defaults(temperature=0.7))
    assert resolved.temperature == 0.0


# ── RoleBlock wiring ─────────────────────────────────────────────────────


async def test_role_block_run_uses_profile_and_warns(monkeypatch):
    captured = {}
    _patch_llm(monkeypatch, captured=captured)
    context = {"brief": "hello"}
    result = await ConfigTestRole().run(context)

    assert result["brief"] == "ok"
    assert captured["model"] == settings.llm_model
    record = result["_executions"][-1]
    assert record["configuration_source"] == "code_default"
    assert record["configuration_id"]
    assert record["system_prompt"] == "Config test system prompt."
    assert any("code-default" in w for w in context["_warnings"])


async def test_role_block_custom_profile_no_warning(monkeypatch):
    provider = DatabaseConfigurationProvider()
    await provider.resolve_llm(ConfigTestRole().code_defaults())
    await provider.upsert_llm_configuration(
        ConfigTestRole().code_defaults(),
        system_prompt="My custom prompt.",
        temperature=0.0,
        max_tokens=64,
        enable_thinking=True,
    )

    captured = {}
    _patch_llm(monkeypatch, captured=captured)
    context = {"brief": "hello"}
    result = await ConfigTestRole(provider=provider).run(context)

    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 64
    assert "reasoning_effort" not in captured  # thinking enabled → omitted
    record = result["_executions"][-1]
    assert record["configuration_source"] == "custom"
    assert record["system_prompt"] == "My custom prompt."
    assert "_warnings" not in context


# ── Engine snapshot immutability ─────────────────────────────────────────


@pytest.fixture
async def engine_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/engine.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "async_session", factory)
    monkeypatch.setattr(app.database, "async_session", factory)
    yield factory
    await engine.dispose()


async def test_executions_around_edit_keep_distinct_snapshots(engine_db):
    # first execution with code defaults, then an edit, then a second run
    import app.blocks.role_block as role_block_module

    provider = DatabaseConfigurationProvider(engine_db)

    async def _run():
        return await engine_module.run_pipeline(
            workflow_name=None,
            block_names=["test_config_role"],
            context={"brief": "audit"},
        )

    from unittest.mock import patch

    with patch.object(role_block_module, "AsyncOpenAI", lambda **_: _fake_llm_client("one")):
        await _run()

    await provider.upsert_llm_configuration(
        ConfigTestRole().code_defaults(),
        system_prompt="Edited prompt.",
        temperature=0.3,
        max_tokens=99,
        enable_thinking=False,
    )

    with patch.object(role_block_module, "AsyncOpenAI", lambda **_: _fake_llm_client("two")):
        await _run()

    async with engine_db() as session:
        executions = (
            (await session.execute(select(RoleExecution).order_by(RoleExecution.id)))
            .scalars()
            .all()
        )
        role = (await session.execute(select(CreativeRole))).scalar_one()

    assert len(executions) == 2
    assert executions[0].system_prompt == "Config test system prompt."
    assert executions[0].configuration_source == "code_default"
    assert executions[1].system_prompt == "Edited prompt."
    assert executions[1].configuration_source == "custom"
    # Same profile row, CreativeRole untouched by the edit
    assert executions[0].configuration_id == executions[1].configuration_id
    assert role.name == "test_config_role"


async def test_failed_llm_call_persists_snapshot(engine_db):
    import app.blocks.role_block as role_block_module

    async def create(**kwargs):
        raise ConnectionError("down")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    from unittest.mock import patch

    with patch.object(role_block_module, "AsyncOpenAI", lambda **_: client):
        job_id = await engine_module.run_pipeline(
            workflow_name=None,
            block_names=["test_config_role"],
            context={"brief": "fail"},
        )

    async with engine_db() as session:
        job = await session.get(Job, job_id)
        execution = (await session.execute(select(RoleExecution))).scalar_one()

    assert job.status == JobStatus.FAILED
    assert execution.status == "failed"
    assert execution.system_prompt == "Config test system prompt."
    assert execution.configuration_source == "code_default"
    assert json.loads(job.warnings)


# ── Media configuration ──────────────────────────────────────────────────


def _media_defaults(backend="placeholder", model="placeholder") -> MediaDefaults:
    from app.blocks.media_producer import code_media_defaults

    return code_media_defaults("media_producer", backend, model)


async def test_media_profile_seeded_and_warns(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    resolved = await provider.resolve_media(_media_defaults())

    assert resolved.source == "code_default"
    assert resolved.warning and "/settings/ai" in resolved.warning
    assert resolved.settings.steps == 69

    async with temp_config_db() as session:
        row = (await session.execute(select(MediaModelConfiguration))).scalar_one()
    assert row.model_name == "placeholder"


def test_build_requests_prefers_explicit_params_over_profile():
    profile = MediaProfileSettings(
        width=512,
        height=512,
        cfg_scale=3.0,
        steps=11,
        sampler="euler",
        scheduler="normal",
        clip_skip=2,
    )
    parsed = {
        "positive_prompt": "p",
        "parameters": {"steps": 99, "width": 64},
    }
    req = _build_requests(parsed, profile=profile)[0]
    assert req.steps == 99  # explicit param wins
    assert req.width == 64
    assert req.cfg_scale == 3.0  # profile default
    assert req.sampler == "euler"


def test_build_requests_falls_back_to_code_defaults():
    req = _build_requests({"positive_prompt": "p"})[0]
    assert req.steps == 69
    assert req.width == 1024


def test_comfyui_backend_uses_injected_workflow_config():
    from app.services.generation.comfyui import ComfyUIBackend
    from app.services.generation.sdxl_workflow import SdxlWorkflowConfig

    cfg = SdxlWorkflowConfig(
        base_checkpoint="injected.safetensors",
        refiner_checkpoint="injected-refiner.safetensors",
        upscale_2x_model="x2.pth",
        upscale_4x_model="x4.pth",
        refiner_steps=42,
    )
    backend = ComfyUIBackend(workflow_config=cfg)
    assert backend.workflow_config is cfg

    from app.services.generation.factory import get_backend

    via_factory = get_backend("comfyui", workflow_config=cfg)
    assert via_factory.workflow_config is cfg


async def test_media_producer_placeholder_profile_and_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    context = {
        "prompt_architect_output": json.dumps({"positive_prompt": "a rose"}),
        "_generation_backend": "placeholder",
    }
    result = await MediaProducer().run(context)

    assert result["generated_images"]
    records = context["_media_executions"]
    assert len(records) == 1
    assert records[0]["status"] == "completed"
    assert records[0]["model_name"] == "placeholder"
    assert records[0]["backend_name"] == "placeholder"
    assert records[0]["configuration_source"] == "code_default"
    snapshot = json.loads(records[0]["settings_snapshot"])
    assert snapshot["profile"]["steps"] == 69
    assert any("code-default" in w for w in context["_warnings"])


async def test_media_producer_failure_audit_record(monkeypatch, tmp_path):
    import app.blocks.media_producer as mp_module

    monkeypatch.setattr(settings, "image_output_dir", tmp_path)

    class FailingBackend:
        name = "placeholder"

        async def generate(self, request):
            raise RuntimeError("backend exploded")

        async def is_available(self):
            return True

    monkeypatch.setattr(mp_module, "get_backend", lambda *a, **k: FailingBackend())

    context = {
        "prompt_architect_output": json.dumps({"positive_prompt": "a rose"}),
        "_generation_backend": "placeholder",
    }
    with pytest.raises(RuntimeError, match="backend exploded"):
        await MediaProducer().run(context)

    record = context["_media_executions"][-1]
    assert record["status"] == "failed"
    assert record["error"] == "backend exploded"
    assert record["finished_at"] is not None


async def test_engine_persists_media_executions(engine_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    job_id = await engine_module.run_pipeline(
        workflow_name=None,
        block_names=["media_producer"],
        context={
            "prompt_architect_output": json.dumps({"positive_prompt": "a rose"}),
            "_generation_backend": "placeholder",
        },
    )

    async with engine_db() as session:
        job = await session.get(Job, job_id)
        media_execs = (await session.execute(select(MediaGenerationExecution))).scalars().all()

    assert job.status == JobStatus.COMPLETED
    assert len(media_execs) == 1
    assert media_execs[0].status == "completed"
    assert media_execs[0].image_paths
    assert json.loads(media_execs[0].settings_snapshot)["profile"]["steps"] == 69
    assert json.loads(job.warnings)


# ── REST API ─────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_api_llm_put_get_reset(client):
    payload = {
        "model_name": "vendor/model-with-slash",
        "system_prompt": "Custom.",
        "temperature": 0.4,
        "max_tokens": 256,
        "enable_thinking": False,
    }
    resp = client.put("/api/configurations/llm/art_director", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "custom"
    assert body["uses_code_defaults"] is False
    assert body["is_selected"] is False  # not the globally selected model

    listing = client.get("/api/configurations/llm").json()
    match = [p for p in listing["profiles"] if p["model_name"] == "vendor/model-with-slash"]
    assert match and match[0]["system_prompt"] == "Custom."

    reset = client.post(
        "/api/configurations/llm/art_director/reset",
        json={"model_name": "vendor/model-with-slash"},
    )
    assert reset.status_code == 200
    assert reset.json()["uses_code_defaults"] is True


def test_api_llm_validation(client):
    resp = client.put(
        "/api/configurations/llm/art_director",
        json={
            "model_name": "m",
            "system_prompt": "p",
            "temperature": 5.0,
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 422

    resp = client.put(
        "/api/configurations/llm/no_such_role",
        json={
            "model_name": "m",
            "system_prompt": "p",
            "temperature": 0.5,
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 404


def test_api_media_put_validation_and_reset(client):
    bad = client.put(
        "/api/configurations/media/media_producer",
        json={
            "backend_name": "comfyui",
            "model_name": "m.safetensors",
            "width": 0,
            "height": 512,
            "cfg_scale": 7.0,
            "steps": 20,
            "sampler": "dpmpp_2m",
            "scheduler": "karras",
            "clip_skip": 1,
        },
    )
    assert bad.status_code == 422

    missing_comfy = client.put(
        "/api/configurations/media/media_producer",
        json={
            "backend_name": "comfyui",
            "model_name": "m.safetensors",
            "width": 512,
            "height": 512,
            "cfg_scale": 7.0,
            "steps": 20,
            "sampler": "dpmpp_2m",
            "scheduler": "karras",
            "clip_skip": 1,
        },
    )
    assert missing_comfy.status_code == 422

    good = client.put(
        "/api/configurations/media/media_producer",
        json={
            "backend_name": "placeholder",
            "model_name": "placeholder",
            "width": 512,
            "height": 512,
            "cfg_scale": 5.0,
            "steps": 10,
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": 1,
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["source"] == "custom"

    reset = client.post(
        "/api/configurations/media/media_producer/reset",
        json={"backend_name": "placeholder", "model_name": "placeholder"},
    )
    assert reset.status_code == 200
    assert reset.json()["uses_code_defaults"] is True


async def test_api_job_detail_includes_warnings(client, engine_db):
    # Seed a job row directly against the patched DB
    async with engine_db() as session:
        job = Job(
            workflow_name="w",
            status=JobStatus.COMPLETED,
            warnings=json.dumps(["warn one"]),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    resp = client.get(f"/api/workflows/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["warnings"] == ["warn one"]


def test_settings_ai_page_renders(client):
    resp = client.get("/settings/ai")
    assert resp.status_code == 200
    assert "AI Settings" in resp.text
    assert settings.llm_model in resp.text


async def test_api_put_populates_role_metadata_on_empty_db(client, temp_config_db):
    """A custom profile created before any execution still upserts role metadata."""
    resp = client.put(
        "/api/configurations/llm/art_director",
        json={
            "model_name": "vendor/new-model",
            "system_prompt": "Custom.",
            "temperature": 0.5,
            "max_tokens": 128,
            "enable_thinking": False,
        },
    )
    assert resp.status_code == 200, resp.text

    async with temp_config_db() as session:
        role = (
            await session.execute(select(CreativeRole).where(CreativeRole.name == "art_director"))
        ).scalar_one()
    assert role.title == "Art Director"
    assert role.description
    assert role.output_format == "text"


def test_api_media_unknown_block_404(client):
    resp = client.put(
        "/api/configurations/media/not_a_block",
        json={
            "backend_name": "placeholder",
            "model_name": "placeholder",
            "width": 512,
            "height": 512,
            "cfg_scale": 5.0,
            "steps": 10,
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": 1,
        },
    )
    assert resp.status_code == 404

    resp = client.post(
        "/api/configurations/media/not_a_block/reset",
        json={"backend_name": "placeholder", "model_name": "placeholder"},
    )
    assert resp.status_code == 404


def test_api_media_unsupported_backend_422(client):
    resp = client.put(
        "/api/configurations/media/media_producer",
        json={
            "backend_name": "midjourney",
            "model_name": "m",
            "width": 512,
            "height": 512,
            "cfg_scale": 5.0,
            "steps": 10,
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": 1,
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/configurations/media/media_producer/reset",
        json={"backend_name": "midjourney", "model_name": "m"},
    )
    assert resp.status_code == 422


def test_api_media_whitespace_strings_422(client):
    resp = client.put(
        "/api/configurations/media/media_producer",
        json={
            "backend_name": "placeholder",
            "model_name": "   ",
            "width": 512,
            "height": 512,
            "cfg_scale": 5.0,
            "steps": 10,
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": 1,
        },
    )
    assert resp.status_code == 422

    resp = client.put(
        "/api/configurations/llm/art_director",
        json={
            "model_name": "m",
            "system_prompt": "   ",
            "temperature": 0.5,
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 422


def test_web_llm_save_invalid_temperature_422(client):
    resp = client.post(
        "/settings/ai/llm/save",
        data={
            "role_name": "art_director",
            "model_name": "m",
            "system_prompt": "p",
            "temperature": "5.0",
            "max_tokens": "100",
        },
    )
    assert resp.status_code == 422


def test_web_llm_save_valid_redirects(client):
    resp = client.post(
        "/settings/ai/llm/save",
        data={
            "role_name": "art_director",
            "model_name": "vendor/web-model",
            "system_prompt": "Custom prompt.",
            "temperature": "0.4",
            "max_tokens": "256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/settings/ai?")
    assert "status=saved" in location
    assert "kind=llm" in location
    assert "name=art_director" in location
    assert "model=vendor%2Fweb-model" in location  # model key is URL-encoded


def test_web_media_save_invalid_denoise_422(client):
    resp = client.post(
        "/settings/ai/media/save",
        data={
            "backend_name": "comfyui",
            "model_name": "m.safetensors",
            "width": "512",
            "height": "512",
            "cfg_scale": "5.0",
            "steps": "10",
            "sampler": "dpmpp_2m",
            "scheduler": "karras",
            "clip_skip": "1",
            "refiner_checkpoint": "r.safetensors",
            "upscale_2x_model": "x2.pth",
            "upscale_4x_model": "x4.pth",
            "refiner_steps": "20",
            "refiner_cfg_scale": "6.0",
            "refiner_sampler": "dpmpp_2m",
            "refiner_scheduler": "karras",
            "refiner_denoise": "2.5",
        },
    )
    assert resp.status_code == 422


def test_web_media_save_comfy_missing_fields_422(client):
    resp = client.post(
        "/settings/ai/media/save",
        data={
            "backend_name": "comfyui",
            "model_name": "m.safetensors",
            "width": "512",
            "height": "512",
            "cfg_scale": "5.0",
            "steps": "10",
            "sampler": "dpmpp_2m",
            "scheduler": "karras",
            "clip_skip": "1",
        },
    )
    assert resp.status_code == 422


async def test_web_media_save_creates_another_model_profile(client, temp_config_db):
    resp = client.post(
        "/settings/ai/media/save",
        data={
            "backend_name": "placeholder",
            "model_name": "other-model",
            "width": "256",
            "height": "256",
            "cfg_scale": "4.0",
            "steps": "8",
            "sampler": "euler",
            "scheduler": "normal",
            "clip_skip": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with temp_config_db() as session:
        row = (
            await session.execute(
                select(MediaModelConfiguration).where(
                    MediaModelConfiguration.model_name == "other-model"
                )
            )
        ).scalar_one()
    assert row.uses_code_defaults is False
    assert json.loads(row.settings_json)["steps"] == 8


def test_settings_ai_page_has_media_prepare_form(client):
    resp = client.get("/settings/ai")
    assert resp.status_code == 200
    assert "Prepare a media profile for another model" in resp.text
    assert 'name="backend_name"' in resp.text


# ── Normalization + defense-in-depth ─────────────────────────────────────


async def test_concurrent_custom_upserts_single_profile(temp_config_db):
    """Two providers racing a custom upsert must yield one profile row."""
    import asyncio

    provider_a = DatabaseConfigurationProvider(temp_config_db)
    provider_b = DatabaseConfigurationProvider(temp_config_db)

    await asyncio.gather(
        provider_a.upsert_llm_configuration(
            _defaults(),
            system_prompt="Custom A.",
            temperature=0.5,
            max_tokens=64,
            enable_thinking=False,
        ),
        provider_b.upsert_llm_configuration(
            _defaults(),
            system_prompt="Custom B.",
            temperature=0.6,
            max_tokens=128,
            enable_thinking=True,
        ),
    )

    async with temp_config_db() as session:
        rows = (await session.execute(select(LlmRoleConfiguration))).scalars().all()
        roles = (await session.execute(select(CreativeRole))).scalars().all()
    assert len(rows) == 1
    assert len(roles) == 1
    assert rows[0].uses_code_defaults is False


async def test_provider_normalizes_key_fields(temp_config_db):
    """Whitespace/case in key fields must not create distinct or dirty rows."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    padded = replace(_defaults(), role_name="  test_config_role  ", model_name=" model-a ")
    resolved = await provider.resolve_llm(padded)
    assert resolved.model_name == "model-a"

    async with temp_config_db() as session:
        role = (await session.execute(select(CreativeRole))).scalar_one()
        config = (await session.execute(select(LlmRoleConfiguration))).scalar_one()
    assert role.name == "test_config_role"
    assert config.model_name == "model-a"

    media_defaults = _media_defaults(backend=" PlaceHolder ", model=" placeholder ")
    resolved_media = await provider.resolve_media(media_defaults)
    assert resolved_media.backend_name == "placeholder"
    assert resolved_media.model_name == "placeholder"


async def test_provider_rejects_invalid_refiner_numerics(temp_config_db):
    """Direct service callers cannot persist malformed profiles."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    with pytest.raises(ValueError, match="refiner_steps"):
        MediaProfileSettings(
            width=1,
            height=1,
            cfg_scale=1.0,
            steps=1,
            sampler="x",
            scheduler="x",
            clip_skip=1,
            refiner_steps=0,
        )
    with pytest.raises(ValueError, match="refiner_cfg_scale"):
        MediaProfileSettings(
            width=1,
            height=1,
            cfg_scale=1.0,
            steps=1,
            sampler="x",
            scheduler="x",
            clip_skip=1,
            refiner_cfg_scale=-1.0,
        )
    with pytest.raises(ValueError, match="sampler"):
        await provider.upsert_media_configuration(
            "media_producer",
            "placeholder",
            "placeholder",
            MediaProfileSettings(
                width=1,
                height=1,
                cfg_scale=1.0,
                steps=1,
                sampler="  ",
                scheduler="x",
                clip_skip=1,
            ),
        )
    with pytest.raises(ValueError, match="backend_name"):
        await provider.upsert_media_configuration(
            "media_producer",
            "midjourney",
            "m",
            MediaProfileSettings(
                width=1,
                height=1,
                cfg_scale=1.0,
                steps=1,
                sampler="x",
                scheduler="x",
                clip_skip=1,
            ),
        )


async def test_resolve_rejects_invalid_defaults(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    with pytest.raises(ValueError, match="model_name"):
        await provider.resolve_llm(_defaults(model="   "))
    with pytest.raises(ValueError, match="backend_name"):
        await provider.resolve_media(_media_defaults(backend="bogus"))


def test_schema_trims_and_normalizes():
    from app.services.configuration.schemas import MediaProfileInput

    body = MediaProfileInput(
        backend_name="  Placeholder ",
        model_name="  m  ",
        width=1,
        height=1,
        cfg_scale=1.0,
        steps=1,
        sampler=" euler ",
        scheduler=" normal ",
        clip_skip=1,
        refiner_checkpoint="  ",  # whitespace optional field → blank
    )
    assert body.backend_name == "placeholder"
    assert body.model_name == "m"
    assert body.sampler == "euler"
    assert body.refiner_checkpoint == ""


# ── LLM change audit trail ───────────────────────────────────────────────


async def test_llm_save_and_reset_write_audit_events(temp_config_db):
    """Each explicit save/reset records one ordered, snapshotted change row."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="Custom prompt.",
        temperature=0.9,
        max_tokens=512,
        enable_thinking=True,
    )
    await provider.reset_llm_configuration(_defaults())

    changes = await provider.list_llm_configuration_changes()

    # Newest first: reset, then save
    assert [c.action for c in changes] == ["reset_to_defaults", "save_custom"]
    assert {c.origin for c in changes} == {"service"}

    save = changes[1]
    before = json.loads(save.before_snapshot)
    after = json.loads(save.after_snapshot)
    assert before["system_prompt"] == "Config test system prompt."
    assert before["uses_code_defaults"] is True
    assert after["system_prompt"] == "Custom prompt."
    assert after["temperature"] == 0.9
    assert after["uses_code_defaults"] is False

    reset = changes[0]
    reset_before = json.loads(reset.before_snapshot)
    reset_after = json.loads(reset.after_snapshot)
    assert reset_before["system_prompt"] == "Custom prompt."
    assert reset_after["system_prompt"] == "Config test system prompt."
    assert reset_after["uses_code_defaults"] is True

    async with temp_config_db() as session:
        rows = (await session.execute(select(LlmConfigurationChange))).scalars().all()
    assert len(rows) == 2


async def test_resolve_never_records_change_events(temp_config_db):
    """Resolution (the runtime/page-load path) must not write audit rows."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.resolve_llm(_defaults())

    assert await provider.list_llm_configuration_changes() == []


async def test_change_events_recorded_even_when_values_identical(temp_config_db):
    """A reset on an already-default profile still records the explicit act."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.reset_llm_configuration(_defaults())

    changes = await provider.list_llm_configuration_changes()
    assert len(changes) == 1
    assert changes[0].action == "reset_to_defaults"
    assert changes[0].before_snapshot == changes[0].after_snapshot


async def test_first_save_before_snapshot_is_code_defaults(temp_config_db):
    """A first-ever save on a never-seeded model audits before=code
    defaults, after=custom — never a misleading seeded-custom mix."""
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.upsert_llm_configuration(
        _defaults(model="never-seeded"),
        system_prompt="First custom.",
        temperature=0.9,
        max_tokens=64,
        enable_thinking=True,
    )

    changes = await provider.list_llm_configuration_changes()
    assert len(changes) == 1
    change = changes[0]
    assert change.action == "save_custom"
    before = json.loads(change.before_snapshot)
    after = json.loads(change.after_snapshot)
    assert before["system_prompt"] == "Config test system prompt."
    assert before["temperature"] == 0.5
    assert before["max_tokens"] == 1024
    assert before["uses_code_defaults"] is True
    assert after["system_prompt"] == "First custom."
    assert after["uses_code_defaults"] is False


async def test_list_llm_configuration_changes_filters(temp_config_db):
    provider = DatabaseConfigurationProvider(temp_config_db)
    await provider.resolve_llm(_defaults())
    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="A.",
        temperature=0.5,
        max_tokens=64,
        enable_thinking=False,
    )
    await provider.upsert_llm_configuration(
        _defaults(),
        system_prompt="B.",
        temperature=0.6,
        max_tokens=128,
        enable_thinking=False,
    )

    async with temp_config_db() as session:
        config = (await session.execute(select(LlmRoleConfiguration))).scalar_one()

    assert await provider.list_llm_configuration_changes([config.id + 999]) == []
    limited = await provider.list_llm_configuration_changes([config.id], limit=1)
    assert len(limited) == 1
    assert json.loads(limited[0].after_snapshot)["system_prompt"] == "B."


# ── History endpoint + mutation origins ──────────────────────────────────


def test_api_llm_history_endpoint(client):
    model = "vendor/model-with-slash"
    put = client.put(
        "/api/configurations/llm/art_director",
        json={
            "model_name": model,
            "system_prompt": "Custom.",
            "temperature": 0.4,
            "max_tokens": 256,
            "enable_thinking": False,
        },
    )
    assert put.status_code == 200, put.text
    reset = client.post("/api/configurations/llm/art_director/reset", json={"model_name": model})
    assert reset.status_code == 200

    resp = client.get(f"/api/configurations/llm/art_director/history?model_name={model}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role_name"] == "art_director"
    assert body["model_name"] == model
    changes = body["changes"]
    assert [c["action"] for c in changes] == ["reset_to_defaults", "save_custom"]
    assert {c["origin"] for c in changes} == {"rest_api"}
    assert changes[1]["after"]["system_prompt"] == "Custom."
    assert changes[0]["after"]["uses_code_defaults"] is True
    assert all(c["created_at"] for c in changes)


def test_api_llm_history_404s(client):
    resp = client.get("/api/configurations/llm/no_such_role/history?model_name=m")
    assert resp.status_code == 404

    # Known role but no profile under this exact model key
    resp = client.get("/api/configurations/llm/art_director/history?model_name=never-saved")
    assert resp.status_code == 404


async def test_web_save_records_web_settings_origin(client, temp_config_db):
    resp = client.post(
        "/settings/ai/llm/save",
        data={
            "role_name": "art_director",
            "model_name": "vendor/web-model",
            "system_prompt": "Web prompt.",
            "temperature": "0.4",
            "max_tokens": "256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    changes = await DatabaseConfigurationProvider().list_llm_configuration_changes()
    assert len(changes) == 1
    assert changes[0].action == "save_custom"
    assert changes[0].origin == "web_settings"
    assert json.loads(changes[0].after_snapshot)["system_prompt"] == "Web prompt."


def test_settings_page_has_separate_reset_forms(client):
    """Reset is its own confirmed form — never a formaction inside Save."""
    resp = client.get("/settings/ai")
    html = resp.text
    assert resp.status_code == 200
    assert 'action="/settings/ai/llm/reset"' in html
    assert 'action="/settings/ai/media/reset"' in html
    assert "window.confirm" in html
    assert "replaced by code defaults" in html
    assert "formaction" not in html


def test_reset_confirm_escapes_apostrophe_model(client):
    """Model keys containing apostrophes must not break the confirm handler —
    the message is JSON-encoded via tojson inside a single-quoted attribute."""
    save = client.post(
        "/settings/ai/llm/save",
        data={
            "role_name": "art_director",
            "model_name": "vendor/it's-model",
            "system_prompt": "p",
            "temperature": "0.5",
            "max_tokens": "100",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303

    resp = client.get("/settings/ai")
    assert resp.status_code == 200
    # tojson escapes apostrophes as \u0027 so the single-quoted attribute stays intact
    assert "it\\u0027s-model" in resp.text
    assert "window.confirm(" in resp.text
    # The unescaped apostrophe form must not appear inside the handler
    assert "confirm('Reset art_director / vendor/it's-model" not in resp.text


def test_settings_status_banner_names_profile(client):
    resp = client.get("/settings/ai?status=reset&kind=llm&name=art_director&model=m1")
    assert resp.status_code == 200
    assert "art_director" in resp.text
    assert "m1" in resp.text
    assert "to code defaults" in resp.text


async def test_web_save_then_page_and_run_never_reset_custom(client, temp_config_db, monkeypatch):
    """Strongest regression: a custom profile for the ACTIVE model survives
    the settings GET (which seeds missing rows) and a real RoleBlock run —
    and the executed system prompt is the custom one."""
    save = client.post(
        "/settings/ai/llm/save",
        data={
            "role_name": "test_config_role",
            "model_name": settings.llm_model,  # the active model key
            "system_prompt": "Custom web prompt.",
            "temperature": "0.4",
            "max_tokens": "256",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303

    page = client.get("/settings/ai")
    assert page.status_code == 200
    assert "web_settings" in page.text  # change history rendered

    captured = {}
    _patch_llm(monkeypatch, captured=captured)
    result = await ConfigTestRole().run({"brief": "hi"})

    record = result["_executions"][-1]
    assert record["system_prompt"] == "Custom web prompt."
    assert record["configuration_source"] == "custom"
    assert captured["model"] == settings.llm_model

    async with temp_config_db() as session:
        role = (
            await session.execute(
                select(CreativeRole).where(CreativeRole.name == "test_config_role")
            )
        ).scalar_one()
        row = (
            await session.execute(
                select(LlmRoleConfiguration).where(
                    LlmRoleConfiguration.role_id == role.id,
                    LlmRoleConfiguration.model_name == settings.llm_model,
                )
            )
        ).scalar_one()
    assert row.uses_code_defaults is False
    assert row.system_prompt == "Custom web prompt."

    # Page load + run added no further change events
    changes = await DatabaseConfigurationProvider().list_llm_configuration_changes()
    assert len(changes) == 1
    assert changes[0].origin == "web_settings"


# ── Effective LLM execution metadata on job detail ───────────────────────


async def test_job_detail_exposes_effective_llm_executions(client, engine_db):
    from unittest.mock import patch

    import app.blocks.role_block as role_block_module

    with patch.object(role_block_module, "AsyncOpenAI", lambda **_: _fake_llm_client("done")):
        job_id = await engine_module.run_pipeline(
            workflow_name=None,
            block_names=["test_config_role"],
            context={"brief": "audit me"},
        )

    api = client.get(f"/api/workflows/jobs/{job_id}")
    assert api.status_code == 200, api.text
    step = api.json()["steps"][0]
    assert len(step["llm_executions"]) == 1
    ex = step["llm_executions"][0]
    assert ex["system_prompt"] == "Config test system prompt."
    assert ex["configuration_source"] == "code_default"
    assert ex["configuration_id"]
    assert ex["model_used"] == settings.llm_model
    assert ex["status"] == "completed"
    assert ex["reasoning_effort"] == "none"
    assert ex["started_at"] and ex["finished_at"]

    # The job LIST endpoint carries no execution data
    listed = client.get("/api/workflows/jobs").json()
    assert all("llm_executions" not in j for j in listed)

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "Config test system prompt." in page.text
    assert "code_default" in page.text
    assert settings.llm_model in page.text
