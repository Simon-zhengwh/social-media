from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from PIL import Image

from app.config import Settings
from app.services import design as design_utils
from app.services import editor
from app.services.core_ai import CoreAIClient, CoreAIError

logger = logging.getLogger(__name__)

MAX_MODEL_IMAGE_SIDE = 1024
MAX_MODEL_IMAGE_BYTES = 1_500_000
ALLOWED_CRITIC_ACTIONS = {
    "update_text_style",
    "move_layer",
    "resize_layer",
    "update_shape_style",
}


def model_image_data_url(image: Image.Image, *, max_side: int = MAX_MODEL_IMAGE_SIDE) -> str:
    current = image.convert("RGB")
    current.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    for quality in (82, 72, 62):
        buffer = io.BytesIO()
        current.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_MODEL_IMAGE_BYTES or quality == 62:
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    raise CoreAIError("critic_image_too_large", "视觉审查图片压缩后仍然过大")


def _normalize_issues(raw: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return issues
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "minor")
        if severity not in {"major", "minor"}:
            severity = "minor"
        issues.append({
            "id": str(item.get("id") or f"issue_{len(issues) + 1}")[:80],
            "severity": severity,
            "category": str(item.get("category") or "visual")[:80],
            "layer_ids": [str(value) for value in item.get("layer_ids") or [] if value],
            "evidence": str(item.get("evidence") or "")[:500],
            "intent": str(item.get("intent") or "")[:500],
        })
    return issues


def _normalize_actions(raw: Any, design: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    valid: list[dict[str, Any]] = []
    for item in raw[:3]:
        if not isinstance(item, dict) or item.get("type") not in ALLOWED_CRITIC_ACTIONS:
            continue
        try:
            checked = editor.validate_actions(design, [item])[0]
        except Exception as exc:
            logger.info("critic action rejected: %s", exc)
            continue
        if checked["type"] == "update_text_style":
            checked["scale_box"] = False
        valid.append(checked)
    return valid


def review_candidate(
    settings: Settings,
    client: CoreAIClient,
    *,
    candidate_id: str,
    image: Image.Image,
    design: dict[str, Any],
    quality_report: dict[str, Any],
    comparison_image: Image.Image | None = None,
    comparison_candidate_id: str | None = None,
) -> dict[str, Any]:
    summary = design_utils.design_summary(design)
    copy_items = design_utils.copy_items_from_design(design)
    payload = {
        "candidate_id": candidate_id,
        "design_intent": design.get("design_intent"),
        "scene": summary,
        "copy_items": copy_items,
        "quality": quality_report,
        "comparison_candidate_id": comparison_candidate_id,
    }
    text = (
        "你是资深海报艺术指导。你会看到真实渲染成图，而不是 Scene JSON。\n"
        "审查构图、主次、阅读顺序、字体气质、图文融合、机械贴图感、装饰密度、重复标记和局部接缝。\n"
        "任何文案改写、价格/日期/单位变化、删除素材、隐藏事实文字、重新生成背景都不允许。\n"
        "只能建议以下动作：update_text_style、move_layer、resize_layer、update_shape_style。\n"
        "最多给出 3 个高影响动作；没有问题就返回空 issues。\n"
        f"数据：{json.dumps(payload, ensure_ascii=False)}\n"
        "输出 JSON：{\"candidate_id\":\"...\",\"issues\":[{\"id\":\"issue_01\",\"severity\":\"major\",\"category\":\"hierarchy\",\"layer_ids\":[\"layer_id\"],\"evidence\":\"...\",\"intent\":\"...\",\"suggested_actions\":[{\"type\":\"update_text_style\",\"layer_id\":\"...\",\"style\":{\"font_size\":100}}]}],"
        "\"comparison\":{\"preferred_candidate_id\":\"...\",\"confidence\":0.7,\"reason\":\"...\"}}"
    )
    content: list[dict[str, Any]] = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": model_image_data_url(image)}},
    ]
    if comparison_image is not None:
        content.append({"type": "image_url", "image_url": {"url": model_image_data_url(comparison_image)}})
    raw = client.chat_json([
        {"role": "system", "content": "你只输出合法 JSON，不要 Markdown 或解释。"},
        {"role": "user", "content": content},
    ], model=settings.core_ai_design_model)
    issues = _normalize_issues(raw.get("issues"))
    actions: list[dict[str, Any]] = []
    for issue in raw.get("issues") if isinstance(raw.get("issues"), list) else []:
        if isinstance(issue, dict):
            actions.extend(_normalize_actions(issue.get("suggested_actions"), design))
        if len(actions) >= 3:
            break
    comparison = raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {}
    return {
        "candidate_id": candidate_id,
        "issues": issues,
        "actions": actions[:3],
        "comparison": {
            "preferred_candidate_id": str(comparison.get("preferred_candidate_id") or candidate_id),
            "confidence": float(comparison.get("confidence") or 0.5),
            "reason": str(comparison.get("reason") or "")[:500],
        },
        "source": "model",
    }


def apply_actions(design: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    if not actions:
        return design
    validated = editor.validate_actions(design, actions)
    return editor.apply_actions(design, validated)["design"]
