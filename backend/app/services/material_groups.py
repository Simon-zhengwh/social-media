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
from threading import Thread
from typing import Any, Callable

from app.services import material_filter_files as files
from app.services import material_filters as store
from app.services import material_menu_naming as menu_naming
from app.services import material_similarity as similarity
from app.services import material_local_match as local_match
from app.time_utils import utc_iso

COLLECTION = "material_filter_groups"
ATTEMPTS = "material_filter_attempts"
MAX_MANUAL_NAME = 60
GROUP_PREVIEW_LIMIT = 8
# Bump when the shortlist rule changes, so markers written by the older rule are swept again.
AUTO_PICK_POLICY = 2


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


@contextmanager
def _scheme_job(db, scheme: dict, *, request_id: str = "", kind: str = "group_edit"):
    """Claim the group-edit lease for whole-scheme maintenance without a group."""
    run, created = store.create_run(db, scheme, request_id=request_id, kind=kind)
    if not created:
        raise store.MaterialFilterError("group_edit_exists", "同一次分组操作已存在，请刷新后查看结果", 409)
    db[store.RUN_COLLECTION].update_one({"_id": run["_id"]}, {"$set": {"status": "running", "started_at": store.now()}})
    status, error = "succeeded", None
    try:
        yield store.get_scheme(db, scheme["_id"])
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
    if sorted(evaluation_ids) != sorted(group.get("reference_evaluation_ids") or []):
        # New representative results re-arm the automatic shortlist for this group.
        values_to_set["auto_picked"] = False
    if group.get("name_manual") and str(group.get("reference_name") or "").strip():
        # A human-confirmed group name is the export file name; recognition must not overwrite it.
        values_to_set.update(reference_name=group["reference_name"], status="manual")
    else:
        values_to_set["reference_name"] = values[0] if values else None
    db[COLLECTION].update_one({"_id": group["_id"]}, {"$set": values_to_set})


def cached_for_file(db, scheme: dict, file: dict) -> dict | None:
    return valid_evaluation(file, cached_evaluations(db, scheme))


def _inherit_evaluation(db, settings, scheme, group, representative, file, *, decision: str = "pass", manual: bool = False):
    """Give a group photo its own record from the representative's result.

    ``decision`` keeps the representative verdict ("pass" for the automatic
    shortlist, the actual verdict when an operator picks a 待复核 photo), and
    ``manual`` marks the pick as a human choice so later clean-up leaves it alone.
    """
    existing = store.find_evaluation(db, scheme["_id"], file["_id"], file.get("sha256") or "")
    if existing and existing.get("human_decision") == "fail":
        return "skipped", None
    try:
        snapshot = files.read_source_file(
            scheme["source_root_snapshot"], file["relative_path"],
            max_bytes=settings.material_filter_max_source_bytes,
        )
        if snapshot.sha256 != file.get("sha256"):
            return "error", f"{file['relative_path']}: 源图片已变化"
        naming = menu_naming.MenuNamingResult(**representative["naming"])
        evaluation_id = existing["_id"] if existing else store.new_id("meval")
        output_name = menu_naming.write_named_copy(
            store.scheme_directory(settings, scheme) / "passed", evaluation_id, naming,
            suffix=Path(file["original_name"]).suffix, data=snapshot.data, sha256=snapshot.sha256,
            allow_unconfirmed=decision != "pass",
        ).name
        output_path = f"passed/{evaluation_id}/{output_name}"
        timestamp = store.now()
        picked_at = (existing or {}).get("picked_at") or timestamp
        previous = existing or {}
        if manual:
            picks = {"manual_pick": True, "picked_at": picked_at, "pick_source": "manual"}
        elif previous.get("pick_source") == "manual":
            # An operator's earlier unpick survives repeated automatic passes.
            picks = {"manual_pick": bool(previous.get("manual_pick")), "picked_at": previous.get("picked_at"), "pick_source": "manual"}
        else:
            picks = {"manual_pick": True, "picked_at": picked_at, "pick_source": "auto_group"}
        values = {
            "scheme_id": scheme["_id"], "folder_id": scheme.get("folder_id"),
            "file_id": file["_id"], "path_key": file["path_key"],
            "relative_path": file["relative_path"], "original_name": file["original_name"],
            "source_sha256": snapshot.sha256, "source_size": snapshot.size,
            "source_mtime_ns": snapshot.mtime_ns,
            "model": representative.get("model"), "prompt_version": representative.get("prompt_version"),
            "naming_mode": "menu_match", "naming": representative.get("naming"),
            "model_result": representative.get("model_result"), "model_decision": decision,
            "decision": decision, "effective_decision": decision,
            "human_decision": (existing or {}).get("human_decision"),
            "human_decided_at": (existing or {}).get("human_decided_at"),
            "display_name": representative.get("display_name"),
            "category": representative.get("category"), "tags": list(representative.get("tags") or []),
            "tag_groups": representative.get("tag_groups") or {},
            "short_id": (existing or {}).get("short_id") or file["_id"],
            "preview_path": file.get("preview_path"),
            "output_path": output_path, "output_name": output_name,
            "output_sha256": snapshot.sha256, "copy_status": "copied", "copy_error": None,
            "error": None,
            **picks,
            "inherited_from_group_id": group["_id"],
            "inherited_from_evaluation_id": representative["_id"],
            "updated_at": timestamp,
        }
        if existing:
            saved = store.update_evaluation(db, evaluation_id, values)
        else:
            values.update(_id=evaluation_id, created_at=timestamp)
            db[store.EVALUATION_COLLECTION].insert_one(values)
            saved = values
        entry = files.SourceEntry(
            file["relative_path"], file["original_name"], snapshot.size, snapshot.mtime_ns,
            scheme["source_root_snapshot"],
        )
        store.upsert_file(db, scheme, entry, size=snapshot.size, mtime_ns=snapshot.mtime_ns,
                          sha256=snapshot.sha256, latest_evaluation_id=saved["_id"])
        return "picked" if saved.get("manual_pick") else "skipped", None
    except (OSError, menu_naming.MenuNamingError, store.MaterialFilterError, files.MaterialFilterFileError) as exc:
        return "error", f"{file['relative_path']}: {exc}"


