from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api_errors import am_http_error
from app.api_paths import asset_url
from app.middleware import is_local_bypass
from app.request_context import request_db, request_merchant_id, request_store
from app.services import am_assets, asset_deletion, asset_records
from app.services.am_client import AMClientError

router = APIRouter(prefix="/assets", tags=["assets"])


class AssetTagsUpdate(BaseModel):
    # The full set replaces the previous tags; an empty list clears them.
    tags: list[str] = Field(default_factory=list, max_length=am_assets.MAX_ASSET_TAGS)


class AssetNameUpdate(BaseModel):
    # The name is display metadata; AM enforces the 256-character stem limit on its side too.
    name: str = Field(min_length=1, max_length=300)


class AssetDeletePayload(BaseModel):
    """Batch delete: the asset page behaves like the AM console, one or many at a time."""

    ids: list[str] = Field(min_length=1, max_length=200)
    force: bool = False


def asset_out(doc: dict[str, Any]) -> dict[str, Any]:
    merchant_id = str(doc.get("merchant_id") or "")
    return {
        "id": doc["_id"],
        "original_name": doc["original_name"],
        "width": doc["width"],
        "height": doc["height"],
        "mime_type": doc["mime_type"],
        "url": doc.get("prepared_url") or doc.get("url") or asset_url(merchant_id, str(doc["_id"])),
        "created_at": doc["created_at"],
        "source": doc.get("source") or "upload",
        "read_only": False,
        "tags": [str(tag) for tag in doc.get("tags") or [] if str(tag).strip()],
    }


@router.get("")
def list_assets(request: Request) -> list[dict[str, Any]]:
    query = asset_records.uploaded_asset_query()
    docs = (
        request_db(request).assets.find(query)
        .sort("created_at", -1)
        .limit(200)
    )
    return [asset_out(doc) for doc in docs]


@router.post("", status_code=201)
def upload_asset(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    settings = request.app.state.settings
    store = request_store(request)
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_bytes / 1_000_000:g} MB 限制")
    document, created = asset_records.persist_upload(
        request_db(request),
        settings,
        store,
        data,
        file.filename or "",
    )
    am = getattr(request.app.state, "am", None)
    if is_local_bypass(request) or am is None:
        # Local dev (and unit-test doubles) keep uploads local; no AM write is attempted.
        return asset_out(document)
    if document.get("am_media_library_id"):
        # Same bytes uploaded before: it already owns a media-library record.
        return asset_out(document)

    try:
        document = am_assets.register_in_library(
            request_db(request),
            am,
            request.state.am_cookie,
            request_merchant_id(request),
            document,
            data,
            file.filename or document["original_name"],
        )
    except AMClientError as exc:
        # Never fall back to a private copy that pretends to be a library asset.
        if created:
            _discard_upload(request, document, store)
        raise am_http_error(exc) from exc
    return asset_out(document)


def _discard_upload(request: Request, document: dict[str, Any], store: Any) -> None:
    for key in (document.get("prepared_path"), document.get("path")):
        if not key:
            continue
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001 - the upload already failed
            pass
    request_db(request).assets.delete_one({"_id": document["_id"]})


@router.put("/{asset_id}/tags")
def update_asset_tags(
    asset_id: str,
    payload: AssetTagsUpdate,
    request: Request,
) -> dict[str, Any]:
    bypass = is_local_bypass(request)
    try:
        return am_assets.update_tags(
            request_db(request),
            None if bypass else getattr(request.app.state, "am", None),
            request.state.am_cookie,
            request_merchant_id(request),
            asset_id,
            payload.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="素材不存在") from exc
    except AMClientError as exc:
        raise am_http_error(exc) from exc


@router.put("/{asset_id}/name")
def update_asset_name(
    asset_id: str,
    payload: AssetNameUpdate,
    request: Request,
) -> dict[str, Any]:
    bypass = is_local_bypass(request)
    try:
        return am_assets.rename_asset(
            request_db(request),
            None if bypass else getattr(request.app.state, "am", None),
            request.state.am_cookie,
            request_merchant_id(request),
            asset_id,
            payload.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="素材不存在") from exc
    except AMClientError as exc:
        raise am_http_error(exc) from exc


@router.get("/{asset_id}/file")
def get_asset_file(asset_id: str, request: Request) -> FileResponse:
    settings = request.app.state.settings
    store = request_store(request)
    asset = request_db(request).assets.find_one({"_id": asset_id})
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


def _delete(request: Request, asset_ids: list[str], *, force: bool) -> dict[str, Any]:
    # Older callers (and tests) build a request without a state; the cookie is only needed
    # once an AM media-library record is actually involved.
    state = getattr(request, "state", None)
    cookie = str(getattr(state, "am_cookie", "") or "")
    try:
        return asset_deletion.delete_assets(
            request_db(request),
            request_store(request),
            getattr(request.app.state, "am", None),
            cookie,
            request_merchant_id(request),
            asset_ids,
            force=force,
        )
    except asset_deletion.AssetDeleteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except AMClientError as exc:
        raise am_http_error(exc) from exc


@router.delete("/{asset_id}", status_code=204)
def delete_asset(asset_id: str, request: Request, force: bool = False) -> Response:
    """Single-asset delete kept for callers that predate the batch route."""
    _delete(request, [asset_id], force=force)
    return Response(status_code=204)


@router.post("/delete")
def delete_asset_batch(request: Request, payload: AssetDeletePayload) -> dict[str, Any]:
    """Delete one or many assets, including the AM media-library record behind each one."""
    return _delete(request, payload.ids, force=payload.force)
