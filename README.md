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

`python main.py` enables uvicorn's auto-reload and applies pending Alembic migrations
automatically before starting. Set `AI_MEDIA_AUTO_MIGRATE=0` to skip the automatic
`alembic upgrade head`. You can override the bind host/port with `HOST`/`PORT` environment
variables (`PORT=0` picks an available port). If a startup dependency check still fails
after migration, the whole process exits with a non-zero status instead of leaving the reloader
running.

## The Pipeline

A concept flows through a chain of "employees", each doing one job:

```
User prompt
  → Shoot Namer           (auto-prepended for untitled runs: generates the job title)
  → Art Director          (expands concept into a detailed creative brief)
  → Prompt Architect      (converts brief into structured generation prompts — JSON with
                           positive / negative / refiner-positive / refiner-negative prompts)
  → Media Producer        (dispatches prompts to the generation backend, collects images)
  → Media Report          (writes media_producer_report.md — the resolved generation
                           settings actually used: media profile + per-request params)
  → Art Critic            (evaluates quality, sets verdict: good / bad)
  → Critic Report         (writes art_critic_report.md next to the images)
  → Routing               (branches based on verdict)
      ├─ on_bad  → (no retry; falls through to always)
      └─ always  → Social Media Specialist (creates marketing content: per-channel post suggestions)
                 → Social Report (writes social_media_specialist.md)
                 → LLM Report (writes llm_report.md — the effective LLM settings of every
                               role call: model, temperature, max_tokens, thinking, system prompt)
  → Delivery Archive      (packages the complete job output — originals plus
                           generated docs — into the customer ZIP under
                           IMAGE_OUTPUT_DIR/_delivery/)
```

Each block writes its report into the pipeline context as
`{role_name}_output`, so downstream blocks can reference any prior report.
The Art Critic's verdict drives conditional branching via the engine's
routing dicts (see `app/pipeline/engine.py`). The chain above is the shipped
"Main" template — workflows are database rows (`steps_json` in the same
`block name | routing dict` format), managed on `/workflows` or via
`/api/workflow-definitions`, and the stored definitions decide the actual
sequence. Art Critic and its routing are optional and workflow-dependent;
Delivery Archive does not require the critic and never inspects the verdict —
it is simply the last top-level step, so it runs after the resolved branch.

Only one pipeline runs at a time; additional submissions queue as `PENDING`
until the running job finishes. The queue is global across workflows — a
submission for one workflow waits behind a running job from any other — so
the LLM and ComfyUI are never hit concurrently. A queued job's step list is
captured at submit time, but its AI profiles resolve when it actually runs
(editing a queued workflow's prompts takes effect; editing its steps does
not), and disabling a workflow does not cancel jobs already queued.

### Workflows

Workflows are named, persisted pipeline definitions — which blocks run, in
what order, with what routing — plus a configuration scope. Every run
belongs to one workflow (`Job.workflow_id`), and every AI profile (role
prompts, media model settings) is scoped to one, so e.g. an "Adobe Stock
Illustration" workflow can use different system prompts and a different
ComfyUI checkpoint than "Main".

- Create/clone/edit/enable/disable on the `/workflows` page (JSON steps
  editor with a rendered preview); the default workflow can't be disabled.
- The dashboard workflow selectors (page header, Run Pipeline, Test
  Pipeline — all connected) scope the pipeline visual, run buttons, and job
  list; `/settings/ai?workflow=<slug>` edits that workflow's profiles.
  `media_model_name` on a workflow overrides the env-selected checkpoint
  for its runs.
- The migration seeds "Main" with the pipeline above and backfills existing
  profiles and jobs to it.

### Photo shoots

Every job is a "photo shoot" and `Job.workflow_name` is its title:

- From the **web UI**, the job starts as `Untitled shoot` and the engine
  auto-prepends a `job_namer` step that generates the title from the brief
  at run start (falling back to the first words of the brief if the LLM
  call fails). Workflows without an Art Director are still titled.
