from __future__ import annotations

import hashlib
import io
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.services import agent_image, brand_rules, platform_presets, storage
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
    "Create one finished image following the user's prompt and the assigned roles of attached images. "
    "Return the final image only."
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
    role: str | None = None,
    target_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            if target_size is not None:
                image = ImageOps.fit(
                    image,
                    target_size,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 - provider payload is untrusted
        raise StudioError("invalid_output", "模型返回的图片无法解码", 502) from exc

    suffix = f"_{role}" if role else ""
    relative = storage.object_key(
        settings,
        "outputs",
        f"{task_id}_v{version}{suffix}.png",
    )
    store.put_bytes(relative, png, "image/png")
    asset_id = f"asset_{uuid.uuid4().hex}"
    db.assets.insert_one(
        {
            "_id": asset_id,
            "source": "generated",
            "kind": "output",
            "original_name": f"{task_id}-v{version}{suffix}.png",
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


def _load_reference_images(
    db: Database,
    store: ObjectStore,
    input_data: dict[str, Any],
    brand: dict[str, Any] | None = None,
    *,
    include_style: bool = True,
) -> list[ImageRef]:
    references: list[ImageRef] = []
    for asset_id in list(input_data.get("product_asset_ids") or [])[:6]:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise StudioError("reference_missing", "素材图不存在", 422)
        try:
            references.append(image_ref_from_asset(db, store, str(asset_id)))
        except Exception as exc:  # noqa: BLE001 - surface a clear user error
            raise StudioError("reference_missing", str(exc), 422) from exc
    logo_id = str(input_data.get("logo_asset_id") or "").strip()
    if logo_id:
        logo = db.assets.find_one({"_id": logo_id})
        if logo is None:
            raise StudioError("logo_missing", "Logo 不存在或类型不正确", 422)
        try:
            references.append(image_ref_from_asset(db, store, logo_id))
        except Exception as exc:  # noqa: BLE001 - surface a clear user error
            raise StudioError("logo_missing", str(exc), 422) from exc
    if not include_style:
        return references
    explicit_style_ids = list(input_data.get("style_reference_asset_ids") or [])[:3]
    for asset_id in dict.fromkeys(explicit_style_ids):
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            raise StudioError("style_reference_missing", "风格参考图不存在", 422)
        try:
            references.append(image_ref_from_asset(db, store, str(asset_id)))
        except Exception as exc:  # noqa: BLE001 - surface a clear user error
            raise StudioError("style_reference_missing", str(exc), 422) from exc
    if input_data.get("brand_mode") == "preset" and brand:
        for asset_id in list(brand.get("reference_asset_ids") or [])[:5]:
            asset = db.assets.find_one({"_id": asset_id})
            if asset is None:
                raise StudioError("style_reference_missing", "风格参考图不存在", 422)
            try:
                references.append(image_ref_from_asset(db, store, str(asset_id)))
            except Exception as exc:  # noqa: BLE001 - surface a clear user error
                raise StudioError("style_reference_missing", str(exc), 422) from exc
    return references


def _reference_role_guidance(
    product_count: int,
    reference_count: int,
    logo_count: int = 0,
) -> str:
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
    if logo_count:
        logo_start = product_count + 1
        logo_range = (
            str(logo_start)
            if logo_count == 1
            else f"{logo_start}-{logo_start + logo_count - 1}"
        )
        lines.append(
            f"- Logo image {logo_range}. Preserve the merchant mark exactly: "
            "keep its visible shape, wording, proportions, and original colors. "
            "Do not substitute a logo from a style reference."
        )
    if reference_count > product_count + logo_count:
        style_start = product_count + logo_count + 1
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


def _brand_style_context(brand: dict[str, Any]) -> str:
    purpose = str(brand.get("purpose") or "").strip()
    colors = [
        str(item.get("hex"))
        for item in brand.get("colors") or []
        if isinstance(item, dict) and item.get("hex")
    ]
    return (
        f"Brand purpose: {purpose or 'unspecified'}\n"
        "Brand palette (explicit priority over reference colors): "
        f"{', '.join(colors) or 'unspecified'}\n"
        "Apply the palette to backgrounds, typography, and decoration while "
        "preserving the original colors of products and the merchant logo."
    )


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
        (asset_id, "product")
        for asset_id in list(input_data.get("product_asset_ids") or [])[:6]
    ]
    logo_id = str(input_data.get("logo_asset_id") or "").strip()
    if logo_id:
        asset_ids.append((logo_id, "logo"))
    if include_style:
        style_ids = list(input_data.get("style_reference_asset_ids") or [])[:3]
        if input_data.get("brand_mode") == "preset" and brand:
            style_ids.extend(list(brand.get("reference_asset_ids") or [])[:5])
        asset_ids.extend(
            (asset_id, "style_reference") for asset_id in dict.fromkeys(style_ids)
        )
    if source_asset_id:
        asset_ids.append((source_asset_id, "current_design"))

    for asset_id, role in asset_ids:
        asset = db.assets.find_one({"_id": asset_id})
        if asset is None:
            continue
        images.append(
            {
                "asset_id": str(asset_id),
                "name": str(asset.get("original_name") or "参考图片"),
                "role": role,
            }
        )
    return images


def _version_outputs(version: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = version.get("outputs")
    if isinstance(outputs, list) and outputs:
        return [item for item in outputs if isinstance(item, dict)]
    asset_id = version.get("output_asset_id")
    if not asset_id:
        return []
    return [
        {
            "id": "gateway_result",
            "channel": "gateway",
            "role": "gateway_result",
            "label": "接口结果",
            "asset_id": str(asset_id),
            "width": int(version.get("width") or 0),
            "height": int(version.get("height") or 0),
            "selectable": True,
            "model_input": version.get("model_input"),
            "created_at": version.get("created_at"),
        }
    ]


def _first_selectable_output(version: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in _version_outputs(version) if item.get("selectable")), None)


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
    outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_outputs = outputs or [
        {
            "id": "gateway_result",
            "channel": "gateway",
            "role": "gateway_result",
            "label": "接口结果",
            "asset_id": output_asset_id,
            "width": width,
            "height": height,
            "selectable": True,
            "model_input": model_input,
            "created_at": utcnow(),
        }
    ]
    return {
        "_id": f"wver_{uuid.uuid4().hex}",
        "task_id": task_id,
        "version": version,
        "parent_version_id": parent_version_id,
        "output_asset_id": output_asset_id,
        "outputs": normalized_outputs,
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
    require_output_selection: bool = False,
    edit_outcome: str | None = None,
    message: str | None = None,
) -> None:
    version_id = str(version_doc["_id"])
    outputs = list(version_doc.get("outputs") or [])
    selectable = next((item for item in outputs if item.get("selectable")), None)
    default_asset_id = (
        str(selectable.get("asset_id"))
        if selectable and selectable.get("asset_id")
        else str(version_doc["output_asset_id"])
    )
    db.whole_versions.insert_one(version_doc)
    update = {
        "selected_version_id": version_id,
        "current_version_id": version_id,
        "current_version": int(version_doc["version"]),
        "current_output_id": None if require_output_selection else (
            str(selectable.get("id")) if selectable else None
        ),
        "output_asset_id": None if require_output_selection else default_asset_id,
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
                "edit.status": "awaiting_selection" if require_output_selection else "idle",
                "edit.error": None,
                "edit.outcome": edit_outcome,
                "provider.status": "completed" if edit_outcome == "success" else edit_outcome,
            }
        )
    try:
        result = db.whole_tasks.update_one({"_id": task_id}, {"$set": update})
        if result.modified_count != 1:
            raise StudioError("version_commit_failed", "版本保存失败，请重试", 500)
    except Exception:
        db.whole_versions.delete_one({"_id": version_id})
        for output in outputs:
            _delete_asset(db, store, output.get("asset_id"))
        raise
    _append_message(
        db,
        task_id,
        role="assistant",
        content=message or f"已生成 v{version_doc['version']}。",
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
        product_count = min(len(input_data.get("product_asset_ids") or []), 6)
        logo_count = 1 if input_data.get("logo_asset_id") else 0
        reference_role_block = _reference_role_guidance(
            product_count, len(reference_images), logo_count
        )
        reference_role_block = (
            f"\n\n{reference_role_block}" if reference_role_block else ""
        )
        brand_context = ""
        if not optimized and brand.get("name"):
            guides = (
                brand_rules.compile_style_fields(brand["style_fields"])
                if brand.get("style_fields") else
                "\n".join(dict.fromkeys(str(brand.get(key) or '').strip() for key in ("layout_guide", "background_guide")))
            )
            brand_context = (
                f"\n\nBrand context: {brand.get('name')}. "
                f"{str(brand.get('summary') or '').strip()}\n"
                f"{_brand_style_context(brand)}\n"
                f"Brand generation guide:\n{guides}"
                + (
                    "\nUse attached brand reference images for brand colors, typography, "
                    "composition, and decorative style. Keep product details faithful "
                    "to the product reference images."
                    if brand.get("reference_asset_ids")
                    else ""
                )
            )
        platform_context = platform_presets.prompt_from_task(
            claimed.get("platform"), claimed.get("placement")
        )
        if optimized:
            canvas = claimed.get("canvas") or {}
            platform_context = (
                f"Canvas: {canvas.get('width')}×{canvas.get('height')}; "
                f"aspect ratio {input_data.get('ratio')}.\n\n{platform_context}"
            )
        platform_block = f"\n\n{platform_context}" if platform_context else ""
        common_context = brand_rules.rules_context(claimed.get("brand_rules"))
        common_block = f"\n\n{common_context}" if common_context else ""
        prompt = (
            f"{OPTIMIZED_GENERATION_SYSTEM_PROMPT if optimized else GENERATION_SYSTEM_PROMPT}"
            f"{reference_role_block}"
            f"{common_block}{brand_context}{platform_block}\n\nUser request:\n"
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


def _set_edit_branch(db: Database, task_id: str, branch: str, values: dict[str, Any]) -> None:
    db.whole_tasks.update_one(
        {"_id": task_id},
        {"$set": {f"edit.branches.{branch}.{key}": value for key, value in values.items()}},
    )


def _gateway_edit_branch(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    version: int,
    task_input: dict[str, Any],
    source_asset: dict[str, Any],
    size: str,
    image_model: str,
    model_input: dict[str, Any],
    prompt: str,
    target_size: tuple[int, int],
) -> list[dict[str, Any]]:
    original_refs: list[ImageRef] = []
    source_ref: ImageRef | None = None
    asset_id: str | None = None
    try:
        original_refs = _load_reference_images(db, store, task_input, include_style=False)
        _set_edit_branch(
            db,
            task_id,
            "gateway",
            {"status": "running", "model_input": model_input, "updated_at": utcnow()},
        )
        source_ref = image_ref_from_asset(db, store, str(source_asset["_id"]))
        data = client.edit_image(
            prompt,
            [*original_refs, source_ref],
            size=size,
            model=image_model,
        )
        saved = _save_output_asset(
            db,
            settings,
            store,
            task_id=task_id,
            version=version,
            data=data,
            role="gateway",
            target_size=target_size,
        )
        asset_id = saved["asset_id"]
        output = {
            "id": f"gateway_result_{uuid.uuid4().hex}",
            "channel": "gateway",
            "role": "gateway_result",
            "label": "接口结果",
            "asset_id": asset_id,
            "width": saved["width"],
            "height": saved["height"],
            "selectable": True,
            "model_input": model_input,
            "created_at": utcnow(),
        }
        _set_edit_branch(
            db,
            task_id,
            "gateway",
            {
                "status": "completed",
                "outputs": [output],
                "completed_at": utcnow(),
                "updated_at": utcnow(),
                "error": None,
            },
        )
        asset_id = None
        return [output]
    except Exception as exc:  # noqa: BLE001 - branch failure must not stop the other branch
        logger.exception("gateway edit branch failed for %s", task_id)
        _delete_asset(db, store, asset_id)
        _set_edit_branch(
            db,
            task_id,
            "gateway",
            {
                "status": "failed",
                "error": _error_text(exc),
                "completed_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        return []
    finally:
        cleanup_image_refs(original_refs)
        if source_ref is not None:
            source_ref.cleanup()


def _agent_edit_branch(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    version: int,
    source_asset: dict[str, Any],
    instruction: str,
    target_size: tuple[int, int],
) -> list[dict[str, Any]]:
    try:
        expires = max(
            settings.cos_presign_expiry_seconds,
            settings.core_ai_image_agent_timeout_seconds + 600,
        )
        image_url = agent_image.asset_public_url(
            db,
            settings,
            store,
            client,
            str(source_asset["_id"]),
            expires=expires,
        )
        agent_input = agent_image.build_agent_edit_input(
            [("当前海报（需要修改的原图）", image_url)],
            instruction,
        )
        model_input = {
            "channel": "agent",
            "endpoint": f"/api/runs/agent/{settings.core_ai_image_agent_id}/trigger",
            "model": "image-gen-skill",
            "prompt": agent_input,
            "parameters": {
                "agent_id": settings.core_ai_image_agent_id,
                "timeout_seconds": settings.core_ai_image_agent_timeout_seconds,
                "image_url": image_url,
            },
            "images": [
                {
                    "asset_id": str(source_asset["_id"]),
                    "name": str(source_asset.get("original_name") or "当前海报"),
                    "kind": "current_design",
                    "role": "edit_target",
                    "url": image_url,
                }
            ],
        }
        _set_edit_branch(
            db,
            task_id,
            "agent",
            {"status": "running", "model_input": model_input, "updated_at": utcnow()},
        )
        result, outputs = agent_image.run_agent_images(
            client,
            agent_id=settings.core_ai_image_agent_id,
            input_text=agent_input,
            timeout_seconds=settings.core_ai_image_agent_timeout_seconds,
        )
        saved_outputs: list[dict[str, Any]] = []
        for index, output in enumerate(outputs, start=1):
            saved = agent_image.save_png_asset(
                db,
                settings,
                store,
                key_prefix=f"{task_id}_v{version}_agent_{index}",
                original_stem=f"{task_id}-v{version}-agent-{index}",
                data=output.data,
                role=output.role,
                target_size=target_size,
            )
            saved_outputs.append(
                {
                    "id": f"{output.role}_{uuid.uuid4().hex}",
                    "channel": "agent",
                    "role": output.role,
                    "label": output.label,
                    "asset_id": saved["asset_id"],
                    "width": saved["width"],
                    "height": saved["height"],
                    "selectable": output.selectable,
                    "model_input": model_input,
                    "source_url": output.source_url,
                    "created_at": utcnow(),
                }
            )
        _set_edit_branch(
            db,
            task_id,
            "agent",
            {
                "status": "completed",
                "outputs": saved_outputs,
                "run_id": result.run_id,
                "agent_output": result.output,
                "completed_at": utcnow(),
                "updated_at": utcnow(),
                "error": None,
            },
        )
        return saved_outputs
    except Exception as exc:  # noqa: BLE001 - keep gateway result available
        logger.exception("agent edit branch failed for %s", task_id)
        _set_edit_branch(
            db,
            task_id,
            "agent",
            {
                "status": "failed",
                "error": _error_text(exc),
                "completed_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        return []


def run_edit(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    task_id: str,
    instruction: str,
    source_version_id: str,
    source_output_id: str | None = None,
) -> None:
    task = db.whole_tasks.find_one(
        {"_id": task_id, "status": "succeeded", "edit.status": "running"}
    )
    if task is None:
        return
    try:
        source = db.whole_versions.find_one(
            {"_id": source_version_id, "task_id": task_id}
        )
        if source is None:
            raise StudioError("version_missing", "来源版本不存在", 404)
        source_output = None
        if source_output_id:
            source_output = next(
                (item for item in _version_outputs(source) if str(item.get("id")) == source_output_id),
                None,
            )
            if source_output is None:
                raise StudioError("output_missing", "来源结果不存在", 404)
            if not source_output.get("selectable"):
                raise StudioError("output_not_selectable", "透明图层不能作为编辑源图", 422)
        source_asset_id = (
            source_output.get("asset_id")
            if source_output
            else source.get("output_asset_id")
        )
        source_asset = db.assets.find_one({"_id": source_asset_id})
        if source_asset is None:
            raise StudioError("source_image_missing", "来源图片不存在", 404)
        width = int(source_output.get("width") if source_output else source.get("width") or source_asset.get("width") or 1024)
        height = int(source_output.get("height") if source_output else source.get("height") or source_asset.get("height") or 1024)
        size = gateway_image_size(width, height)
        task_input = task.get("input") or {}
        image_model = str(task_input.get("image_model") or settings.core_ai_image_model)
        product_count = min(len(task_input.get("product_asset_ids") or []), 6)
        original_count = product_count + (1 if task_input.get("logo_asset_id") else 0)
        image_roles: list[str] = []
        if product_count:
            product_range = "1" if product_count == 1 else f"1-{product_count}"
            image_roles.append(
                f"Original content image(s): {product_range}. "
                "Use these originals as the source of subjects and fine details; "
                "preserve their identity, shapes, colors, and textures."
            )
        if task_input.get("logo_asset_id"):
            image_roles.append(
                f"Image {product_count + 1} is the original logo. "
                "Preserve its original shape, wording, proportions, and colors."
            )
        image_roles.append(
            f"Image {original_count + 1} is the generated design reference for style and layout only. "
            "Do not repaint, retouch, or trace the generated image; "
            "do not use it as a pixel source or image-to-image base."
        )
        prompt = (
            "\n".join(image_roles)
            + "\nApply only the requested changes; "
            "keep all other content, text, composition, and style unchanged."
            f"\n\n{instruction.strip()}"
        )
        gateway_input = {
            "channel": "gateway",
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
                source_asset_id=str(source_asset["_id"]),
                include_style=False,
            ),
        }
        version = _next_version(db, task_id)
        started_at = utcnow()
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "stage": "editing",
                    "progress": 40,
                    "model_input_attempt": gateway_input,
                    "edit": {
                        "status": "running",
                        "instruction": instruction,
                        "source_version_id": source_version_id,
                        "source_output_id": source_output_id,
                        "error": None,
                        "outcome": None,
                        "branches": {
                            "gateway": {
                                "status": "running",
                                "started_at": started_at,
                                "model_input": gateway_input,
                                "outputs": [],
                                "error": None,
                            },
                            "agent": {
                                "status": "preparing",
                                "started_at": started_at,
                                "model_input": None,
                                "outputs": [],
                                "run_id": None,
                                "error": None,
                            },
                        },
                    },
                    "updated_at": started_at,
                }
            },
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    _gateway_edit_branch,
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    version=version,
                    task_input=task_input,
                    source_asset=source_asset,
                    size=size,
                    image_model=image_model,
                    model_input=gateway_input,
                    prompt=prompt,
                    target_size=(width, height),
                ),
                executor.submit(
                    _agent_edit_branch,
                    db,
                    settings,
                    store,
                    client,
                    task_id=task_id,
                    version=version,
                    source_asset=source_asset,
                    instruction=instruction,
                    target_size=(width, height),
                ),
            ]
            all_outputs: list[dict[str, Any]] = []
            for future in as_completed(futures):
                all_outputs.extend(future.result())
        updated = db.whole_tasks.find_one({"_id": task_id}) or {}
        branches = ((updated.get("edit") or {}).get("branches") or {})
        gateway_status = (branches.get("gateway") or {}).get("status")
        agent_status = (branches.get("agent") or {}).get("status")
        if not all_outputs:
            errors = [
                str((branches.get(name) or {}).get("error") or "")
                for name in ("gateway", "agent")
            ]
            message = "；".join(error for error in errors if error) or "两条改图链路都没有返回图片"
            db.whole_tasks.update_one(
                {"_id": task_id},
                {
                    "$set": {
                        "stage": "done",
                        "progress": 100,
                        "edit.status": "failed",
                        "edit.error": message,
                        "edit.outcome": "failed",
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
            return
        outcome = (
            "success"
            if gateway_status == "completed" and agent_status == "completed"
            else "partial"
        )
        gateway_count = 1 if gateway_status == "completed" else 0
        agent_count = sum(1 for item in all_outputs if item.get("channel") == "agent")
        message = f"已完成 v{version}：接口 {gateway_count} 张，Agent {agent_count} 张。"
        if outcome == "partial":
            message += " 有一条链路失败，已保留成功结果。"
        document = _version_document(
            task_id=task_id,
            version=version,
            parent_version_id=source_version_id,
            output_asset_id=str(all_outputs[0]["asset_id"]),
            instruction=instruction,
            operation="edit",
            width=width,
            height=height,
            model_input=all_outputs[0].get("model_input") or gateway_input,
            outputs=all_outputs,
        )
        selectable_count = sum(1 for item in all_outputs if item.get("selectable"))
        _commit_version(
            db,
            store,
            task_id=task_id,
            version_doc=document,
            generation=False,
            require_output_selection=selectable_count > 1,
            edit_outcome=outcome,
            message=message,
        )
    except Exception as exc:  # noqa: BLE001 - preserve last good image
        logger.exception("edit failed for %s", task_id)
        message = _error_text(exc)
        db.whole_tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "stage": "done",
                    "progress": 100,
                    "edit.status": "failed",
                    "edit.error": message,
                    "edit.outcome": "failed",
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


def select_version(db: Database, task_id: str, version_id: str) -> dict[str, Any]:
    version = db.whole_versions.find_one({"_id": version_id, "task_id": task_id})
    if version is None:
        raise StudioError("version_missing", "历史版本不存在", 404)
    output = _first_selectable_output(version)
    if output is None:
        raise StudioError("version_image_missing", "该版本没有可编辑结果", 404)
    asset = db.assets.find_one({"_id": output.get("asset_id")})
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
                "current_output_id": str(output.get("id") or ""),
                "output_asset_id": output.get("asset_id"),
                "canvas": {
                    "width": int(version.get("width") or asset.get("width") or 0),
                    "height": int(version.get("height") or asset.get("height") or 0),
                },
                "edit.status": "idle",
                "edit.error": None,
                "updated_at": utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise StudioError("edit_running", "当前有修改正在执行，请稍候", 409)
    return updated


def select_output(db: Database, task_id: str, version_id: str, output_id: str) -> dict[str, Any]:
    version = db.whole_versions.find_one({"_id": version_id, "task_id": task_id})
    if version is None:
        raise StudioError("version_missing", "历史版本不存在", 404)
    output = next(
        (item for item in _version_outputs(version) if str(item.get("id")) == output_id),
        None,
    )
    if output is None:
        raise StudioError("output_missing", "该结果不存在", 404)
    if not output.get("selectable"):
        raise StudioError("output_not_selectable", "透明图层不能作为编辑源图", 422)
    asset = db.assets.find_one({"_id": output.get("asset_id")})
    if asset is None:
        raise StudioError("version_image_missing", "该结果图片不存在", 404)
    updated = db.whole_tasks.find_one_and_update(
        {"_id": task_id, "status": "succeeded", "edit.status": {"$ne": "running"}},
        {
            "$set": {
                "selected_version_id": version_id,
                "current_version_id": version_id,
                "current_version": int(version.get("version") or 0),
                "current_output_id": output_id,
                "output_asset_id": output.get("asset_id"),
                "canvas": {
                    "width": int(output.get("width") or asset.get("width") or 0),
                    "height": int(output.get("height") or asset.get("height") or 0),
                },
                "model_input_latest": output.get("model_input"),
                "edit.status": "idle",
                "edit.error": None,
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
                "edit.outcome": "failed",
                "edit.error": "本地服务重启，修改已中断，请重新发起。",
                "edit.branches.gateway.status": "failed",
                "edit.branches.gateway.error": "本地服务重启，修改已中断。",
                "edit.branches.agent.status": "failed",
                "edit.branches.agent.error": "本地服务重启，修改已中断。",
                "updated_at": utcnow(),
            }
        },
    )
