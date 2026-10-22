from __future__ import annotations

import hashlib
import io
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.services import platform_presets, storage
from app.services.canvas import canvas_size, gateway_image_size
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.image_ref import ImageRef, cleanup_image_refs, image_ref_from_asset
from app.services.object_store import ObjectStore

logger = logging.getLogger(__name__)

GENERATION_SYSTEM_PROMPT = (
    "You are an expert visual designer. Create one polished, visually compelling, "
    "complete design based on the user's request. You have full creative freedom over "
    "composition, typography, color, visual hierarchy, and art direction. Follow the "
    "requested content and constraints accurately. Reproduce any user-provided text "
    "verbatim; do not add, omit, translate, or rewrite text. Follow the explicit "
    "roles assigned to each input image; never treat a style reference as a source "
    "of subjects or identity. "
    "Return the final complete image only."
)
OPTIMIZED_GENERATION_SYSTEM_PROMPT = (
    "Create one complete, polished image following the user request. "
    "Use the attached images only for the roles stated below. "
    "Reproduce requested text verbatim. Return the final image only."
)

EDIT_SYSTEM_PROMPT = (
    "Edit the provided image according to the user's instruction. Preserve the "
    "existing composition, subject, design identity, text, and unaffected content as "
    "much as possible. Make the requested change naturally and maintain professional "
    "visual quality. If the user explicitly requests a broad redesign, adjust the "
    "overall design freely while retaining the relevant content. Reproduce requested "
    "text exactly; do not rewrite or translate it. Return the final complete image only."
)


