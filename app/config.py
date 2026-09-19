"""
app.config — Application settings

Uses pydantic-settings to read from environment variables and/or a .env file.

Planned settings:
- DATABASE_URL   — SQLite connection string (default: sqlite+aiosqlite:///./data/app.db)
- APP_NAME       — Display name in the UI
- DEBUG          — Toggle debug mode
- LOG_LEVEL      — Logging verbosity
- MEDIA_DIR      — Root path for media file storage
- LLM_BASE_URL   — Base URL for LLM API (default: LM Studio at localhost:1234)
- OPENAI_API_KEY — API key (not needed for local LM Studio)
- LLM_MODEL      — Default model name (e.g. google/gemma-4-12b)
- LLM_TEMPERATURE — Default temperature for LLM calls
- COMFYUI_URL     — ComfyUI API base URL (default: localhost:8188)
- GENERATION_BACKEND — 'comfyui' or 'placeholder' (for testing)
- IMAGE_OUTPUT_DIR   — Where generated images are saved
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

    # LLM settings (defaults target LM Studio local server)
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    openai_api_key: str = "lm-studio"  # LM Studio ignores this but the client requires a value
    llm_model: str = "google/gemma-4-12b"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096

    # Image generation settings
    generation_backend: str = "comfyui"  # 'comfyui' or 'placeholder'
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_poll_interval: float = 2.0  # seconds between status polls
    comfyui_timeout: float = 600.0  # max wait for generation (10 min)
    comfyui_checkpoint: str = "RealVisXL_V5.0_fp16.safetensors"
    image_output_dir: Path = Path("/Volumes/SanDisk Mac AI/ComfyUI/output")


settings = Settings()
