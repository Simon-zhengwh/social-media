from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas import PromptOptimizeRequest
from app.services import prompt_optimizer
from app.services.core_ai import CoreAIError

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.post("/optimize")
def optimize_prompt(payload: PromptOptimizeRequest, request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    if not settings.core_ai_api_key.strip():
        raise HTTPException(status_code=503, detail="未配置 CORE_AI_API_KEY")
    try:
        prompt = prompt_optimizer.optimize_prompt(
            request.app.state.db,
            settings,
            request.app.state.store,
            request.app.state.core_ai,
            request=payload,
        )
    except prompt_optimizer.PromptOptimizerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except CoreAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"prompt": prompt}