def _ordered_members(group: dict, rows: dict) -> list[dict]:
    """Members in the order the browser shows them; the first two become the shortlist."""
    return sorted((rows[fid] for fid in group.get("file_ids") or [] if fid in rows),
                  key=lambda row: row["relative_path"].casefold())


def _shortlist_size(group: dict) -> int:
    return min(2, len(group.get("file_ids") or []))


SHORTLIST_VERDICTS = {"pass", "uncertain"}


def _shortlist_representative(group: dict, rows: dict, cache: dict) -> dict | None:
    ids = representatives(group, 1)
    if not ids:
        return None
    file = rows.get(ids[0])
    return valid_evaluation(file, cache) if file else None


def _group_needs_sync(group: dict, rows: dict, cache: dict, inherited_groups: set[str], *,
                      respect_swept: bool = True) -> bool:
    """True when a 通过/待复核 group still lacks shortlist picks.

    A group the automatic sweep already handled keeps operator edits (unpicked
    photos) until new results arrive; the explicit maintenance action ignores that
    marker so it can complete a partly picked shortlist again.
    """
    if respect_swept and group.get("auto_picked") and group.get("auto_pick_policy") == AUTO_PICK_POLICY:
        return False
    representative = _shortlist_representative(group, rows, cache)
    passing = bool(representative and representative.get("effective_decision") in SHORTLIST_VERDICTS and representative.get("naming"))
    if not passing:
        return group["_id"] in inherited_groups
    shortlist = _ordered_members(group, rows)[:_shortlist_size(group)]
    return any(not (valid_evaluation(file, cache) or {}).get("manual_pick") for file in shortlist)


def _inherited_pick_groups(db, scheme: dict) -> set[str]:
    return set(db[store.EVALUATION_COLLECTION].distinct("inherited_from_group_id", {"scheme_id": scheme["_id"]}))


def pending_pick_groups(db, scheme: dict, current_groups: list[dict], rows: dict, cache: dict) -> int:
    """How many 通过/待复核 groups still need their first two members picked."""
    if not sampling_enabled(scheme):
        return 0
    inherited = _inherited_pick_groups(db, scheme)
    return sum(_group_needs_sync(group, rows, cache, inherited) for group in current_groups)


