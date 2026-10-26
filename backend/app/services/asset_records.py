from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.services import storage
from app.services.object_store import ObjectStore


def is_generated_asset(asset: dict[str, Any]) -> bool:
    return asset.get("source") == "generated" or asset.get("kind") == "output"


def is_uploaded_asset(asset: dict[str, Any]) -> bool:
    return not is_generated_asset(asset)


def uploaded_asset_query() -> dict[str, Any]:
    return {
        "$and": [
            {"source": {"$ne": "generated"}},
            {"kind": {"$ne": "output"}},
        ]
    }


def asset_document(prepared: storage.PreparedUpload) -> dict[str, Any]:
    return {
        "_id": prepared.id,
        "source": "upload",
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


def persist_upload(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    data: bytes,
    original_name: str,
) -> tuple[dict[str, Any], bool]:
    """Persist an uploaded image once per content hash, independent of its future role."""
    digest = hashlib.sha256(data).hexdigest()
    existing = db.assets.find_one(
        {
            "sha256": digest,
            "source": {"$ne": "generated"},
            "kind": {"$ne": "output"},
        }
    )
    if existing is not None:
        return existing, False

    prepared = storage.prepare_upload(settings, store, data, original_name)
    document = asset_document(prepared)
    db.assets.insert_one(document)
    return document, True