class StudioError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error_text(exc: Exception) -> str:
    value = getattr(exc, "message", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(exc).strip() or "图片生成失败，请稍后重试。"


def _append_message(
    db: Database,
    task_id: str,
    *,
    role: str,
    content: str,
    version_id: str | None = None,
    version: int | None = None,
    status: str = "done",
) -> dict[str, Any]:
    message = {
        "id": f"msg_{uuid.uuid4().hex}",
        "role": role,
        "content": content,
        "created_at": utcnow(),
        "version_id": version_id,
        "version": version,
        "actions": [],
        "status": status,
    }
    db.whole_tasks.update_one({"_id": task_id}, {"$push": {"messages": message}})
    return message


def _asset_key(asset: dict[str, Any]) -> str:
    return str(asset.get("prepared_path") or asset["path"])


def _save_output_asset(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    version: int,
    data: bytes,
) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 - provider payload is untrusted
        raise StudioError("invalid_output", "模型返回的图片无法解码", 502) from exc

    relative = storage.object_key(
        settings,
        "outputs",
        f"{task_id}_v{version}.png",
    )
    store.put_bytes(relative, png, "image/png")
    asset_id = f"asset_{uuid.uuid4().hex}"
    db.assets.insert_one(
        {
            "_id": asset_id,
            "kind": "output",
            "original_name": f"{task_id}-v{version}.png",
            "path": relative,
            "prepared_path": relative,
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "created_at": utcnow(),
        }
    )
    return {
        "asset_id": asset_id,
        "path": relative,
        "width": width,
        "height": height,
    }


def _delete_asset(db: Database, store: ObjectStore, asset_id: str | None) -> None:
    if not asset_id:
        return
    asset = db.assets.find_one({"_id": asset_id})
    if asset:
        try:
            store.delete(_asset_key(asset))
        except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
            logger.warning("could not delete output object %s", asset.get("path"))
    db.assets.delete_one({"_id": asset_id})


def _next_version(db: Database, task_id: str) -> int:
    latest = db.whole_versions.find_one({"task_id": task_id}, sort=[("version", -1)])
    return int((latest or {}).get("version") or 0) + 1


def _edit_history(
    db: Database,
    task_id: str,
    source_version_id: str,
) -> list[str]:
    instructions: list[str] = []
    seen: set[str] = set()
    version_id: str | None = source_version_id
    while version_id and version_id not in seen:
        seen.add(version_id)
        version = db.whole_versions.find_one({"_id": version_id, "task_id": task_id})
        if version is None:
            break
        instruction = str(version.get("instruction") or "").strip()
        if version.get("operation") == "edit" and instruction:
            instructions.append(instruction)
        parent = version.get("parent_version_id")
        version_id = str(parent) if parent else None
    return list(reversed(instructions))


def _load_reference_images(
    db: Database,
    store: ObjectStore,
    input_data: dict[str, Any],
    brand: dict[str, Any] | None = None,
    *,
    include_style: bool = True,
) -> list[ImageRef]:
    references: list[ImageRef] = []
    for asset_id in list(input_data.get("product_asset_ids") or [])[:4]:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise StudioError("reference_missing", "参考素材不存在", 422)
        try:
            references.append(image_ref_from_asset(db, store, str(asset_id)))
        except Exception as exc:  # noqa: BLE001 - surface a clear user error
            raise StudioError("reference_missing", str(exc), 422) from exc
    if not include_style:
        return references
    style_ids = list(input_data.get("style_reference_asset_ids") or [])[:3]
    if input_data.get("brand_mode") == "preset" and brand:
        style_ids.extend(list(brand.get("reference_asset_ids") or [])[:5])
    for asset_id in dict.fromkeys(style_ids):
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None or asset.get("kind") != "brand_reference":
            raise StudioError("style_reference_missing", "风格参考图不存在", 422)
        try:
            references.append(image_ref_from_asset(db, store, str(asset_id)))
        except Exception as exc:  # noqa: BLE001 - surface a clear user error
            raise StudioError("style_reference_missing", str(exc), 422) from exc
    return references


def _reference_role_guidance(product_count: int, reference_count: int) -> str:
    if not reference_count:
        return ""

    lines = ["Image roles, in the exact order the input images are attached:"]
    if product_count:
        product_range = "1" if product_count == 1 else f"1-{product_count}"
        lines.append(
            f"- Subject/content reference image(s): {product_range}. Use these as "
            "the only source of featured people, faces, identities, and requested "
            "subjects. Preserve their identity and details."
        )
    if reference_count > product_count:
        style_start = product_count + 1
        style_range = (
            str(style_start)
            if reference_count == style_start
            else f"{style_start}-{reference_count}"
        )
        lines.append(
            f"- Style-only reference image(s): {style_range}. Learn only their "
            "color palette, lighting, typography feel, broad composition, and "
            "atmosphere; do not take content from them."
        )
        lines.append(
            "Never reuse people, faces, identities, poses, clothing, or subjects "
            "from style references, and do not introduce or substitute a person "
            "from a style reference."
        )
    return "\n".join(lines)


def _model_input_images(
    db: Database,
    input_data: dict[str, Any],
    brand: dict[str, Any] | None = None,
    source_asset_id: str | None = None,
    *,
    include_style: bool = True,
) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    asset_ids = [
        (asset_id, None)
        for asset_id in list(input_data.get("product_asset_ids") or [])[:4]
    ]
    if include_style:
        style_ids = list(input_data.get("style_reference_asset_ids") or [])[:3]
        if input_data.get("brand_mode") == "preset" and brand:
            style_ids.extend(list(brand.get("reference_asset_ids") or [])[:5])
        asset_ids.extend((asset_id, None) for asset_id in dict.fromkeys(style_ids))
    if source_asset_id:
        asset_ids.append((source_asset_id, "current_design"))

    for asset_id, kind_override in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        images.append(
            {
                "asset_id": str(asset_id),
                "name": str(asset.get("original_name") or "参考图片"),
                "kind": kind_override or str(asset.get("kind") or "reference"),
            }
        )
    return images


def _version_document(
    *,
    task_id: str,
    version: int,
    parent_version_id: str | None,
    output_asset_id: str,
    instruction: str,
    operation: str,
    width: int,
    height: int,
    model_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        "_id": f"wver_{uuid.uuid4().hex}",
        "task_id": task_id,
        "version": version,
        "parent_version_id": parent_version_id,
        "output_asset_id": output_asset_id,
        "instruction": instruction,
        "operation": operation,
        "width": width,
        "height": height,
        "model_input": model_input,
        "created_at": utcnow(),
    }


def _commit_version(
    db: Database,
    store: ObjectStore,
    *,
    task_id: str,
    version_doc: dict[str, Any],
    generation: bool,
) -> None:
    version_id = str(version_doc["_id"])
    asset_id = str(version_doc["output_asset_id"])
    db.whole_versions.insert_one(version_doc)
    update = {
        "selected_version_id": version_id,
        "current_version_id": version_id,
        "current_version": int(version_doc["version"]),
        "output_asset_id": asset_id,
        "canvas": {
            "width": int(version_doc["width"]),
            "height": int(version_doc["height"]),
        },
        "status": "succeeded",
        "stage": "done",
        "progress": 100,
        "error": None,
        "model_input_latest": version_doc.get("model_input"),
        "model_input_attempt": None,
        "updated_at": utcnow(),
    }
    if generation:
        update.update(
            {
                "generation.status": "completed",
                "generation.error": None,
                "provider.status": "completed",
            }
        )
    else:
        update.update(
            {
                "edit.status": "idle",
                "edit.error": None,
                "provider.status": "completed",
            }
        )
    try:
        result = db.whole_tasks.update_one({"_id": task_id}, {"$set": update})
        if result.modified_count != 1:
            raise StudioError("version_commit_failed", "版本保存失败，请重试", 500)
    except Exception:
        db.whole_versions.delete_one({"_id": version_id})
        _delete_asset(db, store, asset_id)
        raise
    _append_message(
        db,
        task_id,
        role="assistant",
        content=f"已生成 v{version_doc['version']}。",
        version_id=version_id,
        version=int(version_doc["version"]),
    )


def run_generation(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
) -> None:
    claimed = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "generation.status": "queued"},
        {
            "$set": {
                "status": "running",
                "stage": "generating",
                "progress": 15,
                "generation": {
                    "status": "running",
                    "error": None,
                    "started_at": utcnow(),
                },
                "provider.status": "running",
                "updated_at": utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        return

    reference_images: list[ImageRef] = []
    asset_id: str | None = None
    try:
        input_data = claimed.get("input") or {}
        optimized = input_data.get("prompt_optimized") is True
        image_model = str(input_data.get("image_model") or settings.core_ai_image_model)
        brand = claimed.get("brand") if isinstance(claimed.get("brand"), dict) else {}
        reference_images = _load_reference_images(
            db, store, input_data, brand, include_style=not optimized
        )
        product_count = min(len(input_data.get("product_asset_ids") or []), 4)
        reference_role_block = _reference_role_guidance(
            product_count, len(reference_images)
        )
        reference_role_block = (
            f"\n\n{reference_role_block}" if reference_role_block else ""
        )
        brand_context = ""
        if not optimized and brand.get("name"):
            brand_context = (
                f"\n\nBrand context: {brand.get('name')}. "
                f"{str(brand.get('summary') or '').strip()}\n"
                f"Brand layout guide:\n{str(brand.get('layout_guide') or '').strip()}\n"
                f"Brand photography and background guide:\n"
                f"{str(brand.get('background_guide') or '').strip()}"
                + (
                    "\nUse attached brand reference images for brand colors, typography, "
                    "composition, and decorative style. Keep product details faithful "
                    "to the product reference images."
                    if brand.get("reference_asset_ids")
                    else ""
                )
            )
        merchant_info = input_data.get("merchant_info") if isinstance(input_data.get("merchant_info"), dict) else None
        if optimized:
            canvas = claimed.get("canvas") or {}
            platform_context = (
                f"Canvas: {canvas.get('width')}×{canvas.get('height')}; "
                f"aspect ratio {input_data.get('ratio')}."
            )
            if (claimed.get("platform") or {}).get("contact_policy") == "forbidden":
                platform_context += " 小红书图片不得出现地址、电话、微信号、二维码或其他联系方式。"
        else:
            platform_context = platform_presets.prompt_from_task(
                claimed.get("platform"), merchant_info, claimed.get("placement")
            )
        platform_block = f"\n\n{platform_context}" if platform_context else ""
        prompt = (
            f"{OPTIMIZED_GENERATION_SYSTEM_PROMPT if optimized else GENERATION_SYSTEM_PROMPT}"
            f"{reference_role_block}"
            f"{brand_context}{platform_block}\n\nUser request:\n"
            f"{str(input_data.get('prompt') or '').strip()}"
        ).strip()
        canvas = claimed.get("canvas") or {}
        size = gateway_image_size(
            int(canvas.get("width") or 1024),
            int(canvas.get("height") or 1024),
        )
        endpoint = "/images/edits" if reference_images else "/images/generations"
        model_input = {
            "endpoint": endpoint,
            "model": image_model,
            "prompt": prompt,
            "parameters": {
                "size": size,
                "quality": settings.core_ai_image_quality,
                "n": 1,
                "output_format": "png",
            },
            "images": _model_input_images(
                db, input_data, brand, include_style=not optimized
            ),
        }
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "stage": "generating",
                    "progress": 45,
                    "model_input_attempt": model_input,
                    "updated_at": utcnow(),
                }
            },
        )
        if reference_images:
            data = client.edit_image(prompt, reference_images, size=size, model=image_model)
        else:
            data = client.generate_image(prompt, size=size, model=image_model)
        saved = _save_output_asset(
            db,
            settings,
            store,
            task_id=task_id,
            version=1,
            data=data,
        )
        asset_id = saved["asset_id"]
        document = _version_document(
            task_id=task_id,
            version=1,
            parent_version_id=None,
            output_asset_id=asset_id,
            instruction=str(input_data.get("prompt") or ""),
            operation="generate",
            width=saved["width"],
            height=saved["height"],
            model_input=model_input,
        )
        _commit_version(
            db,
            store,
            task_id=task_id,
            version_doc=document,
            generation=True,
        )
        asset_id = None
    except Exception as exc:  # noqa: BLE001 - persist one actionable failure
        logger.exception("generation failed for %s", task_id)
        _delete_asset(db, store, asset_id)
        message = _error_text(exc)
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "status": "failed",
                    "stage": "failed",
                    "progress": 0,
                    "error": {"code": "generation_failed", "message": message},
                    "generation.status": "failed",
                    "generation.error": message,
                    "provider.status": "failed",
                    "updated_at": utcnow(),
                }
            },
        )
    finally:
        cleanup_image_refs(reference_images)


