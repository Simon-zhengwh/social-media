"""Persistence and lifecycle for local SMB material filters.

Collections are merchant-scoped through :class:`ScopedDatabase`; every helper
here takes that scoped database so a background run cannot bypass isolation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import Settings
from app.services import material_filter_classifier as classifier
from app.services import material_filter_files as files
from app.services.material_filter_files import MaterialFilterFileError

FOLDER_COLLECTION = "material_filter_folders"
SCHEME_COLLECTION = "material_filter_schemes"
RUN_COLLECTION = "material_filter_runs"
FILE_COLLECTION = "material_filter_files"
EVALUATION_COLLECTION = "material_filter_evaluations"

DECISIONS = ("pass", "fail", "uncertain", "error")
REVIEW_DECISIONS = ("pass", "fail")
ACTIVE_RUN_STATUSES = ("queued", "running")
FINISHED_RUN_STATUSES = ("succeeded", "interrupted", "failed")
MAX_RULES = 8
MAX_TAG_LENGTH = 20
MAX_MANUAL_TAGS = 10
OUTPUT_ROOT_LABEL = "material-filters"


class MaterialFilterError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


_new_id = new_id


def default_rules() -> list[dict[str, Any]]:
    return [
        {
            "id": "empty_seating_scene",
            "text": "不能出现空座位场景",
            "interpretation": classifier.DEFAULT_INTERPRETATIONS["empty_seating_scene"],
        },
        {
            "id": "crab_legs",
            "text": "不能出现螃蟹腿",
            "interpretation": classifier.DEFAULT_INTERPRETATIONS["crab_legs"],
        },
    ]


def normalize_rules(raw: Iterable[Any] | None) -> list[dict[str, Any]]:
    if not raw:
        raise MaterialFilterError("rules_required", "筛选方案至少需要一条规则")
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            interpretation = str(item.get("interpretation") or "").strip()
            rule_id = str(item.get("id") or "").strip()
        else:
            text = str(item or "").strip()
            interpretation = ""
            rule_id = ""
        if not text:
            raise MaterialFilterError("rule_text_required", "筛选规则内容不能为空")
        rule_id = rule_id or f"rule_{index}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", rule_id):
            rule_id = f"rule_{index}"
        if rule_id in seen:
            raise MaterialFilterError("rule_id_duplicate", f"规则 ID 重复：{rule_id}")
        seen.add(rule_id)
        rules.append({"id": rule_id, "text": text, "interpretation": interpretation})
    if len(rules) > MAX_RULES:
        raise MaterialFilterError("too_many_rules", f"单个方案最多 {MAX_RULES} 条规则")
    return rules


def default_output_root(settings: Settings) -> str:
    return str(settings.storage_path / OUTPUT_ROOT_LABEL)


def _unique_display_name(db, source) -> str:
    base = source.display_name
    candidates = [base]
    if source.relative:
        parent = source.relative.split("\\")[-1]
        candidates.append(f"{base}（{parent}）")
    candidates.append(f"{base}（{source.server}）")
    for candidate in candidates:
        if db[FOLDER_COLLECTION].find_one({"display_name": candidate}) is None:
            return candidate
    return f"{base}（{source.unc[2:12]}）"


def add_folder(db, original_input: str, *, readable_check=None) -> tuple[dict[str, Any], bool]:
    source = files.normalize_smb_source(original_input)
    existing = db[FOLDER_COLLECTION].find_one({"canonical_source_key": source.key})
    if existing:
        return existing, False
    (readable_check or files.check_source_readable)(source.unc)
    timestamp = now()
    document = {
        "_id": _new_id("mfolder"),
        "display_name": _unique_display_name(db, source),
        "original_input": str(original_input or "").strip(),
        "source_root": source.unc,
        "canonical_source_key": source.key,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    db[FOLDER_COLLECTION].insert_one(document)
    return db[FOLDER_COLLECTION].find_one({"_id": document["_id"]}), True


def list_folders(db) -> list[dict[str, Any]]:
    return list(db[FOLDER_COLLECTION].find().sort("created_at", 1))


def get_folder(db, folder_id: str) -> dict[str, Any]:
    folder = db[FOLDER_COLLECTION].find_one({"_id": folder_id})
    if not folder:
        raise MaterialFilterError("folder_not_found", "文件夹不存在", 404)
    return folder


def rename_folder(db, folder_id: str, display_name: str) -> dict[str, Any]:
    name = str(display_name or "").strip()
    if not name:
        raise MaterialFilterError("display_name_required", "文件夹名称不能为空")
    if len(name) > 60:
        raise MaterialFilterError("display_name_too_long", "文件夹名称不能超过 60 个字符")
    folder = get_folder(db, folder_id)
    # Only the display name changes: the SMB source and its records stay untouched.
    db[FOLDER_COLLECTION].update_one(
        {"_id": folder["_id"]},
        {"$set": {"display_name": name, "updated_at": now()}},
    )
    return get_folder(db, folder_id)


def delete_folder(db, settings: Settings, folder: dict[str, Any], *, purge_local: bool = False) -> dict[str, Any]:
    """Remove a folder TAB and its records.

    Local bypass copies are kept unless ``purge_local`` is set; the SMB source is
    never touched either way.
    """
    active = db[RUN_COLLECTION].find_one(
        {"folder_id": folder["_id"], "status": {"$in": list(ACTIVE_RUN_STATUSES)}}
    )
    if active:
        raise MaterialFilterError(
            "folder_busy", "该文件夹还有扫描任务在运行，请等待完成后再删除", 409
        )
    schemes = list(db[SCHEME_COLLECTION].find({"folder_id": folder["_id"]}))
    purged: list[str] = []
    if purge_local:
        for scheme in schemes:
            try:
                directory = scheme_directory(settings, scheme)
            except (MaterialFilterError, MaterialFilterFileError):
                continue
            # Only the generated local scheme directory is removable.
            if directory.name != str(scheme.get("_id")) or not directory.is_dir():
                continue
            shutil.rmtree(directory, ignore_errors=True)
            purged.append(str(directory))
    counts = {
        "schemes": len(schemes),
        "runs": db[RUN_COLLECTION].count_documents({"folder_id": folder["_id"]}),
        "files": db[FILE_COLLECTION].count_documents({"folder_id": folder["_id"]}),
        "evaluations": db[EVALUATION_COLLECTION].count_documents({"folder_id": folder["_id"]}),
        "purged_directories": purged,
    }
    db[EVALUATION_COLLECTION].delete_many({"folder_id": folder["_id"]})
    db[FILE_COLLECTION].delete_many({"folder_id": folder["_id"]})
    db[RUN_COLLECTION].delete_many({"folder_id": folder["_id"]})
    db[SCHEME_COLLECTION].delete_many({"folder_id": folder["_id"]})
    db[FOLDER_COLLECTION].delete_one({"_id": folder["_id"]})
    return counts


def create_scheme(
    db,
    settings: Settings,
    folder: dict[str, Any],
    *,
    name: str,
    rules: Iterable[Any] | None = None,
    recursive: bool = True,
    output_root: str = "",
    model: str = "",
    prompt_template: str = classifier.PROMPT_TEMPLATE,
) -> dict[str, Any]:
    scheme_name = str(name or "").strip()
    if not scheme_name:
        raise MaterialFilterError("scheme_name_required", "方案名称不能为空")
    if len(scheme_name) > 60:
        raise MaterialFilterError("scheme_name_too_long", "方案名称不能超过 60 个字符")
    normalized_rules = normalize_rules(rules if rules is not None else default_rules())
    selected_model = str(model or settings.material_filter_model).strip()
    if not selected_model:
        raise MaterialFilterError("model_required", "筛选模型不能为空")
    root = str(output_root or "").strip() or default_output_root(settings)
    try:
        resolved_root = files.validate_local_output_root(root)
    except MaterialFilterFileError as exc:
        raise MaterialFilterError(exc.code, exc.message, exc.status_code) from exc
    timestamp = now()
    document = {
        "_id": _new_id("mscheme"),
        "folder_id": folder["_id"],
        "name": scheme_name,
        "source_root_snapshot": folder["source_root"],
        "source_key_snapshot": folder["canonical_source_key"],
        "recursive": bool(recursive),
        "rules": normalized_rules,
        "output_root": str(resolved_root),
        "model": selected_model,
        "prompt_version": classifier.PROMPT_VERSION,
        "prompt_template": prompt_template,
        "active_run_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    db[SCHEME_COLLECTION].insert_one(document)
    return db[SCHEME_COLLECTION].find_one({"_id": document["_id"]})


def list_schemes(db, folder_id: str) -> list[dict[str, Any]]:
    get_folder(db, folder_id)
    return list(db[SCHEME_COLLECTION].find({"folder_id": folder_id}).sort("created_at", 1))


def get_scheme(db, scheme_id: str) -> dict[str, Any]:
    scheme = db[SCHEME_COLLECTION].find_one({"_id": scheme_id})
    if not scheme:
        raise MaterialFilterError("scheme_not_found", "筛选方案不存在", 404)
    return scheme


def _scheme_rules(scheme: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = scheme.get("rules") or []
    return [
        {
            "id": str(rule.get("id") or ""),
            "text": str(rule.get("text") or ""),
            "interpretation": str(rule.get("interpretation") or ""),
        }
        for rule in snapshot
        if isinstance(rule, dict)
    ]


def clone_scheme(
    db,
    settings: Settings,
    scheme: dict[str, Any],
    *,
    name: str,
    rules: Iterable[Any] | None = None,
    recursive: bool | None = None,
) -> dict[str, Any]:
    folder = get_folder(db, scheme["folder_id"])
    template = scheme.get("prompt_template") or classifier.PROMPT_TEMPLATE
    return create_scheme(
        db,
        settings,
        folder,
        name=name or f"{scheme['name']} 副本",
        rules=rules if rules is not None else _scheme_rules(scheme),
        recursive=scheme.get("recursive", True) if recursive is None else recursive,
        output_root=scheme.get("output_root") or "",
        model=scheme.get("model") or "",
        prompt_template=template,
    )


def scheme_directory(settings: Settings, scheme: dict[str, Any]) -> Path:
    """Stable local directory: <root>/<merchant>/<folder>/<scheme>."""
    base = str(scheme.get("output_root") or "").strip() or default_output_root(settings)
    try:
        root = files.validate_local_output_root(base)
    except MaterialFilterFileError as exc:
        raise MaterialFilterError(exc.code, exc.message, exc.status_code) from exc
    merchant_id = str(scheme.get("merchant_id") or "merchant")
    folder_id = str(scheme.get("folder_id") or "folder")
    scheme_id = str(scheme.get("_id") or "scheme")
    return root / merchant_id / folder_id / scheme_id


# --- runs -------------------------------------------------------------------


def create_run(
    db,
    scheme: dict[str, Any],
    *,
    request_id: str = "",
    limit: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or return) a manual scan. Idempotent per scheme + request_id."""
    scheme_id = scheme["_id"]
    key = str(request_id or "").strip()
    if key:
        existing = db[RUN_COLLECTION].find_one({"scheme_id": scheme_id, "request_id": key})
        if existing:
            return existing, False
    else:
        key = _new_id("req")

    run_id = _new_id("mrun")
    claimed = db[SCHEME_COLLECTION].find_one_and_update(
        {"_id": scheme_id, "active_run_id": None},
        {"$set": {"active_run_id": run_id, "updated_at": now()}},
    )
    if not claimed:
        raise MaterialFilterError("scheme_busy", "该方案已有扫描任务在运行，请等待完成后再试", 409)

    existing = db[RUN_COLLECTION].find_one({"scheme_id": scheme_id, "request_id": key})
    if existing:
        release_scheme_run(db, scheme_id, run_id)
        return existing, False

    timestamp = now()
    document = {
        "_id": run_id,
        "scheme_id": scheme_id,
        "folder_id": scheme.get("folder_id"),
        "request_id": key,
        "status": "queued",
        "limit": int(limit) if limit else None,
        "counters": empty_counters(),
        "error": None,
        "created_at": timestamp,
        "started_at": None,
        "finished_at": None,
    }
    db[RUN_COLLECTION].insert_one(document)
    return db[RUN_COLLECTION].find_one({"_id": run_id}), True


