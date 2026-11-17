"""One-image metadata recognition: name, flat tags, Chinese/English descriptions.

The model only describes what it can see. Everything it returns is validated here
before any asset field is written, so a malformed answer can never blank out a name
or a tag that already exists. Language equivalence (simplified/traditional/English)
is not handled in this module: only the query agent produces it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.core_ai import CoreAIError

PROMPT_VERSION = "asset-recognition-v1"
DESCRIPTION_PROMPT_VERSION = "asset-description-v1"

IMAGE_NAME_MAX = 40
TAG_MIN = 3
TAG_MAX = 5
TAG_MAX_LENGTH = 40
DESCRIPTION_ZH_MAX = 140
DESCRIPTION_EN_MAX = 320

IMAGE_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif",
})

RECOGNITION_PROMPT = """你是餐饮品牌素材库的图片标注员。只依据图片里看得见的内容回答，不要猜测看不清的菜名、食材、品牌、人名或地址。返回一个JSON对象，不要markdown，不要解释。
输出字段：
- name：简洁中文名称，2-20个汉字，说明画面主体。认不准的具体菜名用概括词（例如「肉类菜品特写」）。不要带文件扩展名，不要写序号或文件名。
- tags：3-5个平级的内容标签，每个不超过20字。只写看得见的内容，不要写用途、适用场景、价格或判定结论，也不要重复名称。
- description_zh：一句中文画面描述，只写有依据的信息，不写「适合火锅」这类用途联想。
- description_en：与 description_zh 对应的英文描述，简短，只写同样的事实。
严格JSON：{"name":"...","tags":["...","...","..."],"description_zh":"...","description_en":"..."}"""

DESCRIPTION_PROMPT = """你是餐饮品牌素材库的图片标注员。只依据图片里看得见的内容回答，不要猜测看不清的菜名、食材、品牌、人名或地址。返回一个JSON对象，不要markdown，不要解释。
只补充中英文描述，不要修改名称和标签。名称和标签只用来理解画面，不能凭空写入描述里没有的内容。
输出字段：
- description_zh：一句中文画面描述，只写有依据的信息，不写用途联想。
- description_en：与 description_zh 对应的英文描述，简短，只写同样的事实。
严格JSON：{"description_zh":"...","description_en":"..."}"""


class RecognitionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RecognitionResult:
    name: str
    tags: list[str]
    description_zh: str
    description_en: str
    token_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DescriptionResult:
    description_zh: str
    description_en: str
    token_usage: dict[str, Any] = field(default_factory=dict)


def recognition_messages(image_url: str, original_name: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": RECOGNITION_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"原文件名：{original_name or '(无)'}。请为这张素材生成名称、标签和中英文描述。"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def description_messages(
    image_url: str, name: str, tags: list[str]
) -> list[dict[str, Any]]:
    listed = "、".join(tags) if tags else "(无)"
    return [
        {"role": "system", "content": DESCRIPTION_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"当前名称：{name or '(无)'}；当前标签：{listed}。请只补充中英文描述。"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def strip_image_suffix(name: str) -> str:
    value = name.strip()
    if "." in value:
        stem, _, suffix = value.rpartition(".")
        if suffix.strip().lower() in IMAGE_SUFFIXES and stem.strip():
            return stem.strip()
    return value


def name_with_extension(name: str, original_name: str) -> str:
    """The stored name keeps the original file extension; only the display stem changes."""
    stem = strip_image_suffix(name)[:IMAGE_NAME_MAX].strip()
    if not stem:
        raise RecognitionError("invalid_result", "模型没有返回可用的素材名称")
    original = str(original_name or "").strip()
    if "." not in original:
        return stem
    _before, _, suffix = original.rpartition(".")
    if not suffix.strip():
        return stem
    return f"{stem}.{suffix.strip()}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise RecognitionError("invalid_result", "模型没有返回标签数组")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RecognitionError("invalid_result", "模型返回的标签不是文本")
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > TAG_MAX_LENGTH:
            raise RecognitionError("invalid_result", f"模型返回的标签超过 {TAG_MAX_LENGTH} 字")
        if any(existing.lower() == tag.lower() for existing in tags):
            continue
        tags.append(tag)
    if not TAG_MIN <= len(tags) <= TAG_MAX:
        raise RecognitionError("invalid_result", f"模型返回的标签数量不在 {TAG_MIN}–{TAG_MAX} 个之间")
    return tags


def _description(value: Any) -> str:
    text = _text(value)
    if not text:
        raise RecognitionError("invalid_result", "模型没有返回描述")
    return text


def validate_recognition(payload: dict[str, Any]) -> RecognitionResult:
    if not isinstance(payload, dict):
        raise RecognitionError("invalid_result", "模型返回内容不是对象")
    name = strip_image_suffix(_text(payload.get("name")))
    if not name:
        raise RecognitionError("invalid_result", "模型没有返回素材名称")
    if len(name) > IMAGE_NAME_MAX:
        name = name[:IMAGE_NAME_MAX].strip()
    tags = _tags(payload.get("tags"))
    description_zh = _description(payload.get("description_zh"))
    if len(description_zh) > DESCRIPTION_ZH_MAX:
        raise RecognitionError("invalid_result", "中文描述过长")
    description_en = _description(payload.get("description_en"))
    if len(description_en) > DESCRIPTION_EN_MAX:
        raise RecognitionError("invalid_result", "英文描述过长")
    if not re.search(r"[A-Za-z]", description_en):
        raise RecognitionError("invalid_result", "英文描述不是英文文本")
    usage = payload.get("_token_usage")
    return RecognitionResult(
        name=name,
        tags=tags,
        description_zh=description_zh,
        description_en=description_en,
        token_usage=usage if isinstance(usage, dict) else {},
    )


def validate_description(payload: dict[str, Any]) -> DescriptionResult:
    if not isinstance(payload, dict):
        raise RecognitionError("invalid_result", "模型返回内容不是对象")
    description_zh = _description(payload.get("description_zh"))
    if len(description_zh) > DESCRIPTION_ZH_MAX:
        raise RecognitionError("invalid_result", "中文描述过长")
    description_en = _description(payload.get("description_en"))
    if len(description_en) > DESCRIPTION_EN_MAX:
        raise RecognitionError("invalid_result", "英文描述过长")
    if not re.search(r"[A-Za-z]", description_en):
        raise RecognitionError("invalid_result", "英文描述不是英文文本")
    usage = payload.get("_token_usage")
    return DescriptionResult(
        description_zh=description_zh,
        description_en=description_en,
        token_usage=usage if isinstance(usage, dict) else {},
    )


def _chat(client, messages: list[dict[str, Any]], *, model: str, timeout_seconds: int) -> dict[str, Any]:
    try:
        payload = client.chat_json(messages, model=model, timeout_seconds=timeout_seconds)
    except CoreAIError as exc:
        raise RecognitionError(exc.code, exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - provider failures stay technical failures
        raise RecognitionError("model_request_failed", f"模型请求失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RecognitionError("invalid_result", "模型返回内容不是对象")
    return payload


def recognize(
    client,
    *,
    model: str,
    image_url: str,
    original_name: str,
    timeout_seconds: int,
) -> RecognitionResult:
    """One image, one request. Retries belong to the caller, never to this function."""
    payload = _chat(
        client,
        recognition_messages(image_url, original_name),
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return validate_recognition(payload)


def describe(
    client,
    *,
    model: str,
    image_url: str,
    name: str,
    tags: list[str],
    timeout_seconds: int,
) -> DescriptionResult:
    payload = _chat(
        client,
        description_messages(image_url, name, tags),
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return validate_description(payload)
