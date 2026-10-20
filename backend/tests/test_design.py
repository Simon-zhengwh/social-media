from __future__ import annotations

import copy

import pytest

from app.services import design as design_utils
from app.services import editor


def test_font_family_accepts_brush_and_falls_back_to_sans():
    assert (
        design_utils.normalize_style({"font_family": "brush"})["font_family"]
        == "brush"
    )
    assert (
        design_utils.normalize_style({"font_family": "comic"})["font_family"]
        == "sans"
    )


def sample_design() -> dict:
    text_headline = design_utils.make_text_layer(
        layer_id="text_headline",
        name="主标题",
        role="headline",
        content="周末限时特惠",
        box={"x": 80, "y": 120, "width": 900, "height": 260},
        style={"font_size": 120, "font_weight": 900, "color": "#FFFFFF"},
        z_index=10,
    )
    text_price = design_utils.make_text_layer(
        layer_id="text_price",
        name="价格",
        role="price",
        content="¥199",
        box={"x": 80, "y": 1000, "width": 500, "height": 180},
        style={"font_size": 96, "font_weight": 900, "color": "#FFD54A"},
        z_index=20,
    )
    logo = design_utils.make_logo_layer(
        layer_id="logo_brand",
        asset_id="asset_logo",
        box={"x": 820, "y": 60, "width": 150, "height": 150},
        z_index=30,
    )
    return design_utils.make_design(
        canvas={"width": 1080, "height": 1920},
        clean_asset_id="asset_bg_clean",
        render_asset_id="asset_bg_render",
        background_prompt="夜晚餐厅",
        logo_integrated=True,
        text_layers=[text_headline, text_price],
        logo_layer=logo,
    )


def sample_full_design() -> dict:
    design = sample_design()
    extras = [
        ("brand", "品牌名", "SEEJOY 喜聚"),
        ("subheadline", "副标题", "火锅 · 寿司 · 甜品"),
        ("details", "活动信息", "营业时间 12PM - 12AM"),
        ("offer", "促销信息", "儿童5折"),
    ]
    for index, (role, name, content) in enumerate(extras):
        design["layers"].append(
            design_utils.make_text_layer(
                layer_id=f"text_{role}",
                name=name,
                role=role,
                content=content,
                box={"x": 60, "y": 400 + index * 120, "width": 600, "height": 90},
                style={"font_size": 48, "font_weight": 700, "color": "#FFFFFF"},
                z_index=40 + index * 10,
            )
        )
    return design


def test_make_design_has_one_background_and_ordered_layers():
    design = sample_design()
    design_utils.validate_design(design)
    types = [layer["type"] for layer in design["layers"]]
    assert types.count("background") == 1
    assert types == ["background", "text", "text", "logo"]


def test_update_text_only_changes_target_layer():
    design = sample_design()
    before = copy.deepcopy(design)
    action = {
        "type": "update_text",
        "layer_id": "text_headline",
        "content": "周末双人套餐",
    }
    result = editor.apply_actions(design, [action])["design"]

    assert design == before, "input design must not be mutated"
    headline = design_utils.find_layer(result, "text_headline")
    assert headline["content"] == "周末双人套餐"
    # Every other layer is byte-for-byte identical.
    for layer_id in ("background", "text_price", "logo_brand"):
        assert design_utils.find_layer(result, layer_id) == design_utils.find_layer(
            before, layer_id
        )
    assert result["background"] == before["background"]


def test_text_style_update_does_not_touch_background_or_logo():
    design = sample_design()
    action = {
        "type": "update_text_style",
        "layer_id": "text_headline",
        "style": {"font_size": 140, "color": "#FFD54A"},
        "scale_box": True,
    }
    result = editor.apply_actions(design, [action])["design"]
    headline = design_utils.find_layer(result, "text_headline")
    assert headline["style"]["font_size"] == 140
    assert headline["style"]["color"] == "#FFD54A"
    assert result["background"] == design["background"]
    assert design_utils.logo_layer(result) == design_utils.logo_layer(design)


def test_logo_move_does_not_touch_text():
    design = sample_design()
    action = {
        "type": "move_layer",
        "layer_id": "logo_brand",
        "position": {"x": 0, "y": 0},
        "anchor": "bottom_right",
    }
    applied = editor.apply_actions(design, [action])
    result = applied["design"]
    logo = design_utils.logo_layer(result)
    assert logo["position"]["x"] == 1080 - int(1080 * 0.05) - logo["size"]["width"]
    assert applied["logo_reintegrate"] is True
    assert result["layers"][1:3] == design["layers"][1:3]
    assert result["background"] == design["background"]


def test_background_regeneration_keeps_text_and_logo_payloads():
    design = sample_design()
    action = {
        "type": "regenerate_background",
        "prompt": "夜晚的高级西餐厅，暖色灯光",
    }
    applied = editor.apply_actions(design, [action])
    result = applied["design"]
    assert applied["background_changed"] is True
    assert result["background"]["prompt"] == action["prompt"]
    assert result["layers"][1:] == design["layers"][1:]


def test_hiding_model_integrated_logo_falls_back_to_clean_background():
    design = sample_design()
    action = {
        "type": "set_visibility",
        "layer_id": "logo_brand",
        "visible": False,
    }
    result = editor.apply_actions(design, [action])["design"]
    assert result["background"]["render_asset_id"] == "asset_bg_clean"
    assert result["background"]["logo_integrated"] is False
    assert result["layers"][0]["asset_id"] == "asset_bg_clean"
    assert design_utils.logo_layer(result)["visible"] is False