def release_scheme_run(db, scheme_id: str, run_id: str) -> None:
    db[SCHEME_COLLECTION].update_one(
        {"_id": scheme_id, "active_run_id": run_id},
        {"$set": {"active_run_id": None, "updated_at": now()}},
    )


def empty_counters() -> dict[str, int]:
    return {
        "scanned": 0,
        "added": 0,
        "changed": 0,
        "skipped": 0,
        "passed": 0,
        "failed": 0,
        "uncertain": 0,
        "errors": 0,
        "copied": 0,
        "copy_errors": 0,
    }


def bump_run(db, run_id: str, updates: dict[str, int]) -> None:
    if not updates:
        return
    db[RUN_COLLECTION].update_one(
        {"_id": run_id},
        {"$inc": {f"counters.{name}": value for name, value in updates.items()}},
    )


def set_run_counters(db, run_id: str, counters: dict[str, int]) -> None:
    db[RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"counters": counters}})


def record_run_error(db, run_id: str, relative_path: str, message: str) -> None:
    db[RUN_COLLECTION].update_one(
        {"_id": run_id},
        {
            "$push": {
                "item_errors": {
                    "$each": [{"path": relative_path, "message": message, "at": now()}],
                    "$slice": -20,
                }
            },
            "$inc": {"counters.errors": 1},
        },
    )


