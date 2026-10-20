from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import create_client, get_database
from app.routes import assets, brands, platforms, prompts, samples, tasks, whole_image
from app.services.core_ai import CoreAIClient
from app.services import storage
from app.services.object_store import create_object_store
from app.services.pipeline import mark_interrupted
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
        mark_interrupted(db)
        whole_image_service.mark_interrupted(db)
    except Exception:  # noqa: BLE001 - health endpoint reports the failure
        logger.warning("MongoDB is not available at startup")
    yield
    app.state.core_ai.close()
    mongo_client.close()


app = FastAPI(title="Social Media Poster Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(assets.router)
app.include_router(brands.router)
app.include_router(platforms.router)
app.include_router(samples.router)
app.include_router(prompts.router)
app.include_router(tasks.router)
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
            "model": settings.core_ai_image_model,
        }
    )
