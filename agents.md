# AI Agent Collaboration Guide

Best practices for AI coding assistants working on this project.
For project overview and setup, see [`README.md`](README.md).

## Project Context

- **Name**: ai-media-workflow
- **Purpose**: A "creative agency" workflow engine — LLM-powered roles collaborate to produce images from concepts
- **Stack**: Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), SQLite + aiosqlite, Jinja2 + HTMX, Tailwind CDN
- **External services**: LM Studio (LLM, `localhost:1234`), ComfyUI (image gen, `localhost:8188`)
- **IDE**: PyCharm

## Architecture Principles

1. **Blocks are the atomic unit.** Every processing step is a `Block` subclass in `app/blocks/`. Blocks declare metadata, inputs, outputs, and implement `run(context)`.
2. **RoleBlock for LLM roles.** LLM-powered "employees" subclass `RoleBlock` (`app/blocks/role_block.py`) which handles LLM calls, prompt management, token tracking, and context threading.
3. **Pipeline engine supports branching.** `app/pipeline/engine.py` runs blocks sequentially and supports conditional routing via verdict-based dicts (`on_good`/`on_bad`/`always`). The original user brief is preserved across routing.
4. **Registry = auto-discovery.** Decorate a `Block` subclass with `@register` and it's available everywhere. No manual wiring.
5. **Generation backends are pluggable.** `app/services/generation/` provides `ComfyUIBackend` (production) and `PlaceholderBackend` (fast testing). Switch via `GENERATION_BACKEND` in `.env`.
6. **UI is server-rendered.** Jinja2 templates + HTMX. No JS build step. Tailwind via CDN.
7. **Config via environment.** `pydantic-settings` reads `.env`. See `.env.example` for all settings.
8. **Startup health checks.** `app/main.py` verifies LLM, ComfyUI (when configured), and output directory before accepting requests.

## Current Pipeline

```
concept → Art Director → Prompt Architect → Media Producer → Art Critic
                                                                 │
                                                          ┌──────┴──────┐
                                                       on_bad        always
                                                          │              │
                                                    Art Director   Social Media
                                                    (retry)        Specialist
```

### Active Blocks

| Block | Type | Purpose | Key output |
|---|---|---|---|
| `art_director` | RoleBlock | Expands concept into creative brief | `art_director_output` |
| `prompt_architect` | RoleBlock | Converts brief to structured JSON prompts | `prompt_architect_output` |
| `media_producer` | Block | Dispatches to generation backend, collects images | `generated_images`, `generation_metadata` |
| `art_critic` | RoleBlock | Evaluates quality, sets verdict | `art_critic_output`, `_verdict` |
| `social_media_specialist` | RoleBlock | Creates platform-optimized post suggestions | `social_media_posts` |
| `echo` | Block | Test/utility block | `echo_output` |

### Context flow

Each RoleBlock writes `context["{role_name}_output"]` so downstream blocks can reference any prior report. The pipeline engine stores input/output snapshots per step in the DB.

Special context keys:
- `_verdict` — set by Art Critic (`"good"` or `"bad"`), read by routing dicts
- `_original_brief` — preserved by engine at pipeline start for re-routing
- `_executions` — accumulated LLM call records (internal)

## Code Style & Conventions

- **Formatting**: Ruff with line-length 100. Run `ruff check --fix . && ruff format .`.
- **Type hints**: Use them everywhere. Prefer `dict[str, Any]` over `Dict[str, Any]`.
- **Async by default**: All DB operations and block `run()` methods are async.
- **Imports**: stdlib → third-party → local, separated by blank lines. Ruff enforces this.
- **Naming**: snake_case for files/functions/variables, PascalCase for classes.
- **Docstrings**: Module-level docstrings in every file. Keep them concise — state purpose and key I/O, not implementation details.

## Working With Blocks

### Creating a new block
1. Create a new file in `app/blocks/` (e.g., `upscaler.py`)
2. Subclass `Block` (or `RoleBlock` for LLM-powered roles), set `meta = BlockMeta(...)`, implement `async def run(self, context)`
3. Decorate the class with `@register`
4. Add tests in `tests/test_blocks.py`
5. The block is now available in the API and UI — no other wiring needed

### Block interface (non-LLM)
```python
from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register

@register
class MyBlock(Block):
    meta = BlockMeta(
        name="my_block",
        description="What this block does",
        category="production",
        inputs=["input_key"],
        outputs=["output_key"],
    )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"output_key": result}
```

### RoleBlock interface (LLM-powered)
```python
from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

@register
class MyRole(RoleBlock):
    meta = BlockMeta(name="my_role", description="...", category="creative",
                     inputs=["brief"], outputs=["brief", "my_role_output"])
    role_name = "my_role"
    role_title = "My Role Title"
    role_description = "What this role does"
    system_prompt = "You are a ..."
    suggested_next = "next_block_name"  # or None
```

### Pipeline routing
The engine accepts a mixed list of block names and routing dicts:
```python
["art_director", "art_critic", {"on_good": ["publisher"], "on_bad": ["art_director"], "always": ["archiver"]}]
```
Routing reads `context["_verdict"]` set by the preceding block.

## Database

- **ORM**: SQLAlchemy 2.0 async sessions. Use `Depends(get_session)` in routes.
- **Models**: `Job`, `JobStep` (pipeline tracking), `CreativeRole`, `RoleExecution`, `Message` (creative audit trail), `Setting` (key/value config)
- **Job.generated_assets**: JSON list of file paths for generated images
- **Migrations**: Alembic with async `env.py`. Schema changes via `ALTER TABLE` for SQLite.

## Testing

- Framework: pytest + pytest-asyncio
- Run: `pytest` from project root
- DB tests use in-memory SQLite (override `DATABASE_URL`)
- Block tests cover: registration, metadata, verdict parsing, JSON extraction, routing logic

## Guiding Questions

When proposing changes:
1. Does this belong in an existing block, or does it need a new one?
2. Is the change backward-compatible with existing pipelines?
3. Are there tests covering the new/changed behavior?
4. Does the UI need updating to reflect the change?
5. Should any new settings be added to `config.py`?
6. Does the pipeline routing need to be updated in `app/web/routes.py` (`DEFAULT_WORKFLOW`)?
