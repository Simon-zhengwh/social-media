from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from app.config import Settings
from app.services import design as design_utils
from app.services.core_ai import CoreAIClient

logger = logging.getLogger(__name__)

# More specific roles must win over generic words. For example, "副标题"
# contains "标题" and "活动信息" contains "活动"; naive substring matching
# would silently edit the headline or offer layer too.
ROLE_PATTERNS = {
    "subheadline": r"副标题|小标题|subheadline",
    "headline": r"主标题|大标题|(?<!副)(?<!小)标题|headline",
    "price": r"价格|价钱|售价|price",
    "details": r"活动信息|营业时间|地址|详情|details",
    "offer": r"优惠|折扣|促销|活动(?!信息)|offer",
    "cta": r"按钮|行动|号召|cta",
    "brand": r"品牌名|品牌|brand",
    "legal": r"补充说明|条款|legal",
}

ROLE_PRIORITY = [
    "subheadline",
    "headline",
    "price",
    "details",
    "offer",
    "cta",
    "brand",
    "legal",
]

COLOR_KEYWORDS = {
    "红": "#E5342B",
    "红色": "#E5342B",
    "绿": "#1E6B3A",
    "绿色": "#1E6B3A",
    "黄": "#FFD54A",
    "黄色": "#FFD54A",
    "白": "#FFFFFF",
    "白色": "#FFFFFF",
    "黑": "#111111",
    "黑色": "#111111",
    "金": "#E8B75A",
    "金色": "#E8B75A",
    "橙": "#F08A24",
    "橙色": "#F08A24",
    "蓝": "#2563EB",
    "蓝色": "#2563EB",
    "紫": "#7C3AED",
    "紫色": "#7C3AED",
    "深色": "#1A1A1A",
    "浅色": "#FFFFFF",
    "亮色": "#FFF5DC",
    "奶油色": "#FFF5DC",
}

POSITION_KEYWORDS = {
    "左上角": "top_left",
    "右上角": "top_right",
    "左下角": "bottom_left",
    "右下角": "bottom_right",
    "顶部": "top",
    "底部": "bottom",
    "居中": "center",
    "中间": "center",
}

ALLOWED_ACTIONS = {
    "update_text",
    "update_text_style",
    "move_layer",
    "resize_layer",
    "set_visibility",
    "regenerate_background",
    "relayout",
    "add_text",
    "delete_layer",
    "update_shape_style",
}


class EditError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _all_text_roles(design: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(layer.get("role")): layer
        for layer in design.get("layers", [])
        if layer.get("type") == "text"
    }