def auto_pick_successful_group(db, settings, scheme: dict, group: dict, *, cache=None) -> dict:
    """Inherit a 通过/待复核 representative result to the first two ordered members.

    待复核 groups are shortlisted too: operators upload them and correct the
    wording afterwards, so the local copy must exist even when the name is
    still marked 待确认.
    """
    if not group.get("file_ids"):
        return {"eligible": False, "photos_picked": 0, "errors": []}
    cache = cache if cache is not None else cached_evaluations(db, scheme)
    representative_ids = representatives(group, 1)
    if not representative_ids:
        _clear_stale_inherited_picks(db, group, None)
        return {"eligible": False, "photos_picked": 0, "errors": []}
    representative_id = representative_ids[0]
    representative_file = get_file(db, representative_id)
    representative = valid_evaluation(representative_file, cache)
    verdict = (representative or {}).get("effective_decision")
    if not representative or verdict not in SHORTLIST_VERDICTS or not representative.get("naming"):
        _clear_stale_inherited_picks(db, group, representative)
        return {"eligible": False, "photos_picked": 0, "errors": []}
    rows = {row["_id"]: row for row in db[store.FILE_COLLECTION].find(
        {"scheme_id": scheme["_id"], "_id": {"$in": group["file_ids"]}}
    )}
    ordered = _ordered_members(group, rows)
    picked: list[str] = []
    errors: list[str] = []
    for file in ordered[:2]:
        outcome, error = _inherit_evaluation(db, settings, scheme, group, representative, file, decision=verdict)
        if outcome == "picked":
            picked.append(file["_id"])
        elif error:
            errors.append(error)
    return {"eligible": True, "photos_picked": len(picked), "errors": errors}


def manual_inherit_pick(db, settings, group_id: str, file_id: str) -> dict:
    """Pick a member that has no result of its own, reusing the representative's result.

    Photos in a group are only recognized one by one on demand, so most members
    carry no record at all. An operator who decides to use such a photo in a
    通过 or 待复核 group should not have to spend a model call first: the record
    inherits the representative naming and keeps that verdict, so the pick is
    honest about the group still waiting for review.
    """
    group = get_group(db, group_id)
    scheme = store.get_scheme(db, group["scheme_id"])
    require_sampling_scheme(scheme)
    if file_id not in (group.get("file_ids") or []):
        raise store.MaterialFilterError("group_stale", "图片不属于当前分组，请刷新后重试", 409)
    cache = cached_evaluations(db, scheme)
    representative_ids = representatives(group, 1)
    representative_file = get_file(db, representative_ids[0]) if representative_ids else None
    representative = valid_evaluation(representative_file, cache) if representative_file else None
    if not representative:
        raise store.MaterialFilterError("recognition_required", "本组还没有代表图结果，请先识别后再入选", 409)
    decision = representative.get("effective_decision") or representative.get("decision")
    if decision not in {"pass", "uncertain"}:
        raise store.MaterialFilterError("group_not_pickable", "代表图已判定不通过，请人工确认后再入选", 409)
    file = get_file(db, file_id)
    existing = store.find_evaluation(db, scheme["_id"], file_id, file.get("sha256") or "")
    if existing and existing.get("manual_pick"):
        return existing
    outcome, error = _inherit_evaluation(db, settings, scheme, group, representative, file, decision=decision, manual=True)
    if outcome == "skipped":
        raise store.MaterialFilterError("photo_rejected", "这张照片已人工判定不通过", 409)
    if error:
        raise store.MaterialFilterError("pick_failed", error, 500)
    saved = store.find_evaluation(db, scheme["_id"], file_id, file.get("sha256") or "")
    if not saved:
        raise store.MaterialFilterError("pick_failed", "入选记录未能保存，请重试", 500)
    return saved


def _clear_stale_inherited_picks(db, group: dict, representative: dict | None) -> None:
    # 通过 and 待复核 groups keep their shortlist; only a fail/lost verdict clears it.
    if representative and representative.get("effective_decision") in SHORTLIST_VERDICTS:
        return
    inherited = {"scheme_id": group["scheme_id"], "inherited_from_group_id": group["_id"]}
    db[store.EVALUATION_COLLECTION].update_many(
        inherited,
        {"$set": {
            "decision": "uncertain", "model_decision": "uncertain", "updated_at": store.now(),
        }},
    )
    db[store.EVALUATION_COLLECTION].update_many(
        {**inherited, "human_decision": None},
        {"$set": {"effective_decision": "uncertain", "updated_at": store.now()}},
    )
    db[store.EVALUATION_COLLECTION].update_many(
        {**inherited, "pick_source": "auto_group", "manual_pick": True},
        {"$set": {"manual_pick": False, "picked_at": None, "updated_at": store.now()}},
    )


AUTO_PICK_BATCH_GROUPS = 40


