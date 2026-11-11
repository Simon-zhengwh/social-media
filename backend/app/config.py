from functools import lru_cache
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


def local_bypass_enabled(settings: Any) -> bool:
    """Share the same local-mode boundary for requests, storage and recovery."""
    if not getattr(settings, "am_bypass_local", False):
        return False
    origin = urlsplit(str(getattr(settings, "social_media_origin", "")).strip().rstrip("/"))
    return (
        origin.scheme in {"http", "https"}
        and origin.hostname in {"127.0.0.1", "localhost", "::1"}
    )


class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://127.0.0.1:27018"
    mongodb_database: str = "social_media"
    storage_dir: str = "../local_media"
    skills_dir: str = "../.agents/skills"

    core_ai_gateway_base_url: str = (
        "https://core-ai-server.connexup-uat.net/api/gateway/v1"
    )
    core_ai_server_base_url: str = "https://core-ai-server.connexup-uat.net"
    core_ai_api_key: str = ""
    core_ai_prompt_agent_id: str = "9a7eb9c3-0d57-4841-83b2-ea9191ff8dd8"
    core_ai_prompt_agent_timeout_seconds: int = 900
    core_ai_image_agent_id: str = "5a162c34-f333-453f-9835-de6a4f96e3fb"
    core_ai_image_agent_timeout_seconds: int = 1800
    core_ai_test_agent_id: str = "5a162c34-f333-453f-9835-de6a4f96e3fb"
    core_ai_test_timeout_seconds: int = 1800
    # Original-photo replacements retain the existing environment variable names.
    core_ai_background_replace_agent_id: str = "5a162c34-f333-453f-9835-de6a4f96e3fb"
    core_ai_background_replace_agent_timeout_seconds: int = 2700
    core_ai_image_model: str = "gpt-image-2.5-sunburst"
    core_ai_design_model: str = "gpt-6.1-sol"
    core_ai_image_quality: str = "high"
    core_ai_image_timeout_seconds: int = 300
    llm_timeout_seconds: int = 120

    am_base_url: str = "https://am.connexup-uat.net"
    am_timeout_seconds: int = 30
    am_media_storage_service_key: str = ""
    am_media_storage_base_path: str = "/fbr-uat/product"
    # Local development only: skips the AM Console session check so the
    # workspace can run without an AM login cookie. Never enable in UAT or
    # production; merchant scoping still applies to every query.
    am_bypass_local: bool = False
    am_bypass_merchant_id: str = ""
    social_media_origin: str = "https://social-media.connexup-uat.net"
    social_media_url: str = "https://social-media.connexup-uat.net"

    max_upload_bytes: int = 20_000_000
    image_compress_threshold_bytes: int = 10_000_000
    image_downscale_pixel_threshold: int = 25_000_000
    max_image_pixels: int = 60_000_000
    max_download_bytes: int = 30_000_000
    # Sized for self-contained HTML editors and result bundles.
    max_agent_artifact_bytes: int = 200_000_000

    image_host_base_url: str = "https://43.139.235.133:5002"
    image_host_upload_token: str = ""
    image_host_public_base_url: str = "https://43.139.235.133/img"
    image_host_timeout_seconds: int = 60

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
    selected = os.environ.get("SOCIAL_MEDIA_ENV_FILE", "").strip()
    if selected:
        path = Path(selected)
        path = (path if path.is_absolute() else BACKEND_DIR / path).resolve()
        if not path.is_file():
            raise ValueError(f"SOCIAL_MEDIA_ENV_FILE 指定的配置文件不存在：{path}")
        # Explicit selection replaces .env; process environment still takes precedence.
        return Settings(_env_file=path)
    return Settings()
