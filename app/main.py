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
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db
from app.blocks.registry import discover_blocks
from app.api import workflows as wf_router
from app.api import blocks as block_router
from app.web.routes import router as web_router

logger = logging.getLogger(__name__)


async def _check_dependencies() -> None:
    """Verify that external services are reachable at startup."""
    errors: list[str] = []

    # LLM endpoint (LM Studio / OpenAI)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.llm_base_url}/models")
            if resp.status_code == 200:
                logger.info("✓ LLM endpoint OK — %s", settings.llm_base_url)
            else:
                errors.append(
                    f"LLM endpoint returned {resp.status_code} at {settings.llm_base_url}"
                )
    except Exception as exc:
        errors.append(f"LLM endpoint unreachable at {settings.llm_base_url}: {exc}")

    # Image generation backend
    backend_name = settings.generation_backend.lower()
    if backend_name == "comfyui":
        try:
            from app.services.generation import get_backend

            backend = get_backend()
            if await backend.is_available():
                logger.info("✓ ComfyUI OK — %s", settings.comfyui_url)
            else:
                errors.append(f"ComfyUI not responding at {settings.comfyui_url}")
        except Exception as exc:
            errors.append(f"ComfyUI check failed: {exc}")
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
        errors.append(f"Cannot write to IMAGE_OUTPUT_DIR ({output_dir}): {exc}")

    # Report results
    if errors:
        logger.error("=" * 60)
        logger.error("STARTUP DEPENDENCY CHECK FAILED:")
        for err in errors:
            logger.error("  ✗ %s", err)
        logger.error("=" * 60)
        raise RuntimeError(
            f"Startup dependency check failed with {len(errors)} error(s). "
            "See log output above for details."
        )

    logger.info("All dependency checks passed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init DB, discover blocks, check deps. Shutdown: cleanup."""
    await init_db()
    discover_blocks()
    await _check_dependencies()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# --- Routers ---
app.include_router(wf_router.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(block_router.router, prefix="/api/blocks", tags=["blocks"])
app.include_router(web_router)  # serves HTML at /

# --- Static files ---
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
