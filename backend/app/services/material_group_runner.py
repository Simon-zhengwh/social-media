"""Background local grouping and bounded, explicitly selected recognition.

One scheme lease covers old/new runners. Each actual model attempt is durably
recorded before calling the client; unknown requests are never auto-replayed.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from app.services import material_filter_files as files
from app.services import material_filter_runner as native
from app.services import material_filters as store
from app.services import material_groups as groups
from app.services import material_similarity as similarity


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _counters():
    return {"scanned": 0, "processed": 0, "groups": 0, "recognized": 0,
            "cache_hits": 0, "model_calls": 0, "errors": 0, "copied": 0, "skipped": 0}


def create_scan_run(db, scheme, *, request_id):
    run, created = store.create_run(db, scheme, request_id="group-scan:" + request_id, kind="group_scan")
    if created:
        db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"kind": "group_scan", "counters": _counters()}})
    return store.get_run(db, run["_id"]), created


def create_recognition_run(db, settings, scheme, *, request_id, mode="representatives", representative_count=1,
                           group_limit=None, file_ids=None, force=False):
    groups.require_sampling_scheme(scheme)
    if mode not in {"representatives", "selected_files"} or representative_count not in {1, 2}:
        raise store.MaterialFilterError("invalid_recognition_plan", "识别模式或代表图数量非法")
    if force and mode != "selected_files":
        raise store.MaterialFilterError("force_selection_required", "重新识别必须明确选择图片")
    if group_limit is not None and not 1 <= group_limit <= 200:
        raise store.MaterialFilterError("invalid_group_limit", "试跑分组数量必须在1–200之间")
    requested = {"mode": mode, "representative_count": representative_count, "group_limit": group_limit,
                 "file_ids": list(dict.fromkeys(file_ids or [])), "force": bool(force)}
    fingerprint = _digest(requested)
    key = "group-recognize:" + request_id
    previous = db[store.RUN_COLLECTION].find_one({"scheme_id": scheme["_id"], "request_id": key})
    if previous:
        if previous.get("input_fingerprint") != fingerprint:
            raise store.MaterialFilterError("request_id_conflict", "同一次操作不能更换所选图片", 409)
        return previous, False
    scheme = store.get_scheme(db, scheme["_id"])
    revision = scheme.get("group_revision")
    if not revision or scheme.get("group_algorithm_version") != similarity.GROUPING_VERSION:
        raise store.MaterialFilterError("group_scan_required", "请先扫描并分组", 409)
    chosen_groups = []
    if mode == "representatives":
        if file_ids:
            raise store.MaterialFilterError("invalid_recognition_plan", "代表图模式不接收额外图片")
        chosen_groups = list(db[groups.COLLECTION].find({"scheme_id": scheme["_id"], "version": revision}).sort([("count", -1), ("_id", 1)]))
        if group_limit:
            chosen_groups = chosen_groups[:group_limit]
        ids = [fid for g in chosen_groups for fid in groups.representatives(g, representative_count)]
    else:
        ids = requested["file_ids"]
        if not 1 <= len(ids) <= 200:
            raise store.MaterialFilterError("file_selection_required", "请选择1–200张照片")
    # Published group membership is authoritative. File pointers are a cache
    # and can lag if a process stops after publishing a new revision.
    current_groups = chosen_groups or list(db[groups.COLLECTION].find({"scheme_id": scheme["_id"], "version": revision}))
    membership = {fid: g["_id"] for g in current_groups for fid in g["file_ids"]}
    plans = []
    seen_group_content = set()
    for fid in ids:
        file = groups.get_file(db, fid)
        if file["scheme_id"] != scheme["_id"] or fid not in membership:
            raise store.MaterialFilterError("file_outside_scheme", "图片不属于当前方案或分组版本", 409)
        if not file.get("sha256") or file.get("feature_error"):
            raise store.MaterialFilterError("file_not_readable", "图片读取失败，请先重新扫描", 409)
        if mode == "representatives":
            content_key = (membership[fid], file["sha256"])
            if content_key in seen_group_content:
                continue
            seen_group_content.add(content_key)
        plans.append({"file_id": fid, "sha256": file["sha256"], "relative_path": file["relative_path"], "group_id": membership[fid]})
    run, created = store.create_run(db, scheme, request_id=key, kind="recognition")
    if created:
        if store.get_scheme(db, scheme["_id"]).get("group_revision") != revision:
            db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "error": "分组在制定计划时更新，请刷新", "finished_at": store.now()}})
            store.release_scheme_run(db, scheme["_id"], run["_id"])
            raise store.MaterialFilterError("group_stale", "分组已更新，请刷新", 409)
        db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"kind": "recognition", "mode": mode,
            "force": bool(force), "representative_count": representative_count, "group_ids": [g["_id"] for g in chosen_groups],
            "group_revision": revision, "group_algorithm_version": scheme["group_algorithm_version"],
            "files": plans, "counters": dict(_counters(), scanned=len(plans)), "input_fingerprint": fingerprint}})
    return store.get_run(db, run["_id"]), created


class _TrackedClient:
    def __init__(self, inner, db, attempt_id, run_id, counters):
        self.inner, self.db, self.attempt_id = inner, db, attempt_id
        self.run_id, self.counters = run_id, counters
        self.last_error = None

    def chat_json(self, *args, **kwargs):
        self.db[groups.ATTEMPTS].update_one({"_id": self.attempt_id}, {"$set": {"actual_call": True, "called_at": store.now()}})
        self.counters["model_calls"] += 1
        store.set_run_counters(self.db, self.run_id, self.counters)
        try:
            return self.inner.chat_json(*args, **kwargs)
        except Exception as exc:
            self.last_error = exc
            raise


def _recognize(db, settings, client, run, scheme, counters):
    prior = {a["file_id"]: a for a in db[groups.ATTEMPTS].find({"run_id": run["_id"]})}
    for old in prior.values():
        if old["status"] == "running" and old.get("actual_call"):
            old["status"] = "unknown"
            db[groups.ATTEMPTS].update_one({"_id": old["_id"]}, {"$set": {"status": "unknown", "error": "进程中断，结果未知；请选择图片主动重试"}})
    settled = [a for a in prior.values() if a["status"] in {"succeeded", "cached", "failed", "unknown"}]
    # Recover counters from the durable ledger, including a call interrupted
    # between result persistence and updating the run progress.
    counters.update(model_calls=sum(bool(a.get("actual_call")) for a in prior.values()),
                    processed=len(settled), errors=sum(a["status"] in {"failed", "unknown"} for a in settled),
                    recognized=sum(a["status"] == "succeeded" for a in settled),
                    cache_hits=sum(a["status"] == "cached" for a in settled),
                    copied=sum(bool(a.get("copied")) for a in settled))
    for plan in run["files"]:
        fid = plan["file_id"]
        old = prior.get(fid)
        if old and old["status"] != "running":
            continue
        aid = old["_id"] if old else "mattempt_" + _digest([run["_id"], fid])[:24]
        file = groups.get_file(db, fid)
        previous_attempt = groups.latest_attempt(db, scheme, file)
        blocked_unknown = previous_attempt and previous_attempt["status"] == "unknown" and not run["force"]
        attempt = {"_id": aid, "run_id": run["_id"], "scheme_id": scheme["_id"], "file_id": fid,
                   "source_sha256": plan["sha256"], "status": "running", "actual_call": False,
                   "created_at": old.get("created_at", store.now()) if old else store.now(), "force": run["force"], "model": scheme["model"],
                   "prompt_version": scheme["prompt_version"], "menu_version": (scheme.get("menu") or {}).get("menu_version")}
        if old:
            db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {k:v for k,v in attempt.items() if k != "_id"}})
        else:
            db[groups.ATTEMPTS].insert_one(attempt)
        tracked = _TrackedClient(client, db, aid, run["_id"], counters)
        try:
            if blocked_unknown and not groups.cached_for_file(db, scheme, file):
                counters["errors"] += 1
                counters["skipped"] += 1
                db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {"status": "unknown", "error": "之前请求结果未知，未再次发送；请选择图片主动重新识别", "finished_at": store.now()}})
                continue
            snapshot = files.read_source_file(scheme["source_root_snapshot"], plan["relative_path"], max_bytes=settings.material_filter_max_source_bytes)
            if snapshot.sha256 != plan["sha256"]:
                raise store.MaterialFilterError("source_changed", "源图片已变化，请重新扫描分组", 409)
            cached = groups.cached_for_file(db, scheme, file)
            if cached and not run["force"]:
                counters["cache_hits"] += 1
                db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {"status": "cached", "evaluation_id": cached["_id"], "result": cached.get("model_result"), "finished_at": store.now()}})
                continue
            existing = store.find_evaluation(db, scheme["_id"], fid, snapshot.sha256)
            db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {"previous_result": existing.get("model_result") if existing else None}})
            if existing:
                result = native.recheck_evaluation(db, settings, tracked, scheme, existing)
            else:
                evaluator = native._Evaluator(db, settings, tracked, scheme, {}, run["_id"])
                entry = files.SourceEntry(file["relative_path"], file["original_name"], snapshot.size, snapshot.mtime_ns, str(scheme["source_root_snapshot"]))
                evaluator.evaluate(entry, snapshot, file, store.scheme_directory(settings, scheme))
                result = store.find_evaluation(db, scheme["_id"], fid, snapshot.sha256)
            if not result or result.get("decision") == "error":
                raise store.MaterialFilterError("recognition_failed", (result or {}).get("error") or "未获得模型结果", 502)
            counters["recognized"] += 1
            counters["copied"] += int(result.get("copy_status") == "copied")
            store.upsert_file(db, scheme, files.SourceEntry(file["relative_path"], file["original_name"], snapshot.size, snapshot.mtime_ns, ""),
                              size=snapshot.size, mtime_ns=snapshot.mtime_ns, sha256=snapshot.sha256, latest_evaluation_id=result["_id"])
            db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {"status": "succeeded", "evaluation_id": result["_id"], "result": result.get("model_result"), "copied": result.get("copy_status") == "copied", "finished_at": store.now()}})
            if run["mode"] == "selected_files" and plan.get("group_id"):
                db[groups.COLLECTION].update_one({"_id": plan["group_id"]}, {"$set": {"reference_stale": True}})
        except Exception as exc:
            transport_code = str(getattr(tracked.last_error, "code", ""))
            unknown = bool(tracked.last_error and (
                "timeout" in transport_code or transport_code == "gateway_error"
                or getattr(tracked.last_error, "status_code", None) in {408, 504}
            ))
            counters["errors"] += 1
            db[groups.ATTEMPTS].update_one({"_id": aid}, {"$set": {"status": "unknown" if unknown else "failed", "error": str(exc)[:1500], "finished_at": store.now()}})
            if getattr(tracked.last_error, "status_code", None) == 400:
                raise store.MaterialFilterError("image_request_rejected", "图片请求被拒绝，已停止后续请求", 502) from exc
        finally:
            counters["processed"] += 1
            store.set_run_counters(db, run["_id"], counters)
    if run["mode"] == "representatives":
        for gid in run.get("group_ids", []):
            group = groups.get_group(db, gid)
            groups._refresh_reference(db, group, count=run["representative_count"])


def _validate_recognition_revision(run, scheme):
    groups.require_sampling_scheme(scheme)
    if (run.get("group_revision") != scheme.get("group_revision")
            or run.get("group_algorithm_version") != similarity.GROUPING_VERSION
            or scheme.get("group_algorithm_version") != similarity.GROUPING_VERSION):
        raise store.MaterialFilterError("group_stale", "分组或算法已更新，请刷新后创建新的识别任务", 409)


def run_job(db, settings, client, run_id):
    claimed = db[store.RUN_COLLECTION].find_one_and_update({"_id": run_id, "status": "queued"}, {"$set": {"status": "running", "started_at": store.now()}})
    if not claimed:
        return store.get_run(db, run_id)
    run = store.get_run(db, run_id)
    scheme = store.get_scheme(db, run["scheme_id"])
    counters = dict(run.get("counters") or _counters())
    status, error = "succeeded", None
    try:
        if run["kind"] == "group_scan":
            def progress(value):
                counters.update(value)
                store.set_run_counters(db, run_id, counters)
            result = groups.scan_scheme(db, settings, scheme, progress=progress)
            counters.update(scanned=result["files"], processed=result["files"], groups=result["groups"], errors=len(result["errors"]))
            db[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"scan_errors": result["errors"]}})
        else:
            _validate_recognition_revision(run, scheme)
            _recognize(db, settings, client, run, scheme, counters)
    except Exception as exc:
        status, error = "failed", str(exc)[:1500]
    finally:
        db[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"status": status, "error": error, "counters": counters, "finished_at": store.now()}})
        store.release_scheme_run(db, scheme["_id"], run_id)
    return store.get_run(db, run_id)


def _spawn(db, settings, client, run_id):
    threading.Thread(target=run_job, args=(db, settings, client, run_id), name=f"material-groups-{run_id}", daemon=True).start()


def start_scan(db, settings, scheme, *, request_id):
    run, created = create_scan_run(db, scheme, request_id=request_id)
    if created:
        _spawn(db, settings, None, run["_id"])
    return store.get_run(db, run["_id"])


def start_recognition(db, settings, client, scheme, **kwargs):
    run, created = create_recognition_run(db, settings, scheme, **kwargs)
    if created:
        _spawn(db, settings, client, run["_id"])
    return store.get_run(db, run["_id"])


def resume(db, settings, client, run_id):
    run = store.get_run(db, run_id)
    if run.get("kind") not in {"group_scan", "recognition"} or run["status"] != "interrupted":
        raise store.MaterialFilterError("run_not_resumable", "只有中断的分组任务可继续", 409)
    scheme = store.get_scheme(db, run["scheme_id"])
    lease_query = {"_id": scheme["_id"], "active_run_id": None}
    if run["kind"] == "recognition":
        _validate_recognition_revision(run, scheme)
        lease_query.update(group_revision=run["group_revision"], group_algorithm_version=run["group_algorithm_version"])
    claimed = db[store.SCHEME_COLLECTION].find_one_and_update(lease_query, {"$set": {"active_run_id": run_id}})
    if not claimed:
        raise store.MaterialFilterError("scheme_busy", "方案正在运行", 409)
    db[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"status": "queued", "error": None}})
    _spawn(db, settings, client, run_id)
    return store.get_run(db, run_id)
