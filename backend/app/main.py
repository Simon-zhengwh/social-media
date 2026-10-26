from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import create_client, get_database
from app.routes import assets, brands, brand_rules, platforms, prompts, whole_image
from app.services.core_ai import CoreAIClient
from app.services import storage
from app.services.object_store import create_object_store
from app.services import whole_image as whole_image_service
from app.services.storage import StorageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    storage.ensure_storage_dirs(settings)
    mongo_client = create_client(settings.mongodb_uri)
    db = get_database(mongo_client, settings.mongodb_database)
    app.state.settings = settings
    app.state.db = db
    app.state.store = create_object_store(settings)
    app.state.core_ai = CoreAIClient(settings)
    try:
        db.command("ping")
        whole_image_service.mark_interrupted(db)
        db.whole_tasks.create_index([("created_at", -1)])
        db.whole_tasks.create_index(
            [("input.candidate_batch_id", 1), ("input.candidate_index", 1)]
        )
    except Exception:  # noqa: BLE001 - health endpoint reports the failure
        logger.warning("MongoDB is not available at startup")
    yield
    app.state.core_ai.close()
    mongo_client.close()


app = FastAPI(title="Social Media Design Agent", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(assets.router)
app.include_router(brands.router)
app.include_router(brand_rules.router)
app.include_router(platforms.router)
app.include_router(prompts.router)
app.include_router(whole_image.router)


@app.exception_handler(StorageError)
async def storage_error_handler(_: Request, exc: StorageError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/api/health")
def health(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    try:
        request.app.state.db.command("ping")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "mongo": "unavailable",
                "message": f"MongoDB 不可用：{exc}",
            },
        )
    try:
        storage.ensure_storage_dirs(settings)
        request.app.state.store.check()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "mongo": "ok",
                "storage": "unavailable",
                "message": f"storage 不可写：{exc}",
            },
        )
    return JSONResponse(
        content={
            "status": "ok",
            "mongo": "ok",
            "storage": "ok",
            "storage_backend": request.app.state.store.describe(),
            "image_host": (
                "configured"
                if settings.image_host_base_url.strip()
                and settings.image_host_upload_token.strip()
                else "disabled"
            ),
            "model": settings.core_ai_image_model,
        }
    )
