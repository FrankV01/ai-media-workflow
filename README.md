# ai-media-workflow

Modular workflow engine for AI-driven media processing. A "creative agency"
of LLM-powered roles collaborates to produce images from high-level concepts.

## Prerequisites

| Dependency | Purpose | Default |
|---|---|---|
| Python 3.11+ | Runtime | — |
| [LM Studio](https://lmstudio.ai/) | Local LLM server (OpenAI-compatible) | `http://127.0.0.1:1234/v1` |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | Image generation backend | `http://127.0.0.1:8188` |

LM Studio and ComfyUI must be running before the server starts — a startup
health check verifies all dependencies and fails fast if anything is missing.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # adjust settings as needed
./start.sh                    # checks SanDisk, then starts on http://127.0.0.1:8000
```

The startup script requires `/Volumes/SanDisk Mac AI` to be mounted and prints the main UI,
API documentation, and major REST endpoint URLs before launching. Override the mount path with
`SAN_DISK_VOLUME` if needed. To bypass the script and its disk prerequisite, run `python main.py`.

## The Pipeline

A concept flows through a chain of "employees", each doing one job:

```
User prompt
  → Art Director          (expands concept into a detailed creative brief)
  → Prompt Architect      (converts brief into structured generation prompts)
  → Media Producer        (dispatches prompts to ComfyUI, collects images)
  → Art Critic            (evaluates quality, sets verdict: good / bad)
  → Routing               (branches based on verdict)
      ├─ on_bad  → (no retry; falls through to always)
      └─ always  → Social Media Specialist (creates platform posts)
```

Each block writes its report into the pipeline context as
`{role_name}_output`, so downstream blocks can reference any prior report.
The Art Critic's verdict drives conditional branching via the engine's
routing dicts (see `app/pipeline/engine.py`).

## Architecture

- **Blocks** — atomic processing units in `app/blocks/`. Subclass `Block`,
  decorate with `@register`, auto-discovered at startup.
- **RoleBlock** — base class for LLM-powered roles. Handles LLM calls,
  prompt management, token tracking, and context threading.
- **Pipeline engine** — runs blocks sequentially, supports conditional
  branching via verdict-based routing dicts (`on_good`/`on_bad`/`always`).
- **Generation backends** — pluggable image generation in
  `app/services/generation/`. ComfyUI for production, placeholder for testing.
  Images are grouped under `IMAGE_OUTPUT_DIR/<shoot-slug>/job<id>/`.
- **Persistence** — SQLite via SQLAlchemy async. Jobs, steps, input/output
  snapshots, and generated asset paths are all stored.
- **Web UI** — Jinja2 + HTMX, Tailwind CDN. No JS build step.
- **Config** — pydantic-settings reads `.env`. See `.env.example`.

## Project Layout

```
main.py              → uvicorn entry point
app/
  main.py            → FastAPI factory, lifespan, dependency checks
  config.py          → pydantic-settings (reads .env)
  database.py        → async engine, session factory, Base
  models/            → ORM models (Job, JobStep, CreativeRole, etc.)
  blocks/            → workflow blocks
    base.py          → Abstract Block + BlockMeta
    registry.py      → auto-discovery & @register decorator
    role_block.py    → LLM-powered RoleBlock base class
    art_director.py  → creative brief from concept
    prompt_architect.py → structured generation prompts
    media_producer.py   → image generation dispatch
    art_critic.py    → quality evaluation + verdict
    social_media_specialist.py → platform post suggestions
  pipeline/
    engine.py        → sequential execution + conditional routing
  services/
    generation/      → backend abstraction (ComfyUI, placeholder)
  api/               → REST endpoints (/api/blocks, /api/workflows)
  web/               → Jinja2 templates, HTMX partials, static assets
tests/               → pytest suite
data/                → SQLite DB + media files (gitignored)
```

## Common Tasks

| Task | Command |
|---|---|
| Run dev server | `./start.sh` |
| Run tests | `pytest` |
| Lint + format | `ruff check --fix . && ruff format .` |
| Install deps | `pip install -e ".[dev]"` |
| API docs | `http://127.0.0.1:8000/docs` |

For detailed AI-agent coding guidelines, see [`agents.md`](agents.md).