def run_edit(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
    instruction: str,
    source_version_id: str,
) -> None:
    task = db.whole_tasks.find_one(
        {"_id": task_id, "status": "succeeded", "edit.status": "running"}
    )
    if task is None:
        return

    original_refs: list[ImageRef] = []
    source_ref: ImageRef | None = None
    asset_id: str | None = None
    try:
        source = db.whole_versions.find_one(
            {"_id": source_version_id, "task_id": task_id}
        )
        if source is None:
            raise StudioError("version_missing", "来源版本不存在", 404)
        source_asset = db.assets.find_one({"_id": source.get("output_asset_id")})
        if source_asset is None:
            raise StudioError("source_image_missing", "来源图片不存在", 404)
        source_ref = image_ref_from_asset(
            db, store, str(source_asset["_id"])
        )
        width = int(source.get("width") or source_asset.get("width") or 1024)
        height = int(source.get("height") or source_asset.get("height") or 1024)
        size = gateway_image_size(width, height)
        task_input = task.get("input") or {}
        image_model = str(task_input.get("image_model") or settings.core_ai_image_model)
        brand = task.get("brand") if isinstance(task.get("brand"), dict) else {}
        original_refs = _load_reference_images(db, store, task_input, brand)
        original_count = len(original_refs)
        product_count = min(len(task_input.get("product_asset_ids") or []), 4)
        reference_roles = _reference_role_guidance(product_count, original_count)
        history = _edit_history(db, task_id, source_version_id)
        merchant_info = task_input.get("merchant_info") if isinstance(task_input.get("merchant_info"), dict) else None
        platform_context = platform_presets.prompt_from_task(
            task.get("platform"), merchant_info, task.get("placement")
        )
        platform_block = f"\n\n{platform_context}" if platform_context else ""
        image_role = (
            f"{reference_roles}\nImage {original_count + 1} is the current design reference."
            if original_count
            else "Image 1 is the current design reference. No original reference images are attached."
        )
        history_block = (
            "\n".join(f"{index}. {item}" for index, item in enumerate(history, 1))
            if history
            else "(none)"
        )
        brand_block = (
            f"Brand context: {brand.get('name', '')}. {brand.get('summary', '')}\n"
            f"Brand layout guide:\n{brand.get('layout_guide', '')}\n"
            f"Brand photography and background guide:\n{brand.get('background_guide', '')}\n\n"
            if brand
            else ""
        )
        prompt = (
            f"{EDIT_SYSTEM_PROMPT}\n\nImage roles:\n{image_role}\n"
            f"{brand_block}"
            "Use the current design as the visual frame. Preserve subject identity "
            "from subject/content references only; style references are for styling only.\n\n"
            "Confirmed prior modifications already applied to the current design "
            f"(oldest to newest):\n{history_block}\n\n"
            f"Current requested modification (authoritative):\n{instruction.strip()}"
            f"{platform_block}"
        ).strip()
        model_input = {
            "endpoint": "/images/edits",
            "model": image_model,
            "prompt": prompt,
            "parameters": {
                "size": size,
                "quality": settings.core_ai_image_quality,
                "n": 1,
                "output_format": "png",
            },
            "images": _model_input_images(
                db,
                task_input,
                brand,
                source_asset_id=str(source_asset["_id"]),
            ),
        }
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "stage": "editing",
                    "progress": 55,
                    "model_input_attempt": model_input,
                    "updated_at": utcnow(),
                }
            },
        )
        data = client.edit_image(prompt, [*original_refs, source_ref], size=size, model=image_model)
        version = _next_version(db, task_id)
        saved = _save_output_asset(
            db,
            settings,
            store,
            task_id=task_id,
            version=version,
            data=data,
        )
        asset_id = saved["asset_id"]
        document = _version_document(
            task_id=task_id,
            version=version,
            parent_version_id=source_version_id,
            output_asset_id=asset_id,
            instruction=instruction,
            operation="edit",
            width=saved["width"],
            height=saved["height"],
            model_input=model_input,
        )
        _commit_version(
            db,
            store,
            task_id=task_id,
            version_doc=document,
            generation=False,
        )
        asset_id = None
    except Exception as exc:  # noqa: BLE001 - never destroy the last good image
        logger.exception("edit failed for %s", task_id)
        _delete_asset(db, store, asset_id)
        message = _error_text(exc)
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "stage": "done",
                    "progress": 100,
                    "edit.status": "failed",
                    "edit.error": message,
                    "provider.status": "failed",
                    "updated_at": utcnow(),
                }
            },
        )
        _append_message(
            db,
            task_id,
            role="assistant",
            content=f"这次修改没有完成：{message}",
            status="failed",
        )
    finally:
        cleanup_image_refs(original_refs)
        if source_ref is not None:
            source_ref.cleanup()


