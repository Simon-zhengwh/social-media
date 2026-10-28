from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageOps

from app.config import Settings
from app.services import storage
from app.services.core_ai import AgentRunResult, CoreAIClient, CoreAIError, DownloadedAgentImage
from app.services.object_store import ObjectStore


@dataclass(frozen=True)
class AgentOutputImage:
    data: bytes
    role: str
    label: str
    selectable: bool
    source_url: str


def asset_public_url(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    asset_id: str,
    *,
    expires: int,
) -> str:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise CoreAIError("reference_missing", "参考图片不存在", 422)
    key = asset.get("prepared_path") or asset.get("path")
    if not key:
        raise CoreAIError("reference_missing", "参考图片缺少存储路径", 422)
    if store.local_path(key) is None:
        try:
            url = store.presign(key, expires=expires)
            if isinstance(url, str) and url.startswith("https://"):
                client._validate_public_url(url)
                return url
        except Exception:
            pass
    data = store.get_bytes(key)
    content_type = str(asset.get("mime_type") or "image/png")
    if not content_type.startswith("image/"):
        content_type = "image/png"
    return client.upload_image_blob(data, content_type=content_type)


def build_agent_edit_input(references: list[tuple[str, str]], instruction: str) -> str:
    urls = "\n".join(url for _, url in references)
    return f"{instruction.strip()}\n\n{urls}"


def run_agent_images(
    client: CoreAIClient,
    *,
    agent_id: str,
    input_text: str,
    timeout_seconds: int,
    run_id: str | None = None,
    on_run_id: Callable[[str], None] | None = None,
) -> tuple[AgentRunResult, list[AgentOutputImage]]:
    if run_id:
        result = client.wait_agent_detail(
            run_id,
            timeout_seconds=timeout_seconds,
        )
    else:
        kwargs = {"on_run_id": on_run_id} if on_run_id is not None else {}
        result = client.run_agent_detail(
            agent_id,
            input_text,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )
    downloaded = client.download_agent_images(result)
    return result, classify_agent_images(downloaded)


def classify_agent_images(images: list[DownloadedAgentImage]) -> list[AgentOutputImage]:
    transparent_index: int | None = None
    for index, image in enumerate(images):
        if _has_transparency(image.data):
            transparent_index = index
            break
    outputs: list[AgentOutputImage] = []
    if transparent_index is not None:
        layer = images[transparent_index]
        outputs.append(
            AgentOutputImage(
                data=layer.data,
                role="agent_transparent",
                label="Agent 透明 PNG",
                selectable=False,
                source_url=layer.source_url,
            )
        )
    final_indexes = [index for index in range(len(images)) if index != transparent_index]
    if not final_indexes and images:
        final_indexes = [0]
    for order, index in enumerate(final_indexes, start=1):
        image = images[index]
        outputs.append(
            AgentOutputImage(
                data=image.data,
                role="agent_result" if order == 1 else f"agent_result_{order}",
                label="Agent 最终图" if order == 1 else f"Agent 结果 {order}",
                selectable=True,
                source_url=image.source_url,
            )
        )
    return outputs


def _has_transparency(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if "A" not in image.getbands():
                return False
            alpha = image.convert("RGBA").getchannel("A")
            low, high = alpha.getextrema()
            return low < 250 and high > 0
    except Exception:
        return False


def save_png_asset(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    *,
    key_prefix: str,
    original_stem: str,
    data: bytes,
    role: str,
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
    except Exception as exc:
        raise CoreAIError("invalid_output", "Agent 返回的图片无法解码", 502) from exc

    safe_role = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in role)
    relative = storage.object_key(
        settings,
        "outputs",
        f"{key_prefix}_{safe_role}.png",
    )
    store.put_bytes(relative, png, "image/png")
    asset_id = f"asset_{uuid.uuid4().hex}"
    db.assets.insert_one(
        {
            "_id": asset_id,
            "kind": "output",
            "original_name": f"{original_stem}-{safe_role}.png",
            "path": relative,
            "prepared_path": relative,
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {
        "asset_id": asset_id,
        "path": relative,
        "width": width,
        "height": height,
    }

