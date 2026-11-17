"""Query vocabulary and local matching for the asset library search box.

The model is only asked to translate/extend the operator's words: simplified,
traditional and English equivalents count as the original condition, extra words count
as expansions that rank lower. Matching itself is plain substring matching over the
indexed name, tags and both descriptions, so paging never re-runs a model and no local
language conversion happens here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.services import asset_index
from app.api_paths import asset_url
from app.services.asset_index import AssetIndexError
from app.services.core_ai import CoreAIError

PROMPT_VERSION = "asset-query-v1"
MODE_LITERAL = "literal"
MODE_SEMANTIC = "semantic"
MAX_EXPANSIONS = 8
MAX_TERM_LENGTH = 40
MAX_QUERY_LENGTH = 200
TIER_ORIGINAL = "original"
TIER_EXPANDED = "expanded"

QUERY_PROMPT = """你是餐饮素材库的搜索词助手。用户输入一组搜索条件，你要为每个条件给出等价表达和扩展词。只返回JSON，不要markdown，不要解释。
规则：
- 按用户的意思分组：同一个对象分词时保持一组的直接表达（例如英文短语「dan dan noodles」是一组），不同条件各成一组。
- 每个分组必须有 input 字段，内容是用户原输入里连续的一段文字，不要改写。
- equivalents：该条件在简体中文 simplified、繁体中文 traditional、英文 english 的等价表达，必须指同一个对象。无法翻译时重复原词，不要用相关词代替。
- expansions：0-3个同义表达或更具体的品类（例如牛肉可扩牛排、肥牛），不要写宽泛类别、关联食材或用途。
- 所有分组的 expansions 合计最多 8 个。
严格JSON：{"groups":[{"input":"...","equivalents":{"simplified":"...","traditional":"...","english":"..."},"expansions":["..."]}]}"""


def split_tokens(query: str) -> list[str]:
    return [token for token in re.split(r"\s+", query.strip()) if token]


def literal_groups(query: str) -> list[dict[str, Any]]:
    """No model, no translation: the typed text is the whole vocabulary."""
    text = query.strip()
    if not text:
        return []
    return [
        {
            "input": text,
            "equivalents": {"simplified": text, "traditional": text, "english": text},
            "expansions": [],
            "original_terms": [text],
            "expansion_terms": [],
        }
    ]


def query_messages(query: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": QUERY_PROMPT},
        {"role": "user", "content": f"搜索条件：{query}"},
    ]


def _clean(value: Any) -> str:
    return str(value or "").strip()[:MAX_TERM_LENGTH]


def _term_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    terms: list[str] = []
    for item in value:
        term = _clean(item)
        if term:
            terms.append(term)
    return terms


def _dedupe(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        key = term.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def _token_spans(tokens: list[str], phrase: str) -> list[tuple[int, int]]:
    """Where a phrase matches a contiguous run of query tokens."""
    wanted = split_tokens(phrase)
    if not wanted:
        return []
    spans: list[tuple[int, int]] = []
    size = len(wanted)
    for start in range(0, len(tokens) - size + 1):
        if [token.casefold() for token in tokens[start : start + size]] == [
            token.casefold() for token in wanted
        ]:
            spans.append((start, start + size))
    return spans


def validate_vocabulary(payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Structural checks only: grouping may come from the model, terms may not be invented."""
    tokens = split_tokens(query)
    if not tokens:
        return []
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise AssetIndexError("invalid_vocabulary", "模型没有返回搜索词分组")
    covered = [False] * len(tokens)
    groups: list[dict[str, Any]] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            raise AssetIndexError("invalid_vocabulary", "搜索词分组格式非法")
        phrase = str(item.get("input") or "").strip()
        if not phrase:
            raise AssetIndexError("invalid_vocabulary", "模型返回的分组缺少原输入")
        spans = [
            span
            for span in _token_spans(tokens, phrase)
            if not any(covered[index] for index in range(*span))
        ]
        if not spans:
            raise AssetIndexError("invalid_vocabulary", "模型返回的分组不是用户输入的连续片段")
        # The operator's own words win: use the exact slice that was typed.
        start, end = spans[0]
        for index in range(start, end):
            covered[index] = True
        original_input = " ".join(tokens[start:end])
        equivalents = item.get("equivalents")
        if not isinstance(equivalents, dict):
            raise AssetIndexError("invalid_vocabulary", "模型返回的分组缺少等价表达")
        simplified = _clean(equivalents.get("simplified"))
        traditional = _clean(equivalents.get("traditional"))
        english = _clean(equivalents.get("english"))
        if not simplified or not traditional or not english:
            raise AssetIndexError("invalid_vocabulary", "等价表达不能为空")
        original_terms = _dedupe([original_input, simplified, traditional, english])
        expansions = _dedupe(_term_list(item.get("expansions")))
        known = {term.casefold() for term in original_terms}
        expansions = [term for term in expansions if term.casefold() not in known]
        groups.append(
            {
                "input": original_input,
                "equivalents": {
                    "simplified": simplified,
                    "traditional": traditional,
                    "english": english,
                },
                "expansions": expansions,
                "original_terms": original_terms,
                "expansion_terms": expansions,
            }
        )
    if not all(covered):
        raise AssetIndexError("invalid_vocabulary", "模型遗漏了部分搜索条件")
    budget = MAX_EXPANSIONS
    for group in groups:
        allowed = group["expansion_terms"][:budget]
        budget -= len(allowed)
        group["expansion_terms"] = allowed
        group["expansions"] = allowed
    return groups


