"""Bilingual asset keywords from the current name/tags and visible image details.

All model output is validated before writing metadata. Current dish names take
precedence over a visual guess or a conflicting old tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.core_ai import CoreAIError

PROMPT_VERSION = "asset-recognition-v4"
KEYWORDS_PROMPT_VERSION = "asset-keywords-v3"

IMAGE_NAME_MAX = 40
TAG_MIN = 3
TAG_MAX = 5
TAG_MAX_LENGTH = 40
KEYWORDS_MAX = 20
KEYWORD_MAX_LENGTH = 80

IMAGE_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif",
})

KEYWORD_NAMING_RULES = """词语原则：
- 使用自然、准确的中英文词语，保留完整短语，不为匹配而删除有语义的限定词。
- 具体菜品、部位查询保持具体含义，不能改成宽泛类别。
- 素材关键词可以同时收录具体品类及当前名称、标签明确确认的基础类别，不能添加未确认或与当前名称冲突的内容。"""

RECOGNITION_PROMPT = """你是餐饮品牌素材库的图片标注员。根据当前名称、标签和图片生成素材元数据。名称包含明确菜名时，菜品身份以当前名称为准，不得因视觉猜测改为别的肉类或菜品；标签与名称冲突时以名称为准。名称只是序号或无意义文件名时才依据画面识别。返回一个JSON对象，不要markdown，不要解释。
输出字段：
- name：简洁中文名称，2-20个汉字，说明画面主体。认不准的具体菜名用概括词（例如「肉类菜品特写」）。不要带文件扩展名，不要写序号或文件名。
- tags：3-5个平级的内容标签，每个不超过20字。只写看得见的内容，不要写用途、适用场景、价格或判定结论，也不要重复名称。
- keywords：对象，zh 是中文关键词数组，en 是对应的英文关键词数组。收录可确认的画面主体、类型、食材、器皿、颜色和拍摄方式，以及输出 name、tags 中的内容词及其可靠译名；不带文件扩展名，不猜测用途或关联食材。
- 每种语言最多20个关键词，每个不超过80字符，去重，按内容多少返回，不强制凑满。保留 hot pot、dan dan noodles 等完整短语，不要拆成单个英文词。两种语言都至少返回一个词语。
严格JSON：{"name":"...","tags":["...","...","..."],"keywords":{"zh":["..."],"en":["..."]}}""" + "\n" + KEYWORD_NAMING_RULES

KEYWORDS_PROMPT = """你是餐饮品牌素材库的关键词标注员。根据素材当前的名称、完整标签和图片生成中英文关键词，不要修改名称和标签。返回一个JSON对象，不要markdown，不要解释。
依据优先级：
- 当前名称和标签是运营确认的最新元数据。菜品身份以当前名称为准，包括菜名、肉类和主要食材；标签与名称冲突时，以当前名称为准，不收录冲突标签的菜品身份。
- 例如名称从「猪排」更新为「牛排」后，关键词应使用牛排、beef steak、beef 等，不得输出猪排、pork chop、pork，即使旧标签或画面让你猜测是猪排。
- 图片用于补充颜色、器皿、摆放、拍摄方式等可见细节。名称和标签未提供明确身份时才按画面识别，不确定的身份用概括词，不猜测用途、关联食材、品牌、人名或地址。
输出字段：
- keywords：对象，zh 是中文关键词数组，en 是对应的英文关键词数组。优先收录当前名称、标签中的有效内容词及其可靠译名，再补充可确认的画面细节。保留有效品牌或自定义标签，不带文件扩展名。
- 每种语言最多20个关键词，每个不超过80字符，去重，按内容多少返回，不强制凑满。保留 hot pot、dan dan noodles 等完整短语，不要拆成单个英文词。两种语言都至少返回一个词语。
严格JSON：{"keywords":{"zh":["..."],"en":["..."]}}""" + "\n" + KEYWORD_NAMING_RULES


class RecognitionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RecognitionResult:
    name: str
    tags: list[str]
    keywords: dict[str, list[str]]
    token_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeywordsResult:
    keywords: dict[str, list[str]]
    token_usage: dict[str, Any] = field(default_factory=dict)


def recognition_messages(image_url: str, original_name: str, tags: list[str] | None = None) -> list[dict[str, Any]]:
    listed = "、".join(tags or []) or "(无)"
    return [
        {"role": "system", "content": RECOGNITION_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"当前名称：{original_name or '(无)'}；当前标签：{listed}。请为这张素材生成名称、标签和中英文关键词。"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def keywords_messages(
    image_url: str, name: str, tags: list[str]
) -> list[dict[str, Any]]:
    listed = "、".join(tags) if tags else "(无)"
    return [
        {"role": "system", "content": KEYWORDS_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"当前名称：{name or '(无)'}；当前标签：{listed}。请生成中英文关键词。"},
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


def _keywords(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise RecognitionError("invalid_result", "模型没有返回中英文关键词对象")
    result: dict[str, list[str]] = {}
    for language in ("zh", "en"):
        items = value.get(language)
        if not isinstance(items, list):
            raise RecognitionError("invalid_result", f"模型返回的 {language} 关键词不是数组")
        terms: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, str):
                raise RecognitionError("invalid_result", "模型返回的关键词不是文本")
            term = item.strip()
            if len(term) > KEYWORD_MAX_LENGTH:
                raise RecognitionError("invalid_result", f"模型返回的关键词超过 {KEYWORD_MAX_LENGTH} 字符")
            key = term.casefold()
            if key and key not in seen:
                terms.append(term)
                seen.add(key)
        if not terms or len(terms) > KEYWORDS_MAX:
            raise RecognitionError("invalid_result", f"模型返回的 {language} 关键词数量不在 1–{KEYWORDS_MAX} 个之间")
        result[language] = terms
    return result


def validate_recognition(payload: dict[str, Any]) -> RecognitionResult:
    if not isinstance(payload, dict):
        raise RecognitionError("invalid_result", "模型返回内容不是对象")
    name = strip_image_suffix(_text(payload.get("name")))
    if not name:
        raise RecognitionError("invalid_result", "模型没有返回素材名称")
    if len(name) > IMAGE_NAME_MAX:
        name = name[:IMAGE_NAME_MAX].strip()
    tags = _tags(payload.get("tags"))
    usage = payload.get("_token_usage")
    return RecognitionResult(
        name=name,
        tags=tags,
        keywords=_keywords(payload.get("keywords")),
        token_usage=usage if isinstance(usage, dict) else {},
    )


def validate_keywords(payload: dict[str, Any]) -> KeywordsResult:
    if not isinstance(payload, dict):
        raise RecognitionError("invalid_result", "模型返回内容不是对象")
    usage = payload.get("_token_usage")
    return KeywordsResult(
        keywords=_keywords(payload.get("keywords")),
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
    original_tags: list[str] | None = None,
) -> RecognitionResult:
    """One image, one request. Retries belong to the caller, never to this function."""
    payload = _chat(
        client,
        recognition_messages(image_url, original_name, original_tags),
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return validate_recognition(payload)


def generate_keywords(
    client,
    *,
    model: str,
    image_url: str,
    name: str,
    tags: list[str],
    timeout_seconds: int,
) -> KeywordsResult:
    payload = _chat(
        client,
        keywords_messages(image_url, name, tags),
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return validate_keywords(payload)
