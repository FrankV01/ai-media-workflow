import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

import app.pipeline.engine as engine
from app.blocks.registry import discover_blocks
from app.config import settings
from app.models.creative import LlmRoleConfiguration, RoleExecution
from app.models.job import Job, JobStatus
from app.models.product import ProductDescription
from app.services import product_descriptions as service
from app.services.open_folder import job_output_dir
from app.services.workflows import create_workflow


@pytest.fixture
async def setup_description(temp_config_db, tmp_path, monkeypatch):
    from PIL import Image

    import app.blocks.role_block as role

    discover_blocks()
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    monkeypatch.setattr(engine, "async_session", temp_config_db)
    scheduled = []
    monkeypatch.setattr(engine, "schedule_pipeline", lambda *args: scheduled.append(args))
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="# Product\n\n**Blue** mug."),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    monkeypatch.setattr(
        role,
        "AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    async with temp_config_db() as session:
        workflow = await create_workflow(session, name="Descriptions", steps=["product_describer"])
        source = Job(workflow_name="Original images", status=JobStatus.COMPLETED)
        session.add(source)
        await session.commit()
        directory = job_output_dir(source)
        directory.mkdir(parents=True)
        Image.new("RGB", (16, 16), "blue").save(directory / "image.png")
        source_id, workflow_id = source.id, workflow.id
    return temp_config_db, source_id, workflow_id, directory, scheduled, create


@pytest.mark.asyncio
async def test_versions_keep_actual_prompts_and_source_job(setup_description):
    factory, source_id, workflow_id, directory, scheduled, create = setup_description
    queued = await service.submit_description(source_id, "descriptions")
    duplicate = await service.submit_description(source_id, "descriptions", regenerate=True)
    assert duplicate["job_id"] == queued["job_id"]
    assert len(scheduled) == 1
    assert scheduled[0][1] == ["product_describer"]
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        first = await service.latest_description(session, source_id)
        prompt_a = first.system_prompt
        assert first.version == 1
        assert first.job_id != source_id
        assert first.source_job_id == source_id
        audit = await session.scalar(
            select(RoleExecution).where(RoleExecution.job_step_id == first.job_step_id)
        )
        assert audit.system_prompt == prompt_a
        await session.execute(
            update(LlmRoleConfiguration)
            .where(LlmRoleConfiguration.workflow_id == workflow_id)
            .values(system_prompt="Prompt B: literal details only", uses_code_defaults=False)
        )
        await session.commit()
    exists = await service.submit_description(source_id, "descriptions")
    assert exists["status"] == "exists"
    assert not scheduled
    await service.submit_description(source_id, "descriptions", regenerate=True)
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ProductDescription).order_by(ProductDescription.version)
                )
            ).all()
        )
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].system_prompt == prompt_a
        assert rows[1].system_prompt == "Prompt B: literal details only"
        assert create.call_args.kwargs["messages"][0]["content"] == rows[1].system_prompt
        source = await session.get(Job, source_id)
        assert source.status == JobStatus.COMPLETED
        assert source.finished_at is None
        assert len(json.loads(source.report_files)) == 1
        assert (directory / "product_description.md").read_text() == rows[1].content
        state = await service.description_state(session, source_id)
        assert state["latest"]["version"] == 2
        assert len(state["versions"]) == 2


@pytest.mark.asyncio
async def test_queued_prompt_resolves_at_execution(setup_description):
    factory, source_id, workflow_id, _, scheduled, create = setup_description
    await service.submit_description(source_id, "descriptions")
    from app.blocks.product_describer import ProductDescriber
    from app.services.configuration.database import DatabaseConfigurationProvider

    await DatabaseConfigurationProvider().resolve_llm(
        ProductDescriber().code_defaults(), workflow_id=workflow_id
    )
    async with factory() as session:
        await session.execute(
            update(LlmRoleConfiguration)
            .where(LlmRoleConfiguration.workflow_id == workflow_id)
            .values(system_prompt="Edited while queued")
        )
        await session.commit()
    await engine._guarded_execute(*scheduled.pop())
    assert create.call_args.kwargs["messages"][0]["content"] == "Edited while queued"


