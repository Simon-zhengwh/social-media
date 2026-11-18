import pytest

from app.services import asset_recognition


def payload(keywords):
    return {"keywords": keywords}


def test_keywords_do_not_require_descriptions_and_keep_deduplicated_phrases():
    result = asset_recognition.validate_keywords(payload({
        "zh": [" 牛肉 ", "牛肉", "白盘"],
        "en": [" sliced beef ", "Sliced Beef", "white plate"],
    }))

    assert result.keywords == {"zh": ["牛肉", "白盘"], "en": ["sliced beef", "white plate"]}


@pytest.mark.parametrize("keywords", [
    None,
    {"zh": ["牛肉"]},
    {"zh": "牛肉", "en": ["beef"]},
    {"zh": ["牛肉"], "en": []},
    {"zh": ["牛肉"], "en": [123]},
    {"zh": ["牛肉"], "en": ["x" * 81]},
])
def test_rejects_invalid_bilingual_keywords(keywords):
    with pytest.raises(asset_recognition.RecognitionError, match="关键词"):
        asset_recognition.validate_keywords(payload(keywords))


def test_keyword_limit_is_twenty_per_language():
    with pytest.raises(asset_recognition.RecognitionError, match="关键词"):
        asset_recognition.validate_keywords(payload({"zh": ["牛肉"], "en": [f"term {i}" for i in range(21)]}))
    result = asset_recognition.validate_keywords(payload({"zh": ["牛肉"], "en": [f"term {i}" for i in range(20)]}))
    assert len(result.keywords["en"]) == 20


def test_keyword_request_prioritizes_latest_name_over_conflicting_tags():
    messages = asset_recognition.keywords_messages("https://example.com/image.png", "牛排.png", ["猪排", "白盘"])
    assert "菜品身份以当前名称为准" in messages[0]["content"]
    assert "不得输出猪排" in messages[0]["content"]
    assert "描述" not in messages[0]["content"]
    assert "当前名称：牛排.png；当前标签：猪排、白盘" in messages[1]["content"][0]["text"]
