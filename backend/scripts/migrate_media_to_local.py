from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services import asset_records
from app.services.object_store import CosObjectStore, LocalObjectStore

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(value: str, fallback: str) -> str:
    name = INVALID_FILENAME.sub("_", Path(value).name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 150:
        suffix = Path(name).suffix
        name = f"{Path(name).stem[: max(1, 150 - len(suffix))]}{suffix}"
    return name


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _asset_ids_for_task(task: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    task_input = task.get("input") or {}
    for key in ("product_asset_ids", "style_reference_asset_ids"):
        ids.update(str(value) for value in task_input.get(key) or [] if value)
    for key in ("logo_asset_id",):
        if task_input.get(key):
            ids.add(str(task_input[key]))
    brand = task.get("brand") or {}
    for key in ("logo_asset_ids", "reference_asset_ids"):
        ids.update(str(value) for value in brand.get(key) or [] if value)
    return ids


def _archive_asset(
    asset: dict[str, Any],
    runtime_root: Path,
    target_dir: Path,
    *,
    prefix: str = "",
) -> Path | None:
    prepared_key = str(asset.get("prepared_path") or asset.get("path") or "")
    source = runtime_root / prepared_key
    if not source.is_file():
        return None
    filename = _safe_filename(str(asset.get("original_name") or ""), f"{asset['_id']}.png")
    target = target_dir / f"{prefix}{asset['_id']}__{filename}"
    _link_or_copy(source, target)
    return target


def migrate_media_to_local(
    db: Any,
    source_store: Any,
    *,
    local_root: Path,
    recent_task_limit: int = 10,
    apply: bool = False,
) -> dict[str, Any]:
    local_root = local_root.resolve()
    runtime_root = local_root / "objects"
    archive_root = local_root / "archive"
    runtime_store = LocalObjectStore(runtime_root)

    materials = list(db.assets.find(asset_records.uploaded_asset_query()).sort("created_at", 1))
    brands = list(db.brands.find().sort("_id", 1))
    tasks = list(
        db.whole_tasks.find({}, {"_id": 1, "created_at": 1, "input": 1, "brand": 1, "output_asset_id": 1})
        .sort("created_at", -1)
        .limit(recent_task_limit)
    )
    task_ids = [str(task["_id"]) for task in tasks]
    versions_by_task = {
        task_id: list(db.whole_versions.find({"task_id": task_id}).sort("version", 1))
        for task_id in task_ids
    }

    selected_ids = {str(asset["_id"]) for asset in materials}
    for brand in brands:
        selected_ids.update(str(value) for value in brand.get("logo_asset_ids") or [] if value)
        selected_ids.update(str(value) for value in brand.get("reference_asset_ids") or [] if value)
        if brand.get("cover_asset_id"):
            selected_ids.add(str(brand["cover_asset_id"]))
    for task in tasks:
        selected_ids.update(_asset_ids_for_task(task))
        if task.get("output_asset_id"):
            selected_ids.add(str(task["output_asset_id"]))
        selected_ids.update(
            str(version["output_asset_id"])
            for version in versions_by_task[str(task["_id"])]
            if version.get("output_asset_id")
        )

    assets = {str(asset["_id"]): asset for asset in db.assets.find({"_id": {"$in": list(selected_ids)}})}
    missing_ids = sorted(selected_ids - assets.keys())
    keys: set[str] = set()
    for asset in assets.values():
        for key in (asset.get("path"), asset.get("prepared_path")):
            if key:
                keys.add(str(key))

    result: dict[str, Any] = {
        "mode": "apply" if apply else "preview",
        "local_root": str(local_root),
        "materials": len(materials),
        "brands": [str(brand["_id"]) for brand in brands],
        "tasks": task_ids,
        "assets": len(assets),
        "objects": len(keys),
        "missing_assets": missing_ids,
        "missing_objects": [],
        "archive_files": 0,
    }
    if not apply:
        return result

    for asset in assets.values():
        for key in dict.fromkeys((asset.get("path"), asset.get("prepared_path"))):
            if not key or runtime_store.exists(str(key)):
                continue
            try:
                runtime_store.put_bytes(
                    str(key),
                    source_store.get_bytes(str(key)),
                    str(asset.get("mime_type") or "application/octet-stream"),
                )
            except Exception as exc:  # noqa: BLE001 - report every missing object
                result["missing_objects"].append({"asset_id": str(asset["_id"]), "key": str(key), "error": str(exc)})

    for asset in materials:
        _archive_asset(asset, runtime_root, archive_root / "materials")

    brand_asset_ids: dict[str, list[str]] = {}
    for brand in brands:
        brand_id = _safe_filename(str(brand["_id"]), "brand")
        cover = str(brand.get("cover_asset_id") or "")
        logos = [str(value) for value in brand.get("logo_asset_ids") or [] if value]
        references = [str(value) for value in brand.get("reference_asset_ids") or [] if value]
        brand_asset_ids[brand_id] = list(dict.fromkeys(([cover] if cover else []) + logos + references))
        if cover and cover in assets:
            _archive_asset(assets[cover], runtime_root, archive_root / "brand-styles" / brand_id / "cover")
        for asset_id in logos:
            if asset_id in assets:
                _archive_asset(assets[asset_id], runtime_root, archive_root / "brand-styles" / brand_id / "logos")
        for asset_id in references:
            if asset_id in assets:
                _archive_asset(assets[asset_id], runtime_root, archive_root / "brand-styles" / brand_id / "references")
        _write_json(
            archive_root / "brand-styles" / brand_id / "brand.json",
            {
                "id": brand["_id"],
                "name": brand.get("name"),
                "platform_id": brand.get("platform_id"),
                "placement_id": brand.get("placement_id"),
                "cover_asset_id": cover or None,
                "logo_asset_ids": logos,
                "reference_asset_ids": references,
            },
        )

    for task in tasks:
        task_id = str(task["_id"])
        task_root = archive_root / "recent-tasks" / task_id
        task_versions = versions_by_task[task_id]
        task_asset_ids = _asset_ids_for_task(task)
        if task.get("output_asset_id"):
            task_asset_ids.add(str(task["output_asset_id"]))
        task_asset_ids.update(
            str(version["output_asset_id"]) for version in task_versions if version.get("output_asset_id")
        )
        for asset_id in sorted(task_asset_ids):
            asset = assets.get(asset_id)
            if not asset:
                continue
            category = "versions" if asset_records.is_generated_asset(asset) else "sources"
            _archive_asset(asset, runtime_root, task_root / category)
        _write_json(
            task_root / "task.json",
            {
                "id": task["_id"],
                "created_at": task.get("created_at"),
                "input": task.get("input"),
                "versions": [
                    {key: version.get(key) for key in ("_id", "version", "parent_version_id", "output_asset_id", "created_at")}
                    for version in task_versions
                ],
            },
        )

    result["archive_files"] = sum(1 for path in archive_root.rglob("*") if path.is_file())
    _write_json(
        local_root / "migration-report.json",
        {**result, "completed_at": datetime.now(timezone.utc).isoformat()},
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Download files and build the local archive.")
    parser.add_argument("--recent-tasks", type=int, default=10)
    parser.add_argument("--local-root", type=Path, default=PROJECT_DIR / "local_media")
    args = parser.parse_args()
    settings = get_settings()
    client = create_client(settings.mongodb_uri)
    try:
        result = migrate_media_to_local(
            get_database(client, settings.mongodb_database),
            CosObjectStore(settings),
            local_root=args.local_root,
            recent_task_limit=args.recent_tasks,
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
