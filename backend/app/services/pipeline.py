from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.services import design as design_utils
from app.services import asset_generator, brand_style, designer, editor, platform_presets, quality, reference_guided, storage, visual_critic
from app.services.canvas import gateway_image_size
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.image_ref import ImageRef, cleanup_image_refs, image_ref_from_asset
from app.services.object_store import ObjectStore
from app.services.renderer import render_design

logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "update_text": "更新文案",
    "update_text_style": "调整文字样式",
    "move_layer": "移动图层",
    "resize_layer": "缩放图层",
    "set_visibility": "切换可见性",
    "regenerate_background": "重生成底图",
    "add_text": "新增文字层",
    "delete_layer": "删除图层",
    "relayout": "重新排版",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_prompt(
    user_prompt: str,
    has_logo: bool = True,
    product_count: int = 1,
    ratio: str = "1:1",
    brand: brand_style.BrandStyle | None = None,
) -> str:
    """Legacy background prompt builder kept for callers and compatibility tests."""
    return designer.build_background_prompt(
        user_prompt,
        has_logo=has_logo,
        product_count=product_count,
        brand=brand,
        ratio=ratio,
    )


def mark_interrupted(db: Database) -> None:
    db.tasks.update_many(
        {"status": {"$in": ["queued", "running"]}},
        {
            "$set": {
                "status": "interrupted",
                "stage": "interrupted",
                "edit": {
                    "status": "failed",
                    "instruction": None,
                    "error": "本地服务重启，任务已中断。可点击恢复，恢复会重新调用 core-ai 网关。",
                },
                "error": {
                    "code": "interrupted",
                    "message": "本地服务重启，任务已中断。可点击恢复，恢复会重新调用 core-ai 网关。",
                },
                "updated_at": utcnow(),
            }
        },
    )
    db.tasks.update_many(
        {"edit.status": "running"},
        {
            "$set": {
                "edit.status": "failed",
                "edit.error": "本地服务重启，编辑已中断，请重新发送修改。",
                "updated_at": utcnow(),
            }
        },
    )


def _fail(db: Database, task_id: str, code: str, message: str) -> None:
    db.tasks.update_one(
        {"_id": task_id},
        {
            "$set": {
                "status": "failed",
                "stage": "failed",
                "error": {"code": code, "message": message},
                "updated_at": utcnow(),
            }
        },
    )


def _set_stage(db: Database, task_id: str, stage: str, **extra: Any) -> None:
    fields = {"stage": stage, "updated_at": utcnow(), **extra}
    db.tasks.update_one({"_id": task_id}, {"$set": fields})


def _save_image_asset(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    name: str,
    data: bytes,
    kind: str,
) -> tuple[str, str]:
    relative = storage.object_key(settings, "backgrounds", name)
    store.put_bytes(relative, data, "image/png")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 - provider payload is untrusted
        raise CoreAIError("invalid_background", "core-ai 返回的图片无法解码") from exc
    asset_id = f"asset_{uuid.uuid4().hex}"
    db.assets.insert_one(
        {
            "_id": asset_id,
            "kind": kind,
            "original_name": name,
            "path": relative,
            "prepared_path": relative,
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": utcnow(),
        }
    )
    return asset_id, relative


def _save_output_asset(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    version: int,
    image: Image.Image,
    name_suffix: str = "",
) -> str:
    data = storage.encode_png(image)
    name = f"{task_id}_v{version}{name_suffix}.png"
    relative = storage.object_key(settings, "outputs", name)
    store.put_bytes(relative, data, "image/png")
    asset_id = f"asset_{uuid.uuid4().hex}"
    db.assets.insert_one(
        {
            "_id": asset_id,
            "kind": "output",
            "original_name": name,
            "path": relative,
            "prepared_path": relative,
            "mime_type": "image/png",
            "width": image.width,
            "height": image.height,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": utcnow(),
        }
    )
    return asset_id


def _background_key(db: Database, asset_id: str) -> str:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise CoreAIError("background_missing", "底图素材不存在")
    return asset.get("prepared_path") or asset["path"]


def _append_message(
    db: Database,
    task_id: str,
    *,
    role: str,
    content: str,
    version: int | None = None,
    actions: list[dict[str, Any]] | None = None,
    status: str = "done",
) -> dict[str, Any]:
    message = {
        "id": f"msg_{uuid.uuid4().hex}",
        "role": role,
        "content": content,
        "created_at": utcnow(),
        "version": version,
        "actions": actions or [],
        "status": status,
    }
    db.tasks.update_one({"_id": task_id}, {"$push": {"messages": message}})
    return message


def _save_version(
    db: Database,
    *,
    task_id: str,
    version: int,
    design: dict[str, Any],
    output_asset_id: str,
    instruction: str,
    summary: str,
    quality_report: dict[str, Any],
    render_metrics: dict[str, Any],
) -> None:
    db.versions.insert_one(
        {
            "_id": f"ver_{uuid.uuid4().hex}",
            "task_id": task_id,
            "version": version,
            "design": copy.deepcopy(design),
            "output_asset_id": output_asset_id,
            "instruction": instruction,
            "summary": summary,
            "quality": quality_report,
            "render_metrics": render_metrics,
            "created_at": utcnow(),
        }
    )
    db.tasks.update_one(
        {"_id": task_id},
        {
            "$set": {
                "design": copy.deepcopy(design),
                "background_asset_id": design["background"]["render_asset_id"],
                "background_clean_asset_id": design["background"]["clean_asset_id"],
                "output_asset_id": output_asset_id,
                "current_version": version,
                "status": "succeeded",
                "stage": "done",
                "progress": 100,
                "error": None,
                "edit": {"status": "idle", "instruction": None, "error": None},
                "quality": quality_report,
                "render_metrics": render_metrics,
                "updated_at": utcnow(),
            }
        },
    )


def _logo_integration_prompt(
    crop_size: tuple[int, int],
    logo_layer: dict[str, Any],
    crop_origin: tuple[int, int],
) -> str:
    box = logo_layer["position"]
    size = logo_layer["size"]
    crop_width, crop_height = crop_size
    x_ratio = (box["x"] - crop_origin[0]) / max(1, crop_width)
    y_ratio = (box["y"] - crop_origin[1]) / max(1, crop_height)
    width_ratio = size["width"] / max(1, crop_width)
    return (
        f"图1是海报的局部裁切，尺寸 {crop_width}×{crop_height}。"
        "图2是品牌提供的透明 Logo 原图。\n"
        "请把图2的 Logo 自然、专业地融入图1，作为画面中的品牌标识。\n"
        f"Logo 在局部图中的位置：左边缘约在宽度 {x_ratio:.2f} 处，"
        f"上边缘约在高度 {y_ratio:.2f} 处，宽度约占局部图宽度的 {width_ratio:.2f}。\n"
        "硬性要求：\n"
        "1. 严格保留图2的图形、文字、比例、颜色和透明边缘，不要重绘、改写、翻译或添加笔画。\n"
        "   必须保持 Logo 原始宽高比，不要拉伸、压扁或裁切。\n"
        "2. 不要给 Logo 添加任何卡片、圆角底板、白底、边框、描边或色块。\n"
        "3. 让 Logo 自然接受环境光影，但不要改变 Logo 本身的颜色和形状。\n"
        "4. 除 Logo 周围区域外，图1的菜品、构图、背景、光影、像素尽量保持不变。\n"
        "5. 不要添加任何其他文字、字母、数字、二维码或水印。"
    )


def overlay_exact_logo(
    composed: Image.Image,
    logo: Image.Image,
    *,
    box: dict[str, Any],
    size: dict[str, Any],
) -> Image.Image:
    """Composite the original transparent logo at the reserved slot."""
    result = composed.convert("RGBA").copy()
    exact_logo = logo.copy().convert("RGBA")
    box_width = int(size["width"])
    box_height = int(size["height"])
    exact_logo.thumbnail((box_width, box_height), Image.Resampling.LANCZOS)
    exact_x = int(box["x"]) + (box_width - exact_logo.width) // 2
    exact_y = int(box["y"]) + (box_height - exact_logo.height) // 2
    result.alpha_composite(exact_logo, (max(0, exact_x), max(0, exact_y)))
    return result