def get_run(db, run_id: str) -> dict[str, Any]:
    run = db[RUN_COLLECTION].find_one({"_id": run_id})
    if not run:
        raise MaterialFilterError("run_not_found", "扫描任务不存在", 404)
    return run


def runtime_scheme(db, scheme_id: str) -> dict[str, Any]:
    return get_scheme(db, scheme_id)


def mark_scheme_interrupted(db) -> list[str]:
    """Mark leftover runs interrupted; the operator resumes explicitly."""
    runs = list(db[RUN_COLLECTION].find({"status": {"$in": list(ACTIVE_RUN_STATUSES)}}))
    run_ids = [str(doc["_id"]) for doc in runs]
    for run_id in run_ids:
        db[RUN_COLLECTION].update_one(
            {"_id": run_id},
            {"$set": {"status": "interrupted", "finished_at": now(), "error": "服务重启，任务已中断"}},
        )
    # Release only the schemes these runs owned; the cluster runs with notablescan,
    # so an unfiltered update_many over active_run_id would be rejected.
    for scheme_id in {str(doc.get("scheme_id") or "") for doc in runs if doc.get("scheme_id")}:
        db[SCHEME_COLLECTION].update_one(
            {"_id": scheme_id, "active_run_id": {"$in": run_ids}},
            {"$set": {"active_run_id": None}},
        )
    return run_ids


