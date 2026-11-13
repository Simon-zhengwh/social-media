"""Merchant-scoped API for local SMB material filtering.

Every route is gated on the local AM bypass: the feature reads folders on the
machine running the backend, so a deployed site must not expose it. Resources are
always located by stored ids, never by a path supplied by the browser.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api_paths import merchant_base
from app.middleware import is_local_bypass
from app.request_context import request_db, request_merchant_id
from app.services import material_filter_runner as runner
from app.services import material_filter_uat_sync as uat_sync
from app.services import material_filters as store
from app.services import material_groups as group_store
from app.services import material_group_runner as group_runner
from app.services.material_filters import MaterialFilterError
from app.time_utils import utc_iso

router = APIRouter(prefix="/material-filters", tags=["material-filters"])


class FolderCreate(BaseModel):
    input: str


class FolderRename(BaseModel):
    display_name: str


class RuleInput(BaseModel):
    id: str = ""
    text: str
    interpretation: str = ""


class SchemeCreate(BaseModel):
    name: str
    rules: list[RuleInput] | None = None
    recursive: bool = True
    output_root: str = ""
    model: str = ""


class SchemeClone(BaseModel):
    name: str = ""
    rules: list[RuleInput] | None = None
    recursive: bool | None = None


class RunCreate(BaseModel):
    request_id: str = ""
    # Trial runs judge at most this many unchecked images.
    limit: int | None = Field(default=None, ge=1, le=200)


class ReviewPayload(BaseModel):
    decision: str
    source_sha256: str = ""
    request_id: str = ""


class SyncUatPayload(BaseModel):
    # AM Console session for the deployed site; never persisted server-side.
    cookie: str = ""
    force: bool = False
    limit: int = Field(default=0, ge=0, le=500)
    dry_run: bool = False


class MetadataPayload(BaseModel):
    display_name: str | None = None
    tags: list[str] | None = None
    tag_groups: dict[str, list[str]] | None = None


class PickPayload(BaseModel):
    picked: bool = True


class BulkPickPayload(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)
    picked: bool = True


class GroupScanPayload(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)


class GroupRecognitionPayload(GroupScanPayload):
    mode: str = "representatives"
    representative_count: int = Field(default=1, ge=1, le=2)
    group_limit: int | None = Field(default=None, ge=1, le=200)
    file_ids: list[str] = Field(default_factory=list, max_length=200)
    force: bool = False


class GroupRepresentativesPayload(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=2)
    version: str


class GroupReferencePayload(BaseModel):
    file_id: str
    version: str


class GroupNamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    version: str


class GroupSplitPayload(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=2000)
    version: str


class GroupMergePayload(GroupScanPayload):
    group_ids: list[str] = Field(min_length=2, max_length=100)
    version: str


def _require_local(request: Request) -> None:
    if not is_local_bypass(request):
        raise MaterialFilterError(
            "material_filters_local_only",
            "素材筛选只在本机开发模式（AM_BYPASS_LOCAL）可用",
            403,
        )


def _rules_payload(rules: list[RuleInput] | None) -> list[dict[str, Any]] | None:
    if rules is None:
        return None
    return [rule.model_dump() for rule in rules]


def folder_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "display_name": doc.get("display_name") or "",
        "original_input": doc.get("original_input") or "",
        "source_root": doc.get("source_root") or "",
        "created_at": utc_iso(doc.get("created_at")),
    }


def scheme_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "folder_id": doc.get("folder_id"),
        "name": doc.get("name") or "",
        "recursive": bool(doc.get("recursive")),
        "rules": [
            {
                "id": str(rule.get("id") or ""),
                "text": str(rule.get("text") or ""),
                "interpretation": str(rule.get("interpretation") or ""),
            }
            for rule in doc.get("rules") or []
            if isinstance(rule, dict)
        ],
        "output_root": doc.get("output_root") or "",
        "model": doc.get("model") or "",
        "prompt_version": doc.get("prompt_version") or "",
        "naming_mode": doc.get("naming_mode") or "descriptive",
        "name_format": (doc.get("menu") or {}).get("name_format", "zh_en"),
        "active_run_id": doc.get("active_run_id"),
        "created_at": utc_iso(doc.get("created_at")),
    }


def run_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "scheme_id": doc.get("scheme_id"),
        "status": doc.get("status"),
        "limit": doc.get("limit"),
        "counters": doc.get("counters") or store.empty_counters(),
        "error": doc.get("error"),
        "item_errors": [
            {
                "path": item.get("path"),
                "message": item.get("message"),
                "at": utc_iso(item.get("at")),
            }
            for item in doc.get("item_errors") or []
            if isinstance(item, dict)
        ],
        "created_at": utc_iso(doc.get("created_at")),
        "started_at": utc_iso(doc.get("started_at")),
        "finished_at": utc_iso(doc.get("finished_at")),
    }


def _evaluation_urls(request: Request, evaluation_id: str) -> dict[str, str]:
    merchant_id = request_merchant_id(request)
    base = f"{merchant_base(merchant_id)}/material-filters/evaluations/{quote(str(evaluation_id), safe='')}"
    return {
        "preview_url": f"{base}/preview",
        "download_url": f"{base}/download",
    }


def evaluation_out(request: Request, doc: dict[str, Any]) -> dict[str, Any]:
    model_result = doc.get("model_result") or {}
    urls = _evaluation_urls(request, doc["_id"])
    return {
        "id": doc["_id"],
        "scheme_id": doc.get("scheme_id"),
        "file_id": doc.get("file_id"),
        "display_name": doc.get("display_name") or "",
        "original_name": doc.get("original_name") or "",
        "relative_path": doc.get("relative_path") or "",
        "category": doc.get("category") or "",
        "tags": list(doc.get("tags") or []),
        "tag_groups": doc.get("tag_groups") or {},
        "naming": doc.get("naming"),
        "decision": doc.get("decision"),
        "model_decision": doc.get("model_decision"),
        "human_decision": doc.get("human_decision"),
        "manual_pick": bool(doc.get("manual_pick")),
        "picked_at": utc_iso(doc.get("picked_at")),
        "effective_decision": doc.get("effective_decision"),
        "description": model_result.get("description") or "",
        "rules": [
            {
                "id": item.get("id"),
                "verdict": item.get("verdict"),
                "reason": item.get("reason"),
            }
            for item in model_result.get("rules") or []
            if isinstance(item, dict)
        ],
        "model": doc.get("model"),
        "source_sha256": doc.get("source_sha256"),
        "source_size": doc.get("source_size"),
        "copy_status": doc.get("copy_status") or "none",
        "uat_asset_id": doc.get("uat_asset_id"),
        "uat_asset_url": doc.get("uat_asset_url"),
        "uat_synced_at": utc_iso(doc.get("uat_synced_at")),
        "uat_sync_error": doc.get("uat_sync_error"),
        "uat_recompressed": bool(doc.get("uat_recompressed")),
        "copy_error": doc.get("copy_error"),
        "output_name": doc.get("output_name"),
        "error": doc.get("error"),
        "preview_url": urls["preview_url"] if doc.get("preview_path") else None,
        "download_url": urls["download_url"] if doc.get("copy_status") == "copied" else None,
        "created_at": utc_iso(doc.get("created_at")),
        "updated_at": utc_iso(doc.get("updated_at")),
    }


def group_run_out(request: Request, doc: dict[str, Any]) -> dict[str, Any]:
    counters = {key: 0 for key in ["scanned", "processed", "groups", "recognized", "cache_hits", "model_calls", "errors", "copied", "skipped"]}
    counters.update(doc.get("counters") or {})
    attempts = {a["file_id"]: a for a in request_db(request)[group_store.ATTEMPTS].find({"run_id": doc["_id"]})}
    items = [{"file_id": p["file_id"], "status": attempts.get(p["file_id"], {}).get("status", "queued"),
              "error": attempts.get(p["file_id"], {}).get("error")} for p in doc.get("files", [])]
    return {"id": doc["_id"], "scheme_id": doc["scheme_id"], "kind": doc.get("kind", "group_scan"),
        "status": doc["status"], "counters": counters, "items": items, "error": doc.get("error"), "scan_errors": doc.get("scan_errors") or [],
        "created_at": utc_iso(doc.get("created_at")), "started_at": utc_iso(doc.get("started_at")),
        "finished_at": utc_iso(doc.get("finished_at"))}


def group_file_out(request: Request, row: dict) -> dict:
    result = dict(row)
    base = merchant_base(request_merchant_id(request)) + "/material-filters"
    result["preview_url"] = f"{base}/files/{quote(row['id'], safe='')}/preview?v={str(row.get('source_sha256') or '')[:12]}"
    result["evaluation"] = evaluation_out(request, row["evaluation"]) if row.get("evaluation") else None
    return result


def group_out(request: Request, row: dict) -> dict:
    return {**row, "cover": group_file_out(request, row["cover"]),
            "previews": [{k:v for k,v in group_file_out(request, p).items() if k != "evaluation"} for p in row.get("previews", [])]}


@router.post("/schemes/{scheme_id}/group-scans", status_code=202)
def start_group_scan(request: Request, scheme_id: str, payload: GroupScanPayload):
    _require_local(request)
    db = request_db(request)
    run = group_runner.start_scan(db, request.app.state.settings, store.get_scheme(db, scheme_id), request_id=payload.request_id)
    return group_run_out(request, run)


@router.get("/schemes/{scheme_id}/groups")
def list_groups(request: Request, scheme_id: str, skip: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100), q: str = "", representative_count: int = Query(default=1, ge=1, le=2)):
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    page = group_store.list_groups(db, request.app.state.settings, scheme, skip=skip, limit=limit, q=q, representative_count=representative_count)
    return {**page, "items": [group_out(request, g) for g in page["items"]], "last_run": group_run_out(request, page["last_run"]) if page["last_run"] else None}


@router.get("/groups/{group_id}/files")
def list_group_files(request: Request, group_id: str, skip: int = Query(default=0, ge=0), limit: int = Query(default=48, ge=1, le=200), representative_count: int = Query(default=1, ge=1, le=2)):
    _require_local(request)
    page = group_store.list_members(request_db(request), request.app.state.settings, group_id, skip=skip, limit=limit, representative_count=representative_count)
    return {**page, "items": [group_file_out(request, r) for r in page["items"]]}


@router.get("/files/{file_id}/preview")
def preview_group_file(request: Request, file_id: str):
    _require_local(request)
    db = request_db(request)
    file = group_store.get_file(db, file_id)
    scheme = store.get_scheme(db, file["scheme_id"])
    directory = store.scheme_directory(request.app.state.settings, scheme)
    target = group_store._preview_path(directory, file.get("preview_path"))
    if target is None:
        raise MaterialFilterError("preview_missing", "缩略图尚未准备好，请先扫描并分组", 404)
    return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/files/{file_id}/attempts")
def list_file_attempts(request: Request, file_id: str, skip: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    _require_local(request)
    return group_store.list_attempts(request_db(request), file_id, skip=skip, limit=limit)


@router.post("/groups/{group_id}/split")
def split_group(request: Request, group_id: str, payload: GroupSplitPayload):
    _require_local(request)
    result = group_store.split_group(request_db(request), request.app.state.settings, group_id, payload.file_ids, version=payload.version)
    return {"groups": [group_out(request, row) for row in result["groups"]]}


@router.post("/schemes/{scheme_id}/groups/merge")
def merge_groups(request: Request, scheme_id: str, payload: GroupMergePayload):
    _require_local(request)
    result = group_store.merge_groups(request_db(request), request.app.state.settings, scheme_id,
        payload.group_ids, version=payload.version, request_id=payload.request_id)
    return {"group": group_out(request, result["group"])}


@router.patch("/groups/{group_id}/representatives")
def change_group_representatives(request: Request, group_id: str, payload: GroupRepresentativesPayload):
    _require_local(request)
    result = group_store.set_representatives(request_db(request), request.app.state.settings, group_id, payload.file_ids, version=payload.version)
    return group_out(request, result)


@router.post("/groups/{group_id}/reference")
def adopt_group_reference(request: Request, group_id: str, payload: GroupReferencePayload):
    _require_local(request)
    result = group_store.adopt_reference(request_db(request), request.app.state.settings, group_id, payload.file_id, version=payload.version)
    return group_out(request, result)


@router.patch("/groups/{group_id}/name")
def rename_group(request: Request, group_id: str, payload: GroupNamePayload):
    _require_local(request)
    result = group_store.rename_group(request_db(request), request.app.state.settings, group_id, payload.name, version=payload.version)
    return group_out(request, result)


@router.post("/schemes/{scheme_id}/recognition-runs", status_code=202)
def start_group_recognition(request: Request, scheme_id: str, payload: GroupRecognitionPayload):
    _require_local(request)
    db = request_db(request)
    if not request.app.state.settings.core_ai_api_key:
        raise MaterialFilterError("not_configured", "未配置Core AI Key", 503)
    result = group_runner.start_recognition(db, request.app.state.settings, request.app.state.core_ai,
        store.get_scheme(db, scheme_id), **payload.model_dump())
    return group_run_out(request, result)


@router.get("/group-runs/{run_id}")
def get_group_run(request: Request, run_id: str):
    _require_local(request)
    doc = store.get_run(request_db(request), run_id)
    if doc.get("kind") not in {"group_scan", "recognition"}:
        raise MaterialFilterError("group_run_not_found", "分组任务不存在", 404)
    return group_run_out(request, doc)


@router.post("/group-runs/{run_id}/resume", status_code=202)
def resume_group_run(request: Request, run_id: str):
    _require_local(request)
    doc = group_runner.resume(request_db(request), request.app.state.settings, request.app.state.core_ai, run_id)
    return group_run_out(request, doc)


@router.get("/defaults")
def get_defaults(request: Request) -> dict[str, Any]:
    _require_local(request)
    settings = request.app.state.settings
    return {
        "rules": store.default_rules(),
        "model": settings.material_filter_model,
        "output_root": store.default_output_root(settings),
    }


@router.get("/folders")
def list_folders(request: Request) -> dict[str, Any]:
    _require_local(request)
    folders = [folder_out(doc) for doc in store.list_folders(request_db(request))]
    return {"items": folders, "total": len(folders)}


@router.post("/folders", status_code=201)
def add_folder(request: Request, payload: FolderCreate) -> dict[str, Any]:
    _require_local(request)
    folder, created = store.add_folder(request_db(request), payload.input)
    return {**folder_out(folder), "created": created}


@router.delete("/folders/{folder_id}")
def delete_folder(
    request: Request,
    folder_id: str,
    purge_local: bool = False,
) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    folder = store.get_folder(db, folder_id)
    deleted = store.delete_folder(
        db, request.app.state.settings, folder, purge_local=purge_local
    )
    return {"deleted": folder_id, "purge_local": purge_local, **deleted}


@router.patch("/folders/{folder_id}")
def rename_folder(request: Request, folder_id: str, payload: FolderRename) -> dict[str, Any]:
    _require_local(request)
    folder = store.rename_folder(request_db(request), folder_id, payload.display_name)
    return folder_out(folder)


@router.get("/folders/{folder_id}/schemes")
def list_schemes(request: Request, folder_id: str) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    schemes = [scheme_out(doc) for doc in store.list_schemes(db, folder_id)]
    return {"items": schemes, "total": len(schemes)}


@router.post("/folders/{folder_id}/schemes", status_code=201)
def create_scheme(request: Request, folder_id: str, payload: SchemeCreate) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    folder = store.get_folder(db, folder_id)
    scheme = store.create_scheme(
        db,
        request.app.state.settings,
        folder,
        name=payload.name,
        rules=_rules_payload(payload.rules),
        recursive=payload.recursive,
        output_root=payload.output_root,
        model=payload.model,
    )
    return scheme_out(scheme)


@router.get("/schemes/{scheme_id}")
def get_scheme(request: Request, scheme_id: str) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    last_run = store.scheme_last_run(db, scheme_id)
    return {
        **scheme_out(scheme),
        "last_run": run_out(last_run) if last_run else None,
    }


@router.post("/schemes/{scheme_id}/clone", status_code=201)
def clone_scheme(request: Request, scheme_id: str, payload: SchemeClone) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    cloned = store.clone_scheme(
        db,
        request.app.state.settings,
        scheme,
        name=payload.name,
        rules=_rules_payload(payload.rules),
        recursive=payload.recursive,
    )
    return scheme_out(cloned)


@router.post("/schemes/{scheme_id}/runs", status_code=202)
def start_run(request: Request, scheme_id: str, payload: RunCreate) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    if scheme.get("naming_mode") == "menu_match" and not scheme.get("rules"):
        raise MaterialFilterError("group_scan_required", "请先扫描并分组，再识别代表图；不会逐张全量请求", 409)
    run = runner.start_run(
        db,
        request.app.state.settings,
        request.app.state.core_ai,
        scheme,
        request_id=payload.request_id,
        limit=payload.limit,
    )
    return run_out(run)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    _require_local(request)
    return run_out(store.get_run(request_db(request), run_id))


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(request: Request, run_id: str) -> dict[str, Any]:
    _require_local(request)
    existing = store.get_run(request_db(request), run_id)
    kind = store.run_kind(existing)
    if kind == "group_edit":
        raise MaterialFilterError("run_not_resumable", "分组编辑不支持作为扫描任务继续", 409)
    if kind in {"group_scan", "recognition"}:
        if not existing.get("kind"):
            request_db(request)[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"kind": kind}})
        return group_run_out(request, group_runner.resume(request_db(request), request.app.state.settings, request.app.state.core_ai, run_id))
    run = runner.resume_run(
        request_db(request),
        request.app.state.settings,
        request.app.state.core_ai,
        run_id,
    )
    return run_out(run)


@router.post("/runs/{run_id}/retry-failed", status_code=202)
def retry_failed(request: Request, run_id: str) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    run = store.get_run(db, run_id)
    if store.run_kind(run) in {"group_scan", "recognition", "group_edit"}:
        raise MaterialFilterError("explicit_selection_required", "请在组内选择需要重试的图片，不自动重试全方案", 409)
    scheme = store.get_scheme(db, run["scheme_id"])
    new_run = runner.retry_failed(db, request.app.state.settings, request.app.state.core_ai, scheme)
    return run_out(new_run)


@router.get("/schemes/{scheme_id}/results")
def list_results(
    request: Request,
    scheme_id: str,
    status: str = "all",
    q: str = "",
    skip: int = Query(default=0, ge=0),
    # The browser widens its visible window on demand; 500 covers a whole folder run.
    limit: int = Query(default=24, ge=1, le=500),
    sort: str = "judged",
) -> dict[str, Any]:
    _require_local(request)
    page = store.list_results(
        request_db(request), scheme_id, status=status, search=q, skip=skip, limit=limit, sort=sort
    )
    return {
        "items": [evaluation_out(request, item) for item in page["items"]],
        "total": page["total"],
        "skip": page["skip"],
        "limit": page["limit"],
        "counts": page["counts"],
    }


@router.patch("/evaluations/{evaluation_id}/metadata")
def update_metadata(request: Request, evaluation_id: str, payload: MetadataPayload) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    updated = store.update_metadata(
        db, evaluation, display_name=payload.display_name, tags=payload.tags,
        tag_groups=payload.tag_groups,
    )
    return evaluation_out(request, updated)


@router.post("/evaluations/{evaluation_id}/pick")
def pick_evaluation(request: Request, evaluation_id: str, payload: PickPayload) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    picked = store.set_manual_pick(db, evaluation, picked=payload.picked)
    return evaluation_out(request, picked)


@router.post("/schemes/{scheme_id}/picks")
def bulk_pick(request: Request, scheme_id: str, payload: BulkPickPayload) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    updated = store.bulk_manual_pick(db, scheme, payload.ids, picked=payload.picked)
    return {"updated": updated, "picked": payload.picked}


@router.post("/evaluations/{evaluation_id}/review")
def review_evaluation(request: Request, evaluation_id: str, payload: ReviewPayload) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    scheme = store.get_scheme(db, evaluation["scheme_id"])
    reviewed = store.review_evaluation(
        db,
        request.app.state.settings,
        scheme,
        evaluation,
        decision=payload.decision,
        source_sha256=payload.source_sha256 or evaluation["source_sha256"],
        request_id=payload.request_id,
    )
    return evaluation_out(request, reviewed)


@router.post("/evaluations/{evaluation_id}/recheck")
def recheck_evaluation(request: Request, evaluation_id: str) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    scheme = store.get_scheme(db, evaluation["scheme_id"])
    rechecked = runner.recheck_evaluation(
        db, request.app.state.settings, request.app.state.core_ai, scheme, evaluation
    )
    return evaluation_out(request, rechecked)


@router.get("/evaluations/{evaluation_id}/preview")
def preview_evaluation(request: Request, evaluation_id: str) -> FileResponse:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    scheme = store.get_scheme(db, evaluation["scheme_id"])
    store.ensure_preview(db, request.app.state.settings, scheme, evaluation)
    path = store.preview_file(evaluation, request.app.state.settings, scheme)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/evaluations/{evaluation_id}/download")
def download_evaluation(request: Request, evaluation_id: str) -> FileResponse:
    _require_local(request)
    db = request_db(request)
    evaluation = store.get_evaluation(db, evaluation_id)
    scheme = store.get_scheme(db, evaluation["scheme_id"])
    path = store.output_file(evaluation, request.app.state.settings, scheme)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=evaluation.get("output_name") or path.name,
    )


@router.post("/schemes/{scheme_id}/sync-uat", status_code=202)
def start_uat_sync(request: Request, scheme_id: str, payload: SyncUatPayload) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    settings = request.app.state.settings
    sync = uat_sync.start_sync(
        db,
        settings,
        scheme,
        cookie=payload.cookie or settings.material_filter_uat_cookie,
        force=payload.force,
        limit=payload.limit,
        dry_run=payload.dry_run,
    )
    return uat_sync.sync_out(sync)


@router.get("/syncs/{sync_id}")
def get_uat_sync(request: Request, sync_id: str) -> dict[str, Any]:
    _require_local(request)
    return uat_sync.sync_out(uat_sync.get_sync(request_db(request), sync_id))


@router.post("/schemes/{scheme_id}/export-picked")
def export_picked(request: Request, scheme_id: str) -> dict[str, Any]:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    return store.export_picked(db, request.app.state.settings, scheme)


@router.get("/schemes/{scheme_id}/export")
def export_scheme(request: Request, scheme_id: str) -> Response:
    _require_local(request)
    db = request_db(request)
    scheme = store.get_scheme(db, scheme_id)
    data = store.export_csv(db, scheme)
    safe_name = "".join(ch for ch in scheme.get("name") or "material-filter" if ch not in '\\/:*?"<>|')
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name)}.csv",
        },
    )
