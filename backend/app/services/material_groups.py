"""Local-only image grouping and explicit representative plans.

No client or model is accepted by the scan API. Files exist before evaluations;
group reference names never masquerade as individual recognition results.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from app.services import material_filter_files as files
from app.services import material_filters as store
from app.services import material_similarity as similarity
from app.services import material_local_match as local_match
from app.time_utils import utc_iso

COLLECTION = "material_filter_groups"
ATTEMPTS = "material_filter_attempts"
MAX_MANUAL_NAME = 60
GROUP_PREVIEW_LIMIT = 8


def sampling_enabled(scheme: dict) -> bool:
    return scheme.get("naming_mode") == "menu_match" and not scheme.get("rules")


def require_sampling_scheme(scheme: dict) -> None:
    if not sampling_enabled(scheme):
        raise store.MaterialFilterError("group_mode_unavailable", "抽样识别只用于无硬过滤规则的菜单命名方案", 409)


def get_file(db, file_id: str) -> dict:
    row = db[store.FILE_COLLECTION].find_one({"_id": file_id})
    if not row:
        raise store.MaterialFilterError("file_not_found", "图片记录不存在", 404)
    return row


def get_group(db, group_id: str, *, current: bool = True) -> dict:
    row = db[COLLECTION].find_one({"_id": group_id})
    if not row:
        raise store.MaterialFilterError("group_not_found", "分组不存在", 404)
    if current and store.get_scheme(db, row["scheme_id"]).get("group_revision") != row["version"]:
        raise store.MaterialFilterError("group_stale", "分组已更新，请刷新页面", 409)
    return row


@contextmanager
def _edit_group(db, group_id: str, version: str, *, request_id: str = ""):
    """Use the same atomic lease as model/scan jobs, including crash recovery."""
    group = get_group(db, group_id)
    scheme = store.get_scheme(db, group["scheme_id"])
    run, created = store.create_run(db, scheme, request_id=request_id, kind="group_edit")
    if not created:
        raise store.MaterialFilterError("group_edit_exists", "同一次分组操作已存在，请刷新后查看结果", 409)
    db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"status": "running"}})
    status, error = "succeeded", None
    try:
        group = get_group(db, group_id)
        if group["version"] != version:
            raise store.MaterialFilterError("group_stale", "分组已更新，请刷新", 409)
        yield group, scheme
    except Exception as exc:
        status, error = "failed", str(exc)[:500]
        raise
    finally:
        db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"status": status, "error": error, "finished_at": store.now()}})
        store.release_scheme_run(db, scheme["_id"], run["_id"])


def _preview_path(directory: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    target = (directory / relative).resolve()
    if not target.is_relative_to(directory.resolve()):
        return None
    return target if target.is_file() else None


def _local_features(directory: Path, relative: str | None, source_sha256: str) -> dict | None:
    path = _preview_path(directory, relative)
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) and value.get("source_sha256") == source_sha256 and local_match.valid_features(value) else None
    except (OSError, ValueError, TypeError):
        return None


def cached_evaluations(db, scheme: dict) -> dict[tuple[str, str], dict]:
    result = {}
    for row in db[store.EVALUATION_COLLECTION].find({"scheme_id": scheme["_id"], "decision": {"$in": ["pass", "fail", "uncertain"]}}):
        if row.get("model") != scheme.get("model") or row.get("prompt_version") != scheme.get("prompt_version"):
            continue
        if scheme.get("naming_mode") == "menu_match" and (row.get("naming") or {}).get("menu_version") != (scheme.get("menu") or {}).get("menu_version"):
            continue
        result[(row["file_id"], row.get("source_sha256"))] = row
    return result


def valid_evaluation(file: dict, cached: dict) -> dict | None:
    return cached.get((file["_id"], file.get("sha256"))) if file.get("sha256") else None


def _prepare(entry, old, directory: Path, settings):
    same = old and old.get("size") == entry.size and old.get("mtime_ns") == entry.mtime_ns
    preview = _preview_path(directory, old.get("preview_path")) if old else None
    hash_cached = bool(same and (old.get("features") or {}).get("version") == similarity.VERSION)
    alignment = (_local_features(directory, old.get("local_match_path"), old["sha256"])
                 if same and old.get("sha256") and not old.get("feature_error") and old.get("local_match_version") == local_match.VERSION else None)
    if same and preview and hash_cached and alignment:
        return {"entry": entry, "old": old, "reuse": True}
    local_preview = bool(same and preview and old.get("sha256"))
    if local_preview:
        jpeg = preview.read_bytes()
        sha = old["sha256"]
        size, mtime = old["size"], old["mtime_ns"]
    else:
        snapshot = files.read_source_snapshot(settings["source"], entry, max_bytes=settings["max_bytes"])
        jpeg = files.build_preview_jpeg(snapshot.data)
        sha, size, mtime = snapshot.sha256, snapshot.size, snapshot.mtime_ns
    return {"entry": entry, "old": old, "reuse": False, "jpeg": jpeg,
            "features": old["features"] if hash_cached else similarity.extract_features(jpeg),
            "alignment": alignment or local_match.extract_features(jpeg), "sha256": sha, "size": size, "mtime_ns": mtime,
            "preview_path": old["preview_path"] if local_preview else None,
            "local_match_path": old["local_match_path"] if alignment else None}


def scan_scheme(db, settings, scheme: dict, *, progress: Callable | None = None) -> dict:
    """Scan all supported images, cache local features, publish one complete revision."""
    scheme = store.get_scheme(db, scheme["_id"])
    directory = store.scheme_directory(settings, scheme)
    entries = files.scan_source(scheme["source_root_snapshot"], recursive=bool(scheme.get("recursive")), max_files=settings.material_filter_max_files_per_run)
    existing = {r["path_key"]: r for r in db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"]})}
    cached = cached_evaluations(db, scheme)
    # Existing successful previews are useful even before the first group scan.
    for old in existing.values():
        evaluation = valid_evaluation(old, cached)
        if not old.get("preview_path") and evaluation:
            old["preview_path"] = evaluation.get("preview_path")
    counters = {"scanned": len(entries), "processed": 0, "groups": 0, "errors": 0, "skipped": 0}
    errors = []
    current = []
    if progress:
        progress(counters)
    config = {"source": scheme["source_root_snapshot"], "max_bytes": settings.material_filter_max_source_bytes}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="material-features") as pool:
        pending = {pool.submit(_prepare, e, existing.get(store.path_key(e.relative_path)), directory, config): e for e in entries}
        for future in as_completed(pending):
            entry = pending[future]
            try:
                prepared = future.result()
                old = prepared["old"]
                if prepared["reuse"]:
                    row = dict(old)
                    store.mark_file_seen(db, row["_id"], size=entry.size, mtime_ns=entry.mtime_ns)
                    counters["skipped"] += 1
                else:
                    row = store.upsert_file(db, scheme, entry, size=prepared["size"], mtime_ns=prepared["mtime_ns"], sha256=prepared["sha256"], latest_evaluation_id=(old or {}).get("latest_evaluation_id"))
                    preview_path = prepared.get("preview_path")
                    if not preview_path:
                        name = f"{row['_id']}-{row['sha256'][:12]}.jpg"
                        preview_dir = files.ensure_local_directory(directory / "group-previews")
                        target = preview_dir / name
                        if not target.is_file() or target.read_bytes() != prepared["jpeg"]:
                            files._atomic_write(preview_dir, name, prepared["jpeg"])
                        preview_path = f"group-previews/{name}"
                    row.update(features=prepared["features"], preview_path=preview_path)
                    local_path = prepared.get("local_match_path")
                    if not local_path:
                        local_dir = files.ensure_local_directory(directory / "group-features")
                        # Keep the full path within Windows limits even under
                        # long scheme roots; version text is never a path.
                        version_key = hashlib.sha256(local_match.VERSION.encode()).hexdigest()[:8]
                        name = f"{row['sha256'][:24]}-{version_key}.json"
                        raw = json.dumps(dict(prepared["alignment"], source_sha256=row["sha256"]), separators=(",", ":")).encode("utf-8")
                        target = local_dir / name
                        if not target.is_file() or target.read_bytes() != raw:
                            files._atomic_write(local_dir, name, raw)
                        local_path = f"group-features/{name}"
                    row.update(local_match_path=local_path, local_match_version=local_match.VERSION)
                    db[store.FILE_COLLECTION].update_one({"_id": row["_id"]}, {"$set": {"features": row["features"], "preview_path": row["preview_path"],
                        "local_match_path": local_path, "local_match_version": local_match.VERSION, "feature_error": None}})
                current.append(row)
            except Exception as exc:
                counters["errors"] += 1
                failed = store.upsert_file(db, scheme, entry, size=entry.size, mtime_ns=entry.mtime_ns, sha256=None, latest_evaluation_id=None)
                db[store.FILE_COLLECTION].update_one({"_id": failed["_id"]}, {"$set": {"features": None, "preview_path": None,
                    "local_match_path": None, "local_match_version": None, "feature_error": str(exc)[:500], "group_id": None, "group_revision": None}})
                errors.append({"file_id": failed["_id"], "relative_path": entry.relative_path, "error": str(exc)[:500]})
            counters["processed"] += 1
            if progress:
                progress(counters)

    version = store.new_id("grev")
    cached_ids = {r["_id"] for r in current if valid_evaluation(r, cached)}
    candidates = [dict(r, id=r["_id"], features=dict(r["features"], alignment=_local_features(directory, r.get("local_match_path"), r["sha256"]))) for r in current]
    by_id = {r["_id"]: r for r in candidates}
    forced = []
    assigned = set()
    for override in scheme.get("manual_groups") or []:
        members = [by_id[m["file_id"]] for m in override["members"] if m["file_id"] in by_id
                   and by_id[m["file_id"]]["sha256"] == m["source_sha256"] and m["file_id"] not in assigned]
        if members:
            forced.append(members)
            assigned.update(m["_id"] for m in members)
    partitions = {}
    for candidate in candidates:
        if candidate["_id"] not in assigned:
            partitions.setdefault(candidate.get("similarity_partition") or "", []).append(candidate)
    clusters = forced + [cluster for partition in partitions.values() for cluster in similarity.cluster_files(partition)]
    previous = list(db[COLLECTION].find({"scheme_id": scheme["_id"], "version": scheme.get("group_revision")})) if scheme.get("group_revision") else []
    used_previous = set()
    group_ids = []
    for members in clusters:
        ids = [row["_id"] for row in members]
        reps = similarity.choose_representatives(members, count=2, cached_ids=cached_ids)
        old_groups = sorted(previous, key=lambda g: len(set(g["file_ids"]) & set(ids)), reverse=True)
        prior = next((g for g in old_groups if g["_id"] not in used_previous and set(g["file_ids"]) & set(ids)), None)
        manual = bool(prior and prior.get("manual_representatives") and all(fid in ids for fid in prior["representative_file_ids"]))
        if manual:
            shas = {m["_id"]: m["sha256"] for m in members}
            seen = set()
            reps = []
            for fid in prior["representative_file_ids"]:
                if shas[fid] not in seen:
                    seen.add(shas[fid])
                    reps.append(fid)
        row = {"_id": store.new_id("mgroup"), "scheme_id": scheme["_id"], "folder_id": scheme["folder_id"],
               "version": version, "algorithm_version": similarity.GROUPING_VERSION, "file_ids": ids, "count": len(ids), "representative_file_ids": reps,
               "manual_representatives": manual, "reference_name": None, "status": "unrecognized", "reference_stale": False,
               "manual_group": bool(set(ids) & assigned),
               "created_at": store.now()}
        inherited_name = str((prior or {}).get("reference_name") or "").strip() if (prior or {}).get("name_manual") else ""
        if inherited_name:
            # A human-confirmed group name survives rescans of the same membership.
            row.update(reference_name=inherited_name, name_manual=True, status="manual")
        if prior:
            used_previous.add(prior["_id"])
        db[COLLECTION].insert_one(row)
        group_ids.append(row["_id"])
        for file in members:
            db[store.FILE_COLLECTION].update_one({"_id": file["_id"]}, {"$set": {"group_id": row["_id"], "group_revision": version}})
        _refresh_reference(db, row, cached)
    db[store.SCHEME_COLLECTION].update_one({"_id": scheme["_id"]}, {"$set": {"group_revision": version, "group_algorithm_version": similarity.GROUPING_VERSION,
        "group_scan_total": len(entries), "group_scan_errors": errors, "group_scan_finished_at": store.now()}})
    counters["groups"] = len(group_ids)
    if progress:
        progress(counters)
    return {"files": len(entries), "groups": len(group_ids), "errors": errors, "version": version}


def representatives(group: dict, count: int = 1) -> list[str]:
    if count not in {1, 2}:
        raise store.MaterialFilterError("invalid_representative_count", "代表图数量只能为1或2")
    ids = group.get("representative_file_ids") or []
    return ids[:count]


def _refresh_reference(db, group: dict, cached=None, *, count: int = 1) -> None:
    scheme = store.get_scheme(db, group["scheme_id"])
    cache = cached if cached is not None else cached_evaluations(db, scheme)
    values = []
    evaluation_ids = []
    uncertain = False
    for fid in representatives(group, count):
        file = db[store.FILE_COLLECTION].find_one({"_id": fid})
        value = valid_evaluation(file, cache) if file else None
        if value:
            values.append(value.get("display_name") or "")
            evaluation_ids.append(value["_id"])
            uncertain |= value.get("decision") == "uncertain"
    names = set(v.casefold().strip() for v in values if v)
    status = "conflict" if len(names) > 1 else "ambiguous" if uncertain else "recognized" if names else "unrecognized"
    values_to_set: dict = {"reference_evaluation_ids": evaluation_ids, "status": status,
        "reference_stale": False, "reference_updated_at": store.now()}
    if group.get("name_manual") and str(group.get("reference_name") or "").strip():
        # A human-confirmed group name is the export file name; recognition must not overwrite it.
        values_to_set.update(reference_name=group["reference_name"], status="manual")
    else:
        values_to_set["reference_name"] = values[0] if values else None
    db[COLLECTION].update_one({"_id": group["_id"]}, {"$set": values_to_set})


def cached_for_file(db, scheme: dict, file: dict) -> dict | None:
    return valid_evaluation(file, cached_evaluations(db, scheme))


def latest_attempt(db, scheme: dict, file: dict) -> dict | None:
    return db[ATTEMPTS].find_one({"scheme_id": scheme["_id"], "file_id": file["_id"],
        "source_sha256": file.get("sha256"), "model": scheme.get("model"),
        "prompt_version": scheme.get("prompt_version"),
        "menu_version": (scheme.get("menu") or {}).get("menu_version")}, sort=[("created_at", -1), ("_id", -1)])


def attempt_out(row: dict | None) -> dict | None:
    if not row:
        return None
    value = {"id": row["_id"], **{key: row.get(key) for key in ["status", "error", "actual_call", "created_at", "called_at", "finished_at",
        "result", "previous_result", "source_sha256", "model", "prompt_version", "force"]}}
    for key in ["created_at", "called_at", "finished_at"]:
        value[key] = utc_iso(value[key])
    return value


def list_attempts(db, file_id: str, *, skip=0, limit=20) -> dict:
    file = get_file(db, file_id)
    query = {"file_id": file_id, "scheme_id": file["scheme_id"]}
    rows = list(db[ATTEMPTS].find(query).sort([("created_at", -1), ("_id", -1)]).skip(skip).limit(limit))
    return {"items": [attempt_out(row) for row in rows], "total": db[ATTEMPTS].count_documents(query), "skip": skip, "limit": limit}


def file_out(db, settings, scheme, row: dict, *, rep_ids=(), cache=None) -> dict:
    cached = cache if cache is not None else cached_evaluations(db, scheme)
    evaluation = valid_evaluation(row, cached)
    state = "uncertain" if evaluation and evaluation.get("decision") == "uncertain" else "recognized" if evaluation else "unrecognized"
    attempt = latest_attempt(db, scheme, row)
    if row.get("feature_error") or (attempt and attempt["status"] == "failed"):
        state = "error"
    elif attempt and attempt["status"] == "unknown":
        state = "unknown"
    return {"id": row["_id"], "group_id": row.get("group_id"), "original_name": row.get("original_name"),
        "relative_path": row["relative_path"], "display_name": evaluation.get("display_name") if evaluation else row.get("original_name"),
        "recognition_status": state, "source_sha256": row.get("sha256"), "source_size": row.get("size"),
        "is_representative": row["_id"] in rep_ids, "preview_url": None, "evaluation": evaluation,
        "latest_attempt": attempt_out(attempt), "attempt_count": db[ATTEMPTS].count_documents({"file_id": row["_id"], "scheme_id": scheme["_id"]})}


def group_out(db, settings, scheme, group: dict, count=1, *, cache=None, file_map=None) -> dict:
    cached = cache if cache is not None else cached_evaluations(db, scheme)
    members = file_map if file_map is not None else {r["_id"]: r for r in db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"], "_id": {"$in": group["file_ids"]}})}
    reps = representatives(group, count)
    cover = members[reps[0] if reps else group["file_ids"][0]]
    evaluation_by_file = {fid: valid_evaluation(members[fid], cached) for fid in group["file_ids"] if fid in members}
    values = list(evaluation_by_file.values())
    picked = [fid for fid, value in evaluation_by_file.items() if value and value.get("manual_pick")]
    ordered = sorted(group["file_ids"], key=lambda fid: members[fid]["relative_path"].casefold())
    preview_ids = list(dict.fromkeys([*sorted(picked, key=lambda fid: members[fid]["relative_path"].casefold()), *reps, *ordered]))[:GROUP_PREVIEW_LIMIT]
    previews = [{"id": fid, "group_id": group["_id"], "original_name": members[fid].get("original_name"),
        "relative_path": members[fid]["relative_path"], "source_sha256": members[fid].get("sha256"),
        "is_representative": fid in reps, "manual_pick": fid in picked, "preview_url": None} for fid in preview_ids]
    return {"id": group["_id"], "scheme_id": scheme["_id"], "version": group["version"], "count": group["count"],
        "manual": bool(group.get("manual_group")), "previews": previews, "picked_file_ids": picked,
        "name_manual": bool(group.get("name_manual")),
        "reference_name": group.get("reference_name"), "status": group.get("status") or "unrecognized",
        "cover": file_out(db, settings, scheme, dict(cover, group_id=group["_id"]), rep_ids=reps, cache=cached), "representative_file_ids": reps,
        "recognized_count": sum(v is not None for v in values), "picked_count": sum(bool(v and v.get("manual_pick")) for v in values),
        "reference_stale": bool(group.get("reference_stale"))}


def list_groups(db, settings, scheme: dict, *, skip=0, limit=30, q="", representative_count=1) -> dict:
    scheme = store.get_scheme(db, scheme["_id"])
    query = {"scheme_id": scheme["_id"], "version": scheme.get("group_revision")}
    all_groups = list(db[COLLECTION].find(query).sort([("count", -1), ("_id", 1)])) if scheme.get("group_revision") else []
    member_ids = [fid for group in all_groups for fid in group["file_ids"]]
    members = {r["_id"]: r for r in db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"], "_id": {"$in": member_ids}})} if all_groups else {}
    cached = cached_evaluations(db, scheme)
    rep_ids = [fid for g in all_groups for fid in representatives(g, representative_count)]
    hits = sum(bool(valid_evaluation(members[fid], cached)) for fid in rep_ids)
    blocked = sum(not valid_evaluation(members[fid], cached) and (latest_attempt(db, scheme, members[fid]) or {}).get("status") == "unknown" for fid in rep_ids)
    stats = {"files": scheme.get("group_scan_total", sum(g["count"] for g in all_groups)), "groups": len(all_groups),
             "recognized": sum(bool(valid_evaluation(f, cached)) for f in members.values()),
             "representatives": len(rep_ids), "cache_hits": hits, "blocked_unknown": blocked,
             "new_calls": len(rep_ids)-hits-blocked if sampling_enabled(scheme) else 0}
    if q.strip():
        term = q.strip().casefold()
        all_groups = [g for g in all_groups if term in str(g.get("reference_name") or "").casefold() or any(term in str(members[fid].get("relative_path") or "").casefold() for fid in g["file_ids"])]
    last = db[store.RUN_COLLECTION].find_one({"_id": scheme["active_run_id"]}) if scheme.get("active_run_id") else None
    if last and last.get("kind") not in {"group_scan", "recognition"}:
        last = None
    last = last or db[store.RUN_COLLECTION].find_one({"scheme_id": scheme["_id"], "kind": {"$in": ["group_scan", "recognition"]}}, sort=[("created_at", -1)])
    return {"items": [group_out(db, settings, scheme, g, representative_count, cache=cached, file_map=members) for g in all_groups[max(0, skip):max(0, skip)+limit]],
            "total": len(all_groups), "stats": stats, "last_run": last, "scan_errors": scheme.get("group_scan_errors") or [],
            "sampling_enabled": sampling_enabled(scheme), "version": scheme.get("group_revision")}


def list_members(db, settings, group_id, *, skip=0, limit=48, representative_count=1) -> dict:
    group = get_group(db, group_id)
    scheme = store.get_scheme(db, group["scheme_id"])
    rows = list(db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"], "_id": {"$in": group["file_ids"]}}).sort("relative_path", 1))
    cached = cached_evaluations(db, scheme)
    reps = representatives(group, representative_count)
    return {"items": [file_out(db, settings, scheme, dict(r, group_id=group_id), rep_ids=reps, cache=cached) for r in rows[skip:skip+limit]], "total": len(rows), "skip": skip, "limit": limit}


def set_representatives(db, settings, group_id, file_ids, *, version: str) -> dict:
    with _edit_group(db, group_id, version) as (group, scheme):
        if not 1 <= len(file_ids) <= 2 or len(set(file_ids)) != len(file_ids) or any(fid not in group["file_ids"] for fid in file_ids):
            raise store.MaterialFilterError("invalid_representatives", "请选择本组1–2张不同的照片")
        shas = [get_file(db, fid).get("sha256") for fid in file_ids]
        if len(set(shas)) != len(shas):
            raise store.MaterialFilterError("duplicate_representatives", "内容完全相同的图片只需选择一张代表图")
        db[COLLECTION].update_one({"_id": group_id}, {"$set": {"representative_file_ids": file_ids, "manual_representatives": True, "reference_stale": True}})
        return group_out(db, settings, scheme, get_group(db, group_id), count=2)


def adopt_reference(db, settings, group_id, file_id, *, version):
    with _edit_group(db, group_id, version) as (group, scheme):
        if file_id not in group["file_ids"]:
            raise store.MaterialFilterError("group_stale", "图片不属于当前分组版本", 409)
        row = cached_for_file(db, scheme, get_file(db, file_id))
        if not row:
            raise store.MaterialFilterError("recognition_required", "请先识别这张图片", 409)
        db[COLLECTION].update_one({"_id": group_id}, {"$set": {"reference_name": row["display_name"], "reference_evaluation_ids": [row["_id"]],
            "name_manual": True, "reference_stale": False,
            "status": "ambiguous" if row["decision"] == "uncertain" else "recognized", "reference_updated_at": store.now()}})
        return group_out(db, settings, scheme, get_group(db, group_id))


def rename_group(db, settings, group_id: str, name: str, *, version: str) -> dict:
    """Set the human group name used as the export/upload file name for picks.

    Only the group label changes; per-photo recognition results, picks and local
    copies stay untouched, and the published grouping revision is not bumped.
    """
    group = get_group(db, group_id)
    scheme = store.get_scheme(db, group["scheme_id"])
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise store.MaterialFilterError("group_name_required", "组名不能为空")
    if len(cleaned) > MAX_MANUAL_NAME:
        raise store.MaterialFilterError("group_name_too_long", f"组名不能超过 {MAX_MANUAL_NAME} 个字符")
    result = db[COLLECTION].update_one({"_id": group_id, "version": version}, {"$set": {
        "reference_name": cleaned, "name_manual": True, "status": "manual",
        "reference_stale": False, "reference_updated_at": store.now()}})
    if not result.matched_count:
        raise store.MaterialFilterError("group_stale", "分组已更新，请刷新页面", 409)
    return group_out(db, settings, scheme, get_group(db, group_id))


def manual_names_by_file(db, scheme: dict) -> dict[str, str]:
    """file_id -> human group name for picked export and UAT upload naming.

    Only menu-naming schemes name deliverables after the group label; other
    schemes keep their per-photo names.
    """
    if scheme.get("naming_mode") != "menu_match":
        return {}
    rows = db[COLLECTION].find({"scheme_id": scheme["_id"], "version": scheme.get("group_revision"), "name_manual": True})
    names: dict[str, str] = {}
    for row in rows:
        name = str(row.get("reference_name") or "").strip()
        if not name:
            continue
        for file_id in row.get("file_ids") or []:
            names[file_id] = name
    return names


def _manual_assignments(scheme, affected, parts, members):
    overrides = []
    for entry in scheme.get("manual_groups") or []:
        remaining = [m for m in entry["members"] if m["file_id"] not in affected]
        if remaining:
            overrides.append(dict(entry, members=remaining))
    for part in parts:
        overrides.append({"id": store.new_id("manual"), "members": [
            {"file_id": fid, "source_sha256": members[fid]["sha256"]} for fid in part]})
    return overrides


def _publish_edit(db, settings, scheme, old_groups, replacements, overrides, *, operation=None):
    """Stage new rows, then atomically publish membership and manual overrides."""
    new_version = store.new_id("grev")
    cache = cached_evaluations(db, scheme)
    output = []
    memberships = []
    for old in old_groups:
        parts = replacements.get(old["_id"])
        if parts == []:
            continue
        for ids, manual in parts if parts is not None else [(old["file_ids"], old.get("manual_group", False))]:
            row = dict(old, _id=store.new_id("mgroup"), version=new_version, file_ids=ids, count=len(ids), manual_group=manual)
            if parts is not None:
                members = [dict(get_file(db, fid), id=fid) for fid in ids]
                row.update(representative_file_ids=similarity.choose_representatives(members, count=2,
                    cached_ids={f["_id"] for f in members if valid_evaluation(f, cache)}), manual_representatives=False)
            db[COLLECTION].insert_one(row)
            memberships.append((row["_id"], ids))
            if parts is not None:
                _refresh_reference(db, row, cache)
                output.append(row["_id"])
    values = {"group_revision": new_version, "manual_groups": overrides}
    if operation:
        values["last_group_edit"] = dict(operation, group_ids=output)
    db[store.SCHEME_COLLECTION].update_one({"_id": scheme["_id"]}, {"$set": values})
    for gid, ids in memberships:
        db[store.FILE_COLLECTION].update_many({"scheme_id": scheme["_id"], "_id": {"$in": ids}},
            {"$set": {"group_id": gid, "group_revision": new_version}})
    return [group_out(db, settings, scheme, get_group(db, gid)) for gid in output]


def merge_groups(db, settings, scheme_id, group_ids, *, version, request_id):
    if not 2 <= len(group_ids) <= 100 or len(set(group_ids)) != len(group_ids):
        raise store.MaterialFilterError("invalid_merge", "请选择2–100个不同的分组")
    scheme = store.get_scheme(db, scheme_id)
    key = "group-merge:" + request_id
    fingerprint = hashlib.sha256(json.dumps([group_ids, version], separators=(",", ":")).encode()).hexdigest()
    previous = db[store.RUN_COLLECTION].find_one({"scheme_id": scheme_id, "request_id": key})
    published = scheme.get("last_group_edit") or {}
    if previous or published.get("request_id") == key:
        data = published if published.get("request_id") == key else previous
        if data.get("input_fingerprint") != fingerprint:
            raise store.MaterialFilterError("request_id_conflict", "同一次操作不能更换合并范围", 409)
        ids = data.get("group_ids") or []
        if ids:
            return {"group": group_out(db, settings, scheme, get_group(db, ids[0]))}
        raise store.MaterialFilterError("group_edit_exists", "同一次操作已存在，请刷新后查看结果", 409)
    chosen = [get_group(db, gid) for gid in group_ids]
    if any(g["scheme_id"] != scheme_id or g["version"] != version for g in chosen):
        raise store.MaterialFilterError("group_outside_scheme", "请选择当前方案同一版本的分组", 409)
    with _edit_group(db, group_ids[0], version, request_id=key) as (first, scheme):
        if first["scheme_id"] != scheme_id:
            raise store.MaterialFilterError("group_outside_scheme", "分组不属于当前方案", 409)
        chosen = [get_group(db, gid) for gid in group_ids]
        if any(g["scheme_id"] != scheme_id or g["version"] != version for g in chosen):
            raise store.MaterialFilterError("group_outside_scheme", "请选择当前方案同一版本的分组", 409)
        run_id = store.get_scheme(db, scheme_id)["active_run_id"]
        db[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"input_fingerprint": fingerprint}})
        ids = [fid for g in chosen for fid in g["file_ids"]]
        members = {fid: get_file(db, fid) for fid in ids}
        overrides = _manual_assignments(scheme, set(ids), [ids], members)
        replacements = {g["_id"]: [] for g in chosen}
        replacements[first["_id"]] = [(ids, True)]
        old_groups = list(db[COLLECTION].find({"scheme_id": scheme_id, "version": version}))
        result = _publish_edit(db, settings, scheme, old_groups, replacements, overrides,
            operation={"request_id": key, "input_fingerprint": fingerprint})
        db[store.RUN_COLLECTION].update_one({"_id": run_id}, {"$set": {"group_ids": [g["id"] for g in result]}})
        return {"group": result[0]}


def split_group(db, settings, group_id, file_ids, *, version):
    """Publish a new grouping revision; only local membership metadata changes."""
    with _edit_group(db, group_id, version) as (group, scheme):
        selected = set(file_ids)
        if not selected or not selected < set(group["file_ids"]) or len(selected) != len(file_ids):
            raise store.MaterialFilterError("invalid_split", "请选择本组一部分图片拆为新组")
        parts = [[fid for fid in group["file_ids"] if fid not in selected], sorted(selected)]
        members = {fid: get_file(db, fid) for fid in group["file_ids"]}
        overrides = _manual_assignments(scheme, set(group["file_ids"]), parts, members)
        old_groups = list(db[COLLECTION].find({"scheme_id": scheme["_id"], "version": version}))
        return {"groups": _publish_edit(db, settings, scheme, old_groups,
            {group_id: [(part, True) for part in parts]}, overrides)}
