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


def asset_referenced(db: Any, asset_id: str) -> bool:
    """True when a brand, logo, task or saved version still points at this asset.

    Deleting a referenced asset is possible, but only deliberately: the caller has to ask
    for a forced delete, otherwise brand configs and old versions would silently dangle.
    """
    if db.background_replace_tasks.find_one({"whole_task_id": {"$type": "string", "$ne": ""}, "$or": [
        {"input.source_asset_id": asset_id}, {"input.replacements.asset_id": asset_id},
    ]}):
        return True
    if db.whole_tasks.find_one(
        {
            "$or": [
                {"input.product_asset_ids": asset_id},
                {"input.reference_asset_ids": asset_id},
                {"input.style_reference_asset_ids": asset_id},
                {"input.logo_asset_id": asset_id},
                {"edit.reference_asset_ids": asset_id},
                {"messages.attachments.asset_id": asset_id},
            ]
        }
    ):
        return True
    if db.whole_versions.find_one({"$or": [
        {"model_input.images.asset_id": asset_id},
        {"outputs.model_input.images.asset_id": asset_id},
    ]}):
        return True
    if db.merchant_logos.find_one({"logo_asset_ids": asset_id}):
        return True
    if db.brands.find_one(
        {
            "$or": [
                {"reference_asset_ids": asset_id},
                {"logo_asset_ids": asset_id},
                {"cover_asset_id": asset_id},
            ]
        }
    ):
        return True
    return False


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
        "url": prepared.url,
        "prepared_url": prepared.prepared_url,
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
    try:
        db.assets.insert_one(document)
    except Exception:
        for key in (prepared.prepared_path, prepared.path):
            try:
                store.delete(key)
            except Exception:  # noqa: BLE001 - preserve the original database failure
                pass
        raise
    return document, True
