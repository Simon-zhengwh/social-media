from __future__ import annotations

import hashlib
import io
import re
import uuid
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
)
from app.services.object_store import ObjectStore


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


STRONG_AUXILIARY = re.compile(
    r"蒙版|遮罩|mask|cutout|抠图|对比|comparison|before[_ -]?after|"
    r"原图|original|模板|template|贴纸|sticker|改动区域|edit-region",
    re.I,
)
# "不透明" means opaque, the opposite of a transparent layer, so only an
# un-negated 透明 counts as a transparency hint.
WEAK_AUXILIARY = re.compile(r"(?<!不)透明|前景|foreground|layer|alpha", re.I)
GENERIC_IMAGE_NAME = re.compile(r"^generated-image\.(png|jpe?g|webp)$", re.I)
FINAL_HINT = re.compile(
    r"最终|成图|成品|替换后|修改后|改字后|海报|poster|final|result|预览|preview|结果|合成",
    re.I,
)
TRANSPARENT_HINT = re.compile(r"(?<!不)透明|前景|foreground|layer|alpha", re.I)
# Tier-2 strength ranking: a real poster title beats a generic preview, and
# raw intermediate renders (e.g. a background repaint) are never tier-2 finals.
STRONG_FINAL_HINT = re.compile(
    r"海报|poster|成图|成品|最终|final|result|替换后|修改后|改字后",
    re.I,
)
WEAK_FINAL_HINT = re.compile(r"预览|preview|结果|合成", re.I)
INTERMEDIATE_HINT = re.compile(
    r"补背景|背景结果|repaint|空背景|中间|intermediate|生成结果",
    re.I,
)
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
            # A fully transparent image must still count as a layer, so only
            # the minimum alpha is relevant.
            low, _ = alpha.getextrema()
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
    described = _describe(artifact)
    if STRONG_AUXILIARY.search(described):
        return True
    # Weak markers (透明层/前景) only decide when no final hint is present, because
    # real posters are titled "…全不透明" or "替换效果预览（新背景 +原设计前景）".
    if WEAK_AUXILIARY.search(described) and not FINAL_HINT.search(described):
        return True
    title = artifact.title.strip().lower()
    return bool(GENERIC_IMAGE_NAME.match(artifact.file_name.strip())) and title in {
        "",
        "generated image",
    }


def _final_strength(artifact: AgentArtifact) -> int:
    described = _describe(artifact)
    if INTERMEDIATE_HINT.search(described):
        return 0
    if STRONG_FINAL_HINT.search(described):
        return 2
    if WEAK_FINAL_HINT.search(described):
        return 1
    return 0


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
    tier_two = [
        artifact
        for artifact in images
        if not _is_auxiliary(artifact)
        and FINAL_HINT.search(_describe(artifact))
        and _final_strength(artifact) > 0
    ]
    # Descending strength; Python's sort is stable, so equal scores keep the
    # Agent's artifact order.
    tier_two.sort(key=lambda artifact: -_final_strength(artifact))
    for artifact in tier_two:
        if artifact.download_url not in ordered:
            ordered.append(artifact.download_url)
    if ordered:
        return ordered
    candidates = [artifact for artifact in images if not _is_auxiliary(artifact)]
    if candidates:
        return [artifact.download_url for artifact in candidates]
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
