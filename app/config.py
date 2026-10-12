"""
app.config — Application settings via pydantic-settings

Reads from environment variables and/or `.env` file.
See `.env.example` for all available settings and defaults.
"""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _app_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as file:
            return str(tomllib.load(file)["project"]["version"])
    try:
        return version("ai-media-workflow")
    except PackageNotFoundError:
        return "unknown"


APP_VERSION = _app_version()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Synthetic Muse AI Media Control Panel"
    build_number: str = "dev"
    debug: bool = True
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # LLM settings (defaults target LM Studio local server).
    # llm_model selects the ACTIVE (role, model) profile; temperature /
    # max_tokens / enable_thinking only seed MISSING profile rows — the
    # database (LlmRoleConfiguration) owns existing values.
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    openai_api_key: str = "lm-studio"  # LM Studio ignores this but the client requires a value
    llm_model: str = "qwen/qwen3.5-9b"  # selects the active LLM profile key
    llm_temperature: float = 0.7
    llm_max_tokens: int = 32768  # reasoning models burn most of this on hidden thinking
    # False sends reasoning_effort="none" (disables hidden reasoning on thinking models)
    llm_enable_thinking: bool = False
    llm_timeout: float = 1800.0  # max wait per LLM request (30 min)

    # Image generation settings. generation_backend selects the backend;
    # comfyui_checkpoint seeds the DEFAULT MediaModelConfiguration profile for
    # new workflows. Runtime model selection is DB-owned (workflow.media_model_name
    # and per-step config overrides). The remaining model/refiner fields only
    # seed MISSING profile rows. URLs/polls/timeouts stay env-only.
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
    image_output_dir: Path = Path("/Volumes/SanDisk Mac AI/ComfyUI/output")

    # FourthWall storefront integration (env-only: credentials are secrets and
    # never enter the database). The Open API user/password pair is created in
    # the Fourthwall admin under "For developers"; requests authenticate with
    # HTTP Basic auth.
    fourthwall_api_user: str = ""
    fourthwall_api_password: str = ""
    fourthwall_base_url: str = "https://api.fourthwall.com/open-api/v1.0"
    fourthwall_timeout: float = 300.0  # max wait per API/upload request

    # Cloudflare R2 delivery hosting (env-only: the S3 API credentials are
    # secrets and never enter the database). Endpoint, bucket, key prefix,
    # and the public base URL are DB-backed settings edited on
    # /settings/general → Store Fronts → Cloudflare R2.
    cloudflare_r2_access_key_id: str = ""
    cloudflare_r2_secret_access_key: str = ""

    @field_validator("image_output_dir")
    @classmethod
    def require_absolute_image_output_dir(cls, value: Path) -> Path:
        output_dir = value.expanduser()
        if not output_dir.is_absolute():
            raise ValueError("IMAGE_OUTPUT_DIR must be an absolute path")
        return output_dir


settings = Settings()
