"""
app.config — Application settings

Uses pydantic-settings to read from environment variables and/or a .env file.

Planned settings:
- DATABASE_URL   — SQLite connection string (default: sqlite+aiosqlite:///./data/app.db)
- APP_NAME       — Display name in the UI
- DEBUG          — Toggle debug mode
- LOG_LEVEL      — Logging verbosity
- MEDIA_DIR      — Root path for media file storage
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Media Workflow"
    debug: bool = True
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    media_dir: Path = Path("./data/media")


settings = Settings()