- From the **API**, `photo_shoot_name` is required and is kept as-is —
  no naming step is added.

Generated images are grouped by the job's UTC creation date, shoot, and job ID:

```
IMAGE_OUTPUT_DIR/
  <yyyy-mm-dd>/
    <shoot-slug>/
      job<id>/
        aimw_main_refined_1x_00001_.png
        aimw_main_upscaled_2x_00001_.png
        aimw_main_upscaled_4x_00001_.png
        aimw_<variant name>_refined_1x_00001_.png
        media_producer_report.md
        art_critic_report.md
        social_media_specialist.md
        llm_report.md
      …
```

`IMAGE_OUTPUT_DIR` defaults to `/Volumes/SanDisk Mac AI/ComfyUI/output`, must be
an absolute path, and can be overridden via the environment.
`scripts/convert_pngs_to_jpegs.py` writes to `IMAGE_OUTPUT_DIR` when
`--output-dir` is omitted; an explicit `--output-dir` overrides it.

### Delivery archives and General Settings

`delivery_archive` packages a job's *entire* output directory into a
customer-facing ZIP — the artwork and the process behind it. Originals stay
untouched: the ZIP preserves every file's relative path (and empty
directories) exactly as produced. Generated delivery documents live under a
reserved `_delivery/` directory inside the ZIP — they are archive additions,
never written into the original source directory:

```
<shoot-slug>-job<id>.zip
  aimw_main_refined_1x_00001_.png     ← originals, same relative paths
  media_producer_report.md
  …
  _delivery/
    README.md            ← navigation + your customer note and attribution
    creative_process.md  ← ordered role executions and messages actually sent,
                           with the effective model/settings snapshots
    LICENSE.md           ← your license text, when configured
    signature.png|.jpg   ← your uploaded signature image, when configured
    manifest.json        ← file inventory, sizes, SHA-256 hashes, workflow identity
```

The archive intentionally includes internal prompts, the ordered model
conversation and outputs, generation settings, and every report/rendering/
cutout — sharing the creative process is the point. The archiver does not
dump `.env` or application credential settings; existing process reports may
include non-secret operational metadata and local paths, and user-supplied
secrets embedded in prompts or files are not automatically redacted — keep
them out of job content. There is no watermark: attribution is your artist
attribution text plus the optional signature image.
Archives are not uploaded anywhere yet — a future uploader block will consume
each attempt's readiness, path, id, and checksum.

Safety: an empty or missing source, a recorded-but-absent expected file
(images, cutouts, or report paths recorded by earlier steps), a `_delivery`
name collision in the source, unsafe/traversal entry names, symlinks, known
credential-style filenames (`.env` and friends), or non-UTF-8 Markdown fail
the attempt rather than silently skipping content.

ZIPs land in a sibling namespace outside the per-job source tree (so they are
never recursively packaged), versioned per attempt — `-v2`, `-v3`, … —
including failed attempts:

```
IMAGE_OUTPUT_DIR/
  _delivery/
    <yyyy-mm-dd>/                      ← job UTC creation date
      <shoot-slug>/
        job<id>/
          <shoot-slug>-job<id>.zip
  _delivery_assets/
    signatures/                        ← uploaded signature images
```

Publication is atomic: the ZIP is built and verified as a temporary file in
the destination directory, then published via a hard link that never
overwrites — so the destination filesystem must support hard links (the
current SanDisk output volume is APFS). Original job files are only read,
never linked or moved.

Customer-facing text and signature are global settings (no per-workflow or
per-run overrides) stored in the database under the `delivery.general_settings`
Setting row, edited on **`/settings/general`** — separate from the
per-workflow `/settings/ai` profiles:

| Field | Limit | Notes |
|---|---|---|
| Customer note | 20,000 chars | your text, shown in `_delivery/README.md` |
| Artist attribution | 20,000 chars | shown in `_delivery/README.md` |
| License text | 100,000 chars | becomes `_delivery/LICENSE.md` — your text, not AI-generated |
| Signature image | PNG/JPEG, ≤5 MB, ≤20 MP | stored under `_delivery_assets/signatures/` with a server-managed filename |