# --- files and evaluations --------------------------------------------------


def path_key(relative_path: str) -> str:
    return str(relative_path).replace("\\", "/").casefold()


def get_file(db, scheme_id: str, key: str) -> dict[str, Any] | None:
    return db[FILE_COLLECTION].find_one({"scheme_id": scheme_id, "path_key": key})


def upsert_file(
    db,
    scheme: dict[str, Any],
    entry: files.SourceEntry,
    *,
    size: int,
    mtime_ns: int,
    sha256: str | None,
    latest_evaluation_id: str | None,
) -> dict[str, Any]:
    key = path_key(entry.relative_path)
    timestamp = now()
    document = {
        "scheme_id": scheme["_id"],
        "folder_id": scheme.get("folder_id"),
        "path_key": key,
        "relative_path": entry.relative_path.replace("\\", "/"),
        "original_name": entry.name,
        "size": int(size),
        "mtime_ns": int(mtime_ns),
        "sha256": sha256,
        "latest_evaluation_id": latest_evaluation_id,
        "last_seen_at": timestamp,
    }
    existing = db[FILE_COLLECTION].find_one({"scheme_id": scheme["_id"], "path_key": key})
    if existing:
        db[FILE_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": document})
        return db[FILE_COLLECTION].find_one({"_id": existing["_id"]})
    document["_id"] = _new_id("mfile")
    document["created_at"] = timestamp
    db[FILE_COLLECTION].insert_one(document)
    return document


def mark_file_seen(db, file_id: str, *, size: int, mtime_ns: int) -> None:
    db[FILE_COLLECTION].update_one(
        {"_id": file_id},
        {"$set": {"size": int(size), "mtime_ns": int(mtime_ns), "last_seen_at": now()}},
    )


def find_evaluation(db, scheme_id: str, file_id: str, sha256: str) -> dict[str, Any] | None:
    return db[EVALUATION_COLLECTION].find_one(
        {"scheme_id": scheme_id, "file_id": file_id, "source_sha256": sha256}
    )


def save_evaluation(
    db,
    scheme: dict[str, Any],
    file_doc: dict[str, Any],
    snapshot: files.SourceSnapshot,
    *,
    evaluation_id: str = "",
    result: classifier.ClassificationResult | None,
    decision: str,
    error: str | None,
    preview_path: str | None,
    output_path: str | None,
    output_name: str | None,
    copy_status: str,
    copy_error: str | None,
    short_id: str,
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise MaterialFilterError("invalid_decision", "判断状态非法")
    timestamp = now()
    model_name = result.name if result else ""
    category = result.category if result else ""
    tags = list(result.tags) if result else []
    rule_verdicts = (
        [{"id": v.id, "verdict": v.verdict, "reason": v.reason} for v in result.verdicts]
        if result
        else []
    )
    document = {
        "_id": evaluation_id or new_id("meval"),
        "scheme_id": scheme["_id"],
        "folder_id": scheme.get("folder_id"),
        "file_id": file_doc["_id"],
        "path_key": file_doc["path_key"],
        "relative_path": file_doc["relative_path"],
        "original_name": file_doc["original_name"],
        "source_sha256": snapshot.sha256,
        "source_size": snapshot.size,
        "source_mtime_ns": snapshot.mtime_ns,
        "model": scheme.get("model"),
        "prompt_version": scheme.get("prompt_version"),
        "model_result": {
            "name": model_name,
            "category": category,
            "tags": tags,
            "description": result.description if result else "",
            "rules": rule_verdicts,
            "decision": result.decision if result else None,
        },
        "model_decision": result.decision if result else None,
        "decision": decision,
        "human_decision": None,
        "human_decided_at": None,
        "manual_pick": False,
        "picked_at": None,
        "effective_decision": decision,
        "display_name": model_name,
        "category": category,
        "tags": tags,
        "short_id": short_id,
        "preview_path": preview_path,
        "output_path": output_path,
        "output_name": output_name,
        "output_sha256": snapshot.sha256 if output_path else None,
        "copy_status": copy_status,
        "copy_error": copy_error,
        "error": error,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    db[EVALUATION_COLLECTION].insert_one(document)
    return document


def update_evaluation(db, evaluation_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    payload = dict(updates)
    payload["updated_at"] = now()
    db[EVALUATION_COLLECTION].update_one({"_id": evaluation_id}, {"$set": payload})
    evaluation = db[EVALUATION_COLLECTION].find_one({"_id": evaluation_id})
    if not evaluation:
        raise MaterialFilterError("evaluation_not_found", "筛选结果不存在", 404)
    return evaluation


def get_evaluation(db, evaluation_id: str) -> dict[str, Any]:
    evaluation = db[EVALUATION_COLLECTION].find_one({"_id": evaluation_id})
    if not evaluation:
        raise MaterialFilterError("evaluation_not_found", "筛选结果不存在", 404)
    return evaluation


RESULT_SORTS = {
    "judged": [("created_at", -1)],
    "name": [("original_name", 1), ("relative_path", 1)],
    "path": [("relative_path", 1)],
}


def _result_filter(query: dict[str, Any], status: str, search: str) -> dict[str, Any]:
    if status and status != "all":
        if status == "picked":
            # Second-stage manual shortlist, independent of the model verdict.
            query["manual_pick"] = True
        elif status not in DECISIONS:
            raise MaterialFilterError("invalid_status", "结果状态筛选非法")
        else:
            query["effective_decision"] = status
    keyword = str(search or "").strip()
    if keyword:
        pattern = re.escape(keyword)
        query["$or"] = [
            {"display_name": {"$regex": pattern, "$options": "i"}},
            {"original_name": {"$regex": pattern, "$options": "i"}},
            {"relative_path": {"$regex": pattern, "$options": "i"}},
            {"category": {"$regex": pattern, "$options": "i"}},
            {"tags": {"$regex": pattern, "$options": "i"}},
        ]
    return query


def list_results(
    db,
    scheme_id: str,
    *,
    status: str = "all",
    search: str = "",
    skip: int = 0,
    limit: int = 24,
    sort: str = "judged",
) -> dict[str, Any]:
    get_scheme(db, scheme_id)
    if sort not in RESULT_SORTS:
        raise MaterialFilterError("invalid_sort", "结果排序方式非法")
    query = _result_filter({"scheme_id": scheme_id}, status, search)
    total = db[EVALUATION_COLLECTION].count_documents(query)
    items = list(
        db[EVALUATION_COLLECTION]
        .find(query)
        .sort(RESULT_SORTS[sort])
        .skip(max(0, skip))
        .limit(max(1, limit))
    )
    counts: dict[str, int] = {"all": db[EVALUATION_COLLECTION].count_documents({"scheme_id": scheme_id})}
    for decision in DECISIONS:
        counts[decision] = db[EVALUATION_COLLECTION].count_documents(
            {"scheme_id": scheme_id, "effective_decision": decision}
        )
    counts["picked"] = db[EVALUATION_COLLECTION].count_documents(
        {"scheme_id": scheme_id, "manual_pick": True}
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit, "counts": counts}


def scheme_last_run(db, scheme_id: str) -> dict[str, Any] | None:
    return db[RUN_COLLECTION].find_one({"scheme_id": scheme_id}, sort=[("created_at", -1)])


def set_manual_pick(db, evaluation: dict[str, Any], *, picked: bool) -> dict[str, Any]:
    """Second-stage human shortlist. Model verdicts stay untouched."""
    return update_evaluation(
        db,
        evaluation["_id"],
        {"manual_pick": bool(picked), "picked_at": now() if picked else None},
    )


def bulk_manual_pick(db, scheme: dict[str, Any], ids: list[str], *, picked: bool) -> int:
    updated = 0
    for evaluation_id in ids:
        evaluation = db[EVALUATION_COLLECTION].find_one(
            {"_id": evaluation_id, "scheme_id": scheme["_id"]}
        )
        if not evaluation:
            continue
        set_manual_pick(db, evaluation, picked=picked)
        updated += 1
    return updated


def update_metadata(
    db,
    evaluation: dict[str, Any],
    *,
    display_name: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if display_name is not None:
        name = str(display_name).strip()
        if not name:
            raise MaterialFilterError("display_name_required", "名称不能为空")
        if len(name) > 60:
            raise MaterialFilterError("display_name_too_long", "名称不能超过 60 个字符")
        updates["display_name"] = name
    if tags is not None:
        cleaned: list[str] = []
        for item in tags:
            if not isinstance(item, str):
                raise MaterialFilterError("invalid_tags", "标签必须是文本")
            tag = item.strip()
            if not tag:
                continue
            if len(tag) > MAX_TAG_LENGTH:
                raise MaterialFilterError("tag_too_long", f"标签不能超过 {MAX_TAG_LENGTH} 个字符")
            if tag not in cleaned:
                cleaned.append(tag)
        if len(cleaned) > MAX_MANUAL_TAGS:
            raise MaterialFilterError("too_many_tags", f"标签最多 {MAX_MANUAL_TAGS} 个")
        updates["tags"] = cleaned
    if not updates:
        return evaluation
    # Metadata edits never rename the local copy: display name and file name stay separate.
    return update_evaluation(db, evaluation["_id"], updates)


def review_evaluation(
    db,
    settings: Settings,
    scheme: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    decision: str,
    source_sha256: str,
    request_id: str = "",
) -> dict[str, Any]:
    if decision not in REVIEW_DECISIONS:
        raise MaterialFilterError("invalid_review_decision", "人工结论只能是通过或不通过")
    key = str(request_id or "").strip()
    if key and evaluation.get("review_request_id") == key:
        return evaluation
    already_reviewed = (
        evaluation.get("effective_decision") == decision
        and evaluation.get("human_decision") == decision
    )
    # A failed copy is retried even when the human verdict itself did not change.
    if already_reviewed and not (decision == "pass" and evaluation.get("copy_status") == "error"):
        return evaluation
    expected = str(source_sha256 or "").strip()
    if expected and expected != evaluation["source_sha256"]:
        raise MaterialFilterError(
            "source_version_mismatch",
            "源文件版本已变化，请重新扫描后再复核",
            409,
        )
    folder = get_folder(db, scheme["folder_id"])
    try:
        snapshot = files.read_source_file(
            folder["source_root"],
            evaluation["relative_path"],
            max_bytes=settings.material_filter_max_source_bytes,
        )
    except MaterialFilterFileError as exc:
        raise MaterialFilterError(exc.code, exc.message, exc.status_code) from exc
    if snapshot.sha256 != evaluation["source_sha256"]:
        raise MaterialFilterError(
            "source_changed",
            "源文件内容已变化，当前结论不能用于新内容，请重新检查",
            409,
        )

    directory = scheme_directory(settings, scheme)
    updates: dict[str, Any] = {
        "human_decision": decision,
        "human_decided_at": now(),
        "effective_decision": decision,
    }
    if key:
        updates["review_request_id"] = key
    if decision == "pass":
        try:
            file_name, written = files.copy_approved_bytes(
                directory,
                display_name=evaluation.get("display_name") or snapshot.relative_path,
                short_id=evaluation.get("short_id") or evaluation["_id"],
                suffix=Path(evaluation["original_name"]).suffix,
                data=snapshot.data,
                sha256=snapshot.sha256,
            )
            updates.update(
                {
                    "output_path": f"passed/{file_name}",
                    "output_name": file_name,
                    "output_sha256": written,
                    "copy_status": "copied",
                    "copy_error": None,
                }
            )
        except MaterialFilterFileError as exc:
            # The human decision is kept; the copy stays retryable and not downloadable.
            updates.update({"copy_status": "error", "copy_error": exc.message})
            update_evaluation(db, evaluation["_id"], updates)
            return get_evaluation(db, evaluation["_id"])
    else:
        updates.update({"copy_status": evaluation.get("copy_status") or "none"})
    return update_evaluation(db, evaluation["_id"], updates)


def ensure_preview(db, settings: Settings, scheme: dict[str, Any], evaluation: dict[str, Any]) -> str | None:
    if evaluation.get("preview_path"):
        return evaluation["preview_path"]
    folder = get_folder(db, scheme["folder_id"])
    try:
        snapshot = files.read_source_file(
            folder["source_root"],
            evaluation["relative_path"],
            max_bytes=settings.material_filter_max_source_bytes,
        )
        if snapshot.sha256 != evaluation["source_sha256"]:
            return None
        relative = files.write_local_preview(
            scheme_directory(settings, scheme), evaluation["_id"], snapshot.data
        )
    except MaterialFilterFileError:
        return None
    db[EVALUATION_COLLECTION].update_one(
        {"_id": evaluation["_id"]}, {"$set": {"preview_path": relative, "updated_at": now()}}
    )
    evaluation["preview_path"] = relative
    return relative


def export_picked(db, settings: Settings, scheme: dict[str, Any]) -> dict[str, Any]:
    """Copy the picked local files into an exports folder with a manifest.

    This is the manual-upload fallback: the folder can be dragged into the
    deployed asset page, while the sync script uploads the same rows directly.
    """
    rows = list(
        db[EVALUATION_COLLECTION]
        .find({"scheme_id": scheme["_id"], "manual_pick": True, "copy_status": "copied"})
        .sort("picked_at", 1)
    )
    if not rows:
        raise MaterialFilterError("nothing_picked", "还没有入选的图片，请先在结果里点「☆ 入选」", 422)
    directory = scheme_directory(settings, scheme)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    try:
        export_dir = files.ensure_local_directory(directory / "exports" / f"picked-{stamp}")
    except MaterialFilterFileError as exc:
        raise MaterialFilterError(exc.code, exc.message, exc.status_code) from exc

    items: list[dict[str, Any]] = []
    for row in rows:
        source = directory / str(row["output_path"])
        if not source.is_file():
            raise MaterialFilterError("output_missing", f"本机副本不存在：{row.get('output_name')}", 404)
        items.append({"name": source.name, "row": row})

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["文件", "名称", "原文件名", "分类", "标签", "内容哈希", "评估ID"])
    for item in items:
        row = item["row"]
        target = export_dir / item["name"]
        if not target.exists():
            shutil.copy2(directory / str(row["output_path"]), target)
        writer.writerow(
            [
                item["name"],
                _csv_safe(row.get("display_name") or ""),
                _csv_safe(row.get("original_name") or ""),
                _csv_safe(row.get("category") or ""),
                _csv_safe("、".join(row.get("tags") or [])),
                row.get("source_sha256") or "",
                row["_id"],
            ]
        )
    manifest = export_dir / "manifest.csv"
    manifest.write_bytes(bytes.fromhex("efbbbf") + buffer.getvalue().encode("utf-8"))
    return {"directory": str(export_dir), "count": len(items), "manifest": str(manifest)}


def export_csv(db, scheme: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["名称", "分类", "标签", "状态", "人工入选", "原文件名", "来源相对路径", "内容哈希", "本机副本"]
    )
    for item in db[EVALUATION_COLLECTION].find({"scheme_id": scheme["_id"]}).sort("created_at", 1):
        writer.writerow(
            [
                _csv_safe(item.get("display_name") or ""),
                _csv_safe(item.get("category") or ""),
                _csv_safe("、".join(item.get("tags") or [])),
                _status_label(item.get("effective_decision")),
                "是" if item.get("manual_pick") else "否",
                _csv_safe(item.get("original_name") or ""),
                _csv_safe(item.get("relative_path") or ""),
                item.get("source_sha256") or "",
                _csv_safe(item.get("output_name") or ""),
            ]
        )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _csv_safe(value: Any) -> str:
    text = str(value)
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def _status_label(status: Any) -> str:
    return {
        "pass": "通过",
        "fail": "不通过",
        "uncertain": "待复核",
        "error": "请求失败",
    }.get(str(status or ""), str(status or ""))


def output_file(evaluation: dict[str, Any], settings: Settings, scheme: dict[str, Any]) -> Path:
    if evaluation.get("copy_status") != "copied" or not evaluation.get("output_path"):
        raise MaterialFilterError("output_not_ready", "该结果还没有可下载的本机副本", 404)
    directory = scheme_directory(settings, scheme)
    target = directory / evaluation["output_path"]
    if not target.is_file():
        raise MaterialFilterError("output_missing", "本机副本不存在，请重试复核或复制", 404)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != evaluation.get("source_sha256"):
        raise MaterialFilterError("output_hash_mismatch", "本机副本与评估内容不一致，已拒绝下载", 409)
    return target


def preview_file(evaluation: dict[str, Any], settings: Settings, scheme: dict[str, Any]) -> Path:
    if not evaluation.get("preview_path"):
        raise MaterialFilterError("preview_missing", "该结果还没有缩略图", 404)
    target = scheme_directory(settings, scheme) / evaluation["preview_path"]
    if not target.is_file():
        raise MaterialFilterError("preview_missing", "缩略图不存在", 404)
    return target