def sync_auto_picks(db, settings, scheme: dict, *, max_groups: int | None = None, respect_swept: bool = True) -> dict:
    """Pick the first two members of every passing group; repeatable and idempotent.

    Runs after recognition and for results captured by older runs, so a group whose
    representative already passed does not stay unshortlisted until someone clicks a
    maintenance button. ``max_groups`` bounds one background batch because every new
    pick copies an original image from the source share.
    """
    totals = {"groups": 0, "photos_picked": 0, "errors": [], "visited": 0, "remaining": False}
    revision = scheme.get("group_revision")
    if not revision or not sampling_enabled(scheme):
        return totals
    current_groups = list(db[COLLECTION].find({"scheme_id": scheme["_id"], "version": revision}).sort("_id", 1))
    if not current_groups:
        return totals
    member_ids = [fid for group in current_groups for fid in group.get("file_ids") or []]
    rows = {row["_id"]: row for row in db[store.FILE_COLLECTION].find({"_id": {"$in": member_ids}})}
    cache = cached_evaluations(db, scheme)
    inherited = _inherited_pick_groups(db, scheme)
    for group in current_groups:
        if not _group_needs_sync(group, rows, cache, inherited, respect_swept=respect_swept):
            continue
        if max_groups is not None and totals["visited"] >= max_groups:
            totals["remaining"] = True
            break
        totals["visited"] += 1
        result = auto_pick_successful_group(db, settings, scheme, group, cache=cache)
        db[COLLECTION].update_one({"_id": group["_id"]}, {"$set": {"auto_picked": True, "auto_pick_policy": AUTO_PICK_POLICY}})
        totals["groups"] += int(result["eligible"])
        totals["photos_picked"] += result["photos_picked"]
        totals["errors"].extend(result["errors"])
    return totals


def _mark_auto_pick(db, scheme: dict, revision: str | None) -> None:
    db[store.SCHEME_COLLECTION].update_one({"_id": scheme["_id"]}, {"$set": {"auto_pick_revision": revision}})


def start_auto_pick(db, settings, scheme: dict) -> bool:
    """Heal missing shortlist picks in the background, at most once per grouping revision."""
    revision = scheme.get("group_revision")
    if not revision or not sampling_enabled(scheme):
        return False
    claimed = db[store.SCHEME_COLLECTION].find_one_and_update(
        {"_id": scheme["_id"], "active_run_id": None, "auto_pick_revision": {"$ne": revision}},
        {"$set": {"auto_pick_revision": revision}},
    )
    if not claimed:
        return False
    Thread(target=_auto_pick_job, args=(db, settings, scheme["_id"], revision),
           name=f"material-auto-pick-{revision}", daemon=True).start()
    return True


def _auto_pick_job(db, settings, scheme_id: str, revision: str) -> None:
    scheme = None
    try:
        scheme = store.get_scheme(db, scheme_id)
        if scheme.get("group_revision") != revision:
            return
        with _scheme_job(db, scheme, kind="auto_pick") as current_scheme:
            totals = sync_auto_picks(db, settings, current_scheme, max_groups=AUTO_PICK_BATCH_GROUPS)
        # Re-arm while batches remain; per-photo copy errors end the automatic run and
        # leave the retry to the explicit maintenance action.
        _mark_auto_pick(db, current_scheme, None if totals["remaining"] else revision)
    except Exception:
        # A busy scheme or a crashed job must not consume this revision's only retry.
        if scheme is not None:
            _mark_auto_pick(db, scheme, None)


def backfill_picks_for_passed_groups(db, settings, scheme: dict) -> dict:
    """Apply representative naming/tags and shortlist to already recognized groups."""
    with _scheme_job(db, scheme) as current_scheme:
        totals = sync_auto_picks(db, settings, current_scheme, respect_swept=False)
        _mark_auto_pick(db, current_scheme, current_scheme.get("group_revision"))
    return {key: totals[key] for key in ("groups", "photos_picked", "errors")}


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


def group_out(db, settings, scheme, group: dict, count=1, *, cache=None, file_map=None, verdict: str | None = None) -> dict:
    cached = cache if cache is not None else cached_evaluations(db, scheme)
    members = file_map if file_map is not None else {r["_id"]: r for r in db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"], "_id": {"$in": group["file_ids"]}})}
    reps = representatives(group, count)
    if verdict is None:
        verdict = _group_verdict(group, members, cached, _failed_files(db, scheme))
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
        "verdict": verdict, "reference_stale": bool(group.get("reference_stale"))}


GROUP_FILTER_STATUSES = ("unrecognized", "pass", "fail", "uncertain", "error", "picked")


def _failed_files(db, scheme: dict) -> set[str]:
    """Photos whose latest recognition attempt failed; groups without results count as errors."""
    latest: dict[str, str] = {}
    rows = db[ATTEMPTS].find({"scheme_id": scheme["_id"], "status": {"$in": ["failed", "unknown"]},
        "model": scheme.get("model"), "prompt_version": scheme.get("prompt_version"),
        "menu_version": (scheme.get("menu") or {}).get("menu_version")}, {"file_id": 1, "status": 1})
    for row in rows.sort([("created_at", -1), ("_id", -1)]):
        latest.setdefault(row["file_id"], row.get("status") or "")
    return {file_id for file_id, status in latest.items() if status == "failed"}