def select_version(db: Database, task_id: str, version_id: str) -> dict[str, Any]:
    version = db.whole_versions.find_one({"_id": version_id, "task_id": task_id})
    if version is None:
        raise StudioError("version_missing", "历史版本不存在", 404)
    asset = db.assets.find_one({"_id": version.get("output_asset_id")})
    if asset is None:
        raise StudioError("version_image_missing", "该版本图片不存在", 404)
    updated = db.whole_tasks.find_one_and_update(
        {
            "_id": task_id,
            "status": "succeeded",
            "edit.status": {"$ne": "running"},
        },
        {
            "$set": {
                "selected_version_id": version_id,
                "current_version_id": version_id,
                "current_version": int(version.get("version") or 0),
                "output_asset_id": version.get("output_asset_id"),
                "canvas": {
                    "width": int(version.get("width") or asset.get("width") or 0),
                    "height": int(version.get("height") or asset.get("height") or 0),
                },
                "updated_at": utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise StudioError("edit_running", "当前有修改正在执行，请稍候", 409)
    return updated


def mark_interrupted(db: Database) -> None:
    message = "本地服务重启，任务已中断。请重新发起设计。"
    db.whole_tasks.update_many(
        {"status": {"$in": ["queued", "running"]}},
        {
            "$set": {
                "status": "failed",
                "stage": "failed",
                "error": {"code": "interrupted", "message": message},
                "generation.status": "failed",
                "generation.error": message,
                "updated_at": utcnow(),
            }
        },
    )
    db.whole_tasks.update_many(
        {"edit.status": "running"},
        {
            "$set": {
                "edit.status": "failed",
                "edit.error": "本地服务重启，修改已中断，请重新发起。",
                "updated_at": utcnow(),
            }
        },
    )
