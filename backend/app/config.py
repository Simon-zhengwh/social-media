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

    toapis_api_key: str = ""
    toapis_image_base_url: str = "https://api.toapis.com"
    toapis_upload_base_url: str = "https://toapis.com"
    toapis_chat_base_url: str = "https://toapis.com"
    toapis_image_model: str = "gpt-image-2.5-flare"
    toapis_design_model: str = "gpt-6-sol-official"
    toapis_edit_model: str = "deepseek-v4-flash"
    image_ratio: str = "1:1"
    image_resolution: str = "1K"
    image_poll_interval_seconds: float = 5.0
    image_wait_timeout_seconds: int = 600
    llm_timeout_seconds: int = 120
    design_model_enabled: bool = True
    logo_integration_enabled: bool = True
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
