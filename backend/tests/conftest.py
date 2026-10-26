from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.services.object_store import LocalObjectStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        core_ai_api_key="test-key",
        core_ai_image_timeout_seconds=5,
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
