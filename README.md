# ai-media-workflow

Modular workflow engine for AI-driven media processing.

## Quick Start

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Initialize the database
alembic upgrade head

# Run the dev server
python main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Architecture

- **Blocks** — self-contained processing units (transcribe, tag, resize, etc.)
- **Pipeline** — chains blocks into a workflow; handles ordering and data flow
- **API** — FastAPI routes to trigger workflows, inspect status, manage settings
- **Web UI** — lightweight HTMX + Jinja2 dashboard; no Node build step

## Project Layout

```
main.py              → Dev server entry point
app/
  main.py            → FastAPI application factory
  config.py          → Settings via pydantic-settings
  database.py        → SQLAlchemy async engine & session
  models/            → ORM models (jobs, settings, history)
  blocks/            → Modular workflow blocks
    base.py          → Abstract Block interface
    registry.py      → Auto-discovery & registration
  pipeline/          → Workflow orchestration
    engine.py        → Runs a sequence of blocks
  api/               → REST route modules
  web/               → Jinja2 templates & static assets
migrations/          → Alembic DB migrations
tests/               → pytest suite
```
