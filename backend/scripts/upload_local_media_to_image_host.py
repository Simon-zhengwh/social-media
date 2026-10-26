from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services import asset_records, image_host
from app.services.object_store import LocalObjectStore


def _task_asset_ids(db: Any, task: dict[str, Any], versions: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    task_input = task.get("input") or {}
    for key in ("product_asset_ids", "style_reference_asset_ids"):
        ids.update(str(value) for value in task_input.get(key) or [] if value)
    if task_input.get("logo_asset_id"):
        ids.add(str(task_input["logo_asset_id"]))
    brand = task.get("brand") or {}
    for key in ("logo_asset_ids", "reference_asset_ids"):
        ids.update(str(value) for value in brand.get(key) or [] if value)
    if task.get("output_asset_id"):
        ids.add(str(task["output_asset_id"]))
    ids.update(str(version["output_asset_id"]) for version in versions if version.get("output_asset_id"))
    return ids


def collect_asset_ids(db: Any, migration_report: Path) -> list[str]:
    selected = {str(asset["_id"]) for asset in db.assets.find(asset_records.uploaded_asset_query())}
    for brand in db.brands.find({}):
        selected.update(str(value) for value in brand.get("logo_asset_ids") or [] if value)
        selected.update(str(value) for value in brand.get("reference_asset_ids") or [] if value)
        if brand.get("cover_asset_id"):
            selected.add(str(brand["cover_asset_id"]))

    task_ids: list[str] = []
    if migration_report.is_file():
        task_ids = [str(value) for value in json.loads(migration_report.read_text(encoding="utf-8")).get("tasks") or []]
    if not task_ids:
        task_ids = [str(task["_id"]) for task in db.whole_tasks.find({}).sort("created_at", -1).limit(10)]
    for task_id in task_ids:
        task = db.whole_tasks.find_one({"_id": task_id})
        if task is None:
            continue
        versions = list(db.whole_versions.find({"task_id": task_id}))
        selected.update(_task_asset_ids(db, task, versions))
    return sorted(selected)


def upload_selected(
    db: Any,
    store: Any,
    settings: Any,
    asset_ids: list[str],
    *,
    apply: bool = False,
    max_workers: int = 3,
) -> dict[str, Any]:
    uploads: list[dict[str, str]] = []
    already: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    pending: list[str] = []
    for asset_id in asset_ids:
        asset = db.assets.find_one({"_id": asset_id}, {"image_host_url": 1})
        if asset and asset.get("image_host_url"):
            already.append({"asset_id": asset_id, "url": str(asset["image_host_url"])})
        else:
            pending.append(asset_id)

    result: dict[str, Any] = {
        "mode": "apply" if apply else "preview",
        "selected": len(asset_ids),
        "already_hosted": already,
        "pending": len(pending),
        "uploaded": [],
        "failed": failed,
    }
    if not apply:
        return result

    host = image_host.ImageHostClient(settings)
    try:
        def upload_one(asset_id: str) -> tuple[str, str]:
            return asset_id, image_host.upload_asset(
                db, store, settings, asset_id, client=host
            )

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            futures = {executor.submit(upload_one, asset_id): asset_id for asset_id in pending}
            for future in as_completed(futures):
                asset_id = futures[future]
                try:
                    _, url = future.result()
                    uploads.append({"asset_id": asset_id, "url": url})
                except Exception as exc:  # noqa: BLE001 - report all individual failures
                    failed.append({"asset_id": asset_id, "error": str(exc)})
    finally:
        host.close()

    result["uploaded"] = sorted(uploads, key=lambda item: item["asset_id"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--report", type=Path, default=PROJECT_DIR / "local_media" / "migration-report.json")
    args = parser.parse_args()

    settings = get_settings()
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, settings.mongodb_database)
    store = LocalObjectStore(settings.storage_path / "objects")
    try:
        ids = collect_asset_ids(db, args.report)
        result = upload_selected(
            db, store, settings, ids, apply=args.apply, max_workers=args.max_workers
        )
        output = Path(args.report).resolve().parent / "image-host-report.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: result[key] for key in ("mode", "selected", "pending", "uploaded", "failed")}, ensure_ascii=False, indent=2))
        if result["failed"]:
            raise SystemExit(1)
    finally:
        mongo.close()


if __name__ == "__main__":
    main()
