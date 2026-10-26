from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from app.schemas import BrandRulesUpdate
from app.services import brand_rules

router = APIRouter(prefix="/api/brand-rules", tags=["brand-rules"])


@router.get("")
def get_rules(request: Request) -> dict[str, Any]:
    return brand_rules.get_rules(request.app.state.db)


@router.put("")
def update_rules(payload: BrandRulesUpdate, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    data = payload.model_dump()
    data.update(version=payload.version + 1, updated_at=datetime.now(timezone.utc))
    try:
        result = db.brand_rules.update_one(
            {"_id": "default", "version": payload.version}, {"$set": data},
            upsert=payload.version == brand_rules.DEFAULT_RULES["version"],
        )
    except DuplicateKeyError as exc:
        raise HTTPException(409, "通用规范已更新，请刷新后重试") from exc
    if not result.matched_count and result.upserted_id is None:
        raise HTTPException(409, "通用规范已更新，请刷新后重试")
    return brand_rules.get_rules(db)
