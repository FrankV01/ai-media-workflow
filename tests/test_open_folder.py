"""
tests.test_open_folder — "Open folder" service and job-detail partial

Covers app.services.open_folder (platform opener command, job output dir
derivation, safety guards) and the POST /partials/jobs/<id>/open-folder
route plus the job detail page's Reports box buttons. subprocess.Popen is
always mocked — tests never launch a real file manager.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
import app.pipeline.engine as engine_module
from app.blocks.registry import discover_blocks
from app.config import settings
from app.database import Base
from app.models.job import Job
from app.services.open_folder import (
    OpenerUnavailableError,
    job_output_dir,
    open_folder,
    opener_command,
)


def test_opener_command_per_platform():
    path = Path("/some/dir")
    assert opener_command(path, "Darwin") == ["open", str(path)]
    assert opener_command(path, "Windows") == ["explorer", str(path)]
    assert opener_command(path, "Linux") == ["xdg-open", str(path)]


def test_job_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    job = Job(
        id=120,
        workflow_name="The Returned Critic",
        created_at=datetime(2026, 9, 30, 12, tzinfo=UTC),
    )
    assert job_output_dir(job) == tmp_path / "2026-09-30" / "the-returned-critic" / "job120"


def test_open_folder_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    with pytest.raises(FileNotFoundError):
        open_folder(tmp_path / "does-not-exist")


def test_open_folder_spawns_opener(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    path = tmp_path / "job-dir"
    path.mkdir()
    monkeypatch.setattr("app.services.open_folder.shutil.which", lambda _: "/usr/bin/open")
    popen = Mock()
    monkeypatch.setattr("app.services.open_folder.subprocess.Popen", popen)

    open_folder(path)

    popen.assert_called_once_with(
        opener_command(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert "shell" not in popen.call_args.kwargs


def test_open_folder_rejects_path_outside_output_dir(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(settings, "image_output_dir", tmp_path / "output")
    with pytest.raises(PermissionError):
        open_folder(outside)


def test_open_folder_no_opener_available(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    path = tmp_path / "job-dir"
    path.mkdir()
    monkeypatch.setattr("app.services.open_folder.shutil.which", lambda _: None)
    with pytest.raises(OpenerUnavailableError):
        open_folder(path)


# ── Web routes ───────────────────────────────────────────────────────────


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    """Point the app session factory at a throwaway SQLite file."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/of_test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "async_session", factory)
    monkeypatch.setattr(app.database, "async_session", factory)
    discover_blocks()
    yield factory
    await engine.dispose()


@pytest.fixture
def api_client(monkeypatch, session_factory):
    """TestClient with the engine's execution stubbed and DBs redirected."""
    from fastapi.testclient import TestClient

    import app.main

    spy = AsyncMock()
    monkeypatch.setattr(engine_module, "_guarded_execute", spy)
    client = TestClient(app.main.app, raise_server_exceptions=False)
    yield client, spy


async def _seed_job(session_factory, report_files=None):
    async with session_factory() as session:
        job = Job(
            workflow_name="The Returned Critic",
            created_at=datetime(2026, 9, 30, 12, tzinfo=UTC),
            report_files=json.dumps(report_files) if report_files else None,
        )
        session.add(job)
        await session.commit()
        return job.id


async def test_open_folder_partial_unknown_job(api_client):
    client, _ = api_client
    resp = client.post("/partials/jobs/999/open-folder")
    assert resp.status_code == 404


async def test_open_folder_partial_success(api_client, session_factory, monkeypatch, tmp_path):
    client, _ = api_client
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    job_id = await _seed_job(session_factory)
    opener = Mock()
    monkeypatch.setattr("app.web.routes.open_folder", opener)

    resp = client.post(f"/partials/jobs/{job_id}/open-folder")
    assert resp.status_code == 200
    assert "Opened folder" in resp.text
    opener.assert_called_once()


async def test_open_folder_partial_missing_dir(api_client, session_factory, monkeypatch, tmp_path):
    client, _ = api_client
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    job_id = await _seed_job(session_factory)
    monkeypatch.setattr("app.web.routes.open_folder", Mock(side_effect=FileNotFoundError("x")))

    resp = client.post(f"/partials/jobs/{job_id}/open-folder")
    assert resp.status_code == 200
    assert "Folder not found" in resp.text


async def test_job_detail_shows_open_folder(api_client, session_factory, tmp_path, monkeypatch):
    client, _ = api_client
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    job_id = await _seed_job(session_factory, report_files=["some/report.md"])

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "Open folder" in resp.text
    expected = tmp_path / "2026-09-30" / "the-returned-critic" / f"job{job_id}"
    assert str(expected) in resp.text


async def test_job_detail_hides_open_folder_without_reports(api_client, session_factory):
    client, _ = api_client
    job_id = await _seed_job(session_factory)

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "Open folder" not in resp.text
