# ai-media-workflow

Modular workflow engine for AI-driven media processing. A "creative agency"
of LLM-powered roles collaborates to produce images from high-level concepts.

## Prerequisites

| Dependency | Purpose | Default |
|---|---|---|
| Python 3.11+ | Runtime | — |
| [LM Studio](https://lmstudio.ai/) | Local LLM server (OpenAI-compatible) | `http://127.0.0.1:1234/v1` |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | Image generation backend (only when `GENERATION_BACKEND=comfyui`) | `http://127.0.0.1:8188` |

LM Studio must be running before the server starts, and ComfyUI too when it is
the configured backend — a startup health check verifies the LLM endpoint, the
generation backend, and that `IMAGE_OUTPUT_DIR` is writable, and fails fast if
anything is missing. `.env.example` defaults to the `placeholder` backend so the
app runs without ComfyUI.

### LLM notes

- Defaults target LM Studio. The model must be loaded there; `LLM_MODEL` names it.
- Thinking models (e.g. Qwen3) spend most of their output budget on hidden
  reasoning. `LLM_ENABLE_THINKING=false` (the default) sends
  `reasoning_effort="none"`, which LM Studio honors, so `LLM_MAX_TOKENS`
  (default 32768) goes to the actual deliverable. Set it to `true` to allow
  reasoning.
- Make sure the model's loaded context length in LM Studio is larger than
  `LLM_MAX_TOKENS` plus the prompt (the largest prompts are ~8k tokens).
- If a role returns empty content, the step fails with a message that
  includes `finish_reason` and token usage instead of passing an empty
  brief downstream.

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
  → Art Director          (expands concept into a detailed creative brief; names the shoot)
  → Prompt Architect      (converts brief into structured generation prompts — JSON with
                           positive / negative / refiner-positive / refiner-negative prompts)
  → Media Producer        (dispatches prompts to the generation backend, collects images)
  → Art Critic            (evaluates quality, sets verdict: good / bad)
  → Critic Report         (writes art_critic_report.md next to the images)
  → Routing               (branches based on verdict)
      ├─ on_bad  → (no retry; falls through to always)
      └─ always  → Social Media Specialist (creates platform posts)
                 → Social Report (writes social_media_specialist.md)
```

Each block writes its report into the pipeline context as
`{role_name}_output`, so downstream blocks can reference any prior report.
The Art Critic's verdict drives conditional branching via the engine's
routing dicts (see `app/pipeline/engine.py`). The default chain is
`DEFAULT_WORKFLOW` in `app/web/routes.py`.

Only one pipeline runs at a time; additional submissions queue as `PENDING`
until the running job finishes.

### Photo shoots

Every job is a "photo shoot" and `Job.workflow_name` is its title:

- From the **web UI**, the job starts as `Untitled shoot` and is renamed
  mid-run as soon as the Art Director emits its `PHOTO SHOOT: <title>` header.
- From the **API**, `photo_shoot_name` is required and is kept as-is.

Generated images are grouped by shoot and job:

```
IMAGE_OUTPUT_DIR/
  <shoot-slug>/
    job<id>/
      aimw_main_refined_1x_00001_.png
      aimw_main_upscaled_2x_00001_.png
      aimw_main_upscaled_4x_00001_.png
      aimw_<variant name>_refined_1x_00001_.png
      art_critic_report.md
      social_media_specialist.md
      …
```

`IMAGE_OUTPUT_DIR` defaults to `/Volumes/SanDisk Mac AI/ComfyUI/output`, must be
an absolute path, and can be overridden via the environment.
`scripts/convert_pngs_to_jpegs.py` writes to `IMAGE_OUTPUT_DIR` when
`--output-dir` is omitted; an explicit `--output-dir` overrides it.

The ComfyUI backend runs an SDXL base → refiner → 2x/4x upscale workflow, so
each prompt variant produces three files.

## Architecture

- **Blocks** — atomic processing units in `app/blocks/`. Subclass `Block`,
  decorate with `@register`, auto-discovered at startup.
- **RoleBlock** — base class for LLM-powered roles. Handles LLM calls,
  reasoning control, token tracking, and context threading.
- **Pipeline engine** — runs blocks sequentially, supports conditional
  branching via verdict-based routing dicts (`on_good`/`on_bad`/`always`),
  titles the job from `photo_shoot_name`.
- **Workload guard** — `app/services/workload_guard.py`, a reentrant async
  lock backed by an OS file lock, serializes LLM and ComfyUI work.
- **Generation backends** — pluggable image generation in
  `app/services/generation/`. ComfyUI for production, placeholder for testing.
  Images are grouped under `IMAGE_OUTPUT_DIR/<shoot-slug>/job<id>/`.
- **Persistence** — SQLite via SQLAlchemy async. Jobs, steps, input/output
  snapshots, and generated asset paths are all stored.
- **Web UI** — Jinja2 + HTMX, Tailwind CDN. No JS build step. Dashboard at
  `/`, job detail at `/jobs/{id}`, AI configuration at `/settings/ai`, run
  buttons for the real and placeholder backends.
- **Config** — split between env and DB. Environment (`.env` via
  pydantic-settings, see `.env.example`) owns secrets, URLs, timeouts, poll
  intervals, filesystem paths, and *selects which profile is active*
  (`LLM_MODEL`, `GENERATION_BACKEND`, `COMFYUI_CHECKPOINT`). Behavior
  profiles — role system prompts/temperature/max_tokens/thinking per
  (role, model), and media request/workflow defaults per
  (block, backend, model) — live in the database and are edited via the web
  UI (`/settings/ai`) or the `/api/configurations` REST endpoints. Missing
  profiles are seeded from code defaults on first use and log a persisted
  warning on every run until customized. Every LLM call and image request
  stores an immutable snapshot of the exact values used, so editing a
  profile never rewrites execution history.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/blocks/` | List registered blocks with metadata |
| `GET` | `/api/blocks/{name}` | Single block detail |
| `POST` | `/api/workflows/run` | Queue a run (`202`); body: `photo_shoot_name`, `block_names`, `context` (set `context._generation_backend` to `"placeholder"` for a test run without ComfyUI) |
| `GET` | `/api/workflows/jobs` | Recent jobs |
| `GET` | `/api/workflows/jobs/{id}` | Job with per-step status, I/O snapshots, timing, warnings |
| `GET` | `/api/configurations/llm` | List LLM role profiles |
| `PUT` | `/api/configurations/llm/{role}` | Save a custom LLM profile (`model_name` in body) |
| `POST` | `/api/configurations/llm/{role}/reset` | Restore code defaults (`model_name` in body) |
| `GET` | `/api/configurations/media` | List media model profiles |
| `PUT` | `/api/configurations/media/{block}` | Save a custom media profile (`backend_name`, `model_name` in body) |
| `POST` | `/api/configurations/media/{block}/reset` | Restore code defaults (`backend_name`, `model_name` in body) |

Interactive docs at `/docs`. `run-tool.http` contains ready-to-send examples.

## Project Layout

```
main.py              → uvicorn entry point
start.sh             → dev launcher (checks external disk, prints URLs)
run-tool.http        → example API requests
alembic.ini
migrations/          → Alembic (async env.py) + versions/
app/
  main.py            → FastAPI factory, lifespan, dependency checks
  config.py          → pydantic-settings (reads .env)
  database.py        → async engine, session factory, Base, init_db
  models/            → ORM models
    job.py           → Job (incl. persisted warnings), JobStep, JobStatus
    creative.py      → CreativeRole (identity), LlmRoleConfiguration (per-role/model
                       LLM profile), RoleExecution + Message (immutable audit snapshots)
    media.py         → MediaModelConfiguration (per-block/backend/model profile),
                       MediaGenerationExecution (per-request audit snapshot)
    setting.py       → Setting (key/value; not yet used)
  blocks/            → workflow blocks
    base.py          → Abstract Block + BlockMeta
    registry.py      → auto-discovery & @register decorator
    role_block.py    → LLM-powered RoleBlock base class
    art_director.py  → creative brief from concept + photo shoot name
    prompt_architect.py → structured generation prompts (validated JSON)
    media_producer.py   → image generation dispatch, output grouping
    art_critic.py    → quality evaluation + verdict
    social_media_specialist.py → platform post suggestions
    example_block.py → `echo` test block
  pipeline/
    engine.py        → sequential execution + conditional routing + job titling
    naming.py        → photo shoot name normalization, slugs, output subdirs
  services/
    workload_guard.py → exclusive lock for LLM / ComfyUI work
    configuration/   → typed config dataclasses + DatabaseConfigurationProvider
    generation/      → backend abstraction
      base.py        → GenerationRequest / GenerationResult / GenerationBackend
      factory.py     → get_backend() from GENERATION_BACKEND
      comfyui.py     → ComfyUI REST client
      sdxl_workflow.py → SDXL base + refiner + upscale workflow JSON
      placeholder.py → instant PNGs for testing
  api/               → REST endpoints (/api/blocks, /api/workflows, /api/configurations)
  web/               → routes.py (pages + HTMX partials, DEFAULT_WORKFLOW), templates/
tests/               → pytest suite
data/                → SQLite DB, media, default image output (gitignored)
```

## Common Tasks

| Task | Command |
|---|---|
| Run dev server | `./start.sh` (or `python main.py`) |
| Apply migrations | `alembic upgrade head` |
| Run tests | `pytest` |
| Lint + format | `ruff check --fix app tests && ruff format app tests` |
| Install deps | `pip install -e ".[dev]"` |
| API docs | `http://127.0.0.1:8000/docs` |

For detailed AI-agent coding guidelines, see [`agents.md`](agents.md).
