from __future__ import annotations

import pytest

from app.services import material_filter_classifier as classifier
from app.services.core_ai import CoreAIError

RULES = [
    {"id": "empty_seating_scene", "text": "不能出现空座位场景", "interpretation": "只有主体是空置就餐区才违反"},
    {"id": "crab_legs", "text": "不能出现螃蟹腿", "interpretation": "整蟹上可见的腿也算"},
]


def payload(**overrides):
    value = {
        "name": "红烧肉砂锅特写",
        "category": "食物",
        "tags": ["红烧肉", "砂锅", "浓汁", "热菜"],
        "description": "一只砂锅装着红烧肉。",
        "rules": [
            {"id": "empty_seating_scene", "verdict": "pass", "reason": "画面只有菜品特写，没有餐桌椅。"},
            {"id": "crab_legs", "verdict": "pass", "reason": "未发现螃蟹或蟹腿。"},
        ],
        "decision": "pass",
    }
    value.update(overrides)
    return value


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls.append({"messages": messages, "model": model, "timeout_seconds": timeout_seconds})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_aggregate_rule_verdicts_is_strict():
    assert classifier.aggregate_rule_verdicts(["pass", "pass"]) == "pass"
    assert classifier.aggregate_rule_verdicts(["pass", "uncertain"]) == "uncertain"
    assert classifier.aggregate_rule_verdicts(["uncertain", "fail"]) == "fail"
    with pytest.raises(classifier.ClassificationError):
        classifier.aggregate_rule_verdicts([])
    with pytest.raises(classifier.ClassificationError):
        classifier.aggregate_rule_verdicts(["maybe"])


def test_messages_carry_rules_and_data_url_but_never_a_source_path():
    messages = classifier.build_messages(
        RULES, "data:image/jpeg;base64,AAAA", classifier.PROMPT_TEMPLATE
    )
    text = messages[0]["content"]
    assert "不能出现空座位场景" in text
    assert "只有主体是空置就餐区才违反" in text
    parts = messages[1]["content"]
    image_part = next(part for part in parts if part["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "\\\\192.168" not in str(messages)
    assert "smb://" not in str(messages)


def test_classify_accepts_a_food_photo_without_people_and_uses_app_aggregation():
    # The model claims "pass" overall while one rule fails: the application wins.
    body = payload(
        rules=[
            {"id": "empty_seating_scene", "verdict": "fail", "reason": "主体是整片空置餐区。"},
            {"id": "crab_legs", "verdict": "pass", "reason": "未见蟹腿。"},
        ],
        decision="pass",
    )
    client = FakeClient(body)
    result = classifier.classify_image(
        client,
        model="deepseek-v4-flash",
        rules=RULES,
        image_data_url="data:image/jpeg;base64,AAAA",
        timeout_seconds=5,
    )
    assert result.decision == "fail"
    assert result.category == "食物"
    assert client.calls[0]["model"] == "deepseek-v4-flash"
    assert client.calls[0]["timeout_seconds"] == 5


def test_classify_uncertain_goes_to_review_not_pass():
    body = payload(
        rules=[
            {"id": "empty_seating_scene", "verdict": "pass", "reason": "有人在画面主体用餐。"},
            {"id": "crab_legs", "verdict": "uncertain", "reason": "局部被遮挡，无法确认。"},
        ]
    )
    result = classifier.classify_image(
        FakeClient(body),
        model="m",
        rules=RULES,
        image_data_url="data:image/jpeg;base64,AAAA",
    )
    assert result.decision == "uncertain"
    assert [verdict.verdict for verdict in result.verdicts] == ["pass", "uncertain"]


@pytest.mark.parametrize(
    "body",
    [
        payload(rules=[{"id": "empty_seating_scene", "verdict": "pass", "reason": "ok"}]),
        payload(
            rules=[
                {"id": "empty_seating_scene", "verdict": "pass", "reason": "ok"},
                {"id": "empty_seating_scene", "verdict": "pass", "reason": "ok"},
            ]
        ),
        payload(
            rules=[
                {"id": "empty_seating_scene", "verdict": "pass", "reason": "ok"},
                {"id": "crab_legs", "verdict": "pass", "reason": ""},
            ]
        ),
        payload(
            rules=[
                {"id": "empty_seating_scene", "verdict": "maybe", "reason": "ok"},
                {"id": "crab_legs", "verdict": "pass", "reason": "ok"},
            ]
        ),
        payload(
            rules=[
                {"id": "empty_seating_scene", "verdict": "pass", "reason": "ok"},
                {"id": "crab_legs", "verdict": "pass", "reason": "ok"},
                {"id": "unknown_rule", "verdict": "pass", "reason": "ok"},
            ]
        ),
        payload(category="饮品"),
        payload(tags=["只有一个", "两个"]),
        payload(tags=["1", "2", "3", "4", "5", "6", "7"]),
        payload(tags="红烧肉"),
        payload(name=" "),
        payload(rules="pass"),
    ],
)
def test_invalid_model_results_become_technical_failures(body):
    with pytest.raises(classifier.ClassificationError):
        classifier.validate_result(body, RULES)


def test_provider_failures_are_technical_not_content_failures():
    client = FakeClient(CoreAIError("gateway_timeout", "超时，结果未知"))
    with pytest.raises(classifier.ClassificationError) as excinfo:
        classifier.classify_image(
            client, model="m", rules=RULES, image_data_url="data:image/jpeg;base64,AAAA"
        )
    assert excinfo.value.code == "gateway_timeout"

    client = FakeClient(RuntimeError("boom"))
    with pytest.raises(classifier.ClassificationError) as excinfo:
        classifier.classify_image(
            client, model="m", rules=RULES, image_data_url="data:image/jpeg;base64,AAAA"
        )
    assert excinfo.value.code == "model_request_failed"


def test_duplicate_tags_are_deduplicated_before_the_count_check():
    body = payload(tags=["红烧肉", "红烧肉", "砂锅", "浓汁"])
    result = classifier.validate_result(body, RULES)
    assert result.tags == ["红烧肉", "砂锅", "浓汁"]


def test_rule_prompt_requires_text_and_ids_are_echoed():
    with pytest.raises(classifier.ClassificationError):
        classifier.build_prompt([{"id": "x", "text": " "}])
    text = classifier.build_prompt(RULES)
    assert '"id":"empty_seating_scene"' in text
    assert '"id":"crab_legs"' in text
