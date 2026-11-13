"""Per-image rule judgement with strict result validation.

The model only proposes per-rule verdicts, a name, a category and tags. The
overall decision is computed here so a malformed or over-confident model answer
can never turn into an automatic pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.services.core_ai import CoreAIError

CATEGORIES = ("食物", "场景", "人物", "其他")
VERDICTS = frozenset({"pass", "fail", "uncertain"})
MIN_TAGS = 3
MAX_TAGS = 6
MAX_TAG_LENGTH = 20
MAX_NAME_LENGTH = 40
PROMPT_VERSION = "material-filter-v1"

DEFAULT_INTERPRETATIONS = {
    "empty_seating_scene": (
        "只有当画面主要展示无人就座的餐桌、座椅或就餐区域，整体呈空置/待客状态时才违反。"
        "有人用餐为画面主体时，即使旁边有空桌也允许。菜品特写、门头或服务员照片不能仅因没有食客而判违规；"
        "背景偶然出现空椅也不违规。"
    ),
    "crab_legs": (
        "画面中可辨认的螃蟹腿（包括整蟹上可见的腿）属于违反；不能把其他食材或虾腿直接当成螃蟹腿。"
        "疑似但无法辨认时标 uncertain，不能假装确定。"
    ),
}

PROMPT_TEMPLATE = """你是餐饮摄影素材库的图片初筛员。必须真正依据图片，不得臆测不可见内容。返回一个中文JSON对象，不要markdown。
本次筛选要求：
{rules}
每条规则分别给pass/fail/uncertain和简短可见证据。总体：任一fail则fail；无fail但有uncertain则uncertain；全部pass才pass。不要用置信分数代替判断。
同时为所有图片（包括淘汰图片）生成：name（简洁中文描述名，8-20字左右，不含文件扩展名）；category（严格为食物/场景/人物/其他之一）；tags（3-6个简洁可复用的中文标签）；description（实际画面的一句话描述）。画面主体为菜品饮品归食物，餐厅内外景或用餐环境归场景，人物肖像/合影/服务动作归人物；认不准的菜名和食材用概括词，不要编造品牌、地址或人名。标签只描述看得见的内容，不要把“通过”“无螃蟹腿”等判定写成内容标签。
严格JSON结构：{{"name":"...","category":"...","tags":["..."],"description":"...","rules":[{rule_shape}],"decision":"pass|fail|uncertain"}}"""


class ClassificationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RuleVerdict:
    id: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class ClassificationResult:
    name: str
    category: str
    tags: list[str]
    description: str
    verdicts: list[RuleVerdict]
    decision: str
    token_usage: dict[str, Any]
    naming: dict[str, Any] | None = None


def aggregate_rule_verdicts(verdicts: list[str]) -> str:
    if not verdicts or any(value not in VERDICTS for value in verdicts):
        raise ClassificationError("invalid_rule_verdicts", "规则判断缺失或状态非法")
    if "fail" in verdicts:
        return "fail"
    if "uncertain" in verdicts:
        return "uncertain"
    return "pass"


def build_prompt(rules: list[dict[str, Any]], template: str = PROMPT_TEMPLATE) -> str:
    if not rules:
        raise ClassificationError("rules_required", "筛选方案至少需要一条规则")
    lines = []
    for index, rule in enumerate(rules, start=1):
        text = str(rule.get("text") or "").strip()
        if not text:
            raise ClassificationError("rule_text_required", "筛选规则内容不能为空")
        interpretation = str(rule.get("interpretation") or "").strip()
        lines.append(f"{index}. {text}" + (f"（{interpretation}）" if interpretation else ""))
    shape = ",".join(
        f'{{"id":"{str(rule.get("id") or "")}","verdict":"pass|fail|uncertain","reason":"..."}}'
        for rule in rules
    )
    try:
        return template.format(rules="\n".join(lines), rule_shape=shape)
    except (KeyError, IndexError) as exc:  # pragma: no cover - guarded template
        raise ClassificationError("prompt_template_invalid", "提示词模板缺少占位符") from exc


def build_messages(rules: list[dict[str, Any]], image_data_url: str, template: str = PROMPT_TEMPLATE) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": build_prompt(rules, template)},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请判断这张图片，并对每条规则分别给出结论。"},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]


def validate_result(payload: dict[str, Any], rules: list[dict[str, Any]]) -> ClassificationResult:
    if not isinstance(payload, dict):
        raise ClassificationError("invalid_result", "模型返回内容不是对象")
    model_rules = payload.get("rules")
    if not isinstance(model_rules, list):
        raise ClassificationError("invalid_result", "模型没有返回逐条规则判断")
    expected_ids = [str(rule.get("id") or "") for rule in rules]
    if len(set(expected_ids)) != len(expected_ids) or any(not value for value in expected_ids):
        raise ClassificationError("invalid_scheme_rules", "方案规则 ID 缺失或重复")

    verdicts: list[RuleVerdict] = []
    seen: list[str] = []
    for item in model_rules:
        if not isinstance(item, dict):
            raise ClassificationError("invalid_result", "规则判断格式非法")
        rule_id = str(item.get("id") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if rule_id not in expected_ids:
            raise ClassificationError("invalid_result", f"模型返回了未知规则：{rule_id or '(空)'}")
        if verdict not in VERDICTS:
            raise ClassificationError("invalid_result", f"规则 {rule_id} 的判断状态非法")
        if not reason:
            raise ClassificationError("invalid_result", f"规则 {rule_id} 缺少可见证据说明")
        seen.append(rule_id)
        verdicts.append(RuleVerdict(id=rule_id, verdict=verdict, reason=reason))
    if len(set(seen)) != len(seen):
        raise ClassificationError("invalid_result", "模型重复返回了同一条规则")
    if set(seen) != set(expected_ids):
        raise ClassificationError("invalid_result", "模型没有覆盖方案中的全部规则")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ClassificationError("invalid_result", "模型没有返回图片名称")
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH].strip()

    category = str(payload.get("category") or "").strip()
    if category not in CATEGORIES:
        raise ClassificationError("invalid_result", "模型返回的主分类不在固定分类中")

    tags: list[str] = []
    for tag in _string_list(payload.get("tags")):
        if len(tag) > MAX_TAG_LENGTH:
            raise ClassificationError("invalid_result", "模型返回的标签过长")
        if tag not in tags:
            tags.append(tag)
    if not MIN_TAGS <= len(tags) <= MAX_TAGS:
        raise ClassificationError("invalid_result", "模型返回的标签数量不在 3–6 个之间")

    description = str(payload.get("description") or "").strip()
    decision = aggregate_rule_verdicts([item.verdict for item in verdicts])

    usage = payload.get("_token_usage")
    return ClassificationResult(
        name=name,
        category=category,
        tags=tags,
        description=description,
        verdicts=verdicts,
        decision=decision,
        token_usage=usage if isinstance(usage, dict) else {},
    )


def classify_image(
    client,
    *,
    model: str,
    rules: list[dict[str, Any]],
    image_data_url: str,
    prompt_template: str = PROMPT_TEMPLATE,
    timeout_seconds: int | None = None,
) -> ClassificationResult:
    """One image, one request. Never retried here: callers decide on explicit retry."""
    messages = build_messages(rules, image_data_url, prompt_template)
    try:
        payload = client.chat_json(
            messages,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    except CoreAIError as exc:
        raise ClassificationError(exc.code, exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - provider failures stay technical failures
        raise ClassificationError("model_request_failed", f"模型请求失败：{exc}") from exc
    return validate_result(payload, rules)


def parse_model_payload(text: str) -> dict[str, Any]:  # pragma: no cover - test helper
    return json.loads(text)
