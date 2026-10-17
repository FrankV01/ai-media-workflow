"""
tests.test_startup — Entry-point startup behavior

Covers the case where `python main.py` runs uvicorn with auto-reload. Uvicorn's
reload supervisor normally keeps the parent process alive after a lifespan
startup failure, so the user must Ctrl+C it. The app should instead terminate the
whole process on startup failure.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_product_version_is_shared_with_fastapi_and_templates() -> None:
    from app.config import APP_VERSION
    from app.main import app
    from app.web.routes import templates

    with (ROOT / "pyproject.toml").open("rb") as file:
        expected_version = tomllib.load(file)["project"]["version"]

    assert APP_VERSION == expected_version
    assert app.version == expected_version
    rendered = templates.get_template("base.html").render()
    assert f"AI Media Workflow v{expected_version} · build " in rendered


def test_main_py_exits_on_startup_failure(tmp_path: Path) -> None:
    """`python main.py` exits promptly when the lifespan fails at startup."""
    bad_db = tmp_path / "app.db"
    with sqlite3.connect(bad_db):
        pass

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{bad_db}"
    env["PORT"] = "0"
    env["AI_MEDIA_AUTO_MIGRATE"] = "0"  # test the mismatch path, not auto-upgrade

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"main.py hung instead of exiting; output:\n{out[-2000:]}")

    assert proc.returncode != 0, f"expected non-zero exit; output:\n{out[-2000:]}"
    assert "Application startup failed" in out
    assert "Database migrations are required" in out


def test_lifespan_backs_up_before_database_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main

    events: list[str] = []

    def backup() -> None:
        events.append("backup")

    async def init_db() -> None:
        events.append("init")
        raise RuntimeError("stop")

    monkeypatch.setattr(app.main, "backup_sqlite_database_once", backup)
    monkeypatch.setattr(app.main, "init_db", init_db)

    async def _enter() -> None:
        async with app.main.lifespan(app.main.app):
            pass

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(_enter())
    assert events == ["backup", "init"]


def test_lifespan_backup_failure_prevents_database_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main

    init_db = AsyncMock()

    def backup() -> None:
        raise OSError("snapshot failed")

    monkeypatch.setattr(app.main, "backup_sqlite_database_once", backup)
    monkeypatch.setattr(app.main, "init_db", init_db)

    async def _enter() -> None:
        async with app.main.lifespan(app.main.app):
            pass

    with pytest.raises(OSError, match="snapshot failed"):
        asyncio.run(_enter())
    init_db.assert_not_awaited()


def test_lifespan_still_raises_without_reloader_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process lifespan entry continues to raise the original exception."""
    monkeypatch.delenv("AI_MEDIA_WORKFLOW_RELOAD", raising=False)
    import app.main

    monkeypatch.setattr(app.main, "backup_sqlite_database_once", lambda: None)
    monkeypatch.setattr(app.main, "init_db", AsyncMock(side_effect=RuntimeError("boom")))

    async def _enter() -> None:
        async with app.main.lifespan(app.main.app):
            pass

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_enter())
