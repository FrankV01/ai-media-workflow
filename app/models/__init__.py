"""
app.models — SQLAlchemy ORM models

Planned models:
- Job          — A single pipeline execution (status, timestamps, result)
- JobStep      — One block's execution within a job (block name, duration, output)
- WorkflowDef  — Saved workflow template (name, ordered list of block IDs, params)
- Setting      — Key/value store for user-configurable settings
- MediaAsset   — Reference to an ingested media file (path, type, metadata)
"""
