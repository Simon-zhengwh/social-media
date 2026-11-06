from __future__ import annotations

import hashlib
import io
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.config import Settings
from app.services import storage
from app.services.core_ai import (
    AgentArtifact,
    AgentRunResult,
    CoreAIClient,
    CoreAIError,
    DownloadedAgentImage,
)
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
    use_original: bool = False,
) -> str:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise CoreAIError("reference_missing", "参考图片不存在", 422)
    key = asset.get("path") if use_original else asset.get("prepared_path") or asset.get("path")
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
    require_aspect_match: bool = False,
) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            if target_size is not None and require_aspect_match:
                target_width, target_height = target_size
                target_ratio = target_width / target_height
                source_ratio = image.width / image.height
                if abs(source_ratio - target_ratio) / target_ratio > 0.02:
                    raise CoreAIError(
                        "output_aspect_mismatch",
                        "Agent 返回画幅与当前海报不一致："
                        f"{image.width}×{image.height}，需要 "
                        f"{target_width}×{target_height}。请重新修改。",
                        502,
                    )
            if target_size is not None:
                image = ImageOps.pad(
                    image,
                    target_size,
                    method=Image.Resampling.LANCZOS,
                    color=(0, 0, 0, 0) if image.mode == "RGBA" else "white",
                    centering=(0.5, 0.5),
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
            width, height = image.size
    except CoreAIError:
        raise
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


AUXILIARY_IMAGE = re.compile(
    r"透明|前景|蒙版|遮罩|mask|cutout|抠图|对比|comparison|before[_ -]?after|"
    r"原图|original|模板|template|贴纸|sticker|改动区域|edit-region",
    re.I,
)
GENERIC_IMAGE_NAME = re.compile(r"^generated-image\.(png|jpe?g|webp)$", re.I)
FINAL_HINT = re.compile(
    r"最终|成图|成品|替换后|修改后|改字后|海报|poster|final|result|预览|preview",
    re.I,
)
TRANSPARENT_HINT = re.compile(r"透明|前景|foreground|layer|alpha", re.I)
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")

ARTIFACT_KINDS = {"text/html": "html", "application/zip": "bundle"}
ARTIFACT_SUFFIXES = {
    "text/html": ".html",
    "application/zip": ".zip",
    "application/json": ".json",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


def artifact_suffix(content_type: str) -> str:
    return ARTIFACT_SUFFIXES.get(content_type.split(";")[0].strip().lower(), ".bin")


def has_transparency(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if "A" not in image.getbands():
                return False
            alpha = image.convert("RGBA").getchannel("A")
            low, _high = alpha.getextrema()
            return low < 250
    except Exception:
        return False


def _image_artifacts(result: AgentRunResult) -> list[AgentArtifact]:
    return [
        artifact
        for artifact in result.artifacts
        if artifact.content_type.split(";")[0].strip().lower().startswith("image/")
    ]


def _describe(artifact: AgentArtifact) -> str:
    return f"{artifact.title} {artifact.file_name}".strip()


def _is_auxiliary(artifact: AgentArtifact) -> bool:
    if AUXILIARY_IMAGE.search(_describe(artifact)):
        return True
    title = artifact.title.strip().lower()
    return bool(GENERIC_IMAGE_NAME.match(artifact.file_name.strip())) and title in {
        "",
        "generated image",
    }


def pick_final_image_urls(result: AgentRunResult) -> list[str]:
    images = _image_artifacts(result)
    by_url = {artifact.download_url: artifact for artifact in images}
    ordered: list[str] = []
    for url in MARKDOWN_IMAGE.findall(result.output or ""):
        artifact = by_url.get(url)
        if artifact is None or _is_auxiliary(artifact):
            continue
        if url not in ordered:
            ordered.append(url)
    if ordered:
        return ordered
    for artifact in images:
        if not _is_auxiliary(artifact) and FINAL_HINT.search(_describe(artifact)):
            if artifact.download_url not in ordered:
                ordered.append(artifact.download_url)
    if ordered:
        return ordered
    return [artifact.download_url for artifact in images]


def pick_transparent_image_url(
    result: AgentRunResult, *, excluded: list[str]
) -> str | None:
    candidates = [
        artifact
        for artifact in _image_artifacts(result)
        if artifact.download_url not in excluded
    ]
    for artifact in candidates:
        if TRANSPARENT_HINT.search(_describe(artifact)):
            return artifact.download_url
    # Titles are not always descriptive; the caller validates the bytes with
    # has_transparency() before using this layer, so the first remaining
    # candidate is a safe fallback.
    return candidates[0].download_url if candidates else None


def pick_html_artifact(result: AgentRunResult) -> AgentArtifact | None:
    for artifact in result.artifacts:
        if artifact.content_type.split(";")[0].strip().lower() == "text/html":
            return artifact
        if artifact.file_name.lower().endswith((".html", ".htm")):
            return artifact
    return None


def pick_bundle_artifact(result: AgentRunResult) -> AgentArtifact | None:
    for artifact in result.artifacts:
        if artifact.content_type.split(";")[0].strip().lower() == "application/zip":
            return artifact
    return None


def save_agent_artifact(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    version: int,
    artifact: AgentArtifact,
    data: bytes,
) -> dict[str, Any]:
    content_type = (
        artifact.content_type.split(";")[0].strip().lower()
        or "application/octet-stream"
    )
    suffix = artifact_suffix(content_type)
    artifact_id = f"wart_{uuid.uuid4().hex}"
    relative = storage.object_key(
        settings, "artifacts", f"{task_id}_v{version}_{artifact_id}{suffix}"
    )
    store.put_bytes(relative, data, content_type)
    file_name = Path(artifact.file_name or f"agent-artifact{suffix}").name
    if not file_name.lower().endswith(suffix):
        file_name = f"{Path(file_name).stem or 'agent-artifact'}{suffix}"
    db.whole_artifacts.insert_one(
        {
            "_id": artifact_id,
            "task_id": task_id,
            "version": version,
            "kind": ARTIFACT_KINDS.get(content_type, "file"),
            "file_name": file_name,
            "content_type": content_type,
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "title": artifact.title,
            "source_url": artifact.download_url,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {
        "id": artifact_id,
        "kind": ARTIFACT_KINDS.get(content_type, "file"),
        "file_name": file_name,
        "content_type": content_type,
        "size": len(data),
        "title": artifact.title,
    }