All fields are optional. Upload, replace, or clear the signature at any time —
clearing/replacing only updates the reference; previous asset files are kept
(there is no automated retention or deletion).

Every attempt is audited in SQLite (`delivery_archives` +
`delivery_archive_entries`): status, timings, error, the effective settings
snapshot, source/archive paths, byte size, and ZIP + manifest SHA-256, plus a
per-path inventory (type/size/hash and the exact Markdown text, including
empty files). The manifest omits its own digest and the ZIP digest — the
database row holds them. ZIPs and images stay on disk; only metadata and text
land in SQLite, never binary blobs. A `ready` attempt also yields stable
block outputs — `delivery_archive_id`, `delivery_archive_path`,
`delivery_archive_sha256`, and `delivery_archive_status` (`"ready"`) — the
keys a future uploader block consumes.

The job detail page has a "Delivery archives" box with download links,
attempt history, and errors. When a job failed at its final archive step
with every earlier non-archive step completed and no unfinished steps, a
**Retry archive** action rebuilds the ZIP using the *current* settings and
current output files — it never reruns naming, LLM, or media generation;
earlier failed attempts remain in the history, and a successful retry
completes the same job.

The `/convert` page is a standalone drag-and-drop PNG→JPEG tool (no pipeline
involved): drop one or more PNGs and conversion starts immediately — no
submit button. Files are converted in memory to sRGB JPEG at quality 85 with
transparency flattened onto white — one file downloads as a `.jpg`, multiple
files as a `.zip`. Limits: 25 MB per file, up to 20 files per drop, 100 MP
decoded. The result card shows each output's dimensions, PNG vs. JPEG size,
percentage saved, and 24-bit sRGB color profile; non-PNG, oversized, or
corrupt files in a batch are skipped and listed rather than failing the
whole drop.

The `/remove-background` page is the companion drop-zone tool for solid-color
backdrops: it cuts the background out in memory (NumPy color-keying — no AI
model) and returns a transparent PNG, or a `.zip` for a batch. Settings on
the page: auto corner color detection or a manual color-picker override, a
tolerance slider (default 15 — raise it if background specks remain), a 1px
edge feather by default, Contiguous vs Global fill (contiguous only
removes background touching the image edges, preserving same-color details
inside the subject), plus optional erode/contract and despill edge polish. Every setting has an inline description and a Contiguous-vs-Global
quick guide. Same 25 MB / 20-file / 100 MP limits as `/convert`. Results
preview over a transparency checkerboard — the single PNG directly, batch
thumbnails extracted from the returned ZIP in the browser (click a preview
to inspect it near-fullscreen) — and a Re-run
button re-processes the same files after tweaking settings. The same
logic powers the `background_remover` pipeline block (below) — add it to a
workflow to cut backgrounds out of generated images automatically; its
settings are stored per-workflow under the Block Settings section of
`/settings/ai`.

The ComfyUI backend runs an SDXL base → refiner → 2x/4x upscale workflow, so
each prompt variant produces three files.

## Architecture

- **Blocks** — atomic processing units in `app/blocks/`. Subclass `Block`,
  decorate with `@register`, auto-discovered at startup.
- **RoleBlock** — base class for LLM-powered roles. Handles LLM calls,
  reasoning control, token tracking, and context threading.
- **Pipeline engine** — runs blocks sequentially, supports conditional
  branching via verdict-based routing dicts (`on_good`/`on_bad`/`always`),
  titles the job from `photo_shoot_name` (auto-prepending a `job_namer`
  step when the run is untitled).
- **Workload guard** — `app/services/workload_guard.py`, a reentrant async
  lock backed by an OS file lock, serializes LLM and ComfyUI work.
- **Generation backends** — pluggable image generation in
  `app/services/generation/`. ComfyUI for production, placeholder for testing.
  Images are grouped under `IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/`,
  where the date is the job's UTC creation date.
