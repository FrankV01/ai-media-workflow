"""
app.main — FastAPI application factory

Responsibilities:
- Create the FastAPI app instance
- Include API routers (api/workflows.py, api/blocks.py)
- Mount static files and Jinja2 template renderer
- Register startup/shutdown hooks (DB init, block discovery)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db
from app.blocks.registry import discover_blocks
from app.api import workflows as wf_router
from app.api import blocks as block_router
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init DB, discover blocks. Shutdown: cleanup."""
    await init_db()
    discover_blocks()
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
