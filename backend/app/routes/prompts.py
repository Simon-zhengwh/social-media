from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.request_context import request_db, request_store
from app.schemas import PromptOptimizeRequest
from app.services import prompt_optimizer, request_assets
from app.services.core_ai import CoreAIError

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.post("/optimize")
def optimize_prompt(payload: PromptOptimizeRequest, request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    if not settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")
    payload = payload.model_copy(update={
        "logo_asset_id": request_assets.resolve_asset_id(request, payload.logo_asset_id),
        "style_reference_asset_ids": request_assets.resolve_asset_ids(request, payload.style_reference_asset_ids),
        "product_asset_ids": request_assets.resolve_asset_ids(request, payload.product_asset_ids),
    })
    try:
        prompt = prompt_optimizer.optimize_prompt(
            request_db(request),
            settings,
            request_store(request),
            request.app.state.core_ai,
            request=payload,
        )
    except prompt_optimizer.PromptOptimizerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except CoreAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"prompt": prompt}