- **Persistence** — SQLite via SQLAlchemy async. Jobs, steps, input/output
  snapshots, generated asset paths, and delivery archive audit
  (`DeliveryArchive`/`DeliveryArchiveEntry`: attempt status, checksums, file
  inventory, Markdown snapshots) are all stored.
- **Web UI** — Jinja2 + HTMX, Tailwind CDN. No JS build step. Dashboard at
  `/`, job detail at `/jobs/{id}` (including the Delivery archives box with
  download and eligible Retry archive actions), workflow management at
  `/workflows`, AI configuration at `/settings/ai` (per-workflow), global
  delivery settings at `/settings/general`, run buttons for the real and
  placeholder backends.
- **Config** — split between env and DB. Environment (`.env` via
  pydantic-settings, see `.env.example`) owns secrets, URLs, timeouts, poll
  intervals, filesystem paths, and *selects which profile is active*
  (`LLM_MODEL`, `GENERATION_BACKEND`, `COMFYUI_CHECKPOINT` — a workflow may
  override the checkpoint via `media_model_name`). Behavior
  profiles — role system prompts/temperature/max_tokens/thinking per
  `(workflow, role, model_name)`, and media request/workflow defaults per
  `(workflow, block, backend, model)` — plus generic per-`(workflow, block)`
  settings profiles for blocks that opt in (`has_db_settings`, e.g.
  `background_remover`) — live in the database and are edited
  via the web UI (`/settings/ai?workflow=<slug>`) or the
  `/api/configurations` REST endpoints (`?workflow=` query param).
  **Only missing profile rows are seeded** from code defaults — page loads
  and pipeline runs never overwrite saved values. Seeded rows warn on every
  run until customized (shown as `default`/`custom`/`active` badges at
  `/settings/ai`). Save and Reset are explicit, audited mutations: every
  one writes an `llm_configuration_changes` row with its action, origin
  (`web_settings`/`rest_api`/`service`), and before/after snapshots —
  visible per profile on the settings page and via the history endpoint.
  Every LLM call and image request stores an immutable snapshot of the
  exact values used, so editing a profile never rewrites execution history;
  the effective system prompt, model, and parameters of each step are
  shown under "LLM executions" on the job detail page (`/jobs/{id}`) and
  in `GET /api/workflows/jobs/{id}`. Global (unscoped) customer delivery
  settings — note, attribution, license, signature reference — live in the
  `settings` table under `delivery.general_settings` and are edited on
  `/settings/general`; they are not `/settings/ai` profiles and have no
  per-workflow or per-run override.

### Troubleshooting configuration

- **A run used the wrong prompt/parameters**: check the run's workflow and
  `LLM_MODEL` — profiles are keyed `(workflow, role, model)`, so editing a
  different workflow's profile (or a different model key) has no effect on
  the run. Inspect what a run actually used via the per-step "LLM
  executions" on the job detail page.