def build_vocabulary(
    client, *, query: str, model: str, timeout_seconds: int
) -> list[dict[str, Any]]:
    text = query.strip()
    if len(text) > MAX_QUERY_LENGTH:
        raise AssetIndexError("query_too_long", f"搜索条件最长 {MAX_QUERY_LENGTH} 个字符", 422)
    if not text:
        return []
    try:
        payload = client.chat_json(
            query_messages(text), model=model, timeout_seconds=timeout_seconds
        )
    except CoreAIError as exc:
        raise AssetIndexError("vocabulary_failed", exc.message, 502) from exc
    except Exception as exc:  # noqa: BLE001 - provider failures degrade to the typed words
        raise AssetIndexError("vocabulary_failed", f"模型请求失败：{exc}", 502) from exc
    if not isinstance(payload, dict):
        raise AssetIndexError("invalid_vocabulary", "模型返回内容不是对象")
    return validate_vocabulary(payload, text)


def resolve_query(
    db: Any,
    *,
    client,
    settings,
    query: str,
    semantic: bool,
) -> dict[str, Any]:
    """Build and remember the vocabulary of one submitted search."""
    text = query.strip()
    if not semantic or not text:
        return asset_index.save_query(
            db,
            query=text,
            mode=MODE_LITERAL,
            groups=literal_groups(text),
            model="",
            prompt_version="",
        )
    try:
        groups = build_vocabulary(
            client,
            query=text,
            model=settings.asset_search_model_name,
            timeout_seconds=settings.asset_search_timeout_seconds,
        )
    except AssetIndexError as exc:
        # Never retry with a repair prompt: fall back to the typed words and say so.
        return asset_index.save_query(
            db,
            query=text,
            mode=MODE_LITERAL,
            groups=literal_groups(text),
            degraded=True,
            degraded_reason=exc.message,
            model=settings.asset_search_model_name,
            prompt_version=PROMPT_VERSION,
        )
    return asset_index.save_query(
        db,
        query=text,
        mode=MODE_SEMANTIC,
        groups=groups,
        model=settings.asset_search_model_name,
        prompt_version=PROMPT_VERSION,
    )


def _haystack(record: dict[str, Any]) -> list[str]:
    values = [str(record.get("name") or "")]
    values.extend(str(tag) for tag in record.get("tags") or [])
    values.append(str(record.get("description_zh") or ""))
    values.append(str(record.get("description_en") or ""))
    return [value.casefold() for value in values if value]


def _hits(haystack: list[str], terms: list[str]) -> list[str]:
    matched: list[str] = []
    for term in terms:
        needle = term.casefold()
        if any(needle in value for value in haystack):
            matched.append(term)
    return matched


def rank_records(
    records: list[dict[str, Any]], groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """AND between conditions, OR inside one condition; original hits rank first."""
    if not groups:
        return sorted(records, key=_sort_key)
    ranked: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        haystack = _haystack(record)
        matched_original: list[str] = []
        satisfied = True
        for group in groups:
            originals = _hits(haystack, group.get("original_terms") or [])
            expansions = _hits(haystack, group.get("expansion_terms") or [])
            if not originals and not expansions:
                satisfied = False
                break
            matched_original.extend(originals)
        if not satisfied:
            continue
        tier = TIER_ORIGINAL if matched_original else TIER_EXPANDED
        ranked.append(
            (
                tier,
                {
                    **record,
                    "match_tier": tier,
                    "matched_terms": _dedupe(matched_original),
                },
            )
        )
    ordered = sorted(
        ranked, key=lambda item: (0 if item[0] == TIER_ORIGINAL else 1, _sort_key(item[1]))
    )
    return [record for _tier, record in ordered]


def _sort_key(record: dict[str, Any]) -> tuple[int, float, str]:
    created = record.get("created_at")
    if isinstance(created, datetime):
        stamp = created if created.tzinfo else created.replace(tzinfo=None)
        # Newest first, undated rows last; the id keeps the order stable across pages.
        return (0, -stamp.timestamp(), str(record.get("_id") or ""))
    return (1, 0.0, str(record.get("_id") or ""))


def search_records(db: Any, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = list(db[asset_index.RECORD_COLLECTION].find({}))
    return rank_records(records, groups)


def search_page(
    db: Any, search_id: str, *, skip: int, limit: int, merchant_id: str = ""
) -> dict[str, Any]:
    document = asset_index.get_query(db, search_id)
    ranked = search_records(db, document.get("groups") or [])
    window = ranked[skip : skip + limit]
    items = [_item(db, record, merchant_id) for record in window]
    page = {
        "items": items,
        "total": len(ranked),
        "skip": skip,
        "limit": limit,
        "search": asset_index.query_out(document, total=len(ranked)),
    }
    return page


def _item(db: Any, record: dict[str, Any], merchant_id: str = "") -> dict[str, Any]:
    asset_id = str(record["_id"])
    asset = db.assets.find_one({"_id": asset_id})
    name = str(record.get("name") or (asset or {}).get("original_name") or asset_id)
    # Local uploads keep no stored URL: the same file endpoint the library grid uses serves them.
    stored = str((asset or {}).get("prepared_url") or (asset or {}).get("url") or record.get("url") or "")
    url = stored or asset_url(merchant_id, asset_id)
    item = {
        "id": asset_id,
        "original_name": name,
        "width": int((asset or {}).get("width") or 0),
        "height": int((asset or {}).get("height") or 0),
        "mime_type": str((asset or {}).get("mime_type") or record.get("mime_type") or "image/png"),
        "url": url,
        "created_at": record.get("created_at"),
        "source": str(record.get("source") or "upload"),
        "read_only": asset is None,
        "tags": [str(tag) for tag in record.get("tags") or []],
        "description_zh": str(record.get("description_zh") or ""),
        "description_en": str(record.get("description_en") or ""),
        "description_status": str(record.get("description_status") or ""),
        "match_tier": record.get("match_tier"),
        "matched_terms": list(record.get("matched_terms") or []),
    }
    return item
