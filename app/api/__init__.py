"""
app.api — FastAPI route modules

Each file exposes an APIRouter. Routers are included in app.main.

Route groups:
- workflows       — trigger pipeline execution, query job history/step detail
- blocks          — list available blocks, inspect metadata
- configurations  — read/write AI behavior profiles (LLM roles + media models)
"""
