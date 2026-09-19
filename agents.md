# AI Agent Collaboration Guide

Best practices for AI coding assistants working on this project.

## Project Context

- **Name**: ai-media-workflow
- **Purpose**: Modular workflow engine for AI-driven media processing
- **Stack**: Python 3.11+, FastAPI, SQLAlchemy (async), SQLite, Jinja2 + HTMX
- **IDE**: PyCharm (project has `.idea/` directory)

## Architecture Principles

1. **Blocks are the atomic unit.** Every processing step is a `Block` subclass in `app/blocks/`. Blocks declare metadata, inputs, outputs, and implement `run(context)`.
2. **Pipelines chain blocks.** The engine in `app/pipeline/engine.py` runs blocks in order, threading a shared `context` dict. Jobs and steps are persisted to SQLite.
3. **Registry = auto-discovery.** Decorate a `Block` subclass with `@register` and it's available everywhere. No manual wiring.
4. **UI is server-rendered.** Jinja2 templates + HTMX. No JS build step. Tailwind via CDN for now.
5. **Config via environment.** `pydantic-settings` reads from `.env`. Never hard-code secrets.

## Code Style & Conventions

- **Formatting**: Ruff with line-length 100. Run `ruff check --fix .` and `ruff format .`.
- **Type hints**: Use them everywhere. Prefer `dict[str, Any]` over `Dict[str, Any]`.
- **Async by default**: All DB operations and block `run()` methods are async.
- **Imports**: stdlib → third-party → local, separated by blank lines. Ruff enforces this.
- **Naming**: snake_case for files/functions/variables, PascalCase for classes.
- **Docstrings**: Module-level docstrings in every file explaining purpose and planned work.

## Working With Blocks

### Creating a new block
1. Create a new file in `app/blocks/` (e.g., `transcribe.py`)
2. Subclass `Block`, set `meta = BlockMeta(...)`, implement `async def run(self, context)`
3. Decorate the class with `@register`
4. Add a test in `tests/test_blocks.py`
5. The block is now available in the API and UI — no other wiring needed

### Block interface
```python
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register

@register
class MyBlock(Block):
    meta = BlockMeta(
        name="my_block",
        description="What this block does",
        category="media",
        inputs=["input_key"],
        outputs=["output_key"],
    )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        # Read from context, do work, return new keys
        return {"output_key": result}
```

## Database

- **ORM**: SQLAlchemy 2.0 with async sessions
- **Models**: `app/models/` — `Job`, `JobStep`, `Setting` (more to come)
- **Migrations**: Alembic (to be initialized). For now, `init_db()` creates tables.
- **Sessions**: Use `Depends(get_session)` in FastAPI routes.

## Testing

- Framework: pytest + pytest-asyncio
- Run: `pytest` from project root
- Tests live in `tests/` mirroring the `app/` structure
- DB tests should use an in-memory SQLite instance (override `DATABASE_URL`)

## File Structure Quick Reference

```
main.py              → uvicorn entry point
pyproject.toml       → dependencies, ruff config, pytest config
app/
  main.py            → FastAPI app factory, lifespan, router includes
  config.py          → pydantic-settings (reads .env)
  database.py        → async engine, session factory, Base
  models/            → ORM models
  blocks/            → workflow blocks (base.py, registry.py, + your blocks)
  pipeline/          → engine.py — runs a sequence of blocks
  api/               → REST endpoints (blocks.py, workflows.py)
  web/               → Jinja2 templates, static assets, HTML routes
tests/               → pytest suite
migrations/          → Alembic (to be initialized)
data/                → SQLite DB + media files (gitignored)
```

## Common Tasks

| Task | Command |
|------|---------|
| Run dev server | `python main.py` |
| Run tests | `pytest` |
| Lint + format | `ruff check --fix . && ruff format .` |
| Install deps | `pip install -e ".[dev]"` |
| API docs | Open `http://127.0.0.1:8000/docs` |

## Guiding Questions for AI Agents

When proposing changes, consider:
1. Does this belong in an existing block, or does it need a new one?
2. Is the change backward-compatible with existing pipelines?
3. Are there tests covering the new/changed behavior?
4. Does the UI need updating to reflect the change?
5. Should any new settings be added to `config.py`?
