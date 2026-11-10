from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import create_client, get_database
from app.middleware import local_bypass_enabled, merchant_context_middleware
from app.routes import (
    assets,
    agent_tests,
    brand_rules,
    brands,
    merchant_context,
    platforms,
    poster_edit,
    prompts,
    task_center,
    whole_image,
)
from app.services.core_ai import CoreAIClient
from app.services import storage
from app.services.object_store import create_object_store
from app.services import original_replacement_runner
from app.services import agent_tests as agent_test_service
from app.services import maintenance, storage, whole_image as whole_image_service
from app.services.am_client import AMClient
from app.services.core_ai import CoreAIClient
from app.services.media_object_store import create_merchant_store
from app.services.object_store import create_object_store
from app.services.scoped_db import ScopedDatabase
from app.services.storage import StorageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _initialize_indexes(db) -> None:
    db.assets.create_index([("merchant_id", 1), ("sha256", 1)])
    db.assets.create_index([("merchant_id", 1), ("created_at", -1)])
    db.brands.create_index([("merchant_id", 1), ("created_at", 1)])
    db.brands.create_index([("merchant_id", 1), ("reference_asset_ids", 1)])
    db.brands.create_index([("merchant_id", 1), ("logo_asset_ids", 1)])
    db.brands.create_index([("merchant_id", 1), ("cover_asset_id", 1)])
    db.brand_rules.create_index([("merchant_id", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("created_at", -1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.candidate_batch_id", 1), ("input.candidate_index", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.product_asset_ids", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.style_reference_asset_ids", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.logo_asset_id", 1)])
    db.whole_tasks.create_index([("status", 1), ("created_at", -1)])
    db.whole_tasks.create_index([("edit.status", 1)])
    db.whole_versions.create_index([("merchant_id", 1), ("task_id", 1), ("version", 1)])
    db.poster_edit_tasks.create_index([("merchant_id", 1), ("created_at", -1)])
    db.poster_edit_tasks.create_index([("merchant_id", 1), ("batch_id", 1), ("candidate_index", 1)])
    db.poster_edit_tasks.create_index([("status", 1), ("run_id", 1)])
    db.background_replace_tasks.create_index([("merchant_id", 1), ("created_at", -1)])
    db.background_replace_tasks.create_index([("status", 1), ("run_id", 1)])
    db.background_replace_tasks.create_index([("merchant_id", 1), ("input.source_asset_id", 1)])
    db.background_replace_tasks.create_index([("merchant_id", 1), ("input.replacements.asset_id", 1)])
    db.media_objects.create_index([("merchant_id", 1), ("status", 1)])
    db.whole_artifacts.create_index([("merchant_id", 1), ("task_id", 1)])
    db.agent_test_runs.create_index([("merchant_id", 1), ("batch_id", 1)])
    db.agent_test_runs.create_index([("status", 1), ("merchant_id", 1)])
    db.agent_test_batches.create_index([("merchant_id", 1), ("created_at", -1)])
    db.app_control.create_index([("enabled", 1)])


def _scoped_store(app: FastAPI, merchant_id: str, db):
    if app.state.settings.am_media_storage_service_key.strip():
        return create_merchant_store(app.state.settings, db, app.state.am, merchant_id)
    return app.state.local_store


def _resume_original_replacements(app: FastAPI) -> None:
    raw = app.state.raw_db
    merchant_ids = raw.background_replace_tasks.distinct(
        "merchant_id", {"whole_task_id": {"$type": "string", "$ne": ""},
                        "status": {"$in": ["queued", "running"]}}
    )
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        db = ScopedDatabase(raw, merchant_id)
        if original_replacement_runner.mark_interrupted(db):
            threading.Thread(
                target=original_replacement_runner.resume_tasks,
                args=(db, app.state.settings, _scoped_store(app, merchant_id, db), app.state.core_ai),
                name=f"resume-original-replacements-{merchant_id}", daemon=True,
            ).start()


def _resume_poster_edits(app: FastAPI) -> None:
    raw = app.state.raw_db
    merchant_ids = raw.poster_edit_tasks.distinct(
        "merchant_id", {"status": {"$in": ["queued", "running"]}}
    )
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        db = ScopedDatabase(raw, merchant_id)
        if poster_edit.mark_interrupted(db):
            threading.Thread(
                target=poster_edit.resume_tasks,
                args=(db, app.state.settings, _scoped_store(app, merchant_id, db), app.state.core_ai),
                name=f"resume-poster-edits-{merchant_id}", daemon=True,
            ).start()


def _resume_agent_tests(app: FastAPI) -> None:
    raw = app.state.raw_db
    merchant_ids = raw.agent_test_runs.distinct(
        "merchant_id", {"status": {"$in": ["queued", "preparing", "triggering", "running"]}}
    )
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        db = ScopedDatabase(raw, merchant_id)
        for batch_id in agent_test_service.recover(db):
            threading.Thread(target=agent_test_service.run_batch,
                args=(db, app.state.settings, app.state.core_ai, batch_id),
                name=f"resume-agent-test-{merchant_id}-{batch_id}", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    storage.ensure_storage_dirs(settings)
    mongo_client = create_client(settings.mongodb_uri)
    db = get_database(mongo_client, settings.mongodb_database)
    app.state.settings = settings
    app.state.raw_db = db
    app.state.db = db
    app.state.local_store = create_object_store(settings)
    app.state.store = app.state.local_store
    app.state.core_ai = CoreAIClient(settings)
    app.state.am = AMClient(settings)
    app.state.ready = False
    app.state.startup_error = None
    try:
        db.command("ping")
        _initialize_indexes(db)
        maintenance.initialize(db)
        whole_image_service.mark_interrupted(db)
        _resume_original_replacements(app)
        _resume_poster_edits(app)
        _resume_agent_tests(app)
        app.state.ready = True
        logger.info("social media initialization complete; database=%s", settings.mongodb_database)
    except Exception as exc: # noqa: BLE001 - readiness reports initialization failure
        app.state.startup_error = str(exc)
        logger.exception("social media initialization failed")
    yield
    app.state.am.close()
    app.state.core_ai.close()
    mongo_client.close()


app = FastAPI(title="Social Media Design Agent", version="0.4.0", lifespan=lifespan)
app.middleware("http")(merchant_context_middleware)

app.include_router(merchant_context.merchant_list_router)

merchant_router = APIRouter(prefix="/api/merchants/{merchant_id}")
merchant_router.include_router(merchant_context.router)
merchant_router.include_router(assets.router)
merchant_router.include_router(brands.router)
merchant_router.include_router(brand_rules.router)
merchant_router.include_router(prompts.router)
merchant_router.include_router(agent_tests.router)
merchant_router.include_router(task_center.router)
merchant_router.include_router(whole_image.router)
merchant_router.include_router(poster_edit.router)
app.include_router(merchant_router)
app.include_router(platforms.router)


@app.exception_handler(StorageError)
async def storage_error_handler(_: Request, exc: StorageError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/api/health/live")
def liveness() -> JSONResponse:
    return JSONResponse(content={"status": "ok"})


@app.get("/api/health")
def health(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    bypass = local_bypass_enabled(settings)
    local_merchant_id = settings.am_bypass_merchant_id.strip() if bypass else ""
    if not getattr(request.app.state, "ready", False):
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "initialized": False,
                "message": getattr(request.app.state, "startup_error", None) or "服务尚未完成初始化",
            },
        )
    try:
        request.app.state.raw_db.command("ping")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"status": "error", "initialized": True, "mongo": "unavailable", "message": f"MongoDB 不可用：{exc}", "local_merchant_id": local_merchant_id},
        )
    try:
        storage.ensure_storage_dirs(settings)
        request.app.state.local_store.check()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "initialized": True,
                "mongo": "ok",
                "storage": "unavailable",
                "message": f"临时存储不可写：{exc}",
                "local_merchant_id": local_merchant_id,
            },
        )
    if not bypass and not settings.am_media_storage_service_key.strip():
        return JSONResponse(
            status_code=503,
            content={"status": "error", "initialized": True, "media_storage": "unconfigured", "message": "未配置媒体存储服务身份"},
        )
    return JSONResponse(
        content={
            "status": "ok",
            "initialized": True,
            "mongo": "ok",
            "storage": "ok",
            "media_storage": "local-bypass" if bypass else "configured",
            "storage_backend": "local" if bypass else "am-media",
            "local_merchant_id": local_merchant_id,
        },
    )
