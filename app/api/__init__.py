"""
app.api — FastAPI route modules

Each file exposes an APIRouter. Routers are included in app.main.

Planned route groups:
- workflows  — CRUD for workflow definitions, trigger execution
- blocks     — List available blocks, inspect metadata
- jobs       — Query job history, view step-level detail  (future)
- settings   — Read/write persistent settings              (future)
"""
