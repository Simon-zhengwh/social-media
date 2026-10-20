from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://127.0.0.1:27018"
    mongodb_database: str = "social_media"
    storage_dir: str = "../storage"
    sample_dir: str = "../素材"
    skills_dir: str = "../.agents/skills"

    core_ai_gateway_base_url: str = (
        "https://core-ai-server.connexup-uat.net/api/gateway/v1"
    )
    core_ai_api_key: str = ""
    core_ai_image_model: str = "gpt-image-2.5-flare"
    core_ai_design_model: str = "gpt-6.1-sol"
    core_ai_edit_model: str = "deepseek-v4-flash"
    core_ai_image_quality: str = "high"
    core_ai_image_timeout_seconds: int = 300
    llm_timeout_seconds: int = 120
    design_model_enabled: bool = True
    visual_critic_enabled: bool = True
    reference_guided_enabled: bool = True
    component_generation_enabled: bool = True
    component_generation_cache_enabled: bool = True
    logo_integration_enabled: bool = False
    logo_exact_overlay: bool = True
    logo_integration_timeout_seconds: int = 420
    max_versions: int = 40

    canvas_width: int = 1024
    canvas_height: int = 1024
    max_upload_bytes: int = 10_000_000
    max_image_pixels: int = 25_000_000
    max_download_bytes: int = 30_000_000

    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = ""
    cos_region: str = ""
    cos_prefix: str = "social-media"
    cos_presign_expiry_seconds: int = 3600

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir)
        return (path if path.is_absolute() else BACKEND_DIR / path).resolve()

    @property
    def sample_path(self) -> Path:
        path = Path(self.sample_dir)
        return (path if path.is_absolute() else BACKEND_DIR / path).resolve()

    @property
    def skills_path(self) -> Path:
        path = Path(self.skills_dir)
        return (path if path.is_absolute() else BACKEND_DIR / path).resolve()

    @property
    def cos_enabled(self) -> bool:
        return bool(
            self.cos_bucket
            and self.cos_region
            and self.cos_secret_id
            and self.cos_secret_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
