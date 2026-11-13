"""Delete assets from this app and from the AM media library behind them.

Deployed sites show AM media-library records, so the asset page is expected to delete like
the AM console does. Deleting here has to do two things at once: remove the AM record (the
authority) and drop our own mirror document and files, otherwise a deleted record keeps
showing up as a local asset with a dead preview URL.

References are still checked first, because AM cannot know that a brand config or an old
version points at the record; a referenced asset needs ``force`` to go away.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.services import asset_records
from app.services.am_assets import AM_ASSET_PREFIX
from app.services.am_client import AMClient
from app.services.object_store import ObjectStore

# AM removes several records per request; small chunks keep one bad id from sinking a batch.
AM_BATCH_SIZE = 20


class AssetDeleteError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def delete_assets(
    db: Any,
    store: ObjectStore,
    am: AMClient | None,
    cookie: str,
    merchant_id: str,
    asset_ids: Iterable[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Delete the given asset ids, which may be local ids or ``am:<media id>`` ids."""
    wanted = list(dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip()))
    if not wanted:
        raise AssetDeleteError("asset_ids_required", "请选择要删除的素材", 422)

    media_ids: list[str] = []
    documents: dict[str, dict[str, Any]] = {}
    for asset_id in wanted:
        if asset_id.startswith(AM_ASSET_PREFIX):
            media_ids.append(asset_id[len(AM_ASSET_PREFIX):])
            continue
        document = db.assets.find_one({"_id": asset_id})
        if document is None:
            raise AssetDeleteError("asset_not_found", "素材不存在", 404)
        if asset_records.is_generated_asset(document):
            raise AssetDeleteError("asset_generated", "生成结果不能从资产库删除", 422)
        documents[asset_id] = document
        media_id = str(document.get("am_media_library_id") or "")
        if media_id and media_id not in media_ids:
            media_ids.append(media_id)

    if not force:
        blocked = [asset_id for asset_id in documents if asset_records.asset_referenced(db, asset_id)]
        if blocked:
            raise AssetDeleteError(
                "asset_in_use",
                "该素材正被品牌配置、任务或历史版本引用；如确认关联可失效，请使用强制删除。",
                409,
            )

    if media_ids:
        if am is None:
            raise AssetDeleteError("am_unavailable", "没有可用的 AM 会话，无法删除 AM 素材", 502)
        for start in range(0, len(media_ids), AM_BATCH_SIZE):
            am.delete_media(cookie, merchant_id, media_ids[start:start + AM_BATCH_SIZE])

    for asset_id, document in documents.items():
        for key in {document.get("path"), document.get("prepared_path")}:
            if key:
                store.delete(key)
        db.assets.delete_one({"_id": asset_id})

    return {
        "deleted": wanted,
        "am_deleted": media_ids,
        "local_deleted": list(documents),
    }