@pytest.mark.asyncio
async def test_failure_preserves_description_and_file(setup_description):
    factory, source_id, _, directory, scheduled, create = setup_description
    await service.submit_description(source_id, "descriptions")
    await engine._guarded_execute(*scheduled.pop())
    before = (directory / "product_description.md").read_text()
    create.side_effect = RuntimeError("Model unavailable")
    await service.submit_description(source_id, "descriptions", regenerate=True)
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        state = await service.description_state(session, source_id)
        assert state["latest"]["version"] == 1
        assert state["request"]["status"] == "failed"
        assert "Model unavailable" in state["request"]["error"]
    assert (directory / "product_description.md").read_text() == before


@pytest.mark.asyncio
async def test_replay_does_not_generate_new_version(setup_description):
    factory, source_id, _, _, scheduled, create = setup_description
    await service.submit_description(source_id, "descriptions")
    args = scheduled.pop()
    await engine._guarded_execute(*args)
    from app.blocks.product_describer import ProductDescriber

    async with factory() as session:
        row = await service.latest_description(session, source_id)
        context = {**args[2], "_job_id": args[0], "_job_step_id": row.job_step_id}
    result = await ProductDescriber().run(context)
    assert result["product_description_version"] == 1
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_submissions_are_deduplicated(setup_description):
    _, source_id, _, _, scheduled, _ = setup_description
    first, second = await asyncio.gather(
        service.submit_description(source_id, "descriptions"),
        service.submit_description(source_id, "descriptions"),
    )
    assert first["job_id"] == second["job_id"]
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_invalid_sources_and_workflows(setup_description):
    factory, source_id, _, _, _, _ = setup_description
    for invalid in (None, 0, -1, "abc", True, 1.5):
        with pytest.raises(service.ProductDescriptionError):
            await service.submit_description(invalid, "descriptions")
    with pytest.raises(service.ProductDescriptionError, match="not found"):
        await service.submit_description(99999, "descriptions")
    with pytest.raises(service.ProductDescriptionError, match="enabled"):
        await service.submit_description(source_id, "missing")
    async with factory() as session:
        source = await session.get(Job, source_id)
        source.status = JobStatus.RUNNING
        await session.commit()
    with pytest.raises(service.ProductDescriptionError, match="previously completed"):
        await service.submit_description(source_id, "descriptions")


@pytest.mark.asyncio
async def test_export_failure_is_visible_without_losing_version(setup_description, monkeypatch):
    factory, source_id, _, directory, scheduled, _ = setup_description
    monkeypatch.setattr(
        service.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("Read only"))
    )
    await service.submit_description(source_id, "descriptions")
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        row = await service.latest_description(session, source_id)
        assert row.version == 1
        assert "Read only" in row.export_error
        execution = await session.get(Job, row.job_id)
        assert execution.status == JobStatus.COMPLETED
    assert not (directory / "product_description.md").exists()