def _resolve_layer(design: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    layer_id = action.get("layer_id")
    if layer_id:
        try:
            return design_utils.find_layer(design, str(layer_id))
        except design_utils.DesignError as exc:
            raise EditError(exc.code, exc.message) from exc
    role = action.get("role")
    if role:
        candidates = design_utils.find_layers_by_role(design, str(role))
        if len(candidates) > 1:
            raise EditError("ambiguous_role", "存在多个同角色图层，请指定 layer_id")
        if candidates:
            return candidates[0]
    raise EditError("layer_not_found", "没有找到要修改的图层")


def _scale_position(
    position: dict[str, int],
    size: dict[str, int],
    anchor: str,
    canvas: dict[str, int],
) -> dict[str, int]:
    margin = int(canvas["width"] * 0.05)
    x = position["x"]
    y = position["y"]
    if anchor in {"top_left", "bottom_left", "left"}:
        x = margin
    elif anchor in {"top_right", "bottom_right", "right"}:
        x = canvas["width"] - margin - size["width"]
    elif anchor == "center":
        x = (canvas["width"] - size["width"]) // 2
    if anchor in {"top_left", "top_right", "left", "right"}:
        y = margin
    elif anchor in {"bottom_left", "bottom_right", "bottom"}:
        y = canvas["height"] - margin - size["height"]
    elif anchor == "center":
        y = (canvas["height"] - size["height"]) // 2
    return {"x": max(0, int(x)), "y": max(0, int(y))}


def _detach_baked_logo(design: dict[str, Any]) -> None:
    """Return to the clean background when a baked-in logo is hidden/deleted."""
    background = design.get("background") or {}
    clean = background.get("clean_asset_id")
    if not clean:
        return
    background["render_asset_id"] = clean
    background["logo_integrated"] = False
    try:
        layer = design_utils.find_layer(design, "background")
    except design_utils.DesignError:
        return
    layer["asset_id"] = clean


def validate_actions(
    design: dict[str, Any], actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(actions, list) or not actions:
        raise EditError("no_actions", "没有识别到可执行的修改")
    if len(actions) > 12:
        raise EditError("too_many_actions", "一次修改包含过多操作")
    validated: list[dict[str, Any]] = []
    canvas = design["canvas"]
    for raw in actions:
        if not isinstance(raw, dict):
            raise EditError("invalid_action", "修改操作格式无效")
        action_type = str(raw.get("type") or "")
        if action_type not in ALLOWED_ACTIONS:
            raise EditError("invalid_action", f"不支持的修改类型：{action_type}")
        action = copy.deepcopy(raw)
        action["type"] = action_type

        if action_type in {
            "update_text",
            "update_text_style",
            "move_layer",
            "resize_layer",
            "set_visibility",
            "delete_layer",
            "update_shape_style",
        }:
            layer = _resolve_layer(design, action)
            action["layer_id"] = layer["id"]
            if design_utils.is_protected(layer):
                raise EditError("protected_layer", f"图层「{layer.get('name')}」已被人工修改保护")
            if action_type == "update_text":
                if layer.get("type") != "text":
                    raise EditError("invalid_target", "只能修改文字图层的内容")
                content = str(action.get("content") or "").strip()
                if not content:
                    raise EditError("empty_content", "新的文字内容不能为空")
                action["content"] = content[:500]
            elif action_type == "update_text_style":
                if layer.get("type") != "text":
                    raise EditError("invalid_target", "只能修改文字图层的样式")
                if not isinstance(action.get("style"), dict) or not action["style"]:
                    raise EditError("empty_style", "没有提供样式修改")
            elif action_type == "move_layer":
                if not isinstance(action.get("position"), dict):
                    raise EditError("invalid_position", "缺少新位置")
            elif action_type == "resize_layer":
                if not isinstance(action.get("size"), dict) and not action.get("scale"):
                    raise EditError("invalid_size", "缺少新尺寸或缩放比例")
            elif action_type == "set_visibility":
                if "visible" not in action:
                    raise EditError("invalid_visibility", "缺少可见性")
            elif action_type == "delete_layer":
                if layer.get("type") == "background":
                    raise EditError("invalid_target", "底图图层不能删除")
            elif action_type == "update_shape_style":
                if layer.get("type") != "shape":
                    raise EditError("invalid_target", "只能修改形状图层样式")
                if not isinstance(action.get("style"), dict) or not action["style"]:
                    raise EditError("empty_style", "没有提供形状样式修改")
        elif action_type == "regenerate_background":
            prompt = str(action.get("prompt") or "").strip()
            if len(prompt) < 4:
                raise EditError("empty_prompt", "请说明新的背景需求")
            action["prompt"] = prompt[:1000]
        elif action_type == "add_text":
            role = str(action.get("role") or "headline")
            if role not in design_utils.TEXT_ROLES:
                role = "headline"
            content = str(action.get("content") or "").strip()
            if not content:
                raise EditError("empty_content", "新增文字不能为空")
            action["role"] = role
            action["content"] = content[:500]
            action["name"] = str(action.get("name") or "新增文字")[:20]
            box = action.get("box") or {
                "x": int(canvas["width"] * 0.08),
                "y": int(canvas["height"] * 0.45),
                "width": int(canvas["width"] * 0.6),
                "height": int(canvas["height"] * 0.12),
            }
            if not isinstance(box, dict):
                raise EditError("invalid_box", "新增文字的位置无效")
            action["box"] = box
        elif action_type == "relayout":
            pass
        validated.append(action)
    return validated


def apply_actions(
    design: dict[str, Any], actions: list[dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(design)
    canvas = result["canvas"]
    background_changed = False
    logo_reintegrate = False
    changed: list[str] = []
    summaries: list[str] = []

    for action in actions:
        action_type = action["type"]
        if action_type == "update_text":
            layer = design_utils.find_layer(result, action["layer_id"])
            updated = design_utils.apply_layer_patch(layer, {"content": action["content"]})
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(f"更新「{layer.get('name')}」文案")
        elif action_type == "update_text_style":
            layer = design_utils.find_layer(result, action["layer_id"])
            style_patch = copy.deepcopy(action["style"])
            updated = design_utils.apply_layer_patch(layer, {"style": style_patch})
            if action.get("scale_box") and "font_size" in style_patch:
                old_size = float((layer.get("style") or {}).get("font_size") or 72)
                new_size = float((updated.get("style") or {}).get("font_size") or old_size)
                if old_size > 0 and new_size > 0:
                    ratio = max(0.4, min(2.5, new_size / old_size))
                    size = updated["size"]
                    updated = design_utils.apply_layer_patch(
                        updated,
                        {
                            "size": {
                                "width": min(
                                    canvas["width"],
                                    max(40, int(size["width"] * ratio)),
                                ),
                                "height": min(
                                    canvas["height"],
                                    max(30, int(size["height"] * ratio)),
                                ),
                            }
                        },
                    )
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(f"调整「{layer.get('name')}」样式")
        elif action_type == "move_layer":
            layer = design_utils.find_layer(result, action["layer_id"])
            position = action["position"]
            if action.get("anchor"):
                new_position = _scale_position(
                    layer["position"],
                    layer.get("size", {"width": 100, "height": 100}),
                    str(action["anchor"]),
                    canvas,
                )
            else:
                new_position = {
                    "x": int(max(-10000, float(position.get("x", layer["position"]["x"])))),
                    "y": int(max(-10000, float(position.get("y", layer["position"]["y"])))),
                }
            updated = design_utils.apply_layer_patch(layer, {"position": new_position})
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(f"移动「{layer.get('name')}」")
            if layer.get("role") == "logo" and (layer.get("render_hint") == "baked" or layer.get("mode") == "model_integrated"):
                _detach_baked_logo(result)
                updated["render_hint"] = "overlay"
                updated["mode"] = "overlay"
                result = design_utils.replace_layer(result, updated)
        elif action_type == "resize_layer":
            layer = design_utils.find_layer(result, action["layer_id"])
            if action.get("size"):
                size = {
                    "width": int(max(16, float(action["size"].get("width", 100)))),
                    "height": int(max(16, float(action["size"].get("height", 100)))),
                }
            else:
                scale = max(0.2, min(4.0, float(action.get("scale") or 1.0)))
                size = {
                    "width": max(16, int(layer.get("size", {}).get("width", 100) * scale)),
                    "height": max(16, int(layer.get("size", {}).get("height", 100) * scale)),
                }
            updated = design_utils.apply_layer_patch(layer, {"size": size})
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(f"缩放「{layer.get('name')}」")
            if layer.get("role") == "logo" and (layer.get("render_hint") == "baked" or layer.get("mode") == "model_integrated"):
                _detach_baked_logo(result)
                updated["render_hint"] = "overlay"
                updated["mode"] = "overlay"
                result = design_utils.replace_layer(result, updated)
        elif action_type == "set_visibility":
            layer = design_utils.find_layer(result, action["layer_id"])
            updated = design_utils.apply_layer_patch(
                layer, {"visible": bool(action["visible"])}
            )
            if (
                layer.get("role") == "logo"
                and not action["visible"]
                and (result.get("background") or {}).get("logo_integrated")
            ):
                _detach_baked_logo(result)
                updated["mode"] = "overlay"
                updated["render_hint"] = "overlay"
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(
                f"{'显示' if action['visible'] else '隐藏'}「{layer.get('name')}」"
            )
        elif action_type == "delete_layer":
            layer = design_utils.find_layer(result, action["layer_id"])
            if layer.get("role") == "logo":
                _detach_baked_logo(result)
            result["layers"] = [
                item for item in result["layers"] if item.get("id") != layer["id"]
            ]
            changed.append(layer["id"])
            summaries.append(f"删除「{layer.get('name')}」")
        elif action_type == "add_text":
            layer = design_utils.make_text_layer(
                layer_id=f"text_{action['role']}_{len(result['layers'])}",
                name=action["name"],
                role=action["role"],
                content=action["content"],
                box=action["box"],
                style=action.get("style"),
                z_index=max(int(item.get("z_index", 0)) for item in result["layers"]) + 5,
            )
            result["layers"].append(layer)
            changed.append(layer["id"])
            summaries.append(f"新增「{layer['name']}」")
        elif action_type == "regenerate_background":
            result["background"]["prompt"] = action["prompt"]
            background_changed = True
            summaries.append("重新生成场景底图")
        elif action_type == "update_shape_style":
            layer = design_utils.find_layer(result, action["layer_id"])
            updated = design_utils.apply_layer_patch(layer, action["style"])
            result = design_utils.replace_layer(result, updated)
            changed.append(layer["id"])
            summaries.append(f"调整「{layer.get('name')}」形状样式")
        elif action_type == "relayout":
            summaries.append("重新排版：广告层级")

    return {
        "design": result,
        "changed_layers": changed,
        "summaries": summaries,
        "background_changed": background_changed,
        "logo_reintegrate": logo_reintegrate,
    }


def _extract_new_content(instruction: str) -> str | None:
    patterns = [
        r"(?:改成|改为|换成|变成|替换为|写作|写成|设为|设置为)[：:]?\s*[“‘\"「『]?(.+?)[”’\"」』]?\s*$",
        r"(?:内容|文案)[：:]\s*[“‘\"「『]?(.+?)[”’\"」』]?\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, instruction)
        if match:
            content = match.group(1).strip().strip("。！!，,；;‘’\"“”")
            if content:
                return content
    return None


def _target_role(instruction: str) -> str | None:
    for role in ROLE_PRIORITY:
        if re.search(ROLE_PATTERNS[role], instruction, flags=re.IGNORECASE):
            return role
    return None


def _roles_in_instruction(instruction: str) -> list[str]:
    return [
        role
        for role in ROLE_PRIORITY
        if re.search(ROLE_PATTERNS[role], instruction, flags=re.IGNORECASE)
    ]


def _clauses(instruction: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[，,；;。\n]+", instruction)
        if item.strip()
    ]


def _color_from_instruction(instruction: str) -> str | None:
    for keyword, color in COLOR_KEYWORDS.items():
        if keyword in instruction:
            return color
    match = re.search(r"#([0-9A-Fa-f]{6})", instruction)
    if match:
        return f"#{match.group(1).upper()}"
    return None


def _style_from_text(instruction: str, layer: dict[str, Any]) -> dict[str, Any]:
    style: dict[str, Any] = {}
    color = _color_from_instruction(instruction)
    if color:
        style["color"] = color
    if any(word in instruction for word in ("居中", "中间对齐")):
        style["align"] = "center"
    elif "右对齐" in instruction:
        style["align"] = "right"
    elif "左对齐" in instruction:
        style["align"] = "left"
    size_match = re.search(r"(?:字号|字体大小|字体尺寸)\D{0,4}(\d{2,3})", instruction)
    if size_match:
        style["font_size"] = int(size_match.group(1))
    elif any(word in instruction for word in ("加大", "放大", "大一点", "大一些")):
        current = float(layer.get("style", {}).get("font_size") or 72)
        style["font_size"] = int(min(400, current * 1.18))
    elif any(word in instruction for word in ("减小", "缩小", "小一点", "小一些")):
        current = float(layer.get("style", {}).get("font_size") or 72)
        style["font_size"] = int(max(12, current * 0.85))
    elif "加粗" in instruction:
        style["font_weight"] = 900
    elif "变细" in instruction or "细一点" in instruction:
        style["font_weight"] = 300
    return style


def _position_from_instruction(instruction: str) -> str | None:
    for keyword, anchor in POSITION_KEYWORDS.items():
        if keyword in instruction:
            return anchor
    return None


def parse_instruction(design: dict[str, Any], instruction: str) -> dict[str, Any]:
    text = instruction.strip()
    if not text:
        raise EditError("empty_instruction", "请输入修改要求")
    if any(
        word in text
        for word in (
            "重新排版",
            "排版",
            "广告感",
            "太死板",
            "像PPT",
            "像ppt",
            "不够醒目",
        )
    ):
        return {
            "actions": [{"type": "relayout"}],
            "reply": "已重新排版：放大标题、下移价格、加入倾斜促销标签，文字内容保持不变。",
            "source": "rules",
        }

    # Multiple copy edits in one sentence: parse each clause independently so
    # the user's exact text is never rewritten by a model.
    clause_actions: list[dict[str, Any]] = []
    for clause in _clauses(text):
        clause_role = _target_role(clause)
        clause_content = _extract_new_content(clause)
        if not clause_role or not clause_content:
            continue
        layer = design_utils.find_layer_by_role(design, clause_role)
        if layer is None:
            continue
        clause_actions.append(
            {
                "type": "update_text",
                "layer_id": layer["id"],
                "content": clause_content,
            }
        )
    if len(clause_actions) > 1:
        return {
            "actions": clause_actions,
            "reply": "已更新指定的文字图层，其他图层保持不变。",
            "source": "rules",
        }

    roles = _roles_in_instruction(text)
    if len(roles) > 1:
        multi_style: list[dict[str, Any]] = []
        for role_name in roles:
            layer = design_utils.find_layer_by_role(design, role_name)
            if layer is None:
                continue
            style = _style_from_text(text, layer)
            if style:
                multi_style.append(
                    {
                        "type": "update_text_style",
                        "layer_id": layer["id"],
                        "style": style,
                        "scale_box": "font_size" in style,
                    }
                )
        if multi_style:
            return {
                "actions": multi_style,
                "reply": "已调整指定文字图层的样式，底图和 Logo 保持不变。",
                "source": "rules",
            }

    role = _target_role(text)
    logo = design_utils.logo_layer(design)
    result: dict[str, Any] = {"actions": [], "reply": "", "source": "rules"}

    is_background = any(word in text for word in ("背景", "底图", "场景", "氛围"))
    if is_background and any(
        word in text for word in ("换", "改", "生成", "调整", "更", "变")
    ):
        prompt = re.sub(
            r"^(?:把|将)?(?:背景|底图|场景|氛围)(?:换成|改为|变成|调整成|改成)?",
            "",
            text,
        ).strip("，。 ")
        result["actions"].append(
            {"type": "regenerate_background", "prompt": prompt or text}
        )
        result["reply"] = "已安排重新生成底图，文字与 Logo 图层保持不变。"
        return result

    if logo is not None and any(
        word in text.lower() for word in ("logo", "标志", "商标")
    ):
        actions: list[dict[str, Any]] = []
        anchor = _position_from_instruction(text)
        if anchor:
            actions.append(
                {
                    "type": "move_layer",
                    "layer_id": logo["id"],
                    "position": logo["position"],
                    "anchor": anchor,
                }
            )
        if any(word in text for word in ("放大", "大一点", "大一些", "调大")):
            actions.append(
                {"type": "resize_layer", "layer_id": logo["id"], "scale": 1.25}
            )
        elif any(word in text for word in ("缩小", "小一点", "小一些", "调小")):
            actions.append(
                {"type": "resize_layer", "layer_id": logo["id"], "scale": 0.8}
            )
        if not actions:
            raise EditError("unsupported", "暂时只支持移动、放大或缩小 Logo")
        result["actions"] = actions
        result["reply"] = "已调整 Logo 图层；Logo 使用原始透明素材直接覆盖，文字层保持不变，也不会调用生图。"
        return result

    if role:
        layer = design_utils.find_layer_by_role(design, role)
        if layer is None:
            raise EditError("layer_not_found", f"当前设计没有「{role}」图层")
        new_content = _extract_new_content(text)
        style_phrase = bool(
            new_content
            and (
                new_content in COLOR_KEYWORDS
                or (new_content.endswith("色") and len(new_content) <= 4)
                or re.fullmatch(r"#[0-9A-Fa-f]{6}", new_content)
                or any(
                    word in new_content
                    for word in ("大一点", "小一点", "加粗", "变细", "居中", "对齐", "字号")
                )
            )
        )
        if new_content and not style_phrase:
            result["actions"].append(
                {
                    "type": "update_text",
                    "layer_id": layer["id"],
                    "content": new_content,
                }
            )
            result["reply"] = f"已更新「{layer.get('name')}」，其他图层保持不变。"
            return result

        style = _style_from_text(text, layer)
        if style:
            result["actions"].append(
                {
                    "type": "update_text_style",
                    "layer_id": layer["id"],
                    "style": style,
                    "scale_box": "font_size" in style,
                }
            )
            result["reply"] = f"已调整「{layer.get('name')}」样式，其他图层保持不变。"
            return result

    raise EditError(
        "unsupported",
        "没有识别出要修改的对象。可以说“把主标题改成…”“价格改为…”“Logo 放大一点”或“背景换成…”。",
    )


def plan_edit(
    settings: Settings,
    client: CoreAIClient | None,
    *,
    instruction: str,
    design: dict[str, Any],
) -> dict[str, Any]:
    # Explicit "改成/改为" copy edits are handled by rules first: the model must
    # not silently add suffixes (e.g. appending "/位" to a new price).
    if _extract_new_content(instruction) is not None:
        return parse_instruction(design, instruction)
    roles = _roles_in_instruction(instruction)
    if len(roles) <= 1:
        try:
            return parse_instruction(design, instruction)
        except EditError:
            pass
    if settings.design_model_enabled and client is not None:
        try:
            plan = _llm_edit_plan(settings, client, instruction, design)
            actions = validate_actions(design, plan.get("actions") or [])
            normalized_instruction = re.sub(r"\s+", "", instruction)
            for action in actions:
                if action.get("type") != "update_text":
                    continue
                normalized_content = re.sub(r"\s+", "", action.get("content", ""))
                if normalized_content not in normalized_instruction:
                    raise EditError(
                        "model_rewrote_copy",
                        "模型试图改写用户文案，已拒绝执行",
                    )
            return {
                "actions": actions,
                "reply": str(plan.get("reply") or "已完成修改。"),
                "source": "model",
            }
        except Exception as exc:  # noqa: BLE001 - deterministic parser is the fallback
            logger.info("edit model unavailable or invalid, using rules: %s", exc)
    return parse_instruction(design, instruction)


def _llm_edit_plan(
    settings: Settings,
    client: CoreAIClient,
    instruction: str,
    design: dict[str, Any],
) -> dict[str, Any]:
    summary = design_utils.design_summary(design)
    system = (
        "你是海报图层的精准编辑路由器。你只能输出 JSON，不能直接改图。"
        "你的首要原则：只修改用户明确指定的图层，其他图层的内容、样式、位置、"
        "底图和 Logo 一律不得改动。改文字时绝对不要重新生成底图。"
    )
    user = (
        "可用图层如下（layer_id 是唯一标识）：\n"
        f"{json.dumps(summary, ensure_ascii=False)}\n\n"
        f"用户修改要求：{instruction}\n\n"
        "输出 JSON：\n"
        '{"actions":[...],"reply":"用一句中文说明改了什么、哪些没动"}\n'
        "action 类型只能是：\n"
        '- {"type":"update_text","layer_id":"...","content":"完整新文案"}\n'
        '- {"type":"update_text_style","layer_id":"...","style":{"font_size":120,'
        '"font_weight":900,"color":"#FFFFFF","align":"left"},"scale_box":true}\n'
        '- {"type":"move_layer","layer_id":"...","position":{"x":0,"y":0}}\n'
        '- {"type":"move_layer","layer_id":"...","position":{"x":0,"y":0},"anchor":"top_right"}\n'
        '- {"type":"resize_layer","layer_id":"...","scale":1.2}\n'
        '- {"type":"set_visibility","layer_id":"...","visible":false}\n'
        '- {"type":"regenerate_background","prompt":"新的场景描述"}\n'
        "规则：改文字只用 update_text；改字号/颜色/对齐只用 update_text_style；"
        "移动 Logo 用 move_layer；换背景才允许 regenerate_background。"
        "不要输出未列出的图层 id，不要一次改多个无关图层。"
    )
    response = client.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=settings.core_ai_edit_model,
    )
    return response
