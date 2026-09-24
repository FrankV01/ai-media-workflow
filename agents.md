# AI Agent Collaboration Guide

Best practices for AI coding assistants working on this project.
For project overview and setup, see [`README.md`](README.md).

## Project Context

- **Name**: ai-media-workflow
- **Purpose**: A "creative agency" workflow engine — LLM-powered roles collaborate to produce images from concepts
- **Stack**: Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), SQLite + aiosqlite, Jinja2 + HTMX, Tailwind CDN
- **External services**: LM Studio (LLM, `localhost:1234`), ComfyUI (image gen, `localhost:8188`, only when `GENERATION_BACKEND=comfyui`)
- **IDE**: PyCharm

## Architecture Principles

1. **Blocks are the atomic unit.** Every processing step is a `Block` subclass in `app/blocks/`. Blocks declare metadata (`BlockMeta`: name, description, version, category, inputs, outputs), implement `async run(context)`, and may override `async validate(context)` (called by the engine before `run`; raise to fail the step early).
2. **RoleBlock for LLM roles.** LLM-powered "employees" subclass `RoleBlock` (`app/blocks/role_block.py`), which resolves the per-(role, model) profile via `app/services/configuration`, handles the OpenAI-compatible chat call, token tracking, and context threading. It raises `ValueError` if the model returns empty content (the step fails instead of silently passing an empty brief downstream), and sends `reasoning_effort="none"` unless the resolved profile has `enable_thinking` set, so thinking models don't burn `max_tokens` on hidden reasoning. The JSON-output roles (`prompt_architect`, `art_critic`, `social_media_specialist`) additionally override `resolve_configuration` to append a canonical JSON response contract (`*_RESPONSE_SCHEMA`, rendered by `build_output_contract` in `prompt_architect.py`) to the resolved prompt — applied to default and customized prompts alike; a JSON object embedded in a custom prompt merges over the schema (official key names always remain).
3. **Pipeline engine supports branching.** `app/pipeline/engine.py` runs blocks sequentially and supports conditional routing via verdict-based dicts (`on_good`/`on_bad`/`always`). The original user brief is preserved across routing.
4. **One workload at a time.** `app/services/workload_guard.py` provides a reentrant async lock backed by an OS file lock. The engine holds it for the whole pipeline; RoleBlocks and the ComfyUI backend re-acquire it (reentrantly) per call. Additional submissions stay `PENDING` until the running job finishes, so the LLM and ComfyUI are never hit concurrently.
5. **Registry = auto-discovery.** Decorate a `Block` subclass with `@register` and it's available everywhere. `discover_blocks()` imports every module in `app/blocks/` at startup. No manual wiring.
6. **Generation backends are pluggable.** `app/services/generation/` provides `ComfyUIBackend` (production; SDXL base → refiner → 2x/4x upscale workflow built in `sdxl_workflow.py`) and `PlaceholderBackend` (fast testing). Switch via `GENERATION_BACKEND` in `.env`, or per run via `context["_generation_backend"]`.
7. **UI is server-rendered.** Jinja2 templates + HTMX. No JS build step. Tailwind via CDN.
8. **Config via environment.** `pydantic-settings` reads `.env`. See `.env.example` for all settings; defaults live in `app/config.py`.
9. **Startup health checks.** `app/main.py` verifies the LLM endpoint, ComfyUI (only when it's the configured backend), and output directory writability before accepting requests, and refuses to start otherwise. When launched via `python main.py` (auto-reload enabled), a startup failure exits the whole process with a non-zero status instead of leaving a hung reloader. `main.py` also honors `HOST`/`PORT` environment overrides.

## Current Pipeline

```
concept → Art Director → Prompt Architect → Media Producer → Art Critic → Critic Report
                                                                 │
                                                          ┌──────┴──────┐
                                                       on_bad        always
                                                          │              │
                                                   (nothing —      Social Media
                                                    continues    Specialist →
                                                    to always)   Social Report
```

Defined as `DEFAULT_WORKFLOW` in `app/web/routes.py`. There is no retry loop: a bad verdict has no
blocks attached, so the run falls through to the `always` branch.

### Active Blocks

| Block | Type | Purpose | Key output |
|---|---|---|---|
| `art_director` | RoleBlock | Expands concept into creative brief; names the shoot | `art_director_output`, `photo_shoot_name` |
| `prompt_architect` | RoleBlock | Converts brief to structured JSON prompts; appends canonical JSON contract to resolved prompt; fails the step unless all four prompts are present | `prompt_architect_output`, `generation_params` |
| `media_producer` | Block | Dispatches to generation backend, collects images | `generated_images`, `generation_metadata`, `media_producer_output` |
| `art_critic` | RoleBlock | Evaluates quality, sets verdict; appends canonical JSON contract to resolved prompt | `art_critic_output`, `_verdict` |
| `social_media_specialist` | RoleBlock | Creates marketing content (social posts + licensing metadata); appends canonical JSON contract to resolved prompt | `social_media_specialist_output`, `social_media_posts` |
| `art_critic_report` | Block | Writes critique to art_critic_report.md | `art_critic_report_path`, `report_files` |
| `social_media_report` | Block | Writes post suggestions to social_media_specialist.md | `social_media_report_path`, `report_files` |
| `echo` | Block | Test/utility block (`example_block.py`) | `echo_result` |

### Context flow

Every `RoleBlock` returns `brief` (the next role's input), `output_deliverable`,
`suggested_next_role`, `_executions`, and `{role_name}_output`, so downstream blocks can
reference any prior report by name. Roles that need more than the previous `brief` (Art
Critic, Social Media Specialist) assemble their own enriched input from those keys. Before each
step, the engine snapshots the full context except `_executions`, `_media_executions`, and
`_warnings` into `JobStep.input_context`; after success, it similarly snapshots the block
result into `JobStep.output`. Those keys are excluded because their records are normalized
into `RoleExecution`/`Message`/`MediaGenerationExecution` rows and `Job.warnings` after
each step.

Special context keys:
- `_verdict` — set by Art Critic (`"good"` or `"bad"`), read by routing dicts
- `_original_brief` — captured by the engine at pipeline start; restored to `brief` whenever a routing dict is resolved
- `_job_id` — current job id, set by the engine
- `_job_created_date` — job creation date in UTC (`YYYY-MM-DD`), set by the engine and used to group output files
- `_generation_backend` — per-run backend override (`"placeholder"`), used by the UI's test-run button and accepted via the API's `context` field
- `_executions` — in-memory LLM call records accumulated by RoleBlocks, including failures; after each step the engine persists only the newly added records and their ordered messages into the normalized audit tables
- `_media_executions` — in-memory per-request image generation records accumulated by MediaProducer, including failures; persisted after each step into `MediaGenerationExecution` rows
- `_warnings` — deduplicated AI-configuration warnings accumulated by blocks; persisted to `Job.warnings` (JSON list) after every step and surfaced in the API and job detail page
- `report_files` — list of markdown report paths written by report blocks; the engine copies it to `Job.report_files`
- `photo_shoot_name` — the shoot title. Set by the Art Director from its `PHOTO SHOOT:` header line (falling back to the first words of the concept), unless an API caller already supplied one. The engine copies it to `Job.workflow_name` after each step, which is the job title shown in the UI

### Prompt Architect contract

The canonical response schema (`PROMPT_ARCHITECT_RESPONSE_SCHEMA`) is appended to the
resolved system prompt at run time — applied to default and customized prompts alike,
with any JSON embedded in a custom prompt merged over it (official key names always
remain). It requires `positive_prompt`, `negative_prompt`, `positive_refiner_prompt`,
and `negative_refiner_prompt` (all non-blank), plus optional `parameters` (a per-request
override of the media profile) and `variants`. `PromptArchitect.run` parses the response
(tolerating markdown fences/preamble), raises `ValueError` if the JSON is missing or any
required prompt is blank, and rewrites `prompt_architect_output`/`brief` as clean JSON.
The Media Producer builds one `GenerationRequest` for the main prompt plus one per variant.

### Job naming

- **Web UI** (`/partials/run-pipeline`, `/partials/run-test-pipeline`) passes
  `workflow_name=None`. The job is created as `"Untitled shoot"` and retitled mid-run once the
  Art Director sets `photo_shoot_name`.
- **API** (`POST /api/workflows/run`) requires `photo_shoot_name` (legacy `workflow_name` is
  accepted as a fallback). The engine seeds it into `context["photo_shoot_name"]`, so the Art
  Director keeps the caller's name.
- Helpers live in `app/pipeline/naming.py`: `resolve_photo_shoot_name` (normalize/validate),
  `slugify_photo_shoot_name`, `output_subdir`.

### Output layout

Generated images land in `IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/`, using
`Job.created_at`'s UTC date (e.g. `/Volumes/SanDisk Mac AI/ComfyUI/output/2026-09-23/cyber-chic/job25/aimw_main_refined_1x_00001_.png`).
`IMAGE_OUTPUT_DIR` must be an absolute path and defaults to
`/Volumes/SanDisk Mac AI/ComfyUI/output`; `scripts/convert_pngs_to_jpegs.py` shares that
default unless `--output-dir` is given. The slug is derived from
`photo_shoot_name`; jobs without a name use `untitled-shoot`. The Media Producer passes the
subdir via `GenerationRequest.extras["output_subdir"]`; both backends save under it, and the
ComfyUI `filename_prefix` includes it so ComfyUI's own output folder is grouped the same way.
Each variant yields three files: `_refined_1x`, `_upscaled_2x`, `_upscaled_4x`.
Report blocks also write `art_critic_report.md` and `social_media_specialist.md`
into the same per-job directory.

### PNG→JPEG converter tool

`/convert` is a standalone drop-zone page (not part of `DEFAULT_WORKFLOW`) that posts
uploads to `POST /api/convert/png-to-jpeg` (multipart field `files`, 1–20 PNGs,
≤25 MB each, ≤100 MP decoded). Conversion is fully in memory via
`app/services/image_convert.py` — EXIF transpose, embedded ICC → sRGB, alpha
flattened onto white, JPEG quality 85, progressive+optimized, embedded sRGB ICC;
dimensions and source DPI are preserved (unlike the stock-oriented
`scripts/convert_pngs_to_jpegs.py`, which imports the same sRGB/alpha helpers but
keeps its own 4 MP upscale / quality 95 / 300 DPI policy). The endpoint returns the
JPEG body for a single conversion or an in-memory ZIP for a batch, with a per-file
manifest (`converted`/`skipped`) in the `X-Convert-Results` response header.
`app/web/static/convert.js` drives the five UI states and the blob-URL download.
Status mapping: 415 non-PNG (bad extension or signature), 413 oversized
(>25 MB, >20 files, or >100 MP), 422 corrupt or nothing convertible; batch
mode instead records bad files in `skipped[]` and still returns the valid
conversions — the response body is a ZIP only when ≥2 files converted.

## Code Style & Conventions

- **Formatting**: Ruff with line-length 100. Run `ruff check --fix . && ruff format .` (note: `migrations/` has pre-existing lint findings; prefer `ruff check --fix app tests`).
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
6. Update the "Active Blocks" table above and the README pipeline diagram if the default workflow changes

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
    default_temperature = 0.7  # optional; falls back to LLM_TEMPERATURE
```

To post-process the LLM response (parse JSON, set a verdict, name the shoot), override `run`,
call `result = await super().run(context)`, and mutate `result` — see `art_critic.py`,
`prompt_architect.py`, and `art_director.py`.

### Pipeline routing
The engine accepts a mixed list of block names and routing dicts:
```python
["art_director", "art_critic", {"on_good": ["publisher"], "on_bad": ["revise"], "always": ["archiver"]}]
```
Routing reads `context["_verdict"]` set by the preceding block. If no verdict is set, only the
`always` branch runs. Blocks in the chosen branch get `JobStep` rows created at resolution
time, and `brief` is reset to `_original_brief` before they run.

## Database

- **ORM**: SQLAlchemy 2.0 async sessions. Use `Depends(get_session)` in routes; the engine opens its own session per run.
- **Models in use**: `Job` (`workflow_name` = photo shoot title, `status`, `error`, `warnings`, `generated_assets`, `report_files`); `JobStep` (`block_name`, `order`, `status`, input/output snapshots, structured error details, timings); the AI configuration models `LlmRoleConfiguration` and `MediaModelConfiguration`; the mutation-audit model `LlmConfigurationChange`; and the normalized AI audit models `CreativeRole`, `RoleExecution`, `Message`, and `MediaGenerationExecution`.
- **AI configuration**: `CreativeRole` is stable identity metadata only. Mutable behavior profiles live in `LlmRoleConfiguration` keyed by `(role, model_name)` — `LLM_MODEL` selects which one is active — and `MediaModelConfiguration` keyed by `(block, backend, model)`. `app/services/configuration/` resolves profiles: **missing rows only** are seeded from code defaults (`uses_code_defaults=True`) and emit a warning on every use until customized via `/settings/ai` or `/api/configurations`. Resolution (`resolve_llm`/`resolve_media`), the settings page GET, and pipeline runs **never overwrite existing rows** — the only mutations are the explicit Save/Reset paths. Secrets, URLs, timeouts, poll intervals, and filesystem paths stay env-only.
- **LLM configuration change audit**: Every explicit LLM save/reset writes an `LlmConfigurationChange` row in the same transaction — `action` (`save_custom`/`reset_to_defaults`), `origin` (`service`/`web_settings`/`rest_api` — the web and API layers pass their own origin; the provider default is `service`), and JSON `before_snapshot`/`after_snapshot` of the mutable fields (`system_prompt`, `temperature`, `max_tokens`, `enable_thinking`, `uses_code_defaults`). An event is recorded even when values are identical. Newest-first listing via `list_llm_configuration_changes`; the settings page renders the 5 most recent per profile and `GET /api/configurations/llm/{role}/history?model_name=…` exposes them (404 for unknown role or missing exact model profile). There is no media audit table yet.
- **AI execution audit**: Every attempted LLM call and image-generation request stores an *immutable snapshot* — copied prompt/model/parameters (`RoleExecution.system_prompt`, `MediaGenerationExecution.settings_snapshot`), not the mutable configuration FK, are the audit authority. The engine persists each record against the corresponding `JobStep`, including failed calls, and never updates configuration or role rows. Job detail (web `/jobs/{id}` and `GET /api/workflows/jobs/{id}`) exposes per-step `llm_executions` — the effective system prompt, model, config id/source, and parameters actually used.
- **Media precedence**: explicit Prompt Architect `parameters` override the media profile's request defaults, which override code defaults. Seed is per-request, never a profile default. The ComfyUI backend receives its model/workflow settings via an injected `SdxlWorkflowConfig`; operational URL/poll/timeout/output remain env settings.
- **Models not yet used**: `Setting` (`app/models/setting.py`).

## Database Migrations

- Tool: Alembic (async `env.py`), migration scripts in `migrations/versions/`
- Apply pending migrations: `alembic upgrade head` (run from project root)
- After pulling new code that adds model columns, **always run `alembic upgrade head`** before starting the server; SQLAlchemy will query columns that don't exist yet otherwise
- Startup helpers auto-upgrade by default: `./start.sh` and `python main.py` run `alembic upgrade head` before the server starts (disable with `AI_MEDIA_AUTO_MIGRATE=0`). `init_db()` remains a safety check: it verifies the database is at the expected Alembic head and that every ORM table/column exists, and refuses to start otherwise — `alembic upgrade head` is required even for a brand-new empty database
- `tests/test_database.py` covers this contract: rejection of unversioned/stale/falsely-stamped databases and recovery of the mixed schema left by the former create_all startup (the current head `c6e1f0a4b8d3` adds `llm_configuration_changes`; migrations that create tables must skip when the table already exists because recovered DBs may have gotten it from create_all)

## Testing

- Framework: pytest + pytest-asyncio (`asyncio_mode = "auto"`, so async tests need no marker)
- Run: `pytest` from project root
- Files: `test_blocks.py` (registration, metadata, JSON/verdict parsing, Art Director naming, Prompt Architect validation, Media Producer + placeholder backend, routing resolution), `test_pipeline.py` (engine persistence against a throwaway SQLite file, including successful/failed LLM audit records, ordered messages, snapshots, and structured errors), `test_configuration.py` (profile seeding/custom/reset, audited change history and origins, history endpoint, settings-page forms, job-detail LLM executions, media profiles), `test_naming.py`, `test_generation_workflow.py` (SDXL workflow JSON), `test_workload_guard.py`, `test_convert_pngs_to_jpegs.py` (stock-oriented CLI converter), `test_image_convert.py` (shared PNG→JPEG service + `/api/convert/png-to-jpeg`: sRGB output, white alpha flatten, signature sniffing, pixel cap, ZIP dedupe, batch skip semantics, status mapping)
- LLM calls are mocked by monkeypatching `app.blocks.role_block.AsyncOpenAI`; see `_fake_llm_client` in `test_blocks.py`
- Tests that generate files monkeypatch `settings.image_output_dir` to `tmp_path` — never write into the real output dir

## Guiding Questions

When proposing changes:
1. Does this belong in an existing block, or does it need a new one?
2. Is the change backward-compatible with existing pipelines?
3. Are there tests covering the new/changed behavior?
4. Does the UI need updating to reflect the change?
5. Should any new settings be added to `config.py` **and** `.env.example`?
6. Does the pipeline routing need to be updated in `app/web/routes.py` (`DEFAULT_WORKFLOW`)?
7. Do this file and `README.md` still describe the code accurately?
