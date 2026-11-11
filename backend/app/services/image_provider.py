"""Pick the image provider for a workbench model id.

The poster workbench stores one `image_model` per task. Gateway models go through
`CoreAIClient`; `minimax-*` ids use the temporary direct MiniMax client until core-ai
publishes the same models. Callers only depend on `generate_image` / `edit_image`.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.services.core_ai import CoreAIError
from app.services.minimax_image import MINIMAX_MODELS, MODEL_PREFIX, MiniMaxImageClient


def is_minimax_model(image_model: str | None) -> bool:
    return str(image_model or "").strip().startswith(MODEL_PREFIX)


def provider_name(image_model: str | None) -> str:
    return "minimax" if is_minimax_model(image_model) else "core-ai"


def supports_model(image_model: str | None) -> bool:
    """Unknown minimax ids would reach the API as-is; only accept documented ones."""
    model = str(image_model or "").strip()
    if not is_minimax_model(model):
        return True
    return model in MINIMAX_MODELS


def image_client(
    settings: Settings,
    core_ai: Any,
    image_model: str | None,
) -> Any:
    """Return the client that can run `generate_image` / `edit_image` for this model."""
    if not is_minimax_model(image_model):
        return core_ai
    if not supports_model(image_model):
        raise CoreAIError("invalid_image_model", f"不支持的图片模型：{image_model}", 422)
    if not settings.minimax_image_api_key.strip():
        raise CoreAIError(
            "not_configured",
            "未配置 MINIMAX_IMAGE_API_KEY，无法使用 MiniMax 图片模型",
            503,
        )
    return MiniMaxImageClient(settings)
