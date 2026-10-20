from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pymongo.database import Database

from app.config import Settings
from app.services import storage
from app.services.object_store import ObjectStore
from app.services.storage import StorageError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def iter_sample_files(settings: Settings) -> Iterator[tuple[Path, str]]:
    for path in sorted(settings.sample_path.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        yield path, "product"


def _asset_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "kind": doc["kind"],
        "original_name": doc["original_name"],
        "width": doc["width"],
        "height": doc["height"],
        "mime_type": doc["mime_type"],
        "url": f"/api/assets/{doc['_id']}/file",
        "created_at": doc["created_at"],
    }


def import_samples(
    db: Database,
    settings: Settings,
    store: ObjectStore,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path, kind in iter_sample_files(settings):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        existing = db.assets.find_one({"kind": kind, "sha256": digest})
        if existing is not None:
            imported.append(_asset_out(existing))
            continue
        try:
            prepared = storage.prepare_upload(
                settings, store, data, path.name, kind
            )
        except StorageError as exc:
            skipped.append({"file": path.name, "reason": exc.message})
            continue
        doc = {
            "_id": prepared.id,
            "kind": prepared.kind,
            "original_name": prepared.original_name,
            "path": prepared.path,
            "prepared_path": prepared.prepared_path,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "bytes": prepared.bytes,
            "sha256": prepared.sha256,
            "created_at": datetime.now(timezone.utc),
        }
        db.assets.insert_one(doc)
        imported.append(_asset_out(doc))
    return {"imported": imported, "skipped": skipped}
