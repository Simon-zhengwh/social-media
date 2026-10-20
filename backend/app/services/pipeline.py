from __future__ import annotations

import copy
import hashlib
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
from app.services import brand_style, designer, editor, quality, storage
from app.services.canvas import resolution_for
from app.services.object_store import ObjectStore
from app.services.renderer import render_design
from app.services.toapis import ToAPIsClient, ToAPIsError

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
    "replace_logo_asset": "替换 Logo",
    "relayout": "重新排版",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ratio_for_canvas(canvas: dict[str, int]) -> str:
    width, height = int(canvas["width"]), int(canvas["height"])
    ratio = width / height
    candidates = {"1:1": 1.0, "9:16": 9 / 16, "4:5": 4 / 5, "16:9": 16 / 9}
    return min(candidates, key=lambda key: abs(candidates[key] - ratio))


def build_prompt(
    user_prompt: str,
    has_logo: bool = True,
    product_count: int = 1,
    ratio: str = "1:1",
    brand: brand_style.BrandStyle | None = None,
) -> str:
    """Legacy background prompt builder kept for callers and compatibility tests."""
    return (
        designer.build_background_prompt(
            user_prompt,
            has_logo=has_logo,
            product_count=product_count,
            brand=brand,
        )
        + f"画面比例为 {ratio}，适合社交媒体海报。"
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
                    "error": "本地服务重启，任务已中断。可点击继续查询恢复。",
                },
                "error": {
                    "code": "interrupted",
                    "message": "本地服务重启，任务已中断。可点击继续查询恢复。",
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
        raise ToAPIsError("invalid_background", "供应商返回的图片无法解码") from exc
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
) -> str:
    data = storage.encode_png(image)
    name = f"{task_id}_v{version}.png"
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
        raise ToAPIsError("background_missing", "底图素材不存在")
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
    client: ToAPIsClient,
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
        logo_path = store.download_to_temp(
            logo_asset.get("prepared_path") or logo_asset["path"], suffix=".png"
        )
        temp_paths.append(logo_path)
        background_url = client.upload_reference(crop_path)
        logo_url = client.upload_reference(logo_path)
        ratio = _ratio_for_canvas({"width": crop.width, "height": crop.height})
        result = client.submit_generation(
            _logo_integration_prompt(
                (crop.width, crop.height), logo_layer, (crop_left, crop_top)
            ),
            [background_url, logo_url],
            ratio=ratio,
            resolution=resolution_for(ratio),
        )
        result_url = client.wait_for_generation(result["id"])
        data = client.download_result(result_url)
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
            erase_pad = max(10, int(max(size["width"], size["height"]) * 0.14))
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
            with Image.open(logo_path) as exact_source:
                exact_logo = exact_source.convert("RGBA")
                exact_logo.load()
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
    canvas = design["canvas"]
    background = storage.open_image(
        store, _background_key(db, design["background"]["render_asset_id"])
    )
    assets: dict[str, Image.Image] = {}
    logo = design_utils.logo_layer(design)
    if logo is not None and logo.get("mode") != "model_integrated":
        asset = db.assets.find_one({"_id": logo["asset_id"]})
        if asset is None:
            raise ToAPIsError("logo_missing", "Logo 素材不存在")
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
    quality_report = quality.assess_legibility(
        background,
        design_utils.sorted_layers(design),
        result.design.get("layers") if isinstance(result.design, dict) else None,
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


def run_task(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: ToAPIsClient,
    task_id: str,
) -> None:
    task = db.tasks.find_one_and_update(
        {"_id": task_id, "status": {"$in": ["queued", "waiting", "interrupted"]}},
        {"$set": {"status": "running", "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if task is None:
        return

    try:
        input_data = task["input"]
        products = _load_products(db, input_data)
        logo = _load_logo(db, input_data)
        canvas = {
            "width": int((task.get("canvas") or {}).get("width", settings.canvas_width)),
            "height": int((task.get("canvas") or {}).get("height", settings.canvas_height)),
        }
        ratio = input_data.get("ratio") or _ratio_for_canvas(canvas)
        brand = brand_style.brand_for_task(task, settings.skills_path)

        plan = task.get("creative_plan")
        if not isinstance(plan, dict):
            _set_stage(db, task_id, "planning", progress=4)
            temp_paths: list[Path] = []
            try:
                for product in products:
                    temp_paths.append(
                        store.download_to_temp(
                            product.get("prepared_path") or product["path"],
                            suffix=".png",
                        )
                    )
                plan = designer.make_creative_plan(
                    settings,
                    client,
                    brief=str(input_data.get("prompt") or ""),
                    product_paths=temp_paths,
                    has_logo=logo is not None,
                    brand_hint=brand.name,
                    brand=brand,
                )
            finally:
                for path in temp_paths:
                    path.unlink(missing_ok=True)
            db.tasks.update_one(
                {"_id": task_id},
                {"$set": {"creative_plan": plan, "updated_at": utcnow()}},
            )

        clean_asset_id = task.get("background_clean_asset_id")
        provider = task.setdefault("provider", {})
        if not clean_asset_id:
            provider_task_id = provider.get("task_id")
            if not provider_task_id:
                _set_stage(db, task_id, "uploading", progress=8)
                reference_urls: list[str] = []
                temp_paths = []
                try:
                    for product in products:
                        product_path = store.download_to_temp(
                            product.get("prepared_path") or product["path"],
                            suffix=".png",
                        )
                        temp_paths.append(product_path)
                        reference_urls.append(client.upload_reference(product_path))
                except Exception as exc:
                    logger.exception("reference upload failed for %s", task_id)
                    raise ToAPIsError("upload_failed", "参考图上传失败") from exc
                finally:
                    for temp_path in temp_paths:
                        temp_path.unlink(missing_ok=True)

                db.tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "provider.upload_urls": reference_urls,
                            "updated_at": utcnow(),
                        }
                    },
                )
                _set_stage(db, task_id, "submitting", progress=12)
                scene_prompt = str(plan.get("background_prompt") or "").strip()
                if len(scene_prompt) < 4:
                    scene_prompt = str(input_data.get("prompt") or "").strip()
                prompt = designer.build_background_prompt(
                    scene_prompt,
                    has_logo=logo is not None,
                    product_count=len(products),
                    brand=brand,
                )
                try:
                    submitted = client.submit_generation(
                        prompt,
                        reference_urls,
                        ratio=ratio,
                        resolution=resolution_for(ratio),
                    )
                except Exception as exc:
                    logger.exception("generation submit failed for %s", task_id)
                    if isinstance(exc, ToAPIsError):
                        raise
                    db.tasks.update_one(
                        {"_id": task_id},
                        {
                            "$set": {
                                "status": "waiting",
                                "stage": "submitting",
                                "error": {
                                    "code": "submit_unknown",
                                    "message": "提交结果未知，请先核对供应商任务，不要重复提交。",
                                },
                                "updated_at": utcnow(),
                            }
                        },
                    )
                    return
                provider_task_id = submitted["id"]
                db.tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "provider.task_id": provider_task_id,
                            "provider.status": submitted.get("status"),
                            "provider.progress": int(submitted.get("progress") or 0),
                            "updated_at": utcnow(),
                        }
                    },
                )
                _set_stage(db, task_id, "generating", progress=20)
            else:
                _set_stage(db, task_id, "generating", progress=35)

            def on_update(payload: dict[str, Any]) -> None:
                db.tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "provider.status": payload.get("status"),
                            "provider.progress": int(payload.get("progress") or 0),
                            "progress": min(82, 25 + int(payload.get("progress") or 0) // 2),
                            "updated_at": utcnow(),
                        }
                    },
                )

            try:
                result_url = client.wait_for_generation(
                    provider_task_id, on_update=on_update
                )
            except ToAPIsError as exc:
                if exc.code == "generation_timeout":
                    db.tasks.update_one(
                        {"_id": task_id},
                        {
                            "$set": {
                                "status": "waiting",
                                "stage": "generating",
                                "error": {"code": exc.code, "message": exc.message},
                                "updated_at": utcnow(),
                            }
                        },
                    )
                    return
                raise

            _set_stage(db, task_id, "downloading", progress=84)
            image_bytes = client.download_result(result_url)
            clean_asset_id, _ = _save_image_asset(
                db,
                settings,
                store,
                task_id=task_id,
                name=f"{task_id}_clean.png",
                data=image_bytes,
                kind="background",
            )
            db.tasks.update_one(
                {"_id": task_id},
                {
                    "$set": {
                        "background_clean_asset_id": clean_asset_id,
                        "background_asset_id": clean_asset_id,
                        "updated_at": utcnow(),
                    }
                },
            )

        design = task.get("design")
        if not isinstance(design, dict):
            _set_stage(db, task_id, "layout", progress=88)
            background_path = store.download_to_temp(
                _background_key(db, clean_asset_id), suffix=".png"
            )
            try:
                layout = designer.make_layout(
                    settings,
                    client,
                    background_path=background_path,
                    canvas=canvas,
                    copy=dict(plan.get("copy") or {}),
                    has_logo=logo is not None,
                    brand=brand,
                )
            finally:
                background_path.unlink(missing_ok=True)
            if logo is not None and layout.get("logo_box"):
                logo_width = int(logo.get("width") or 1)
                logo_height = int(logo.get("height") or 1)
                designer.fit_logo_box(
                    layout,
                    canvas,
                    logo_aspect=logo_width / max(1, logo_height),
                )
                designer._resolve_logo_overlap(layout, canvas)
            text_layers, logo_layer = designer.layout_to_layers(
                layout,
                canvas=canvas,
                logo_asset_id=logo["_id"] if logo else None,
            )
            if not text_layers:
                text_layers, logo_layer = designer.layout_to_layers(
                    designer.fallback_layout(
                        canvas,
                        dict(plan.get("copy") or {}),
                        has_logo=logo is not None,
                        brand=brand,
                    ),
                    canvas=canvas,
                    logo_asset_id=logo["_id"] if logo else None,
                )
            render_asset_id = clean_asset_id
            logo_integrated = False
            if logo is not None and logo_layer is not None:
                _set_stage(db, task_id, "integrating_logo", progress=92)
                integrated = _integrate_logo(
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    version=1,
                    background_asset_id=clean_asset_id,
                    logo_asset=logo,
                    logo_layer=logo_layer,
                    canvas=canvas,
                )
                if integrated:
                    render_asset_id = integrated
                    logo_integrated = True
                else:
                    logo_layer["mode"] = "overlay"
            design = design_utils.make_design(
                canvas=canvas,
                clean_asset_id=clean_asset_id,
                render_asset_id=render_asset_id,
                background_prompt=str(plan.get("background_prompt") or ""),
                logo_integrated=logo_integrated,
                text_layers=text_layers,
                logo_layer=logo_layer,
            )
            db.tasks.update_one(
                {"_id": task_id},
                {"$set": {"design": design, "layout_source": layout.get("source")}},
            )

        _render_and_commit(
            db,
            settings,
            store,
            task_id=task_id,
            design=design,
            version=int(task.get("current_version") or 1),
            instruction=str(input_data.get("prompt") or ""),
            summary="首次生成：底图、文字层与品牌层已分开保存",
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content="海报已生成。文字和 Logo 已是独立图层，之后改文案不会重新生成底图。",
            version=int(task.get("current_version") or 1),
        )
    except ToAPIsError as exc:
        logger.warning("pipeline failed for %s: %s", task_id, exc.message)
        _fail(db, task_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - keep the task state honest
        logger.exception("unexpected pipeline failure for %s", task_id)
        _fail(db, task_id, "internal_error", f"生成流程失败：{exc}")


def _load_products(db: Database, input_data: dict[str, Any]) -> list[dict[str, Any]]:
    product_ids = list(input_data.get("product_asset_ids") or [])
    if not product_ids:
        legacy_id = input_data.get("product_asset_id")
        if legacy_id:
            product_ids = [legacy_id]
    if not product_ids:
        raise ToAPIsError("product_missing", "商品素材不存在")
    products = []
    for product_id in product_ids:
        product = db.assets.find_one({"_id": product_id})
        if product is None or product.get("kind") != "product":
            raise ToAPIsError("product_missing", "商品素材不存在")
        products.append(product)
    return products


def _load_logo(db: Database, input_data: dict[str, Any]) -> dict[str, Any] | None:
    logo_asset_id = input_data.get("logo_asset_id")
    if not logo_asset_id:
        return None
    logo = db.assets.find_one({"_id": logo_asset_id})
    if logo is None:
        raise ToAPIsError("logo_missing", "Logo 素材不存在")
    return logo


def _load_logo_for_layer(db: Database, layer: dict[str, Any] | None) -> dict[str, Any]:
    if layer is None:
        raise ToAPIsError("logo_missing", "Logo 图层不存在")
    asset = db.assets.find_one({"_id": layer.get("asset_id")})
    if asset is None or asset.get("kind") != "logo":
        raise ToAPIsError("logo_missing", "Logo 素材不存在")
    return asset


def _relayout_design(
    design: dict[str, Any],
    *,
    has_logo: bool,
    brand: brand_style.BrandStyle | None = None,
) -> dict[str, Any]:
    canvas = design["canvas"]
    copy_fields = design_utils.copy_from_design(design)
    layout = designer.promo_layout(
        canvas, copy_fields, has_logo=has_logo, brand=brand
    )
    text_layers, _ = designer.layout_to_layers(
        layout,
        canvas=canvas,
        logo_asset_id=None,
    )
    logo = design_utils.logo_layer(design)
    logo_layer = copy.deepcopy(logo) if logo is not None else None
    background = design["background"]
    return design_utils.make_design(
        canvas=canvas,
        clean_asset_id=background["clean_asset_id"],
        render_asset_id=background["render_asset_id"],
        background_prompt=background.get("prompt") or "",
        logo_integrated=bool(background.get("logo_integrated")),
        text_layers=text_layers,
        logo_layer=logo_layer,
    )


def _next_version(db: Database, task_id: str) -> int:
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise ToAPIsError("task_missing", "任务不存在")
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
    client: ToAPIsClient,
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
            raise ToAPIsError("design_missing", "当前任务没有可编辑的设计状态")
        _set_stage(db, task_id, "editing", progress=40)
        plan = editor.plan_edit(
            settings, client, instruction=instruction, design=design
        )
        brand = brand_style.brand_for_task(task, settings.skills_path)
        if any(action.get("type") == "relayout" for action in plan["actions"]):
            applied = {
                "design": _relayout_design(
                    design,
                    has_logo=design_utils.logo_layer(design) is not None,
                    brand=brand,
                ),
                "changed_layers": [layer["id"] for layer in design["layers"]],
                "summaries": ["重新排版：广告层级"],
                "background_changed": False,
                "logo_reintegrate": False,
            }
        else:
            applied = editor.apply_actions(design, plan["actions"])
        new_design = applied["design"]
        version = _next_version(db, task_id)

        if applied["background_changed"]:
            _set_stage(db, task_id, "regenerating_background", progress=45)
            new_clean = _regenerate_background(
                db,
                settings,
                store,
                client,
                task_id=task_id,
                task=task,
                design=new_design,
                version=version,
            )
            new_design["background"]["clean_asset_id"] = new_clean
            new_design["background"]["render_asset_id"] = new_clean
            new_design["background"]["logo_integrated"] = False
            background_layer = design_utils.find_layer(new_design, "background")
            background_layer["asset_id"] = new_clean
            logo = design_utils.logo_layer(new_design)
            if logo is not None:
                logo["mode"] = "model_integrated"
                integrated = _integrate_logo(
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    version=version,
                    background_asset_id=new_clean,
                    logo_asset=_load_logo_for_layer(db, logo),
                    logo_layer=logo,
                    canvas=new_design["canvas"],
                )
                if integrated:
                    new_design["background"]["render_asset_id"] = integrated
                    new_design["background"]["logo_integrated"] = True
                    background_layer["asset_id"] = integrated
                else:
                    logo["mode"] = "overlay"
        elif applied["logo_reintegrate"]:
            _set_stage(db, task_id, "integrating_logo", progress=60)
            logo = design_utils.logo_layer(new_design)
            if logo is not None:
                logo_asset = _load_logo_for_layer(db, logo)
                logo["mode"] = "model_integrated"
                integrated = _integrate_logo(
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    version=version,
                    background_asset_id=new_design["background"]["clean_asset_id"],
                    logo_asset=logo_asset,
                    logo_layer=logo,
                    canvas=new_design["canvas"],
                )
                background_layer = design_utils.find_layer(new_design, "background")
                if integrated:
                    new_design["background"]["render_asset_id"] = integrated
                    new_design["background"]["logo_integrated"] = True
                    background_layer["asset_id"] = integrated
                else:
                    logo["mode"] = "overlay"
                    clean = new_design["background"]["clean_asset_id"]
                    new_design["background"]["render_asset_id"] = clean
                    new_design["background"]["logo_integrated"] = False
                    background_layer["asset_id"] = clean

        summary = "；".join(applied["summaries"]) or "更新设计"
        output_asset_id = _render_and_commit(
            db,
            settings,
            store,
            task_id=task_id,
            design=new_design,
            version=version,
            instruction=instruction,
            summary=summary,
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content=plan.get("reply") or f"已完成：{summary}",
            version=version,
            actions=[
                {
                    "type": action.get("type"),
                    "layer_id": action.get("layer_id"),
                    "summary": ACTION_LABELS.get(
                        str(action.get("type")), str(action.get("type"))
                    ),
                }
                for action in plan["actions"]
            ],
        )
        db.tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "edit.status": "idle",
                    "edit.error": None,
                    "output_asset_id": output_asset_id,
                    "updated_at": utcnow(),
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 - editing must not destroy the last good render
        message = (
            exc.message
            if isinstance(exc, (editor.EditError, ToAPIsError))
            else "修改过程中出现异常，请稍后重试。"
        )
        logger.exception("edit failed for %s", task_id)
        db.tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "edit.status": "failed",
                    "edit.error": message,
                    "stage": "done",
                    "progress": 100,
                    "updated_at": utcnow(),
                }
            },
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content=f"这次修改没有执行：{message}",
            status="failed",
        )