def _group_verdict(group: dict, members: dict, cached: dict, failed: set[str]) -> str:
    """Group-level filter bucket, taken from the representative photo."""
    ids = representatives(group, 1)
    row = members.get(ids[0]) if ids else None
    if not row:
        return "unrecognized"
    value = valid_evaluation(row, cached)
    if value:
        decision = value.get("effective_decision") or value.get("human_decision") or value.get("decision")
        if decision in {"pass", "fail", "uncertain"}:
            return decision
        return "error" if decision == "error" else "unrecognized"
    if row.get("feature_error") or row["_id"] in failed:
        return "error"
    return "unrecognized"


def group_filter_counts(current_groups: list[dict], members: dict, cached: dict, failed: set[str]) -> tuple[dict[str, int], dict[str, str], set[str]]:
    """Counts per group filter tab, plus the verdict and picked sets they filter on."""
    counts = {"all": len(current_groups), **{status: 0 for status in GROUP_FILTER_STATUSES}}
    verdicts: dict[str, str] = {}
    picked: set[str] = set()
    for group in current_groups:
        verdict = _group_verdict(group, members, cached, failed)
        verdicts[group["_id"]] = verdict
        counts[verdict] += 1
        if any((valid_evaluation(members[fid], cached) or {}).get("manual_pick") for fid in group["file_ids"] if fid in members):
            picked.add(group["_id"])
    counts["picked"] = len(picked)
    return counts, verdicts, picked


def unsettled_representatives(db, scheme: dict, *, verdict: str = "uncertain", limit: int = 200) -> dict:
    """Representative photos of groups in one verdict bucket, in the browser's group order."""
    scheme = store.get_scheme(db, scheme["_id"])
    query = {"scheme_id": scheme["_id"], "version": scheme.get("group_revision")}
    all_groups = list(db[COLLECTION].find(query).sort([("count", -1), ("_id", 1)])) if scheme.get("group_revision") else []
    rep_ids = [fid for group in all_groups for fid in representatives(group, 1)]
    members = {r["_id"]: r for r in db[store.FILE_COLLECTION].find({"scheme_id": scheme["_id"], "_id": {"$in": rep_ids}})} if rep_ids else {}
    cached = cached_evaluations(db, scheme)
    failed = _failed_files(db, scheme)
    targets: list[str] = []
    for group in all_groups:
        ids = representatives(group, 1)
        if ids and _group_verdict(group, members, cached, failed) == verdict:
            targets.append(ids[0])
    return {"file_ids": targets[:limit], "total": len(targets)}


def list_groups(db, settings, scheme: dict, *, skip=0, limit=30, q="", status="all", representative_count=1) -> dict:
    scheme = store.get_scheme(db, scheme["_id"])
    status = (status or "all").strip() or "all"
    if status != "all" and status not in GROUP_FILTER_STATUSES:
        raise store.MaterialFilterError("invalid_status", "分组筛选状态非法")
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
             "new_calls": len(rep_ids)-hits-blocked if sampling_enabled(scheme) else 0,
             "pending_picks": pending_pick_groups(db, scheme, all_groups, members, cached)}
    counts, verdicts, picked = group_filter_counts(all_groups, members, cached, _failed_files(db, scheme))
    if status == "picked":
        all_groups = [g for g in all_groups if g["_id"] in picked]
    elif status != "all":
        all_groups = [g for g in all_groups if verdicts.get(g["_id"]) == status]
    if q.strip():
        term = q.strip().casefold()
        all_groups = [g for g in all_groups if term in str(g.get("reference_name") or "").casefold() or any(term in str(members[fid].get("relative_path") or "").casefold() for fid in g["file_ids"])]
    last = db[store.RUN_COLLECTION].find_one({"_id": scheme["active_run_id"]}) if scheme.get("active_run_id") else None
    if last and last.get("kind") not in {"group_scan", "recognition"}:
        last = None
    last = last or db[store.RUN_COLLECTION].find_one({"scheme_id": scheme["_id"], "kind": {"$in": ["group_scan", "recognition"]}}, sort=[("created_at", -1)])
    window = all_groups[max(0, skip):max(0, skip)+limit]
    return {"items": [group_out(db, settings, scheme, g, representative_count, cache=cached, file_map=members, verdict=verdicts.get(g["_id"])) for g in window],
            "total": len(all_groups), "counts": counts, "stats": stats, "last_run": last, "scan_errors": scheme.get("group_scan_errors") or [],
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
