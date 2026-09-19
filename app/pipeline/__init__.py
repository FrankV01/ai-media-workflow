"""
app.pipeline — Workflow orchestration

The pipeline module chains blocks into executable workflows.
It manages:
- Block ordering and dependency resolution
- Context (shared data dict) passing between blocks
- Job creation and step-level status tracking in the DB
- Error handling and optional retry logic
"""
