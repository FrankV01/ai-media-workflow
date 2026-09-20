"""
app.config — Application settings via pydantic-settings

Reads from environment variables and/or `.env` file.
See `.env.example` for all available settings and defaults.
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
    llm_model: str = "qwen/qwen3.5-9b"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096*4
    llm_timeout: float = 1800.0  # max wait per LLM request (30 min)

    # Image generation settings
    generation_backend: str = "comfyui"  # 'comfyui' or 'placeholder'
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_poll_interval: float = 2.0  # seconds between status polls
    comfyui_timeout: float = 3600.0  # max wait for generation (1 hour)
    comfyui_checkpoint: str = "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"
    comfyui_refiner_checkpoint: str = "sd_xl_refiner_1.0.safetensors"
    comfyui_upscale_2x_model: str = "RealESRGAN_x2.pth"
    comfyui_upscale_4x_model: str = "RealESRGAN_x4.pth"
    refiner_steps: int = 20
    refiner_cfg_scale: float = 6.0
    refiner_sampler: str = "dpmpp_2m"
    refiner_scheduler: str = "karras"
    refiner_denoise: float = 0.25
    image_output_dir: Path = Path("./data/output")


settings = Settings()
