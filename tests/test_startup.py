"""
tests.test_startup — Entry-point startup behavior

Covers the case where `python main.py` runs uvicorn with auto-reload. Uvicorn's
reload supervisor normally keeps the parent process alive after a lifespan
startup failure, so the user must Ctrl+C it. The app should instead terminate the
whole process on startup failure.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_main_py_exits_on_startup_failure(tmp_path: Path) -> None:
    """`python main.py` exits promptly when the lifespan fails at startup."""
    bad_db = tmp_path / "app.db"
    bad_db.touch()  # existing but unmigrated file triggers the schema check failure

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


def test_lifespan_still_raises_without_reloader_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process lifespan entry continues to raise the original exception."""
    monkeypatch.delenv("AI_MEDIA_WORKFLOW_RELOAD", raising=False)
    import app.main

    monkeypatch.setattr(app.main, "init_db", AsyncMock(side_effect=RuntimeError("boom")))

    async def _enter() -> None:
        async with app.main.lifespan(app.main.app):
            pass

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_enter())