- **A custom profile reverted to defaults**: someone ran Reset (the
  settings page asks for confirmation) or the reset API. Check the
  profile's change history on `/settings/ai` or
  `GET /api/configurations/llm/{role}/history?model_name=…` — each event
  names its action and origin. Audit history begins with migration
  `c6e1f0a4b8d3`; resets made before it cannot be attributed retroactively.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/blocks/` | List registered blocks with metadata |
| `GET` | `/api/blocks/{name}` | Single block detail |
| `POST` | `/api/workflows/run` | Queue a run (`202`); body: `photo_shoot_name`, optional `workflow` (slug), `block_names`, `context`. Step precedence: explicit `block_names` > the workflow's stored steps > the default workflow's. Set `context._generation_backend` to `"placeholder"` for a test run without ComfyUI |
| `GET` | `/api/workflows/jobs` | Recent jobs (`?workflow=<slug>` filters) |
| `GET` | `/api/workflows/jobs/{id}` | Job with per-step status, I/O snapshots, timing, warnings, and delivery archive attempt metadata |
| `GET` | `/api/workflow-definitions/` | List workflow definitions |
| `POST` | `/api/workflow-definitions/` | Create a workflow (`name`, `description`, `steps`, `media_model_name`) |
| `GET` | `/api/workflow-definitions/{slug}` | Workflow detail (steps decoded) |
| `PUT` | `/api/workflow-definitions/{slug}` | Update name/description/steps/media_model_name |
| `POST` | `/api/workflow-definitions/{slug}/clone` | Clone (`name` in body) — copies steps + all scoped AI profiles |
| `POST` | `/api/workflow-definitions/{slug}/enable` | Re-enable a disabled workflow |
| `POST` | `/api/workflow-definitions/{slug}/disable` | Disable (409 for the default workflow) |
| `POST` | `/api/workflow-definitions/{slug}/set-default` | Make it the default (force-enables it) |
| `GET` | `/api/workflows/jobs/{id}/archives` | List a job's delivery archive attempts (metadata: status, `archive_path`, `archive_name`, `byte_size`, `sha256`, `download_url`) |
| `GET` | `/api/workflows/jobs/{id}/archives/{archive_id}` | Archive detail — full manifest and per-entry Markdown snapshots |
| `GET` | `/api/workflows/jobs/{id}/archives/{archive_id}/download` | Download a ready ZIP (`404` unknown job/archive or missing file, `409` not ready or unsafe path) |
| `POST` | `/api/workflows/jobs/{id}/archives/retry` | Archive-only retry (`202` reserved/retrying — never reruns naming/LLM/media; `409` ineligible or a retry already reserved; `404` unknown job) |
| `GET` | `/api/configurations/llm` | List LLM role profiles |
| `PUT` | `/api/configurations/llm/{role}` | Save a custom LLM profile (`model_name` in body; audited) |
| `POST` | `/api/configurations/llm/{role}/reset` | Restore code defaults (`model_name` in body; audited) |
| `GET` | `/api/configurations/llm/{role}/history` | Audited save/reset events (`model_name` in query — it may contain `/`) |
| `GET` | `/api/configurations/media` | List media model profiles |
| `PUT` | `/api/configurations/media/{block}` | Save a custom media profile (`backend_name`, `model_name` in body) |
| `POST` | `/api/configurations/media/{block}/reset` | Restore code defaults (`backend_name`, `model_name` in body) |
| `GET` | `/api/configurations/block` | List generic block profiles (blocks with `has_db_settings`) |
| `PUT` | `/api/configurations/block/{block}` | Save a custom block profile (`settings` dict in body — validated by the block) |
| `POST` | `/api/configurations/block/{block}/reset` | Restore code defaults (`block_name` in body) |
| `POST` | `/api/convert/png-to-jpeg` | Convert uploaded PNGs to sRGB JPEG (q85, white alpha fill); returns the JPEG, or a ZIP for multiple files — manifest in `X-Convert-Results` |
| `POST` | `/api/remove-background/` | Cut solid-color backgrounds out of uploaded PNGs (form fields: `key_color`, `tolerance`, `contiguous`, `feather`, `erode`, `despill`); returns the RGBA PNG, or a ZIP for multiple files — manifest in `X-Removal-Results` |

All `/api/configurations` endpoints accept an optional `?workflow=<slug>`
query parameter selecting which workflow's profiles to read or modify —
the default workflow is used when omitted (unknown slug → 404).

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
    workflow.py      → Workflow (named pipeline definition: steps_json,
                       enable/disable, default flag, media model override)
    job.py           → Job (incl. persisted warnings), JobStep, JobStatus
    creative.py      → CreativeRole (identity), LlmRoleConfiguration (per-workflow/
                       role/model LLM profile), LlmConfigurationChange (audited
                       save/reset history), RoleExecution + Message (immutable
                       audit snapshots)
    media.py         → MediaModelConfiguration (per-workflow/block/backend/model
                       profile), MediaGenerationExecution (per-request audit snapshot)
    block.py         → BlockConfiguration (per-workflow/block generic settings
                       profile for non-LLM/non-media blocks)
    setting.py       → Setting (namespaced key/value; global delivery
                       settings + migration backups)
    delivery.py      → DeliveryArchive + DeliveryArchiveEntry (attempt audit:
                       status, checksums, file inventory, Markdown snapshots)
  blocks/            → workflow blocks
    base.py          → Abstract Block + BlockMeta
    registry.py      → auto-discovery & @register decorator
    role_block.py    → LLM-powered RoleBlock base class
    art_director.py  → creative brief from concept
    job_namer.py     → engine-prepended step that titles untitled jobs
    prompt_architect.py → structured generation prompts (validated JSON)
    media_producer.py   → image generation dispatch, output grouping
    art_critic.py    → quality evaluation + verdict
    social_media_specialist.py → platform post suggestions
    report_writer.py → markdown report blocks (art_critic_report,
                       social_media_report, media_producer_report, llm_report)
    background_remover.py → solid-background cutout block (settings in
                       block_configurations; writes <name>_cutout.png)
    delivery_archive.py → final block: customer ZIP delivery + retry outputs
    example_block.py → `echo` test block
  pipeline/
    engine.py        → sequential execution + conditional routing + job titling
    naming.py        → photo shoot name normalization, slugs, output subdirs
  services/
    workload_guard.py → exclusive lock for LLM / ComfyUI work
    workflows.py     → workflow CRUD/validation/clone + DEFAULT_WORKFLOW_STEPS
    configuration/   → typed config dataclasses + DatabaseConfigurationProvider
    generation/      → backend abstraction
      base.py        → GenerationRequest / GenerationResult / GenerationBackend
      factory.py     → get_backend() from GENERATION_BACKEND
      comfyui.py     → ComfyUI REST client
      sdxl_workflow.py → SDXL base + refiner + upscale workflow JSON
      placeholder.py → instant PNGs for testing
    image_convert.py → shared in-memory PNG→JPEG service (sRGB, white alpha fill)
    background_removal.py → shared in-memory solid-background removal service
                       (NumPy chroma keying; API page + block call it)
    delivery_archive.py → ZIP build/verify/publish, audit persistence,
                       download-path safety, archive-only retry
    general_settings.py → global delivery settings + signature upload handling
  api/               → REST endpoints (/api/blocks, /api/workflows, /api/workflow-definitions, /api/configurations, /api/convert, /api/remove-background)
  web/               → routes.py (pages + HTMX partials), templates/, static/
tests/               → pytest suite
data/                → SQLite DB (gitignored) + local backups (keep out of
                       Git — backup filenames aren't universally covered by
                       the current ignore rules); generated images and
                       delivery ZIPs live under IMAGE_OUTPUT_DIR
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

### Database migrations

`alembic upgrade head` applies pending revisions — `./start.sh` and
`python main.py` run it automatically unless `AI_MEDIA_AUTO_MIGRATE=0`, and
`init_db()` performs a read-only schema-vs-ORM check at startup.
`alembic current` shows the applied revision.

The delivery-archive revision `f7c3a9e1b5d8` follows `d1e4f6a8b3c5`: it
creates the archive audit tables and appends `delivery_archive` once to the
top level of each stored workflow definition that doesn't already contain it
(enabled or disabled; every definition is validated before any update and the
exact before/after `steps_json` is kept under the
`delivery_archive.workflow_migration_backup` Setting key). The shipped
`DEFAULT_WORKFLOW_STEPS` template already includes the block; explicit
`block_names` API calls are untouched, and no past jobs are archived
retroactively. Downgrading restores only rows still matching the
migration-written JSON.

Back up the SQLite file before applying migrations with a consistent snapshot
(the `sqlite3.Connection.backup` API), not a raw copy of a live database —
startup does not create backups automatically.

For detailed AI-agent coding guidelines, see [`agents.md`](agents.md).