def test_deleting_model_integrated_logo_removes_it_from_background():
    design = sample_design()
    result = editor.apply_actions(
        design,
        [{"type": "delete_layer", "layer_id": "logo_brand"}],
    )["design"]
    assert design_utils.logo_layer(result) is None
    assert result["background"]["render_asset_id"] == "asset_bg_clean"
    assert result["background"]["logo_integrated"] is False


def test_parse_chinese_text_edit():
    design = sample_design()
    plan = editor.parse_instruction(design, "把主标题改成‘周末双人套餐’")
    assert plan["actions"][0]["type"] == "update_text"
    assert plan["actions"][0]["layer_id"] == "text_headline"
    assert plan["actions"][0]["content"] == "周末双人套餐"


def test_parse_price_edit():
    design = sample_design()
    plan = editor.parse_instruction(design, "价格改成 169 元")
    assert plan["actions"][0]["layer_id"] == "text_price"
    assert plan["actions"][0]["content"] == "169 元"


def test_parse_multiple_copy_edits_keeps_exact_text():
    design = sample_design()
    plan = editor.parse_instruction(
        design, "主标题改成“周末双人套餐”，价格改成 169 元"
    )
    assert len(plan["actions"]) == 2
    by_layer = {action["layer_id"]: action["content"] for action in plan["actions"]}
    assert by_layer["text_headline"] == "周末双人套餐"
    assert by_layer["text_price"] == "169 元"


def test_plan_edit_prefers_exact_user_copy_over_model(monkeypatch):
    design = sample_design()

    class RewritingClient:
        def chat_json(self, *args, **kwargs):
            return {
                "actions": [
                    {
                        "type": "update_text",
                        "layer_id": "text_price",
                        "content": "169元/位",
                    }
                ],
                "reply": "done",
            }

    settings = type("S", (), {"design_model_enabled": True})()
    plan = editor.plan_edit(
        settings,
        RewritingClient(),
        instruction="价格改成 169 元",
        design=design,
    )
    assert plan["source"] == "rules"
    assert plan["actions"][0]["content"] == "169 元"


def test_parse_bigger_headline_scales_type_and_box():
    design = sample_design()
    plan = editor.parse_instruction(design, "标题再大一点")
    action = plan["actions"][0]
    assert action["type"] == "update_text_style"
    assert action["layer_id"] == "text_headline"
    assert action["style"]["font_size"] > 120
    assert action["scale_box"] is True


def test_parse_multi_layer_color_fix():
    design = sample_design()
    plan = editor.parse_instruction(
        design, "把主标题和价格改成深色"
    )
    assert len(plan["actions"]) == 2
    assert all(action["type"] == "update_text_style" for action in plan["actions"])
    assert all(action["style"]["color"] == "#1A1A1A" for action in plan["actions"])


def test_specific_roles_do_not_match_generic_substrings():
    assert editor._target_role("副标题改成深色") == "subheadline"
    assert editor._target_role("活动信息改成深色") == "details"
    assert editor._target_role("促销信息改成深色") == "offer"
    assert editor._target_role("主标题改成深色") == "headline"


def test_multi_role_color_does_not_touch_headline_or_offer():
    design = sample_full_design()
    plan = editor.parse_instruction(
        design, "把品牌名、副标题和活动信息改成深色"
    )
    roles = {
        design_utils.find_layer(design, action["layer_id"])["role"]
        for action in plan["actions"]
    }
    assert roles == {"brand", "subheadline", "details"}
    assert "headline" not in roles
    assert "offer" not in roles


def test_dark_color_instruction_changes_style_not_content():
    design = sample_design()
    plan = editor.parse_instruction(design, "价格改成深色")
    action = plan["actions"][0]
    assert action["type"] == "update_text_style"
    assert action["style"]["color"] == "#1A1A1A"


def test_parse_logo_position_and_scale():
    design = sample_design()
    plan = editor.parse_instruction(design, "把 Logo 移到右上角并放大一点")
    types = [action["type"] for action in plan["actions"]]
    assert "move_layer" in types
    assert "resize_layer" in types


def test_parse_background_regeneration():
    design = sample_design()
    plan = editor.parse_instruction(design, "背景换成夜晚的西餐厅")
    assert plan["actions"][0]["type"] == "regenerate_background"
    assert "夜晚的西餐厅" in plan["actions"][0]["prompt"]


def test_unknown_target_is_rejected():
    design = sample_design()
    with pytest.raises(editor.EditError) as exc:
        editor.parse_instruction(design, "让它更高级一点")
    assert exc.value.code == "unsupported"


def test_relayout_instruction_is_deterministic():
    design = sample_full_design()
    plan = editor.parse_instruction(design, "重新排版，广告感强一点")
    assert plan["actions"] == [{"type": "relayout"}]


def test_validate_rejects_unknown_layer():
    design = sample_design()
    with pytest.raises(editor.EditError):
        editor.validate_actions(
            design,
            [{"type": "update_text", "layer_id": "nope", "content": "x"}],
        )


def test_layer_patch_rejects_background_edits():
    design = sample_design()
    background = design_utils.find_layer(design, "background")
    with pytest.raises(design_utils.DesignError):
        design_utils.apply_layer_patch(background, {"content": "x"})


def test_design_summary_is_compact_and_has_no_asset_paths():
    design = sample_design()
    summary = design_utils.design_summary(design)
    text = str(summary)
    assert "asset_logo" not in text
    assert "asset_bg_clean" not in text
    assert any(item["type"] == "text" for item in summary["layers"])
