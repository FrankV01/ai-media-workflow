"""
app.main — FastAPI application factory

Responsibilities:
- Create the FastAPI app instance with lifespan hooks
- Startup: init DB, discover blocks, verify external dependencies
- Include API routers and web UI routes
- Mount static files

Dependency checks at startup verify LLM endpoint, ComfyUI
(when configured), and image output directory writability.
Server refuses to start if any check fails.

When launched via `python main.py` (auto-reload enabled), a startup failure
also terminates the reloader parent so the command exits with a non-zero
status instead of leaving a hung supervisor process.
"""

import logging
import multiprocessing
import os
import signal
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.api import background_removal as bg_removal_router
from app.api import blocks as block_router
from app.api import configurations as config_router
from app.api import convert as convert_router
from app.api import markup_convert as markup_convert_router
from app.api import workflow_definitions as wf_def_router
from app.api import workflows as wf_router
from app.blocks.registry import discover_blocks
from app.config import APP_VERSION, settings
from app.database import backup_sqlite_database_once, init_db
from app.services.job_recovery import recover_interrupted_jobs
from app.services.pinterest_scheduler import (
    start_scheduler as start_pinterest_scheduler,
)
from app.services.pinterest_scheduler import (
    stop_scheduler as stop_pinterest_scheduler,
)
from app.services.service_errors import (
    StartupCheckError,
    comfyui_service_error,
    llm_service_error,
    output_dir_error_message,
)
from app.web.pinterest_routes import router as pinterest_web_router
from app.web.routes import router as web_router

# Uvicorn only attaches handlers to its own loggers — without this, app.*
# loggers' INFO records are silently dropped. Scoped to the "app" namespace
# (not root) so library loggers like sqlalchemy don't double-print.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.addHandler(_handler)
_app_logger.setLevel(settings.log_level.upper())

logger = logging.getLogger(__name__)


def _is_reloader_child() -> bool:
    """True when this process is a spawned uvicorn reload worker from main.py."""
    return (
        os.environ.get("AI_MEDIA_WORKFLOW_RELOAD") == "1"
        and multiprocessing.parent_process() is not None
    )


async def _check_dependencies() -> None:
    """Verify that external services are reachable at startup."""
    errors: list[str] = []

    # LLM endpoint (LM Studio / OpenAI)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.llm_base_url}/models")
            resp.raise_for_status()
        logger.info("✓ LLM endpoint OK — %s", settings.llm_base_url)
        model_ids = _model_ids(resp)
        if model_ids is not None and settings.llm_model not in model_ids:
            logger.warning(
                "LLM_MODEL '%s' is not among the models LM Studio reports (%s). "
                "Roles using it will fail unless the model is downloaded.",
                settings.llm_model,
                ", ".join(sorted(model_ids)) or "none",
            )
    except Exception as exc:
        friendly = llm_service_error(exc)
        errors.append(str(friendly) if friendly else f"LLM endpoint check failed: {exc}")

    # Image generation backend
    backend_name = settings.generation_backend.lower()
    if backend_name == "comfyui":
        try:
            from app.services.generation.comfyui import ComfyUIBackend

            await ComfyUIBackend().ensure_available()
            logger.info("✓ ComfyUI OK — %s", settings.comfyui_url)
        except Exception as exc:
            friendly = comfyui_service_error(exc)
            errors.append(str(friendly) if friendly else f"ComfyUI check failed: {exc}")
    else:
        logger.info("✓ Generation backend: %s (no external service needed)", backend_name)

    # Image output directory
    output_dir = Path(settings.image_output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / ".startup_write_test"
        test_file.write_text("ok")
        test_file.unlink()
        logger.info("✓ Image output dir writable — %s", output_dir)
    except Exception as exc:
        errors.append(output_dir_error_message(output_dir, exc))

    # Report results
    if errors:
        logger.error("=" * 60)
        logger.error("STARTUP DEPENDENCY CHECK FAILED — the server was not started:")
        for err in errors:
            logger.error("  ✗ %s", err)
        logger.error("Fix the issue(s) above, then start the server again.")
        logger.error("=" * 60)
        raise StartupCheckError(
            f"Startup dependency check failed with {len(errors)} error(s). "
            "See log output above for details."
        )

    logger.info("All dependency checks passed")


def _model_ids(resp: httpx.Response) -> set[str] | None:
    """Model ids from an OpenAI-style /models response, or None if unparseable."""
    try:
        return {str(m["id"]) for m in resp.json()["data"]}
    except (ValueError, KeyError, TypeError):
        return None


class _StartupFailureLogFilter(logging.Filter):
    """Collapse the framework traceback for StartupCheckError into one line.

    Starlette reports lifespan failures to uvicorn as a full formatted
    traceback. For dependency-check failures the actionable details are
    already logged, so the traceback is only noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Traceback" in message and f"{StartupCheckError.__name__}:" in message:
            record.msg = message.strip().splitlines()[-1].split(": ", 1)[-1]
            record.args = ()
        return True


logging.getLogger("uvicorn.error").addFilter(_StartupFailureLogFilter())


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init DB, discover blocks, check deps. Shutdown: cleanup.

    When running under main.py's auto-reload supervisor, a startup failure is
    fatal for the whole command: the child signals the reloader parent so it
    shuts down instead of waiting forever for file changes.
    """
    try:
        backup_sqlite_database_once()
        await init_db()
        discover_blocks()
        await _check_dependencies()
        # Relaunch jobs left PENDING/RUNNING by a previous process
        await recover_interrupted_jobs()
    except Exception:
        if _is_reloader_child():
            # A startup dependency failure here would normally leave the
            # reloader process waiting forever for file changes. Notify the
            # supervisor so it shuts down the whole command instead.
            if hasattr(signal, "SIGUSR1"):
                with suppress(ProcessLookupError, OSError):
                    os.kill(os.getppid(), signal.SIGUSR1)
        raise
    pin_scheduler = start_pinterest_scheduler()
    try:
        yield
    finally:
        await stop_pinterest_scheduler(pin_scheduler)


app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    lifespan=lifespan,
)

# --- Routers ---
app.include_router(wf_router.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(
    wf_def_router.router, prefix="/api/workflow-definitions", tags=["workflow-definitions"]
)
app.include_router(block_router.router, prefix="/api/blocks", tags=["blocks"])
app.include_router(config_router.router, prefix="/api/configurations", tags=["configurations"])
app.include_router(convert_router.router, prefix="/api/convert", tags=["convert"])
app.include_router(markup_convert_router.router, prefix="/api/convert", tags=["convert"])
app.include_router(
    bg_removal_router.router, prefix="/api/remove-background", tags=["background-removal"]
)
app.include_router(web_router)  # serves HTML at /
app.include_router(pinterest_web_router)  # Pinterest pages + OAuth


# --- Static files ---
class DevStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation so the browser never serves stale JS/CSS."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-cache"
        return response


app.mount("/static", DevStaticFiles(directory="app/web/static"), name="static")
