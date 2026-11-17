"""Asset index and search routes: one status page, one full pass, one query agent call."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.middleware import is_local_bypass
from app.request_context import request_db, request_merchant_id, request_store
from app.services import asset_index, asset_index_runner, asset_search

router = APIRouter(tags=["asset-index"])
logger = logging.getLogger(__name__)


class AssetIndexRunPayload(BaseModel):
    mode: str = Field(default=asset_index.RUN_REINDEX, pattern="^(reindex|retry)$")
    # A double click sends the same id and joins the run that already exists.
    request_id: str = Field(default="", max_length=80)


class AssetSearchPayload(BaseModel):
    query: str = Field(default="", max_length=asset_search.MAX_QUERY_LENGTH)
    semantic: bool = False


def _client(request: Request) -> Any:
    return getattr(request.app.state, "core_ai", None)


@router.get("/asset-index")
def asset_index_status(request: Request) -> dict[str, Any]:
    db = request_db(request)
    return asset_index.summary(db)


@router.post("/asset-index-runs", status_code=201)
def create_asset_index_run(payload: AssetIndexRunPayload, request: Request) -> dict[str, Any]:
    db = request_db(request)
    mode = payload.mode
    if mode == asset_index.RUN_REINDEX:
        # The pass re-reads the whole library first, so AM-side additions are picked up.
        asset_index_runner.sync_catalog(db)
    scope = asset_index_runner.scope_for_mode(db, mode)
    if not scope:
        raise HTTPException(status_code=409, detail="没有需要处理的素材")
    client = _client(request)
    if client is None:
        raise HTTPException(status_code=503, detail="模型服务尚未就绪")
    try:
        run = asset_index_runner.start_run(
            db,
            request.app.state.settings,
            client,
            request_store(request),
            mode=mode,
            asset_ids=scope,
            request_id=payload.request_id,
            am=None if is_local_bypass(request) else getattr(request.app.state, "am", None),
            cookie=str(getattr(request.state, "am_cookie", "") or ""),
            merchant_id=request_merchant_id(request),
        )
    except asset_index.AssetIndexError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return asset_index.run_out(run)


@router.get("/asset-index-runs/{run_id}")
def get_asset_index_run(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return asset_index.run_out(asset_index.get_run(request_db(request), run_id))
    except asset_index.AssetIndexError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/asset-searches", status_code=201)
def create_asset_search(payload: AssetSearchPayload, request: Request) -> dict[str, Any]:
    db = request_db(request)
    if not db[asset_index.RECORD_COLLECTION].find_one({}, {"_id": 1}):
        # First search of a merchant (or a fresh deployment): mirror the library once.
        asset_index_runner.sync_catalog(db)
    settings = request.app.state.settings
    query = payload.query.strip()
    client = _client(request)
    if payload.semantic and query and client is None:
        raise HTTPException(status_code=503, detail="模型服务尚未就绪")
    document = asset_search.resolve_query(
        db,
        client=client,
        settings=settings,
        query=query,
        semantic=payload.semantic and bool(query),
    )
    ranked = asset_search.search_records(db, document.get("groups") or [])
    return asset_index.query_out(document, total=len(ranked))