def _regenerate_background(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: ToAPIsClient,
    *,
    task_id: str,
    task: dict[str, Any],
    design: dict[str, Any],
    version: int,
) -> str:
    products = _load_products(db, task["input"])
    brand = brand_style.brand_for_task(task, settings.skills_path)
    temp_paths: list[Path] = []
    try:
        reference_urls: list[str] = []
        for product in products:
            path = store.download_to_temp(
                product.get("prepared_path") or product["path"], suffix=".png"
            )
            temp_paths.append(path)
            reference_urls.append(client.upload_reference(path))
        scene_prompt = str(design["background"].get("prompt") or "").strip()
        prompt = designer.build_background_prompt(
            scene_prompt,
            has_logo=design_utils.logo_layer(design) is not None,
            product_count=len(products),
            brand=brand,
        )
        canvas = design["canvas"]
        ratio = _ratio_for_canvas(canvas)
        submitted = client.submit_generation(
            prompt,
            reference_urls,
            ratio=ratio,
            resolution=resolution_for(ratio),
        )
        result_url = client.wait_for_generation(submitted["id"])
        data = client.download_result(result_url)
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
        for path in temp_paths:
            path.unlink(missing_ok=True)


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
    provider_task_id = (task.get("provider") or {}).get("task_id")
    if provider_task_id:
        return True, ""
    return False, "未找到供应商任务 ID，请重新创建任务"