def test_safe_render_and_workflow_shape():
    assert "<script>" not in service.render_description("<script>alert(1)</script>")
    assert service.description_steps(["product_describer"])
    assert service.description_steps([{"block": "product_describer", "config": {}}])
    assert not service.description_steps(["media_producer", "product_describer"])


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_description_request_recovers_after_restart(setup_description, monkeypatch, running):
    from datetime import UTC, datetime

    import app.services.job_recovery as recovery
    from app.models.job import JobStep

    factory, source_id, _, _, scheduled, create = setup_description
    result = await service.submit_description(source_id, "descriptions")
    job_id, _, context = scheduled.pop()
    if running:
        async with factory() as session:
            job = await session.get(Job, job_id)
            job.status = JobStatus.RUNNING
            session.add(
                JobStep(
                    job_id=job_id,
                    block_name="product_describer",
                    order=0,
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(UTC),
                    input_context=json.dumps(context),
                )
            )
            await session.commit()
    monkeypatch.setattr(recovery, "async_session", factory)
    tasks = await recovery.recover_interrupted_jobs()
    await asyncio.gather(*tasks)
    async with factory() as session:
        row = await service.latest_description(session, source_id)
        assert row.job_id == result["job_id"]
        assert row.version == 1
        assert (await session.get(Job, job_id)).status == JobStatus.COMPLETED
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_old_request_replay_cannot_overwrite_latest_export(setup_description):
    factory, source_id, _, directory, scheduled, create = setup_description
    await service.submit_description(source_id, "descriptions")
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        old = await service.latest_description(session, source_id)
    create.return_value.choices[0].message.content = "Newer description"
    await service.submit_description(source_id, "descriptions", regenerate=True)
    await engine._guarded_execute(*scheduled.pop())
    await service.export_latest(old)
    assert (directory / "product_description.md").read_text() == "Newer description"


@pytest.mark.asyncio
async def test_retry_export_does_not_call_llm(setup_description, monkeypatch):
    _, source_id, _, directory, scheduled, create = setup_description
    replace = service.os.replace
    monkeypatch.setattr(
        service.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("Read only"))
    )
    await service.submit_description(source_id, "descriptions")
    await engine._guarded_execute(*scheduled.pop())
    monkeypatch.setattr(service.os, "replace", replace)
    assert await service.retry_description_export(source_id) is None
    assert (directory / "product_description.md").is_file()
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_job_description_ui_and_api(setup_description):
    import httpx

    from app.main import app

    _, source_id, _, _, scheduled, _ = setup_description
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        empty = await client.get(f"/jobs/{source_id}")
        assert empty.status_code == 200
        assert "Generate product description" in empty.text
        assert 'name="image_directory"' not in empty.text
        response = await client.post(
            f"/api/workflows/jobs/{source_id}/product-descriptions",
            json={"workflow": "descriptions"},
        )
        assert response.status_code == 202
        active = await client.get(f"/partials/jobs/{source_id}/product-descriptions")
        assert "every 3s" in active.text
        assert "Generation pending" in active.text
        await engine._guarded_execute(*scheduled.pop())
        page = await client.get(f"/jobs/{source_id}")
        assert page.status_code == 200
        assert "Regenerate description" in page.text
        assert "System prompt used" in page.text
        assert "Version history" in page.text
        assert 'data-copy-output="html"' in page.text
        assert "**Blue** mug." in page.text
        data = (await client.get(f"/api/workflows/jobs/{source_id}/product-descriptions")).json()
        assert data["latest"]["version"] == 1
        assert data["latest"]["system_prompt"]
        origin = await client.get(f"/jobs/{response.json()['job_id']}")
        assert f"source job #{source_id}" in origin.text
        picker = await client.get(f"/partials/product-description-sources?q={source_id}")
        assert "Description v1 exists" in picker.text
        notice = await client.post(
            f"/partials/jobs/{source_id}/product-descriptions", data={"workflow": "descriptions"}
        )
        assert "A description already exists" in notice.text
        assert not scheduled


@pytest.mark.asyncio
async def test_no_image_and_corrupt_image_fail_without_llm(setup_description):
    factory, source_id, _, directory, scheduled, create = setup_description
    image = directory / "image.png"
    image.unlink()
    with pytest.raises(service.ProductDescriptionError, match="No PNG/JPEG"):
        await service.submit_description(source_id, "descriptions")
    image.write_bytes(b"not a real PNG")
    await service.submit_description(source_id, "descriptions")
    await engine._guarded_execute(*scheduled.pop())
    async with factory() as session:
        state = await service.description_state(session, source_id)
        assert state["latest"] is None
        assert state["request"]["status"] == "failed"
        assert "none of the source images" in state["request"]["error"]
    create.assert_not_awaited()


def test_scan_images_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    (root / "linked.png").symlink_to(outside)
    assert service.scan_images(root)[0] == []