def _integrate_logo(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    version: int,
    background_asset_id: str,
    logo_asset: dict[str, Any] | None,
    logo_layer: dict[str, Any],
    canvas: dict[str, int],
) -> str | None:
    if not settings.logo_integration_enabled or logo_asset is None:
        return None
    temp_paths: list[Path] = []
    try:
        clean = storage.open_image(store, _background_key(db, background_asset_id))
        canvas_width = int(canvas["width"])
        canvas_height = int(canvas["height"])
        clean = ImageOps.fit(
            clean.convert("RGB"),
            (canvas_width, canvas_height),
            method=Image.Resampling.LANCZOS,
        )
        box = logo_layer["position"]
        size = logo_layer["size"]
        pad = max(70, int(max(size["width"], size["height"]) * 0.55))
        crop_left = max(0, int(box["x"]) - pad)
        crop_top = max(0, int(box["y"]) - pad)
        crop_right = min(canvas_width, int(box["x"]) + int(size["width"]) + pad)
        crop_bottom = min(canvas_height, int(box["y"]) + int(size["height"]) + pad)
        if crop_right - crop_left < 40 or crop_bottom - crop_top < 40:
            return None
        crop = clean.crop((crop_left, crop_top, crop_right, crop_bottom))
        crop_path = store.download_to_temp(
            _background_key(db, background_asset_id), suffix=".png"
        )
        temp_paths.append(crop_path)
        crop_path.write_bytes(storage.encode_png(crop))
        logo_ref = image_ref_from_asset(db, store, str(logo_asset["_id"]))
        data = client.edit_image(
            _logo_integration_prompt(
                (crop.width, crop.height), logo_layer, (crop_left, crop_top)
            ),
            [crop_path, logo_ref],
            size=gateway_image_size(crop.width, crop.height),
        )
        with Image.open(io.BytesIO(data)) as generated_source:
            generated = generated_source.convert("RGB")
            generated.load()
        generated = ImageOps.fit(
            generated,
            (crop.width, crop.height),
            method=Image.Resampling.LANCZOS,
        )
        # Guard against the model silently returning the untouched crop.
        logo_region = (
            int(box["x"]) - crop_left,
            int(box["y"]) - crop_top,
            int(box["x"]) - crop_left + int(size["width"]),
            int(box["y"]) - crop_top + int(size["height"]),
        )
        logo_region = (
            max(0, logo_region[0]),
            max(0, logo_region[1]),
            min(crop.width, logo_region[2]),
            min(crop.height, logo_region[3]),
        )
        if logo_region[2] <= logo_region[0] or logo_region[3] <= logo_region[1]:
            return None
        diff = ImageChops.difference(crop, generated)
        mean_diff = ImageStat.Stat(diff.crop(logo_region)).mean[0]
        if mean_diff < 1.0:
            logger.warning("logo integration produced no visible change for %s", task_id)
            return None
        composed = clean.copy()
        feather = min(24, max(6, min(crop.width, crop.height) // 12))
        mask = Image.new("L", crop.size, 255)
        mask_draw = ImageDraw.Draw(mask)
        for index in range(feather):
            alpha = int(255 * index / max(1, feather))
            mask_draw.rectangle(
                (index, index, crop.width - 1 - index, crop.height - 1 - index),
                outline=alpha,
            )
        composed.paste(generated, (crop_left, crop_top), mask)
        if settings.logo_exact_overlay:
            # Keep the brand mark pixel-exact. The model still contributes the
            # surrounding lighting/placement; the original transparent asset is
            # composited on top so text/letterforms can never be redrawn.
            # First remove the model's own logo rendition (which can be a few
            # pixels larger and leave a ghost ring) by restoring the clean crop
            # in a feathered region around the reserved slot.
            # Model-rendered logos often spill well beyond the reserved box.
            # Restore a generous clean halo around the slot before exact
            # overlay, otherwise duplicate rings/ghosts remain beside the mark.
            erase_pad = max(28, int(max(size["width"], size["height"]) * 0.55))
            erase_left = max(0, int(box["x"]) - crop_left - erase_pad)
            erase_top = max(0, int(box["y"]) - crop_top - erase_pad)
            erase_right = min(
                crop.width,
                int(box["x"]) - crop_left + int(size["width"]) + erase_pad,
            )
            erase_bottom = min(
                crop.height,
                int(box["y"]) - crop_top + int(size["height"]) + erase_pad,
            )
            if erase_right > erase_left and erase_bottom > erase_top:
                erase_mask = Image.new("L", crop.size, 0)
                erase_draw = ImageDraw.Draw(erase_mask)
                erase_draw.rounded_rectangle(
                    (erase_left, erase_top, erase_right, erase_bottom),
                    radius=max(6, erase_pad // 2),
                    fill=255,
                )
                erase_mask = erase_mask.filter(ImageFilter.GaussianBlur(4))
                composed.paste(crop, (crop_left, crop_top), erase_mask)
            exact_logo = storage.open_image(
                store, logo_asset.get("prepared_path") or logo_asset["path"]
            ).convert("RGBA")
            composed = overlay_exact_logo(
                composed,
                exact_logo,
                box=box,
                size=size,
            )
        data = storage.encode_png(composed)
        asset_id, _ = _save_image_asset(
            db,
            settings,
            store,
            task_id=task_id,
            name=f"{task_id}_v{version}_logo.png",
            data=data,
            kind="background",
        )
        return asset_id
    except Exception as exc:  # noqa: BLE001 - fall back to transparent overlay
        logger.warning("logo integration failed for %s: %s", task_id, exc)
        return None
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


def _render_and_commit(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    design: dict[str, Any],
    version: int,
    instruction: str,
    summary: str,
) -> str:
    _set_stage(db, task_id, "rendering", progress=96)
    task_doc = db.tasks.find_one({"_id": task_id}) or {}
    task_input = task_doc.get("input") or {}
    design = quality.clamp_design_to_safe_area(design, task_doc.get("placement"))
    canvas = design["canvas"]
    background = storage.open_image(
        store, _background_key(db, design["background"]["render_asset_id"])
    )
    assets: dict[str, Image.Image] = {}
    logo = design_utils.logo_layer(design)
    if logo is not None and logo.get("mode") != "model_integrated":
        asset = db.assets.find_one({"_id": logo["asset_id"]})
        if asset is None:
            raise CoreAIError("logo_missing", "Logo 素材不存在")
        assets[logo["asset_id"]] = storage.open_image(
            store, asset.get("prepared_path") or asset["path"]
        )
    result = render_design(
        settings,
        background,
        design_utils.sorted_layers(design),
        (int(canvas["width"]), int(canvas["height"])),
        assets=assets,
    )
    quality_report = quality.assess_scene(
        design,
        result.image,
        result.design.get("layers") if isinstance(result.design, dict) else None,
        design_utils.copy_items_from_design(design),
        platform=task_doc.get("platform"),
        placement=task_doc.get("placement"),
        merchant_info=task_input.get("merchant_info"),
    )
    platform_failures = [
        item for item in quality_report.get("hard_failures") or []
        if str(item.get("code") or "").startswith("platform_") or item.get("code") == "safe_area"
    ]
    if platform_failures:
        raise CoreAIError(
            "platform_violation",
            "；".join(str(item.get("message") or "平台规则检查失败") for item in platform_failures[:3]),
        )
    render_metrics = result.design if isinstance(result.design, dict) else {}
    output_asset_id = _save_output_asset(
        db,
        settings,
        store,
        task_id=task_id,
        version=version,
        image=result.image,
    )
    _save_version(
        db,
        task_id=task_id,
        version=version,
        design=design,
        output_asset_id=output_asset_id,
        instruction=instruction,
        summary=summary,
        quality_report=quality_report,
        render_metrics=render_metrics,
    )
    return output_asset_id


def _layer_tokens(layer: dict[str, Any]) -> str:
    return " ".join([
        str(layer.get("id") or ""),
        str(layer.get("name") or ""),
        str(layer.get("role") or ""),
    ]).lower()


def _is_photo_container_shape(layer: dict[str, Any], canvas: dict[str, int]) -> bool:
    if layer.get("type") != "shape":
        return False
    try:
        area = float((layer.get("size") or {}).get("width", 0)) * float((layer.get("size") or {}).get("height", 0))
    except (TypeError, ValueError):
        area = 0.0
    canvas_area = max(1, int(canvas["width"]) * int(canvas["height"]))
    tokens = _layer_tokens(layer)
    frame_words = (
        "photo_paper", "photo_frame", "image_frame", "picture_frame",
        "photo_card", "image_card", "照片", "相框", "图片边框",
    )
    return area >= canvas_area * 0.14 and any(word in tokens for word in frame_words)


def _is_duplicate_component_shape(layer: dict[str, Any], component_kinds: set[str]) -> bool:
    if layer.get("type") != "shape":
        return False
    tokens = _layer_tokens(layer)
    if "price" in component_kinds and any(word in tokens for word in ("price", "价格")):
        return True
    if "offer" in component_kinds and any(word in tokens for word in ("offer", "promo", "优惠", "促销")):
        return True
    if "title" in component_kinds and any(word in tokens for word in ("title_brush", "headline_patch", "标题底")):
        return True
    return False


def _render_context_preview(
    settings: Settings,
    store: ObjectStore,
    db: Database,
    *,
    canvas: dict[str, int],
    clean_asset_id: str,
    layers: list[dict[str, Any]],
) -> Image.Image:
    background = storage.open_image(store, _background_key(db, clean_asset_id))
    scene = {"layers": layers}
    assets = _load_scene_assets(db, store, scene)
    result = render_design(
        settings,
        background,
        design_utils.sorted_layers(scene),
        (int(canvas["width"]), int(canvas["height"])),
        assets=assets,
    )
    return result.image


def run_task(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
) -> None:
    task = db.tasks.find_one_and_update(
        {"_id": task_id, "status": {"$in": ["queued", "waiting", "interrupted"]}},
        {"$set": {"status": "running", "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if task is None:
        return
    temp_paths: list[Path] = []
    try:
        input_data = task["input"]
        products = _load_products(db, input_data)
        logo = None
        canvas = {
            "width": int((task.get("canvas") or {}).get("width", settings.canvas_width)),
            "height": int((task.get("canvas") or {}).get("height", settings.canvas_height)),
        }
        brand = brand_style.brand_for_task(task, db, store)
        if brand is not None:
            for reference in brand.reference_inputs:
                if reference.owned and reference.path is not None:
                    temp_paths.append(reference.path)
        style_refs = _load_style_references(db, store, input_data) if brand is None else []
        for reference in style_refs:
            if reference.owned and reference.path is not None:
                temp_paths.append(reference.path)
        merchant_info = input_data.get("merchant_info") if isinstance(input_data.get("merchant_info"), dict) else None
        platform_context = platform_presets.prompt_from_task(
            task.get("platform"), merchant_info, task.get("placement")
        )
        brief = (str(input_data.get("prompt") or "") + ("\n\n" + platform_context if platform_context else "")).strip()

        plan = task.get("creative_plan")
        if not isinstance(plan, dict) or not plan.get("copy_items"):
            _set_stage(db, task_id, "planning", progress=4)
            product_refs: list[ImageRef] = []
            try:
                product_refs = [
                    image_ref_from_asset(db, store, str(item["_id"]))
                    for item in products
                ]
                plan = designer.make_creative_plan(
                    settings,
                    client,
                    brief=brief,
                    product_paths=product_refs,
                    has_logo=logo is not None,
                    brand_hint=brand.name if brand else None,
                    brand=brand,
                    canvas=canvas,
                    capabilities=list(design_utils.SUPPORTED_CAPABILITIES),
                    logo_asset_id=logo["_id"] if logo else None,
                    product_asset_ids=[str(item["_id"]) for item in products],
                    style_reference_paths=style_refs,
                )
            finally:
                cleanup_image_refs(product_refs)
            plan["copy_items"] = plan.get("copy_items") or designer._copy_items_from_raw(plan, str(input_data.get("prompt") or ""))
            plan["copy_items"] = platform_presets.ensure_merchant_copy_items(
                plan.get("copy_items") or [], task.get("platform"), merchant_info
            )
            db.tasks.update_one(
                {"_id": task_id},
                {"$set": {"creative_plan": plan, "brief_snapshot": {"raw": str(input_data.get("prompt") or ""), "brand_id": brand.id if brand else None, "brand_mode": str(input_data.get("brand_mode") or "none"), "ratio": str(input_data.get("ratio") or "")}, "updated_at": utcnow()}},
            )

        generation_mode = str(input_data.get("generation_mode") or "classic")
        reference_guided_active = bool(settings.reference_guided_enabled and generation_mode == "reference_guided" and brand is not None)
        reference_asset_id = task.get("design_reference_asset_id")
        reference_path: ImageRef | None = None
        reference_meta: dict[str, Any] = {}
        if reference_guided_active and not reference_asset_id:
            _set_stage(db, task_id, "designing_reference", progress=10)
            db.tasks.update_one({"_id": task_id}, {"$set": {"provider.status": "running", "provider.progress": 10, "updated_at": utcnow()}})
            ref_product_refs: list[ImageRef] = []
            try:
                ref_product_refs = [
                    image_ref_from_asset(db, store, str(item["_id"]))
                    for item in products
                ]
                reference = reference_guided.generate_global_reference(
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    brand=brand,
                    brief=brief,
                    copy_items=plan.get("copy_items") or [],
                    intent=design_utils.normalize_design_intent(plan.get("design_intent")),
                    ratio=str(input_data.get("ratio") or ""),
                    canvas=canvas,
                    product_paths=ref_product_refs,
                )
                reference_asset_id = reference.asset_id
                reference_meta = reference.summary()
                db.tasks.update_one(
                    {"_id": task_id},
                    {"$set": {
                        "design_reference_asset_id": reference_asset_id,
                        "reference_guided": {"enabled": True, "status": "reference_ready", "reference": reference_meta},
                        "updated_at": utcnow(),
                    }},
                )
            except Exception as exc:  # noqa: BLE001 - classic path remains a safe fallback
                logger.warning("global design reference failed for %s: %s", task_id, exc)
                reference_guided_active = False
                db.tasks.update_one(
                    {"_id": task_id},
                    {"$set": {"reference_guided": {"enabled": False, "status": "fallback", "reason": str(exc)[:500]}, "updated_at": utcnow()}},
                )
            finally:
                cleanup_image_refs(ref_product_refs)
        if reference_asset_id and reference_guided_active:
            reference_asset = db.assets.find_one({"_id": reference_asset_id})
            if reference_asset is not None:
                reference_path = image_ref_from_asset(db, store, str(reference_asset_id))
                if reference_path.owned and reference_path.path is not None:
                    temp_paths.append(reference_path.path)

        clean_asset_id = task.get("background_clean_asset_id")
        if not clean_asset_id:
            _set_stage(db, task_id, "generating", progress=20)
            db.tasks.update_one({"_id": task_id}, {"$set": {"provider.name": "core-ai", "provider.model": settings.core_ai_image_model, "provider.task_id": None, "provider.status": "running", "provider.progress": 20, "updated_at": utcnow()}})
            scene_prompt = str(plan.get("background_prompt") or "").strip() or str(input_data.get("prompt") or "").strip()
            if platform_context:
                scene_prompt = f"{scene_prompt}\n\n{platform_context}"
            independent = any(isinstance(item, dict) and item.get("role") == "product" and item.get("asset_id") for item in (plan.get("asset_requests") or []))
            prompt = designer.build_background_prompt(
                scene_prompt,
                has_logo=logo is not None,
                product_count=len(products),
                brand=brand,
                ratio=str(input_data.get("ratio") or ""),
                design_intent=plan.get("design_intent"),
                independent_products=independent,
            )
            product_refs: list[ImageRef] = []
            used_reference_clean = False
            try:
                product_refs = [
                    image_ref_from_asset(db, store, str(item["_id"]))
                    for item in products
                ]
                image_bytes: bytes | None = None
                if reference_guided_active and reference_path is not None:
                    try:
                        image_bytes = reference_guided.generate_clean_plate(
                            settings,
                            client,
                            reference_path=reference_path,
                            brand=brand,
                            brief=scene_prompt,
                            canvas=canvas,
                        )
                        used_reference_clean = True
                    except Exception as exc:  # noqa: BLE001 - fallback to classic background
                        logger.warning("reference clean plate failed for %s: %s", task_id, exc)
                if image_bytes is None:
                    generation_refs = [*product_refs, *style_refs]
                    if generation_refs:
                        image_bytes = client.edit_image(prompt, generation_refs, size=gateway_image_size(canvas["width"], canvas["height"]))
                    else:
                        image_bytes = client.generate_image(prompt, size=gateway_image_size(canvas["width"], canvas["height"]))
                if used_reference_clean:
                    image_bytes = reference_guided.harmonize_scene_image(image_bytes, brand)
            finally:
                cleanup_image_refs(product_refs)
            _set_stage(db, task_id, "generating", progress=70)
            clean_asset_id, _ = _save_image_asset(db, settings, store, task_id=task_id, name=f"{task_id}_clean.png", data=image_bytes, kind="background")
            db.tasks.update_one({"_id": task_id}, {"$set": {"background_clean_asset_id": clean_asset_id, "background_asset_id": clean_asset_id, "provider.status": "completed", "provider.progress": 100, "updated_at": utcnow()}})

        design = task.get("design")
        if not isinstance(design, dict):
            _set_stage(db, task_id, "layout", progress=86)
            background_ref = image_ref_from_asset(db, store, str(clean_asset_id))
            if background_ref.owned and background_ref.path is not None:
                temp_paths.append(background_ref.path)
            try:
                image_layers, shape_layers, asset_usage = asset_generator.build_scene_assets(
                    plan=plan,
                    canvas=canvas,
                    products=products,
                    logo=logo,
                    db=db,
                )
                if reference_guided_active:
                    # The clean plate already contains the food scene from the global
                    # reference. Re-overlaying the source product photo recreates the
                    # exact rectangular "photo card" this architecture is meant to remove.
                    image_layers = [layer for layer in image_layers if layer.get("role") != "product"]
                    shape_layers = [layer for layer in shape_layers if not _is_photo_container_shape(layer, canvas)]
                    asset_usage = [item for item in asset_usage if item.get("role") != "product"]
                layout = designer.make_layout(
                    settings,
                    client,
                    background_path=background_ref,
                    canvas=canvas,
                    copy=dict(plan.get("copy") or {}),
                    has_logo=logo is not None,
                    brand=brand,
                    design_intent=plan.get("design_intent"),
                    copy_items=plan.get("copy_items") or [],
                    initial_scene_plan=plan.get("initial_scene_plan"),
                    capabilities=list(design_utils.SUPPORTED_CAPABILITIES),
                    fixed_layers=image_layers + shape_layers,
                    reference_path=reference_path,
                )
                text_layers, _ = designer.layout_to_layers(layout, canvas=canvas, logo_asset_id=None, brand=brand)
                if not text_layers:
                    fallback = designer.fallback_layout(canvas, dict(plan.get("copy") or {}), has_logo=logo is not None, brand=brand, copy_items=plan.get("copy_items"))
                    text_layers, _ = designer.layout_to_layers(fallback, canvas=canvas, logo_asset_id=None, brand=brand)
                logo_layer = next((layer for layer in image_layers if layer.get("role") == "logo"), None)
                if logo_layer is not None and layout.get("logo_box"):
                    logo_layer["position"] = {"x": int(layout["logo_box"]["x"]), "y": int(layout["logo_box"]["y"])}
                    logo_layer["size"] = {"width": int(layout["logo_box"]["width"]), "height": int(layout["logo_box"]["height"])}

                artwork: dict[str, reference_guided.GeneratedAsset | None] = {}
                components: list[dict[str, Any]] = []
                active_shape_layers = list(shape_layers)
                style_reference_state: dict[str, Path | None] = {"path": None}
                if reference_guided_active and reference_path is not None:
                    groups = reference_guided.plan_component_groups(text_layers, canvas)
                    grouped_ids = {
                        str(layer.get("id"))
                        for group in groups
                        for layer in group.get("layers") or []
                    }
                    base_text = [layer for layer in text_layers if str(layer.get("id")) not in grouped_ids]
                    fallback_text: list[dict[str, Any]] = []
                    _set_stage(db, task_id, "generating_components", progress=90)

                    def generate_group(group: dict[str, Any], context_layers: list[dict[str, Any]]):
                        context_paths: list[Path] = []
                        try:
                            context_image = _render_context_preview(
                                settings,
                                store,
                                db,
                                canvas=canvas,
                                clean_asset_id=clean_asset_id,
                                layers=context_layers,
                            )
                            context_paths = reference_guided.make_context_images(
                                context_image,
                                target_box=group["box"],
                                canvas=canvas,
                            )
                            component_style_reference = reference_path
                            if str(group["kind"]) != "title" and style_reference_state["path"] is not None:
                                component_style_reference = style_reference_state["path"]
                            art = reference_guided.generate_component_art(
                                db,
                                settings,
                                store,
                                client,
                                task_id=task_id,
                                kind=str(group["kind"]),
                                brand=brand,
                                intent=design_utils.normalize_design_intent(plan.get("design_intent")),
                                reference_path=component_style_reference,
                                text_hint=" / ".join(str(layer.get("content") or "") for layer in group["layers"]),
                                context_paths=context_paths,
                                target=group.get("target"),
                            )
                            return group, art
                        finally:
                            for path in context_paths:
                                path.unlink(missing_ok=True)

                    # Title is the dominant visual anchor. Generate it first, composite it,
                    # then price and offer can be generated in parallel from the same context.
                    title_group = next((group for group in groups if group.get("kind") == "title"), None)
                    remaining_groups = [group for group in groups if group is not title_group]
                    if title_group is not None:
                        context_layers = image_layers + active_shape_layers + base_text + components
                        group, art = generate_group(title_group, context_layers)
                        if art is not None:
                            artwork[str(group["kind"])] = art
                            components.append(reference_guided.component_from_group(group, art))
                            active_shape_layers = [
                                layer for layer in active_shape_layers
                                if not _is_duplicate_component_shape(layer, {str(group["kind"])})
                            ]
                            if title_group is not None and str(group["kind"]) == "title":
                                title_art_asset = db.assets.find_one({"_id": art.asset_id})
                                if title_art_asset is not None:
                                    title_art_path = store.download_to_temp(
                                        title_art_asset.get("prepared_path") or title_art_asset["path"],
                                        suffix=".png",
                                    )
                                    temp_paths.append(title_art_path)
                                    style_reference_state["path"] = title_art_path
                        else:
                            fallback_text.extend(group["layers"])

                    if remaining_groups:
                        context_layers = image_layers + active_shape_layers + base_text + components
                        if len(remaining_groups) > 1:
                            with ThreadPoolExecutor(max_workers=min(2, len(remaining_groups))) as pool:
                                futures = [pool.submit(generate_group, group, context_layers) for group in remaining_groups]
                                results = [future.result() for future in futures]
                        else:
                            results = [generate_group(remaining_groups[0], context_layers)]
                        for group, art in results:
                            if art is not None:
                                artwork[str(group["kind"])] = art
                                components.append(reference_guided.component_from_group(group, art))
                                active_shape_layers = [
                                    layer for layer in active_shape_layers
                                    if not _is_duplicate_component_shape(layer, {str(group["kind"])})
                                ]
                            else:
                                fallback_text.extend(group["layers"])
                    shape_layers = active_shape_layers
                    text_layers = base_text + fallback_text
                layers = image_layers + shape_layers + text_layers + components
                layers.sort(key=lambda item: int(item.get("z_index", 0)))
                layers = quality.clamp_layers_to_safe_area(
                    layers, canvas, task.get("placement")
                )
                design = design_utils.make_scene(
                    canvas=canvas,
                    clean_asset_id=clean_asset_id,
                    render_asset_id=clean_asset_id,
                    background_prompt=str(plan.get("background_prompt") or ""),
                    logo_integrated=False,
                    layers=layers,
                    design_intent=plan.get("design_intent"),
                    scene_revision=1,
                )
                design["asset_usage"] = asset_usage
                if reference_guided_active:
                    design["design_reference_asset_id"] = reference_asset_id
                    design["generation_mode"] = "reference_guided"
                    design["reference_guided"] = {
                        "reference": reference_meta,
                        "components": reference_guided.split_component_assets(artwork),
                        "component_count": len(components),
                    }
                else:
                    design["generation_mode"] = "classic"
                db.tasks.update_one(
                    {"_id": task_id},
                    {"$set": {
                        "design": design,
                        "layout_source": layout.get("source"),
                        "design_intent": design.get("design_intent"),
                        "scene_revision": 1,
                        "reference_guided": {
                            "enabled": bool(reference_guided_active),
                            "status": "components_ready" if components else "text_only",
                            "reference": reference_meta,
                            "components": reference_guided.split_component_assets(artwork),
                        },
                    }},
                )
            finally:
                background_ref.cleanup()

        _set_stage(db, task_id, "rendering", progress=96)
        initial = render_candidate(db, settings, store, task_id=task_id, design=design, candidate_id="candidate_v1")
        run_id = initial["run_id"]
        selected, corrected, review1, review2 = run_visual_review(db, settings, store, client, task_id=task_id, design=design, initial=initial, run_id=run_id)
        if review1 is not None:
            review = _review_summary(initial, corrected, selected, review1, review2)
        else:
            review = review2 or {"status": "not_run", "issues": [], "candidates": [{"candidate_id": initial["candidate_id"], "selected": True, "issues": 0, "hard_failures": len((initial.get("quality") or {}).get("hard_failures") or [])}]}
        version = int(task.get("current_version") or 1) or 1
        output_asset_id = commit_candidate(db, task_id=task_id, candidate=selected, version=version, instruction=str(input_data.get("prompt") or ""), summary="首次生成：DesignIntent、独立对象与视觉审查", review=review)
        if review.get("status") == "corrected":
            review_message = "海报已生成。视觉审查发现的问题已应用修正，文字和图片素材保持独立图层。"
        elif review.get("status") == "issues_kept":
            review_message = "海报已生成。视觉审查已完成并保留初稿，文字和图片素材保持独立图层。"
        elif review.get("status") == "passed":
            review_message = "海报已生成。视觉审查通过，文字和图片素材保持独立图层。"
        else:
            review_message = "海报已生成。Critic 未执行或不可用，已提交初稿。"
        _append_message(db, task_id, role="assistant", content=review_message, version=version)
        db.tasks.update_one({"_id": task_id}, {"$set": {"output_asset_id": output_asset_id, "updated_at": utcnow()}})
    except CoreAIError as exc:
        logger.warning("pipeline failed for %s: %s", task_id, exc.message)
        _fail(db, task_id, exc.code, exc.message)
    except Exception as exc:
        logger.exception("unexpected pipeline failure for %s", task_id)
        _fail(db, task_id, "internal_error", f"生成流程失败：{exc}")
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


def _load_products(db: Database, input_data: dict[str, Any]) -> list[dict[str, Any]]:
    product_ids = list(input_data.get("product_asset_ids") or [])
    if not product_ids:
        legacy_id = input_data.get("product_asset_id")
        if legacy_id:
            product_ids = [legacy_id]
    if not product_ids:
        return []
    products = []
    for product_id in product_ids:
        product = db.assets.find_one({"_id": product_id})
        if product is None or product.get("kind") not in {"product", "logo"}:
            raise CoreAIError("product_missing", "参考素材不存在")
        products.append(product)
    return products


def _load_style_references(
    db: Database,
    store: ObjectStore,
    input_data: dict[str, Any],
) -> list[ImageRef]:
    references: list[ImageRef] = []
    for asset_id in list(input_data.get("style_reference_asset_ids") or [])[:3]:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") != "brand_reference":
            raise CoreAIError("style_reference_missing", "风格参考图不存在或类型不正确")
        references.append(image_ref_from_asset(db, store, str(asset_id)))
    return references


def _load_logo(db: Database, input_data: dict[str, Any]) -> dict[str, Any] | None:
    logo_asset_id = input_data.get("logo_asset_id")
    if not logo_asset_id:
        return None
    logo = db.assets.find_one({"_id": logo_asset_id})
    if logo is None:
        raise CoreAIError("logo_missing", "Logo 素材不存在")
    return logo


def _load_logo_for_layer(db: Database, layer: dict[str, Any] | None) -> dict[str, Any]:
    if layer is None:
        raise CoreAIError("logo_missing", "Logo 图层不存在")
    asset = db.assets.find_one({"_id": layer.get("asset_id")})
    if asset is None or asset.get("kind") != "logo":
        raise CoreAIError("logo_missing", "Logo 素材不存在")
    return asset


def _layout_items_from_design(design: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for layer in design_utils.text_layers(design):
        items.append({
            "id": layer.get("id"),
            "copy_id": layer.get("copy_id"),
            "role": layer.get("role"),
            "content": layer.get("content"),
            "source": (layer.get("provenance") or {}).get("copy_source") or "creative",
            "box": {**layer.get("position", {}), **layer.get("size", {})},
            "font_size": (layer.get("style") or {}).get("font_size"),
            "font_weight": (layer.get("style") or {}).get("font_weight"),
            "font_family": (layer.get("style") or {}).get("font_family"),
            "color": (layer.get("style") or {}).get("color"),
            "align": (layer.get("style") or {}).get("align"),
            "vertical_align": (layer.get("style") or {}).get("vertical_align"),
            "line_height": (layer.get("style") or {}).get("line_height"),
            "letter_spacing": (layer.get("style") or {}).get("letter_spacing"),
            "stroke": (layer.get("style") or {}).get("stroke"),
            "shadow": (layer.get("style") or {}).get("shadow"),
            "background": (layer.get("style") or {}).get("background"),
            "rotation": layer.get("rotation", 0),
            "z_index": layer.get("z_index", 100),
        })
    return items


def _relayout_design(
    design: dict[str, Any],
    *,
    settings: Settings,
    client: CoreAIClient,
    store: ObjectStore,
    db: Database,
    brand: brand_style.BrandStyle | None = None,
) -> dict[str, Any]:
    protected = [layer for layer in design_utils.text_layers(design) if design_utils.is_protected(layer)]
    if protected:
        raise editor.EditError("protected_layer", "存在人工保护的文字图层，不能自动重新排版")
    background_ref = image_ref_from_asset(
        db, store, str(design["background"]["render_asset_id"])
    )
    try:
        copy_items = design_utils.copy_items_from_design(design)
        fixed = [layer for layer in design.get("layers", []) if layer.get("type") in {"image", "shape"}]
        layout = designer.make_layout(
            settings,
            client,
            background_path=background_ref,
            canvas=design["canvas"],
            copy=design_utils.copy_from_design(design),
            has_logo=design_utils.logo_layer(design) is not None,
            brand=brand,
            design_intent=design.get("design_intent"),
            copy_items=copy_items,
            initial_scene_plan={"layers": _layout_items_from_design(design)},
            capabilities=list(design_utils.SUPPORTED_CAPABILITIES),
            fixed_layers=fixed,
        )
        text_layers, _ = designer.layout_to_layers(layout, canvas=design["canvas"], logo_asset_id=None, brand=brand)
        if not text_layers:
            raise editor.EditError("relayout_failed", "没有生成可用的新排版")
        layers = fixed + text_layers
        layers.sort(key=lambda item: int(item.get("z_index", 0)))
        return design_utils.make_scene(
            canvas=design["canvas"],
            clean_asset_id=design["background"]["clean_asset_id"],
            render_asset_id=design["background"]["render_asset_id"],
            background_prompt=design["background"].get("prompt") or "",
            logo_integrated=bool(design["background"].get("logo_integrated")),
            layers=layers,
            design_intent=design.get("design_intent"),
            scene_revision=int(design.get("scene_revision") or 1) + 1,
        )
    finally:
        background_ref.cleanup()


def _next_version(db: Database, task_id: str) -> int:
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise CoreAIError("task_missing", "任务不存在")
    latest = db.versions.find_one(
        {"task_id": task_id}, sort=[("version", -1)]
    )
    return max(
        int(task.get("current_version") or 0),
        int((latest or {}).get("version") or 0),
    ) + 1


def run_edit(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
    instruction: str,
) -> None:
    task = db.tasks.find_one_and_update(
        {"_id": task_id, "status": "succeeded", "edit.status": "running"},
        {"$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if task is None:
        return
    try:
        design = task.get("design")
        if not isinstance(design, dict):
            raise CoreAIError("design_missing", "当前任务没有可编辑的设计状态")
        design = design_utils.upgrade_design(design)
        _set_stage(db, task_id, "editing", progress=40)
        plan = editor.plan_edit(settings, client, instruction=instruction, design=design)
        brand = brand_style.brand_for_task(task, db, store)
        if any(action.get("type") == "relayout" for action in plan["actions"]):
            new_design = _relayout_design(
                design,
                settings=settings,
                client=client,
                store=store,
                db=db,
                brand=brand,
            )
            summaries = ["重新排版：DesignIntent 驱动"]
            changed_layers = [layer["id"] for layer in design["layers"]]
            background_changed = False
        else:
            applied = editor.apply_actions(design, plan["actions"])
            new_design = applied["design"]
            summaries = applied["summaries"]
            changed_layers = applied["changed_layers"]
            background_changed = applied["background_changed"]
        version = _next_version(db, task_id)
        if background_changed:
            _set_stage(db, task_id, "regenerating_background", progress=45)
            new_clean = _regenerate_background(db, settings, store, client, task_id=task_id, task=task, design=new_design, version=version)
            new_design["background"]["clean_asset_id"] = new_clean
            new_design["background"]["render_asset_id"] = new_clean
            new_design["background"]["logo_integrated"] = False
            design_utils.find_layer(new_design, "background")["asset_id"] = new_clean
            logo = design_utils.logo_layer(new_design)
            if logo is not None:
                logo["render_hint"] = "overlay"
                logo["mode"] = "overlay"
        summary = "；".join(summaries) or "更新设计"
        output_asset_id = _render_and_commit(db, settings, store, task_id=task_id, design=new_design, version=version, instruction=instruction, summary=summary)
        _append_message(db, task_id, role="assistant", content=plan.get("reply") or f"已完成：{summary}", version=version, actions=[{"type": action.get("type"), "layer_id": action.get("layer_id"), "summary": ACTION_LABELS.get(str(action.get("type")), str(action.get("type")))} for action in plan["actions"]])
        db.tasks.update_one({"_id": task_id}, {"$set": {"edit.status": "idle", "edit.error": None, "output_asset_id": output_asset_id, "updated_at": utcnow()}})
    except Exception as exc:
        message = exc.message if isinstance(exc, (editor.EditError, CoreAIError)) else "修改过程中出现异常，请稍后重试。"
        logger.exception("edit failed for %s", task_id)
        db.tasks.update_one({"_id": task_id}, {"$set": {"edit.status": "failed", "edit.error": message, "stage": "done", "progress": 100, "updated_at": utcnow()}})
        _append_message(db, task_id, role="assistant", content=f"这次修改没有执行：{message}", status="failed")


def _regenerate_background(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    task: dict[str, Any],
    design: dict[str, Any],
    version: int,
) -> str:
    products = _load_products(db, task["input"])
    brand = brand_style.brand_for_task(task, db, store)
    product_refs: list[ImageRef] = []
    try:
        product_refs = [
            image_ref_from_asset(db, store, str(item["_id"]))
            for item in products
        ]
        scene_prompt = str(design["background"].get("prompt") or "").strip()
        task_input = task.get("input") or {}
        merchant_info = task_input.get("merchant_info") if isinstance(task_input.get("merchant_info"), dict) else None
        platform_context = platform_presets.prompt_from_task(task.get("platform"), merchant_info, task.get("placement"))
        if platform_context:
            scene_prompt = f"{scene_prompt}\n\n{platform_context}"
        prompt = designer.build_background_prompt(
            scene_prompt,
            has_logo=design_utils.logo_layer(design) is not None,
            product_count=len(products),
            brand=brand,
            ratio=str((task.get("input") or {}).get("ratio") or ""),
        )
        canvas = design["canvas"]
        data = client.edit_image(
            prompt,
            product_refs,
            size=gateway_image_size(canvas["width"], canvas["height"]),
        )
        asset_id, _ = _save_image_asset(
            db,
            settings,
            store,
            task_id=task_id,
            name=f"{task_id}_bg_v{version}.png",
            data=data,
            kind="background",
        )
        return asset_id
    finally:
        cleanup_image_refs(product_refs)


def can_resume(db: Database, task_id: str) -> tuple[bool, str]:
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        return False, "任务不存在"
    if task.get("status") not in {"waiting", "interrupted"}:
        return False, "只有等待中或已中断的任务可以恢复"
    if task.get("design") or task.get("background_clean_asset_id"):
        return True, ""
    if task.get("background_asset_id"):
        return True, ""
    return True, ""


def run_layer_update(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
    layer_id: str,
    patch: dict[str, Any],
) -> None:
    try:
        task = db.tasks.find_one({"_id": task_id})
        if task is None or not isinstance(task.get("design"), dict):
            raise CoreAIError("design_missing", "当前任务没有可编辑的设计状态")
        design = design_utils.upgrade_design(task["design"])
        layer = design_utils.find_layer(design, layer_id)
        updated = design_utils.apply_layer_patch(layer, patch)
        updated = design_utils.mark_manual_edit(updated)
        if updated.get("role") == "logo" and any(key in patch for key in ("position", "size", "rotation", "asset_id")):
            if updated.get("render_hint") == "baked" or updated.get("mode") == "model_integrated":
                editor._detach_baked_logo(design)
                updated["render_hint"] = "overlay"
                updated["mode"] = "overlay"
        new_design = design_utils.replace_layer(design, updated)
        version = _next_version(db, task_id)
        _render_and_commit(db, settings, store, task_id=task_id, design=new_design, version=version, instruction=f"直接编辑图层 {layer_id}", summary=f"直接调整「{layer.get('name')}」")
        _append_message(db, task_id, role="assistant", content=f"已在画布上直接调整「{layer.get('name')}」，其他图层保持不变。", version=version, actions=[{"type": "update_layer", "layer_id": layer_id, "summary": "直接编辑图层属性"}])
        db.tasks.update_one({"_id": task_id}, {"$set": {"edit.status": "idle", "edit.error": None, "updated_at": utcnow()}})
    except Exception as exc:
        message = exc.message if isinstance(exc, (design_utils.DesignError, CoreAIError)) else "图层修改过程中出现异常，请稍后重试。"
        logger.exception("direct layer update failed for %s", task_id)
        db.tasks.update_one({"_id": task_id}, {"$set": {"edit.status": "failed", "edit.error": message, "stage": "done", "progress": 100, "updated_at": utcnow()}})


def run_component_regeneration(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
    component_id: str,
) -> None:
    reference_path: ImageRef | None = None
    style_reference_path: ImageRef | None = None
    try:
        task = db.tasks.find_one({"_id": task_id})
        if task is None or not isinstance(task.get("design"), dict):
            raise CoreAIError("design_missing", "当前任务没有可编辑的设计状态")
        design = design_utils.upgrade_design(task["design"])
        component = design_utils.find_layer(design, component_id)
        if component.get("type") != "visual_component":
            raise CoreAIError("not_visual_component", "目标图层不是可重生成的视觉组件")
        kind = str(((component.get("provenance") or {}).get("regeneration") or {}).get("kind") or "")
        if not kind:
            kind = {"headline": "title", "promotion_price": "price", "offer_badge": "offer"}.get(
                str(component.get("semantic_type") or ""), ""
            )
        if not kind:
            raise CoreAIError("unsupported_component", "该组件暂不支持视觉重生成")
        reference_asset_id = (
            design.get("design_reference_asset_id")
            or task.get("design_reference_asset_id")
            or (design.get("background") or {}).get("render_asset_id")
        )
        reference_asset = db.assets.find_one({"_id": reference_asset_id})
        if reference_asset is None:
            raise CoreAIError("reference_missing", "全局设计参考不存在，无法保持组件风格一致")
        reference_path = image_ref_from_asset(db, store, str(reference_asset_id))
        if kind != "title":
            title_component = next(
                (layer for layer in design.get("layers") or [] if layer.get("id") == "component_headline"),
                None,
            )
            title_art = next(
                (
                    child for child in ((title_component or {}).get("children") or [])
                    if isinstance(child, dict) and (child.get("provenance") or {}).get("component_art")
                ),
                None,
            )
            if title_art is not None:
                title_asset = db.assets.find_one({"_id": title_art.get("asset_id")})
                if title_asset is not None:
                    style_reference_path = image_ref_from_asset(
                        db, store, str(title_asset["_id"])
                    )
        brand = brand_style.brand_for_task(task, db, store)
        variation = int(datetime.now(timezone.utc).timestamp())
        _set_stage(db, task_id, "regenerating_component", progress=45)
        canvas = design["canvas"]
        component_box = {
            "x": int((component.get("position") or {}).get("x", 0)),
            "y": int((component.get("position") or {}).get("y", 0)),
            "width": int((component.get("size") or {}).get("width", 100)),
            "height": int((component.get("size") or {}).get("height", 100)),
        }
        context_image = _render_context_preview(
            settings,
            store,
            db,
            canvas=canvas,
            clean_asset_id=design["background"]["clean_asset_id"],
            layers=design.get("layers") or [],
        )
        context_paths = reference_guided.make_context_images(
            context_image,
            target_box=component_box,
            canvas=canvas,
        )
        try:
            art = reference_guided.generate_component_art(
                db,
                settings,
                store,
                client,
                task_id=task_id,
                kind=kind,
                brand=brand,
                intent=design_utils.normalize_design_intent(design.get("design_intent")),
                reference_path=style_reference_path or reference_path,
                text_hint=str((component.get("content") or {}).get("text") or ""),
                context_paths=context_paths,
                target=reference_guided.describe_target(component_box, canvas),
                variation=variation,
            )
        finally:
            for path in context_paths:
                path.unlink(missing_ok=True)
        if art is None:
            raise CoreAIError("component_generation_failed", "组件重生成失败，已保留原组件")
        art_children = [
            child for child in component.get("children") or []
            if isinstance(child, dict) and (child.get("provenance") or {}).get("component_art")
        ]
        if not art_children:
            raise CoreAIError("component_asset_missing", "组件缺少可替换的视觉素材")
        art_children[0]["asset_id"] = art.asset_id
        art_children[0]["fit"] = "cover" if kind == "price" else "contain"
        art_children[0]["provenance"] = {
            **(art_children[0].get("provenance") or {}),
            **art.summary(),
            "component_art": True,
        }
        component["provenance"] = {
            **(component.get("provenance") or {}),
            "generated_art_asset_id": art.asset_id,
            "regeneration": {
                "kind": kind,
                "variation": variation,
                "regenerated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        component["edit_policy"] = {
            **(component.get("edit_policy") or {}),
            "manual_edited": True,
        }
        new_design = design_utils.replace_layer(design, component)
        version = _next_version(db, task_id)
        _render_and_commit(
            db,
            settings,
            store,
            task_id=task_id,
            design=new_design,
            version=version,
            instruction=f"重生成组件 {component_id}",
            summary=f"重生成「{component.get('name')}」视觉素材",
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content=f"已单独重生成「{component.get('name')}」的视觉素材，文字内容和其他图层保持不变。",
            version=version,
            actions=[{"type": "regenerate_component", "layer_id": component_id, "summary": "组件级视觉重生成"}],
        )
        db.tasks.update_one(
            {"_id": task_id},
            {"$set": {"edit.status": "idle", "edit.error": None, "stage": "done", "progress": 100, "updated_at": utcnow()}},
        )
    except Exception as exc:
        message = exc.message if isinstance(exc, (design_utils.DesignError, CoreAIError)) else "组件重生成过程中出现异常，请稍后重试。"
        logger.exception("component regeneration failed for %s", task_id)
        db.tasks.update_one(
            {"_id": task_id},
            {"$set": {"edit.status": "failed", "edit.error": message, "stage": "done", "progress": 100, "updated_at": utcnow()}},
        )
    finally:
        if reference_path is not None:
            reference_path.cleanup()
        if style_reference_path is not None:
            style_reference_path.cleanup()


def restore_version(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    task_id: str,
    version: int,
) -> int:
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise CoreAIError("task_missing", "任务不存在")
    snapshot = db.versions.find_one({"task_id": task_id, "version": version})
    if snapshot is None:
        raise CoreAIError("version_missing", "历史版本不存在")
    design = copy.deepcopy(snapshot["design"])
    design_utils.validate_design(design)
    new_version = _next_version(db, task_id)
    _render_and_commit(
        db,
        settings,
        store,
        task_id=task_id,
        design=design,
        version=new_version,
        instruction=f"恢复到 v{version}",
        summary=f"恢复到 v{version}",
    )
    _append_message(
        db,
        task_id,
        role="assistant",
        content=f"已恢复到 v{version}，并保存为 v{new_version}。",
        version=new_version,
    )
    return new_version

def _load_scene_assets(
    db: Database,
    store: ObjectStore,
    design: dict[str, Any],
) -> dict[str, Image.Image]:
    assets: dict[str, Image.Image] = {}

    def load_asset(asset_id: str) -> None:
        if not asset_id or asset_id in assets:
            return
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise CoreAIError("asset_missing", f"图层素材不存在：{asset_id}")
        assets[asset_id] = storage.open_image(
            store, asset.get("prepared_path") or asset["path"]
        )

    def walk(layer: dict[str, Any]) -> None:
        layer_type = layer.get("type")
        if layer_type in {"image", "logo"}:
            load_asset(str(layer.get("asset_id") or ""))
        if layer_type == "visual_component":
            load_asset(str(layer.get("asset_id") or ""))
            for child in layer.get("children") or []:
                if isinstance(child, dict):
                    walk(child)

    for layer in design.get("layers", []):
        if isinstance(layer, dict):
            walk(layer)
    return assets


def render_candidate(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    design: dict[str, Any],
    candidate_id: str,
    parent_candidate_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    design = design_utils.upgrade_design(design)
    design_utils.validate_design(design)
    task_doc = db.tasks.find_one({"_id": task_id}) or {}
    task_input = task_doc.get("input") or {}
    design = quality.clamp_design_to_safe_area(design, task_doc.get("placement"))
    canvas = design["canvas"]
    background = storage.open_image(
        store, _background_key(db, design["background"]["render_asset_id"])
    )
    assets = _load_scene_assets(db, store, design)
    result = render_design(
        settings,
        background,
        design_utils.sorted_layers(design),
        (int(canvas["width"]), int(canvas["height"])),
        assets=assets,
    )
    quality_report = quality.assess_scene(
        design,
        result.image,
        result.design.get("layers") if isinstance(result.design, dict) else None,
        design_utils.copy_items_from_design(design),
        platform=task_doc.get("platform"),
        placement=task_doc.get("placement"),
        merchant_info=task_input.get("merchant_info"),
    )
    platform_failures = [
        item for item in quality_report.get("hard_failures") or []
        if str(item.get("code") or "").startswith("platform_") or item.get("code") == "safe_area"
    ]
    if platform_failures:
        raise CoreAIError(
            "platform_violation",
            "；".join(str(item.get("message") or "平台规则检查失败") for item in platform_failures[:3]),
        )
    output_asset_id = _save_output_asset(
        db,
        settings,
        store,
        task_id=task_id,
        version=0,
        image=result.image,
        name_suffix=f"_{candidate_id}",
    )
    doc = {
        "_id": f"candidate_{uuid.uuid4().hex}",
        "task_id": task_id,
        "run_id": run_id or f"run_{uuid.uuid4().hex}",
        "candidate_id": candidate_id,
        "parent_candidate_id": parent_candidate_id,
        "design": copy.deepcopy(design),
        "output_asset_id": output_asset_id,
        "quality": quality_report,
        "render_metrics": result.design if isinstance(result.design, dict) else {},
        "review": None,
        "actions": [],
        "selected": False,
        "created_at": utcnow(),
    }
    db.candidates.insert_one(doc)
    return {
        **doc,
        "image": result.image,
        "output_asset_id": output_asset_id,
        "quality": quality_report,
        "render_metrics": result.design if isinstance(result.design, dict) else {},
    }


def save_candidate_review(
    db: Database,
    candidate: dict[str, Any],
    review: dict[str, Any],
) -> None:
    db.candidates.update_one(
        {"_id": candidate["_id"]},
        {"$set": {"review": review, "actions": review.get("actions") or [], "selected": False}},
    )


def commit_candidate(
    db: Database,
    *,
    task_id: str,
    candidate: dict[str, Any],
    version: int,
    instruction: str,
    summary: str,
    review: dict[str, Any] | None = None,
) -> str:
    design = design_utils.upgrade_design(candidate["design"], scene_revision=version)
    design["scene_revision"] = int(version)
    output_asset_id = candidate["output_asset_id"]
    db.versions.insert_one({
        "_id": f"ver_{uuid.uuid4().hex}",
        "task_id": task_id,
        "version": version,
        "scene_revision": int(version),
        "design": copy.deepcopy(design),
        "output_asset_id": output_asset_id,
        "instruction": instruction,
        "summary": summary,
        "quality": candidate.get("quality") or {"warnings": []},
        "render_metrics": candidate.get("render_metrics") or {},
        "review": review or candidate.get("review"),
        "created_at": utcnow(),
    })
    db.candidates.update_one(
        {"_id": candidate["_id"]},
        {"$set": {"selected": True, "version": version}},
    )
    db.tasks.update_one(
        {"_id": task_id},
        {"$set": {
            "design": copy.deepcopy(design),
            "background_asset_id": design["background"]["render_asset_id"],
            "background_clean_asset_id": design["background"]["clean_asset_id"],
            "output_asset_id": output_asset_id,
            "current_version": version,
            "scene_revision": int(version),
            "status": "succeeded",
            "stage": "done",
            "progress": 100,
            "error": None,
            "edit": {"status": "idle", "instruction": None, "error": None},
            "quality": candidate.get("quality") or {"warnings": []},
            "render_metrics": candidate.get("render_metrics") or {},
            "review": review or candidate.get("review") or {"status": "not_run", "issues": [], "candidates": []},
            "updated_at": utcnow(),
        }},
    )
    return output_asset_id


def _review_summary(
    initial: dict[str, Any],
    corrected: dict[str, Any] | None,
    selected: dict[str, Any],
    review1: dict[str, Any] | None,
    review2: dict[str, Any] | None,
) -> dict[str, Any]:
    if review1 is None:
        status = "not_run"
    elif corrected is None:
        status = "passed" if not (review1.get("issues") or []) else "issues_kept"
    elif selected["candidate_id"] == corrected["candidate_id"]:
        status = "corrected"
    else:
        status = "kept_initial"
    candidates = [
        {
            "candidate_id": initial["candidate_id"],
            "selected": selected["candidate_id"] == initial["candidate_id"],
            "issues": len((review1 or {}).get("issues") or []),
            "hard_failures": len((initial.get("quality") or {}).get("hard_failures") or []),
        }
    ]
    if corrected is not None:
        candidates.append({
            "candidate_id": corrected["candidate_id"],
            "selected": selected["candidate_id"] == corrected["candidate_id"],
            "issues": len((review2 or {}).get("issues") or []),
            "hard_failures": len((corrected.get("quality") or {}).get("hard_failures") or []),
        })
    return {
        "status": status,
        "issues": (review2 or review1 or {}).get("issues") or [],
        "reason": (review2 or review1 or {}).get("comparison", {}).get("reason") if isinstance((review2 or review1 or {}).get("comparison"), dict) else "",
        "candidates": candidates,
    }


def run_visual_review(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    design: dict[str, Any],
    initial: dict[str, Any],
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    review1 = None
    corrected = None
    review2 = None
    if not settings.visual_critic_enabled:
        return initial, None, None, None
    _set_stage(db, task_id, "reviewing", progress=97)
    try:
        review1 = visual_critic.review_candidate(
            settings,
            client,
            candidate_id=initial["candidate_id"],
            image=initial["image"],
            design=initial["design"],
            quality_report=initial.get("quality") or {},
        )
        save_candidate_review(db, initial, review1)
    except Exception as exc:
        logger.warning("visual critic unavailable for %s: %s", task_id, exc)
        return initial, None, None, {"status": "fallback", "issues": [], "candidates": [], "reason": str(exc)[:300]}
    if not review1.get("actions"):
        return initial, None, review1, None
    try:
        _set_stage(db, task_id, "correcting", progress=98)
        corrected_design = visual_critic.apply_actions(design, review1["actions"])
        corrected = render_candidate(
            db,
            settings,
            store,
            task_id=task_id,
            design=corrected_design,
            candidate_id=f"{initial['candidate_id']}_r1",
            parent_candidate_id=initial["candidate_id"],
            run_id=run_id,
        )
        review2 = visual_critic.review_candidate(
            settings,
            client,
            candidate_id=corrected["candidate_id"],
            image=corrected["image"],
            design=corrected["design"],
            quality_report=corrected.get("quality") or {},
            comparison_image=initial["image"],
            comparison_candidate_id=initial["candidate_id"],
        )
        save_candidate_review(db, corrected, review2)
        preferred = str(review2.get("comparison", {}).get("preferred_candidate_id") or "")
        hard = (corrected.get("quality") or {}).get("hard_failures") or []
        if preferred == corrected["candidate_id"] and not hard:
            return corrected, corrected, review1, review2
        return initial, corrected, review1, review2
    except Exception as exc:
        logger.warning("visual correction failed for %s: %s", task_id, exc)
        return initial, corrected, review1, None


def _render_and_commit(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    design: dict[str, Any],
    version: int,
    instruction: str,
    summary: str,
) -> str:
    candidate = render_candidate(
        db,
        settings,
        store,
        task_id=task_id,
        design=design,
        candidate_id=f"candidate_v{version}",
    )
    return commit_candidate(
        db,
        task_id=task_id,
        candidate=candidate,
        version=version,
        instruction=instruction,
        summary=summary,
    )
