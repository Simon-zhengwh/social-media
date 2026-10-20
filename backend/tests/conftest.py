from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.config import PROJECT_DIR, Settings
from app.services.object_store import LocalObjectStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        sample_dir=str(PROJECT_DIR / "素材"),
        toapis_api_key="test-key",
        image_poll_interval_seconds=0.01,
        image_wait_timeout_seconds=5,
        design_model_enabled=False,
        logo_integration_enabled=False,
    )


@pytest.fixture
def store(settings: Settings) -> LocalObjectStore:
    return LocalObjectStore(settings.storage_path / "objects")


def png_bytes(width: int = 320, height: int = 240, color=(180, 90, 50)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def product_png() -> bytes:
    return png_bytes()


@pytest.fixture
def logo_png() -> bytes:
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for x in range(30, 98):
        for y in range(30, 98):
            image.putpixel((x, y), (190, 20, 20, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
