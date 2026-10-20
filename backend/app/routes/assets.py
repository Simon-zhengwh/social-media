from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app.services import storage

router = APIRouter(prefix="/api/assets", tags=["assets"])


def asset_out(doc: dict[str, Any]) -> dict[str, Any]:
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


@router.get("")
def list_assets(request: Request, kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None and kind not in {"product", "logo"}:
        raise HTTPException(status_code=422, detail="kind 只支持 product 或 logo")
    query: dict[str, Any] = (
        {"kind": kind}
        if kind is not None
        else {"kind": {"$in": ["product", "logo"]}}
    )
    docs = (
        request.app.state.db.assets.find(query)
        .sort("created_at", -1)
        .limit(200)
    )
    return [asset_out(doc) for doc in docs]


@router.post("", status_code=201)
def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
) -> dict[str, Any]:
    settings = request.app.state.settings
    store = request.app.state.store
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
    prepared = storage.prepare_upload(
        settings,
        store,
        data,
        file.filename or "",
        kind,
    )
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
    request.app.state.db.assets.insert_one(doc)
    return asset_out(doc)


@router.get("/{asset_id}/file")
def get_asset_file(asset_id: str, request: Request) -> FileResponse:
    settings = request.app.state.settings
    store = request.app.state.store
    asset = request.app.state.db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    relative = asset.get("prepared_path") or asset["path"]
    local_path = store.local_path(relative)
    if local_path is not None:
        media_type = "image/png" if local_path.suffix.lower() == ".png" else asset["mime_type"]
        return FileResponse(
            local_path, media_type=media_type, content_disposition_type="inline"
        )
    url = store.presign(
        relative,
        expires=settings.cos_presign_expiry_seconds,
    )
    return RedirectResponse(url, status_code=302)


@router.delete("/{asset_id}", status_code=204)
def delete_asset(asset_id: str, request: Request) -> Response:
    db = request.app.state.db
    store = request.app.state.store
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    if asset.get("kind") not in {"product", "logo", "style"}:
        raise HTTPException(status_code=422, detail="只能删除商品图、Logo 或风格参考")
    keys = {asset.get("path"), asset.get("prepared_path")}
    for key in keys:
        if key:
            store.delete(key)
    db.assets.delete_one({"_id": asset_id})
    return Response(status_code=204)
