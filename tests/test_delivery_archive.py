"""
tests.test_delivery_archive — Customer delivery archive service, API, retry

Everything runs against the autouse tmp SQLite database and a tmp
IMAGE_OUTPUT_DIR — no real output volume, no external services, no live
migrations. Covers packaging (recursive inventory, empty dirs, markdown
snapshots, manifest/hash agreement), failure cleanup, signature/license/
note inclusion, engine step binding, archive-only retry, and the archive
API surface.
"""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

import app.database
import app.pipeline.engine as engine_module
import app.services.delivery_archive as da
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import discover_blocks, register
from app.config import settings
from app.models.creative import CreativeRole, RoleExecution
from app.models.delivery import DeliveryArchive, DeliveryArchiveStatus
from app.models.job import Job, JobStatus, JobStep
from app.models.media import MediaGenerationExecution
from app.pipeline.naming import output_subdir, slugify_photo_shoot_name
from app.services import general_settings as gs

discover_blocks()


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path / "output")
    return tmp_path / "output"


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def _read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


async def _make_job(
    output_dir: Path,
    files: dict[str, bytes],
    *,
    name: str = "Test Shoot",
    status: JobStatus = JobStatus.COMPLETED,
    with_audit: bool = False,
) -> tuple[Job, Path]:
    """Create a job row plus a populated output directory for it."""
    async with app.database.async_session() as session:
        job = Job(
            workflow_name=name,
            status=status,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
            finished_at=datetime(2026, 1, 15, 0, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        source = output_dir / output_subdir(name, job.id, job.created_at.date())
        for rel, data in files.items():
            target = source / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if with_audit:
            step = JobStep(
                job_id=job.id,
                block_name="art_director",
                order=0,
                status=JobStatus.COMPLETED,
                input_context=json.dumps({"brief": "the concept brief"}),
            )
            session.add(step)
            await session.flush()
            role = CreativeRole(name="art_director", title="Art Director")
            session.add(role)
            await session.flush()
            session.add(
                RoleExecution(
                    job_step_id=step.id,
                    role_id=role.id,
                    input_brief="the concept brief",
                    output_deliverable="the creative brief",
                    system_prompt="be artistic",
                    model_used="test-model",
                    temperature=0.5,
                    max_tokens=100,
                    status="completed",
                )
            )
            session.add(
                MediaGenerationExecution(
                    job_step_id=step.id,
                    block_name="media_producer",
                    backend_name="placeholder",
                    model_name="placeholder",
                    positive_prompt="a flower",
                    settings_snapshot='{"request": {"width": 8}}',
                    image_paths=json.dumps([str(source / "img.png")]),
                    status="completed",
                )
            )
            job.generated_assets = json.dumps([str(source / "img.png")])
            job.report_files = json.dumps([str(source / "report.md")])
        await session.commit()
        return job, source


async def _archive(job_id: int) -> DeliveryArchive:
    async with app.database.async_session() as session:
        return (
            (
                await session.execute(
                    select(DeliveryArchive)
                    .where(DeliveryArchive.job_id == job_id)
                    .order_by(DeliveryArchive.attempt.desc())
                )
            )
            .scalars()
            .first()
        )


async def _entries(archive_id: int):
    from app.models.delivery import DeliveryArchiveEntry

    async with app.database.async_session() as session:
        rows = (
            (
                await session.execute(
                    select(DeliveryArchiveEntry).where(
                        DeliveryArchiveEntry.archive_id == archive_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return {e.path: e for e in rows}


SOURCE_FILES = {
    "img.png": _png_bytes(),
    "report.md": b"# Report\nNice work.\n",
    "nested/deep.txt": b"deep",
}


async def test_create_archive_packages_everything(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES, with_audit=True)
    (source / "empty").mkdir()

    result = await da.create_delivery_archive(job.id)

    slug = slugify_photo_shoot_name(job.workflow_name)
    expected_dir = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    expected = expected_dir / f"{slug}-job{job.id}.zip"
    assert Path(result["delivery_archive_path"]) == expected
    assert expected.is_file()
    assert result["delivery_archive_status"] == "ready"

    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.READY
    assert archive.attempt == 1
    assert archive.archive_name == expected.name
    assert archive.byte_size == expected.stat().st_size
    assert archive.sha256 == hashlib.sha256(expected.read_bytes()).hexdigest()
    assert archive.manifest_sha256 == hashlib.sha256(archive.manifest.encode("utf-8")).hexdigest()

    members = _read_zip(expected)
    assert members["img.png"] == SOURCE_FILES["img.png"]
    assert members["report.md"].decode() == SOURCE_FILES["report.md"].decode()
    assert members["nested/deep.txt"] == b"deep"
    assert "empty/" in members
    for generated in ("README.md", "creative_process.md", "manifest.json"):
        assert f"_delivery/{generated}" in members

    manifest = json.loads(members["_delivery/manifest.json"])
    assert manifest["schema_version"] == 1
    assert manifest["job_id"] == job.id
    assert manifest["shoot"] == "Test Shoot"
    assert manifest["attempt_id"] == archive.id
    paths = {e["path"] for e in manifest["entries"]}
    assert "_delivery/manifest.json" not in paths
    assert "sha256" not in manifest or manifest.get("sha256") is None
    entry_by_path = {e["path"]: e for e in manifest["entries"]}
    for rel in ("img.png", "report.md", "nested/deep.txt"):
        assert entry_by_path[rel]["sha256"] == hashlib.sha256(members[rel]).hexdigest()
        assert entry_by_path[rel]["size"] == len(members[rel])
    assert entry_by_path["nested/"]["type"] == "directory"
    assert entry_by_path["empty/"]["type"] == "directory"

    entries = await _entries(archive.id)
    assert set(entries) == paths | {"_delivery/manifest.json"}
    assert entries["report.md"].markdown_contents == SOURCE_FILES["report.md"].decode()
    assert (
        entries["_delivery/README.md"].markdown_contents == members["_delivery/README.md"].decode()
    )
    assert entries["img.png"].markdown_contents is None

    process = members["_delivery/creative_process.md"].decode()
    assert "the concept brief" in process
    assert "the creative brief" in process
    assert "be artistic" in process
    assert "test-model" in process
    assert "a flower" in process

    readme = members["_delivery/README.md"].decode()
    assert "Test Shoot" in readme
    assert f"Job: #{job.id}" in readme


async def test_archive_includes_note_license_signature(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    signature = gs.save_signature_file(_png_bytes(), "sig.png")
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(
            session,
            customer_note="Dear customer",
            artist_attribution="© Studio X",
            license_markdown="# License\nYou may use this.",
            signature_asset=signature,
        )
        await session.commit()

    await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    members = _read_zip(Path(archive.archive_path))
    readme = members["_delivery/README.md"].decode()
    assert "Dear customer" in readme
    assert "© Studio X" in readme
    assert members["_delivery/LICENSE.md"].decode() == "# License\nYou may use this.\n"
    assert members["_delivery/signature.png"] == _png_bytes()

    snapshot = json.loads(archive.settings_json)
    assert snapshot["customer_note"] == "Dear customer"
    assert snapshot["signature_asset"] == signature


async def test_attempts_version_and_settings_immutable(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, customer_note="first")
        await session.commit()
    first = await da.create_delivery_archive(job.id)

    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, customer_note="second")
        await session.commit()
    second = await da.create_delivery_archive(job.id)

    assert first["delivery_archive_id"] != second["delivery_archive_id"]
    name2 = Path(second["delivery_archive_path"]).name
    assert name2.endswith("-v2.zip")

    async with app.database.async_session() as session:
        rows = (
            (await session.execute(select(DeliveryArchive).where(DeliveryArchive.job_id == job.id)))
            .scalars()
            .all()
        )
    by_attempt = {r.attempt: r for r in rows}
    assert json.loads(by_attempt[1].settings_json)["customer_note"] == "first"
    assert json.loads(by_attempt[2].settings_json)["customer_note"] == "second"
    async with app.database.async_session() as session:
        await gs.save_delivery_settings(session, customer_note="third")
        await session.commit()
    earlier = by_attempt[1]
    assert json.loads(earlier.settings_json)["customer_note"] == "first"


async def test_publish_never_overwrites(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    dest.mkdir(parents=True)
    slug = slugify_photo_shoot_name(job.workflow_name)
    squat = dest / f"{slug}-job{job.id}.zip"
    squat.write_bytes(b"taken")

    with pytest.raises(da.DeliveryArchiveError, match="already exists"):
        await da.create_delivery_archive(job.id)
    assert squat.read_bytes() == b"taken"
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED


async def _expect_failure(output_dir, job, files_before) -> None:
    archive = await _archive(job.id)
    assert archive is not None
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert archive.error
    assert archive.error_type
    assert archive.archive_path is None
    entries = await _entries(archive.id)
    assert entries == {}
    for rel, data in files_before.items():
        assert (Path(archive.source_dir) / rel).read_bytes() == data
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    if dest.exists():
        leftover = [p for p in dest.rglob("*") if p.is_file()]
        assert leftover == []


async def test_fails_when_source_missing(output_dir):
    async with app.database.async_session() as session:
        job = Job(
            workflow_name="Ghost",
            status=JobStatus.COMPLETED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(job)
        await session.commit()
    with pytest.raises(da.DeliveryArchiveError, match="missing"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED


async def test_fails_when_source_empty(output_dir):
    job, source = await _make_job(output_dir, {})
    (source / "sub").mkdir(parents=True)
    with pytest.raises(da.DeliveryArchiveError, match="empty"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED


async def test_fails_on_symlink(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    (source / "linked").symlink_to(source / "img.png")
    with pytest.raises(da.DeliveryArchiveError, match="symlink"):
        await da.create_delivery_archive(job.id)
    await _expect_failure(output_dir, job, SOURCE_FILES)


async def test_fails_on_reserved_namespace(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    (source / "_delivery").mkdir()
    (source / "_delivery" / "README.md").write_text("hijack")
    with pytest.raises(da.DeliveryArchiveError, match="reserved"):
        await da.create_delivery_archive(job.id)


async def test_fails_on_credential_files(output_dir):
    files = dict(SOURCE_FILES)
    files[".env"] = b"SECRET=1"
    job, _ = await _make_job(output_dir, files)
    with pytest.raises(da.DeliveryArchiveError, match="credential"):
        await da.create_delivery_archive(job.id)


async def test_fails_on_invalid_utf8_markdown(output_dir):
    files = dict(SOURCE_FILES)
    files["broken.md"] = b"\xff\xfe not utf8"
    job, _ = await _make_job(output_dir, files)
    with pytest.raises(da.DeliveryArchiveError, match="UTF-8"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert await _entries(archive.id) == {}


async def test_fails_when_recorded_output_missing(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES, with_audit=True)
    (source / "img.png").unlink()
    with pytest.raises(da.DeliveryArchiveError, match="missing"):
        await da.create_delivery_archive(job.id)
    await _expect_failure(
        output_dir, job, {k: v for k, v in SOURCE_FILES.items() if k != "img.png"}
    )


async def test_fails_when_recorded_output_outside_source(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES, with_audit=True)
    outside = output_dir / "elsewhere.png"
    outside.write_bytes(b"x")
    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        job.generated_assets = json.dumps([str(outside)])
        await session.commit()
    with pytest.raises(da.DeliveryArchiveError, match="outside"):
        await da.create_delivery_archive(job.id)


async def test_fails_when_file_changes_during_packaging(output_dir, monkeypatch):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    real = da._sha256_file

    def mutated_first_pass(path, stat_signature=None):
        digest = real(path, stat_signature)
        if str(path).endswith("img.png") and stat_signature is None:
            return "0" * 64
        return digest

    monkeypatch.setattr(da, "_sha256_file", mutated_first_pass)
    with pytest.raises(da.DeliveryArchiveError, match="changed"):
        await da.create_delivery_archive(job.id)
    await _expect_failure(output_dir, job, SOURCE_FILES)


async def test_fails_when_source_tree_changes_during_packaging(output_dir, monkeypatch):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    real = da._inventory
    calls = 0

    def flaky_inventory(source):
        nonlocal calls
        calls += 1
        rows = real(source)
        if calls == 2:
            return rows + [("added.txt", "file", Path(source) / "added.txt")]
        return rows

    monkeypatch.setattr(da, "_inventory", flaky_inventory)
    with pytest.raises(da.DeliveryArchiveError, match="source tree changed"):
        await da.create_delivery_archive(job.id)
    await _expect_failure(output_dir, job, SOURCE_FILES)


async def test_fails_when_post_verify_hash_errors(output_dir, monkeypatch):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    real = da._sha256_file

    def flaky(path, stat_signature=None):
        if Path(path).name.startswith(".delivery-"):
            raise RuntimeError("hash backend gone")
        return real(path, stat_signature)

    monkeypatch.setattr(da, "_sha256_file", flaky)
    with pytest.raises(RuntimeError, match="hash backend gone"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    leftover = [p for p in dest.rglob("*") if p.is_file()] if dest.exists() else []
    assert leftover == []


async def test_fails_when_destination_unwritable(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"file in the way")

    with pytest.raises(Exception):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert dest.read_bytes() == b"file in the way"


async def test_fails_on_zip_verification(output_dir, monkeypatch):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "corrupt-member")
    with pytest.raises(da.DeliveryArchiveError, match="verification"):
        await da.create_delivery_archive(job.id)
    await _expect_failure(output_dir, job, SOURCE_FILES)


async def test_ready_commit_failure_cleans_artifact(output_dir, monkeypatch):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    commits = 0

    async def flaky_commit(self):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("db gone")
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)
    with pytest.raises(RuntimeError, match="db gone"):
        await da.create_delivery_archive(job.id)

    archive = await _archive(job.id)
    assert archive.status != DeliveryArchiveStatus.READY
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    leftover = [p for p in dest.rglob("*") if p.is_file()] if dest.exists() else []
    assert leftover == []


@register
class OutputMakerBlock(Block):
    """Writes one file into the job output dir so the archive has a payload."""

    meta = BlockMeta(
        name="test_output_maker",
        category="test",
        outputs=["made_output"],
    )

    async def run(self, context):
        directory = Path(settings.image_output_dir) / output_subdir(
            context.get("photo_shoot_name"),
            context.get("_job_id"),
            context.get("_job_created_date"),
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "render.png").write_bytes(_png_bytes())
        return {"made_output": str(directory / "render.png")}


@register
class VerdictStepBlock(Block):
    meta = BlockMeta(name="da_test_verdict", category="test", outputs=["_verdict"])

    async def run(self, context):
        return {"_verdict": "good"}


async def test_engine_routes_then_archives_on_correct_step(output_dir, monkeypatch):
    monkeypatch.setattr(engine_module, "async_session", app.database.async_session)
    job_id = await engine_module.run_pipeline(
        workflow_name="Routed Shoot",
        block_names=[
            "test_output_maker",
            "da_test_verdict",
            {"on_good": ["echo"], "on_bad": [], "always": []},
            "delivery_archive",
        ],
        context={},
    )

    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        steps = (
            (
                await session.execute(
                    select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
                )
            )
            .scalars()
            .all()
        )
        archives = (await session.execute(select(DeliveryArchive))).scalars().all()

    assert job.status == JobStatus.COMPLETED
    assert [s.block_name for s in steps] == [
        "test_output_maker",
        "da_test_verdict",
        "echo",
        "delivery_archive",
    ]
    assert [s.order for s in steps] == [0, 1, 2, 3]
    assert all(s.status == JobStatus.COMPLETED for s in steps)

    archive = archives[0]
    assert archive.status == DeliveryArchiveStatus.READY
    assert archive.job_step_id == steps[-1].id
    step_output = json.loads(steps[-1].output)
    assert step_output["delivery_archive_id"] == archive.id
    assert step_output["delivery_archive_status"] == "ready"


async def _failed_archive_job(output_dir, populate=True) -> tuple[Job, Path]:
    """A terminal job that failed at its final delivery_archive step."""
    files = SOURCE_FILES if populate else {}
    job, source = await _make_job(output_dir, files, status=JobStatus.FAILED)
    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        job.error = "Block 'delivery_archive' failed: boom"
        session.add(
            JobStep(
                job_id=job.id,
                block_name="test_output_maker",
                order=0,
                status=JobStatus.COMPLETED,
            )
        )
        session.add(
            JobStep(
                job_id=job.id,
                block_name="delivery_archive",
                order=1,
                status=JobStatus.FAILED,
                error="boom",
            )
        )
        await session.commit()
    return job, source


async def test_retry_reserve_requires_terminal_archive_failure(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)
        assert step_id is not None
        job = await session.get(Job, job.id)
        assert job.status == JobStatus.RUNNING
        steps = (
            (
                await session.execute(
                    select(JobStep).where(JobStep.job_id == job.id).order_by(JobStep.order)
                )
            )
            .scalars()
            .all()
        )
        assert steps[-1].block_name == "delivery_archive"
        assert steps[-1].status == JobStatus.PENDING

        with pytest.raises(da.ArchiveRetryError):
            await da.reserve_archive_retry(session, job.id)


async def test_retry_reserve_rejects_ineligible(output_dir):
    async with app.database.async_session() as session:
        with pytest.raises(da.ArchiveRetryError) as missing:
            await da.reserve_archive_retry(session, 9999)
        assert missing.value.status_code == 404

    job, _ = await _make_job(output_dir, SOURCE_FILES, status=JobStatus.COMPLETED)
    async with app.database.async_session() as session:
        with pytest.raises(da.ArchiveRetryError):
            await da.reserve_archive_retry(session, job.id)

    async with app.database.async_session() as session:
        other = Job(
            workflow_name="Early Fail",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(other)
        await session.flush()
        session.add(
            JobStep(job_id=other.id, block_name="art_director", order=0, status=JobStatus.FAILED)
        )
        await session.commit()
        with pytest.raises(da.ArchiveRetryError, match="delivery archive step"):
            await da.reserve_archive_retry(session, other.id)

    async with app.database.async_session() as session:
        third = Job(
            workflow_name="Half Fail",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(third)
        await session.flush()
        session.add(
            JobStep(job_id=third.id, block_name="art_director", order=0, status=JobStatus.FAILED)
        )
        session.add(
            JobStep(
                job_id=third.id, block_name="delivery_archive", order=1, status=JobStatus.FAILED
            )
        )
        await session.commit()
        with pytest.raises(da.ArchiveRetryError, match="earlier steps"):
            await da.reserve_archive_retry(session, third.id)


async def test_retry_succeeds_without_regeneration(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)

    await da.run_archive_retry(job.id, step_id)

    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        assert job.status == JobStatus.COMPLETED
        assert job.error is None
        step = await session.get(JobStep, step_id)
        assert step.status == JobStatus.COMPLETED
        old = (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job.id, JobStep.order == 1)
            )
        ).scalar_one()
        assert old.status == JobStatus.FAILED
        assert old.error == "boom"
        assert (await session.execute(select(RoleExecution))).scalars().all() == []
        assert (await session.execute(select(MediaGenerationExecution))).scalars().all() == []

    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.READY
    assert archive.job_step_id == step_id


async def test_failed_retry_keeps_job_failed(output_dir, monkeypatch):
    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)

    async def explode(_job_id, *, job_step_id=None):
        raise da.DeliveryArchiveError("still broken")

    monkeypatch.setattr(da, "create_delivery_archive", explode)
    await da.run_archive_retry(job.id, step_id)

    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        assert job.status == JobStatus.FAILED
        assert "still broken" in job.error
        step = await session.get(JobStep, step_id)
        assert step.status == JobStatus.FAILED
        assert step.error == "still broken"


def _client():
    import app.main

    transport = httpx.ASGITransport(app=app.main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_archives_api_list_detail_download(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES, with_audit=True)
    result = await da.create_delivery_archive(job.id)
    archive_id = result["delivery_archive_id"]

    async with _client() as client:
        listing = await client.get(f"/api/workflows/jobs/{job.id}/archives")
        assert listing.status_code == 200
        assert len(listing.json()) == 1
        meta = listing.json()[0]
        assert meta["status"] == "ready"
        assert meta["sha256"]
        assert "entries" not in meta
        assert "markdown_contents" not in meta

        detail = await client.get(f"/api/workflows/jobs/{job.id}/archives/{archive_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["manifest"]["job_id"] == job.id
        paths = {e["path"] for e in body["entries"]}
        assert "_delivery/manifest.json" in paths
        md = {e["path"]: e for e in body["entries"]}["report.md"]
        assert md["markdown_contents"].startswith("# Report")

        download = await client.get(f"/api/workflows/jobs/{job.id}/archives/{archive_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert archive_id and "attachment" in download.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert "_delivery/README.md" in archive.namelist()


async def test_archives_api_ownership(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    other, _ = await _make_job(output_dir, SOURCE_FILES, name="Other")
    result = await da.create_delivery_archive(job.id)

    async with _client() as client:
        for suffix in ("", "/download"):
            response = await client.get(
                f"/api/workflows/jobs/{other.id}/archives/{result['delivery_archive_id']}{suffix}"
            )
            assert response.status_code == 404
        assert (await client.get("/api/workflows/jobs/9999/archives")).status_code == 404


async def test_download_requires_ready_existing_file(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    result = await da.create_delivery_archive(job.id)
    archive_id = result["delivery_archive_id"]

    async with _client() as client:
        Path(result["delivery_archive_path"]).unlink()
        response = await client.get(f"/api/workflows/jobs/{job.id}/archives/{archive_id}/download")
        assert response.status_code == 404

        async with app.database.async_session() as session:
            archive = await session.get(DeliveryArchive, archive_id)
            archive.archive_path = str(output_dir / "escape.zip")
            await session.commit()
        response = await client.get(f"/api/workflows/jobs/{job.id}/archives/{archive_id}/download")
        assert response.status_code == 409


async def test_job_detail_api_includes_archives(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    await da.create_delivery_archive(job.id)
    async with _client() as client:
        response = await client.get(f"/api/workflows/jobs/{job.id}")
    assert response.status_code == 200
    archives = response.json()["archives"]
    assert len(archives) == 1
    assert archives[0]["status"] == "ready"
    assert archives[0]["download_url"]


async def test_retry_api_flow(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    async with _client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/archives/retry")
        assert response.status_code == 202
        duplicate = await client.post(f"/api/workflows/jobs/{job.id}/archives/retry")
        assert duplicate.status_code == 409

        import asyncio

        archives_list = []
        for _ in range(200):
            listing = await client.get(f"/api/workflows/jobs/{job.id}/archives")
            archives_list = listing.json()
            if archives_list and archives_list[0]["status"] in {"ready", "failed"}:
                break
            await asyncio.sleep(0.05)
        assert archives_list and archives_list[0]["status"] == "ready"
        job_detail = await client.get(f"/api/workflows/jobs/{job.id}")
        assert job_detail.json()["status"] == "completed"


async def test_retry_api_rejects_running_job(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES, status=JobStatus.RUNNING)
    async with _client() as client:
        response = await client.post(f"/api/workflows/jobs/{job.id}/archives/retry")
    assert response.status_code == 409


async def test_fails_when_source_root_is_symlink(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    real = source.with_name(source.name + "-real")
    source.rename(real)
    source.symlink_to(real)
    with pytest.raises(da.DeliveryArchiveError, match="symlink|outside"):
        await da.create_delivery_archive(job.id)


async def test_fails_when_source_outside_output_root(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    source = output_dir.parent / "not-output" / "elsewhere"
    source.mkdir(parents=True)
    (source / "img.png").write_bytes(_png_bytes())
    with pytest.raises(da.DeliveryArchiveError, match="outside"):
        da._build_archive(
            job=job,
            workflow=None,
            attempt=1,
            archive_id=1,
            source=source,
            destination_dir=output_dir / "_delivery" / "x",
            delivery_settings={},
            process_md="",
            expected=[],
        )


async def test_fails_when_destination_parent_redirects(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    outside = output_dir.parent / "escape"
    outside.mkdir()
    (output_dir / "_delivery").symlink_to(outside)
    with pytest.raises(da.DeliveryArchiveError, match="outside"):
        await da.create_delivery_archive(job.id)
    assert list(outside.iterdir()) == []


async def test_fails_on_unsafe_entry_names(output_dir):
    files = dict(SOURCE_FILES)
    files["..\\outside.txt"] = b"windows-escape"
    job, _ = await _make_job(output_dir, files)
    with pytest.raises(da.DeliveryArchiveError, match="unsafe"):
        await da.create_delivery_archive(job.id)


async def test_fails_on_windows_colon_name(output_dir):
    files = dict(SOURCE_FILES)
    files["c:temp.txt"] = b"drive"
    job, _ = await _make_job(output_dir, files)
    with pytest.raises(da.DeliveryArchiveError, match="unsafe"):
        await da.create_delivery_archive(job.id)


async def test_reserved_namespace_file_and_case(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    (source / "_delivery").write_bytes(b"not a dir")
    with pytest.raises(da.DeliveryArchiveError, match="reserved"):
        await da.create_delivery_archive(job.id)

    job2, source2 = await _make_job(output_dir, SOURCE_FILES, name="Other Shoot")
    (source2 / "_DELIVERY").mkdir()
    with pytest.raises(da.DeliveryArchiveError, match="reserved"):
        await da.create_delivery_archive(job2.id)


async def test_unicode_space_special_names_package(output_dir):
    files = dict(SOURCE_FILES)
    files["render one.png"] = _png_bytes()
    files["ünïcode répørt.md"] = "# Café notes\n".encode()
    files["odd]name[2].md"] = b"brackets\n"
    job, _ = await _make_job(output_dir, files)
    result = await da.create_delivery_archive(job.id)
    members = _read_zip(Path(result["delivery_archive_path"]))
    assert members["render one.png"] == files["render one.png"]
    readme = members["_delivery/README.md"].decode()
    assert "](../render%20one.png)" in readme
    assert "](../%C3%BCn%C3%AFcode%20r%C3%A9p%C3%B8rt.md)" in readme
    assert "odd\\]name\\[2\\].md" in readme
    entries = await _entries(result["delivery_archive_id"])
    assert entries["ünïcode répørt.md"].markdown_contents == "# Café notes\n"


async def test_delivery_namespace_directory_recorded(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    result = await da.create_delivery_archive(job.id)
    members = _read_zip(Path(result["delivery_archive_path"]))
    assert "_delivery/" in members
    manifest = json.loads(members["_delivery/manifest.json"])
    kinds = {e["path"]: e["type"] for e in manifest["entries"]}
    assert kinds["_delivery/"] == "directory"
    entries = await _entries(result["delivery_archive_id"])
    assert entries["_delivery/"].entry_type == "directory"


async def test_manifest_records_workflow_identity(output_dir):
    async with app.database.async_session() as session:
        from app.models.workflow import Workflow

        workflow = Workflow(name="Main WF", slug="main-wf", steps_json='["delivery_archive"]')
        session.add(workflow)
        await session.flush()
        job = Job(
            workflow_name="Scoped",
            workflow_id=workflow.id,
            status=JobStatus.COMPLETED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(job)
        await session.commit()
        source = output_dir / output_subdir("Scoped", job.id, job.created_at.date())
        source.mkdir(parents=True)
        (source / "img.png").write_bytes(_png_bytes())
    result = await da.create_delivery_archive(job.id)
    members = _read_zip(Path(result["delivery_archive_path"]))
    manifest = json.loads(members["_delivery/manifest.json"])
    assert manifest["workflow"] == {"id": workflow.id, "slug": "main-wf", "name": "Main WF"}


async def test_zip64_streamed_entries(output_dir, monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    result = await da.create_delivery_archive(job.id)
    path = Path(result["delivery_archive_path"])
    members = _read_zip(path)
    assert members["img.png"] == SOURCE_FILES["img.png"]
    archive = await _archive(job.id)
    assert archive.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@register
class _GeneratedMaker(Block):
    meta = BlockMeta(name="da_gen_maker", category="test", outputs=["generated_images"])

    async def run(self, context):
        directory = Path(settings.image_output_dir) / output_subdir(
            context.get("photo_shoot_name"),
            context.get("_job_id"),
            context.get("_job_created_date"),
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "render.png"
        path.write_bytes(_png_bytes())
        return {"generated_images": [str(path)]}


@register
class _GeneratedDeleter(Block):
    meta = BlockMeta(name="da_gen_deleter", category="test")

    async def run(self, context):
        Path(context["generated_images"][0]).unlink()
        return {}


async def test_pipeline_missing_recorded_rendering_fails_archive(output_dir, monkeypatch):
    monkeypatch.setattr(engine_module, "async_session", app.database.async_session)
    job_id = await engine_module.run_pipeline(
        workflow_name="Lost Render",
        block_names=["da_gen_maker", "da_gen_deleter", "delivery_archive"],
        context={},
    )
    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.FAILED
        assert "missing" in (job.error or "")
        archives = await da.list_archives(session, job_id)
        assert archives and archives[0].status == DeliveryArchiveStatus.FAILED


async def test_step_output_generated_images_are_packaged(output_dir, monkeypatch):
    monkeypatch.setattr(engine_module, "async_session", app.database.async_session)
    job_id = await engine_module.run_pipeline(
        workflow_name="Kept Render",
        block_names=["da_gen_maker", "delivery_archive"],
        context={},
    )
    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.COMPLETED
    archive = await _archive(job_id)
    assert archive.status == DeliveryArchiveStatus.READY
    members = _read_zip(Path(archive.archive_path))
    assert members["render.png"] == _png_bytes()


async def _seed_messages(job: Job, source: Path) -> None:
    async with app.database.async_session() as session:
        step = JobStep(
            job_id=job.id,
            block_name="art_director",
            order=0,
            status=JobStatus.COMPLETED,
            input_context=json.dumps({"brief": "seeded brief"}),
        )
        session.add(step)
        await session.flush()
        role = CreativeRole(name="art_director", title="Art Director")
        session.add(role)
        await session.flush()
        execution = RoleExecution(
            job_step_id=step.id,
            role_id=role.id,
            input_brief="seeded brief",
            output_deliverable="deliverable",
            system_prompt="seeded system",
            model_used="m",
            status="completed",
        )
        session.add(execution)
        await session.flush()
        from app.models.creative import Message, MessageRole

        session.add(
            Message(
                execution_id=execution.id,
                role=MessageRole.SYSTEM,
                content="system line",
                ordinal=0,
            )
        )
        session.add(
            Message(
                execution_id=execution.id,
                role=MessageRole.USER,
                content="threaded user turn",
                ordinal=1,
            )
        )
        session.add(
            Message(
                execution_id=execution.id,
                role=MessageRole.ASSISTANT,
                content="threaded assistant reply",
                ordinal=2,
            )
        )
        await session.commit()


async def test_creative_process_includes_threaded_messages(output_dir):
    job, source = await _make_job(output_dir, SOURCE_FILES)
    await _seed_messages(job, source)
    result = await da.create_delivery_archive(job.id)
    members = _read_zip(Path(result["delivery_archive_path"]))
    process = members["_delivery/creative_process.md"].decode()
    assert "Messages sent" in process
    ordered = [
        process.index("system line"),
        process.index("threaded user turn"),
        process.index("threaded assistant reply"),
    ]
    assert ordered == sorted(ordered)
    assert "1. user" in process
    assert "2. assistant" in process


async def test_retry_archive_includes_threaded_messages(output_dir):
    job, source = await _failed_archive_job(output_dir)
    await _seed_messages(job, source)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)
    await da.run_archive_retry(job.id, step_id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.READY
    members = _read_zip(Path(archive.archive_path))
    process = members["_delivery/creative_process.md"].decode()
    assert "threaded user turn" in process
    assert "threaded assistant reply" in process


async def test_retry_reserve_concurrent_claims_once(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    import asyncio

    async def claim():
        async with app.database.async_session() as session:
            return await da.reserve_archive_retry(session, job.id)

    results = await asyncio.gather(claim(), claim(), return_exceptions=True)
    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, da.ArchiveRetryError)]
    assert len(successes) == 1
    assert len(failures) == 1
    async with app.database.async_session() as session:
        steps = (
            (
                await session.execute(
                    select(JobStep).where(
                        JobStep.job_id == job.id,
                        JobStep.status == JobStatus.PENDING,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(steps) == 1


async def test_failed_retry_is_retriable_again(output_dir, monkeypatch):
    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)

    async def explode(_job_id, *, job_step_id=None):
        raise da.DeliveryArchiveError("still broken")

    real_create = da.create_delivery_archive
    monkeypatch.setattr(da, "create_delivery_archive", explode)
    await da.run_archive_retry(job.id, step_id)
    monkeypatch.setattr(da, "create_delivery_archive", real_create)

    async with app.database.async_session() as session:
        step_id2 = await da.reserve_archive_retry(session, job.id)
    await da.run_archive_retry(job.id, step_id2)

    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        assert job.status == JobStatus.COMPLETED
        steps = (
            (
                await session.execute(
                    select(JobStep).where(JobStep.job_id == job.id).order_by(JobStep.order)
                )
            )
            .scalars()
            .all()
        )
        archive_steps = [s for s in steps if s.block_name == "delivery_archive"]
        assert [s.status for s in archive_steps] == [
            JobStatus.FAILED,
            JobStatus.FAILED,
            JobStatus.COMPLETED,
        ]
        retry_step = await session.get(JobStep, step_id2)
        identity = json.loads(retry_step.input_context)
        assert identity["_job_id"] == job.id
        assert identity["photo_shoot_name"] == job.workflow_name


async def test_run_retry_invoked_twice_does_not_rerun(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        step_id = await da.reserve_archive_retry(session, job.id)

    await da.run_archive_retry(job.id, step_id)
    await da.run_archive_retry(job.id, step_id)

    async with app.database.async_session() as session:
        rows = await da.list_archives(session, job.id)
    assert len([r for r in rows if r.status == DeliveryArchiveStatus.READY]) == 1


async def test_run_retry_rejects_foreign_step(output_dir):
    job, _ = await _failed_archive_job(output_dir)
    other, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        other_step = JobStep(
            job_id=other.id,
            block_name="delivery_archive",
            order=9,
            status=JobStatus.PENDING,
        )
        session.add(other_step)
        await session.commit()
        await da.run_archive_retry(job.id, other_step.id)
    async with app.database.async_session() as session:
        rows = await da.list_archives(session, job.id)
        assert rows == []


async def test_retry_button_gating(output_dir):
    async with app.database.async_session() as session:
        bad = Job(
            workflow_name="Creative Fail",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        session.add(bad)
        await session.flush()
        session.add(
            JobStep(job_id=bad.id, block_name="art_director", order=0, status=JobStatus.FAILED)
        )
        await session.commit()
        assert await da.archive_retryable(session, bad.id) is False

    job, _ = await _failed_archive_job(output_dir)
    async with app.database.async_session() as session:
        assert await da.archive_retryable(session, job.id) is True


async def test_fails_when_job_asset_metadata_is_malformed(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    async with app.database.async_session() as session:
        job = await session.get(Job, job.id)
        job.generated_assets = "{not json"
        await session.commit()

    with pytest.raises(da.DeliveryArchiveError, match="malformed recorded output"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert archive.error_type == "DeliveryArchiveError"
    assert archive.archive_path is None
    assert await _entries(archive.id) == {}


async def test_fails_when_step_output_metadata_is_malformed(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    async with app.database.async_session() as session:
        session.add(
            JobStep(
                job_id=job.id,
                block_name="media_producer",
                order=0,
                status=JobStatus.COMPLETED,
                output=json.dumps({"generated_images": {"not": "a list"}}),
            )
        )
        await session.commit()

    with pytest.raises(da.DeliveryArchiveError, match="generated_images"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert archive.archive_path is None


async def test_fails_when_step_input_context_is_malformed(output_dir):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    async with app.database.async_session() as session:
        session.add(
            JobStep(
                job_id=job.id,
                block_name="art_director",
                order=0,
                status=JobStatus.COMPLETED,
                input_context="[1, 2, 3]",
            )
        )
        await session.commit()

    with pytest.raises(AttributeError):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert archive.error_type == "AttributeError"
    assert archive.archive_path is None


async def test_empty_markdown_records_empty_snapshot(output_dir):
    files = dict(SOURCE_FILES)
    files["empty.md"] = b""
    files["EMPTY.MARKDOWN"] = b""
    job, _ = await _make_job(output_dir, files)

    await da.create_delivery_archive(job.id)

    archive = await _archive(job.id)
    entries = await _entries(archive.id)
    members = _read_zip(Path(archive.archive_path))
    assert members["empty.md"] == b""
    assert members["EMPTY.MARKDOWN"] == b""
    assert entries["empty.md"].markdown_contents == ""
    assert entries["EMPTY.MARKDOWN"].markdown_contents == ""
    assert entries["empty.md"].sha256 == hashlib.sha256(b"").hexdigest()


async def test_fails_when_temp_cleanup_fails_after_publication(output_dir, monkeypatch):
    job, _ = await _make_job(output_dir, SOURCE_FILES)
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name.startswith(".delivery-"):
            raise OSError("cleanup refused")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(OSError, match="cleanup refused"):
        await da.create_delivery_archive(job.id)

    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    slug = slugify_photo_shoot_name(job.workflow_name)
    dest = (
        output_dir / "_delivery" / output_subdir(job.workflow_name, job.id, job.created_at.date())
    )
    assert not (dest / f"{slug}-job{job.id}.zip").exists()


async def test_fails_when_earlier_file_changes_during_later_copy(output_dir, monkeypatch):
    files = {"a_first.bin": b"a" * 64, "z_last.bin": b"z" * 64}
    job, source = await _make_job(output_dir, files)
    real_open = Path.open
    mutated = False

    def hooked_open(self, *args, **kwargs):
        nonlocal mutated
        handle = real_open(self, *args, **kwargs)
        if self.name == "z_last.bin" and not mutated:
            mutated = True
            with real_open(source / "a_first.bin", "ab") as extra:
                extra.write(b"mutated")
        return handle

    monkeypatch.setattr(Path, "open", hooked_open)

    with pytest.raises(da.DeliveryArchiveError, match="changed"):
        await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)
    assert archive.status == DeliveryArchiveStatus.FAILED
    assert archive.archive_path is None


async def test_download_path_rejects_redirected_delivery_root(output_dir):
    import shutil

    job, _ = await _make_job(output_dir, SOURCE_FILES)
    await da.create_delivery_archive(job.id)
    archive = await _archive(job.id)

    delivery = output_dir / "_delivery"
    outside = output_dir.parent / "outside-delivery"
    shutil.move(str(delivery), str(outside))
    delivery.symlink_to(outside)

    with pytest.raises(da.DeliveryArchiveError, match="delivery root"):
        da.archive_download_path(archive)