def run_layer_update(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: ToAPIsClient,
    task_id: str,
    layer_id: str,
    patch: dict[str, Any],
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
            raise ToAPIsError("design_missing", "当前任务没有可编辑的设计状态")
        layer = design_utils.find_layer(design, layer_id)
        updated_layer = design_utils.apply_layer_patch(layer, patch)
        new_design = design_utils.replace_layer(design, updated_layer)
        version = _next_version(db, task_id)
        if layer.get("type") == "logo" and "visible" in patch:
            if not patch["visible"]:
                if (new_design.get("background") or {}).get("logo_integrated"):
                    editor._detach_baked_logo(new_design)
                updated_layer["mode"] = "overlay"
                new_design = design_utils.replace_layer(new_design, updated_layer)
            else:
                # A hidden baked logo has already been detached. Showing it
                # again uses the transparent overlay without another model call.
                if not (new_design.get("background") or {}).get("logo_integrated"):
                    updated_layer["mode"] = "overlay"
                    new_design = design_utils.replace_layer(
                        new_design, updated_layer
                    )
        logo_reintegrate = bool(
            layer.get("type") == "logo"
            and any(key in patch for key in ("position", "size", "asset_id"))
            and updated_layer.get("mode") != "overlay"
        )
        if logo_reintegrate:
            _set_stage(db, task_id, "integrating_logo", progress=70)
            logo_asset = _load_logo_for_layer(db, updated_layer)
            updated_layer["mode"] = "model_integrated"
            integrated = _integrate_logo(
                db,
                settings,
                store,
                client,
                task_id=task_id,
                version=version,
                background_asset_id=new_design["background"]["clean_asset_id"],
                logo_asset=logo_asset,
                logo_layer=updated_layer,
                canvas=new_design["canvas"],
            )
            background_layer = design_utils.find_layer(new_design, "background")
            if integrated:
                new_design["background"]["render_asset_id"] = integrated
                new_design["background"]["logo_integrated"] = True
                background_layer["asset_id"] = integrated
            else:
                updated_layer["mode"] = "overlay"
                clean = new_design["background"]["clean_asset_id"]
                new_design["background"]["render_asset_id"] = clean
                new_design["background"]["logo_integrated"] = False
                background_layer["asset_id"] = clean

        _render_and_commit(
            db,
            settings,
            store,
            task_id=task_id,
            design=new_design,
            version=version,
            instruction=f"直接编辑图层 {layer_id}",
            summary=f"直接调整「{layer.get('name')}」",
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content=f"已在画布上直接调整「{layer.get('name')}」，其他图层保持不变。",
            version=version,
            actions=[
                {
                    "type": "update_layer",
                    "layer_id": layer_id,
                    "summary": "直接编辑图层属性",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        message = (
            exc.message
            if isinstance(exc, (design_utils.DesignError, ToAPIsError))
            else "图层修改过程中出现异常，请稍后重试。"
        )
        logger.exception("direct layer update failed for %s", task_id)
        db.tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "edit.status": "failed",
                    "edit.error": message,
                    "stage": "done",
                    "progress": 100,
                    "updated_at": utcnow(),
                }
            },
        )


def restore_version(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    task_id: str,
    version: int,
) -> int:
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise ToAPIsError("task_missing", "任务不存在")
    snapshot = db.versions.find_one({"task_id": task_id, "version": version})
    if snapshot is None:
        raise ToAPIsError("version_missing", "历史版本不存在")
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
