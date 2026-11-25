from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import create_client, get_database
from app.middleware import local_bypass_enabled, merchant_context_middleware
from app.services import poster_series as poster_series_service
from app.services import poster_series_round
from app.routes import (
    assets,
    agent_tests,
    asset_index,
    brand_logos,
    brand_rules,
    brands,
    material_filters,
    media_migration,
    merchant_context,
    platforms,
    poster_edit,
    poster_series,
    prompts,
    task_center,
    whole_image,
)
from app.services.core_ai import CoreAIClient
from app.services import storage
from app.services.object_store import create_object_store
from app.services import original_replacement_runner
from app.services import agent_tests as agent_test_service
from app.services import (
    asset_index as asset_index_store,
    maintenance,
    storage,
    whole_image as whole_image_service,
)
from app.services import material_filter_files, material_filter_uat_sync
from app.services import material_filters as material_filter_store
from app.services import media_migration as media_migration_service
from app.services.am_client import AMClient
from app.services.core_ai import CoreAIClient
from app.services.media_object_store import create_merchant_store
from app.services.object_store import create_object_store
from app.services.scoped_db import ScopedDatabase
from app.services.storage import StorageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _initialize_indexes(db, settings=None) -> None:
    db.material_filter_groups.create_index([("merchant_id", 1), ("scheme_id", 1), ("version", 1), ("count", -1)])
    db.material_filter_groups.create_index([("merchant_id", 1), ("folder_id", 1)])
    db.material_filter_attempts.create_index([("merchant_id", 1), ("run_id", 1), ("file_id", 1)], unique=True)
    db.material_filter_attempts.create_index([("merchant_id", 1), ("scheme_id", 1)])
    db.material_filter_attempts.create_index([("merchant_id", 1), ("scheme_id", 1), ("file_id", 1), ("source_sha256", 1), ("created_at", -1)])
    db.assets.create_index([("merchant_id", 1), ("sha256", 1)])
    db.merchants.create_index([("name", 1)])
    db.assets.create_index([("merchant_id", 1), ("created_at", -1)])
    db.assets.create_index([("merchant_id", 1), ("am_media_library_id", 1)])
    db.brands.create_index([("merchant_id", 1), ("created_at", 1)])
    db.brands.create_index([("merchant_id", 1), ("reference_asset_ids", 1)])
    db.brands.create_index([("merchant_id", 1), ("logo_asset_ids", 1)])
    db.brands.create_index([("merchant_id", 1), ("cover_asset_id", 1)])
    db.brand_rules.create_index([("merchant_id", 1)])
    db.merchant_logos.create_index([("merchant_id", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("created_at", -1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.candidate_batch_id", 1), ("input.candidate_index", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.generation_workspace_id", 1), ("created_at", 1)])
    db.whole_cost_calls.create_index([("merchant_id", 1), ("workspace_id", 1), ("created_at", 1)])
    db.whole_cost_calls.create_index([("merchant_id", 1), ("workspace_id", 1), ("needs_sync", 1), ("next_check_at", 1)])
    db.whole_workspace_titles.create_index([("merchant_id", 1), ("workspace_id", 1)], unique=True)
    if settings is None or getattr(settings, "poster_series_enabled", False):
        # Opt-in feature: do not create series-only indexes while it is disabled.
        db.whole_workspace_modes.create_index([("merchant_id", 1), ("workspace_id", 1)], unique=True)
        db.whole_series_runs.create_index([("merchant_id", 1), ("workspace_id", 1), ("submit_id", 1)], unique=True)
        db.whole_series_runs.create_index([("merchant_id", 1), ("workspace_id", 1), ("created_at", 1)])
        db.whole_series_runs.create_index([("workflow", 1), ("status", 1), ("merchant_id", 1)])
        db.whole_series_groups.create_index([("merchant_id", 1), ("workspace_id", 1), ("order", 1)])
        db.whole_series_pages.create_index([("merchant_id", 1), ("group_id", 1), ("order", 1)])
        db.whole_series_html_saves.create_index([("merchant_id", 1), ("group_id", 1), ("page_id", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.product_asset_ids", 1)])
    db.whole_tasks.create_index([("merchant_id", 1), ("input.reference_asset_ids", 1)])
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
    db.whole_finalizations.create_index([("merchant_id", 1), ("group_key", 1)], unique=True)
    db.whole_finalizations.create_index([("merchant_id", 1), ("asset_keys", 1), ("finalized_at", -1)])
    db.whole_finalizations.create_index([("merchant_id", 1), ("task_id", 1)])
    db.agent_test_runs.create_index([("merchant_id", 1), ("batch_id", 1)])
    db.agent_test_runs.create_index([("status", 1), ("merchant_id", 1)])
    db.agent_test_batches.create_index([("merchant_id", 1), ("created_at", -1)])
    db.app_control.create_index([("enabled", 1)])
    db.material_filter_folders.create_index(
        [("merchant_id", 1), ("canonical_source_key", 1)], unique=True
    )
    db.material_filter_schemes.create_index([("merchant_id", 1), ("folder_id", 1), ("created_at", 1)])
    db.material_filter_runs.create_index([("merchant_id", 1), ("scheme_id", 1), ("created_at", -1)])
    db.material_filter_runs.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("request_id", 1)], unique=True
    )
    db.material_filter_files.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("path_key", 1)], unique=True
    )
    db.material_filter_evaluations.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("file_id", 1), ("source_sha256", 1)], unique=True
    )
    db.material_filter_evaluations.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("effective_decision", 1), ("created_at", -1)]
    )
    # The deployment cluster runs with notablescan: every filter needs an index.
    db.material_filter_runs.create_index([("status", 1), ("merchant_id", 1)])
    db.material_filter_runs.create_index([("merchant_id", 1), ("folder_id", 1), ("status", 1)])
    db.material_filter_files.create_index([("merchant_id", 1), ("folder_id", 1)])
    db.material_filter_evaluations.create_index([("merchant_id", 1), ("folder_id", 1)])
    db.material_filter_evaluations.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("manual_pick", 1), ("picked_at", -1)]
    )
    db.material_filter_evaluations.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("copy_status", 1)]
    )
    db.material_filter_syncs.create_index(
        [("merchant_id", 1), ("scheme_id", 1), ("created_at", -1)]
    )
    db.material_filter_syncs.create_index([("status", 1), ("merchant_id", 1)])
    db[asset_index_store.RECORD_COLLECTION].create_index([("merchant_id", 1), ("description_status", 1)])
    db[asset_index_store.RECORD_COLLECTION].create_index([("merchant_id", 1), ("created_at", -1)])
    db[asset_index_store.RUN_COLLECTION].create_index([("merchant_id", 1), ("created_at", -1)])
    db[asset_index_store.RUN_COLLECTION].create_index([("merchant_id", 1), ("request_id", 1)], unique=True)
    db[asset_index_store.RUN_COLLECTION].create_index([("status", 1), ("merchant_id", 1)])
    db[asset_index_store.QUERY_COLLECTION].create_index([("merchant_id", 1), ("created_at", -1)])
    db[asset_index_store.QUERY_COLLECTION].create_index([("merchant_id", 1), ("expires_at", 1)])
    # The UAT→Prod media migration keeps its checkpoints and run history in the local database.
    db[media_migration_service.ITEM_COLLECTION].create_index(
        [
            ("uat_base_url", 1),
            ("uat_merchant_id", 1),
            ("prod_base_url", 1),
            ("prod_merchant_id", 1),
            ("source_media_id", 1),
        ],
        unique=True,
    )
    db[media_migration_service.ITEM_COLLECTION].create_index(
        [("uat_merchant_id", 1), ("prod_merchant_id", 1), ("updated_at", -1)]
    )
    db[media_migration_service.JOB_COLLECTION].create_index([("status", 1)])
    db[media_migration_service.JOB_COLLECTION].create_index([("created_at", -1)])
    db[media_migration_service.JOB_COLLECTION].create_index(
        [("uat_merchant_id", 1), ("prod_merchant_id", 1), ("created_at", -1)]
    )
    db[media_migration_service.JOB_COLLECTION].create_index(
        [("prod_merchant_id", 1), ("created_at", -1)]
    )


def _scoped_store(app: FastAPI, merchant_id: str, db):
    if local_bypass_enabled(app.state.settings):
        return app.state.local_store
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


def _resume_poster_series(app: FastAPI) -> None:
    if not getattr(app.state.settings, "poster_series_enabled", False):
        return
    raw = app.state.raw_db
    merchant_ids = raw.whole_tasks.distinct(
        "merchant_id",
        {"series.run_id": {"$exists": True},
         "series.status": {"$in": ["queued", "running"]},
         "status": {"$in": ["queued", "running", "failed"]}},
    )
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        db = ScopedDatabase(raw, merchant_id)
        threading.Thread(
            target=poster_series_service.resume_tasks,
            args=(db, app.state.settings, _scoped_store(app, merchant_id, db), app.state.core_ai),
            name=f"resume-poster-series-{merchant_id}", daemon=True,
        ).start()
    round_merchants = raw.whole_series_runs.distinct(
        "merchant_id", {"workflow": poster_series_round.WORKFLOW, "status": {"$in": ["queued", "running"]}},
    )
    for merchant_id in sorted(str(value) for value in round_merchants if value):
        db = ScopedDatabase(raw, merchant_id)
        threading.Thread(
            target=poster_series_round.resume_runs,
            args=(db, app.state.settings, _scoped_store(app, merchant_id, db), app.state.core_ai),
            name=f"resume-poster-series-round-{merchant_id}", daemon=True,
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


def _mark_material_filter_runs_interrupted(app: FastAPI) -> None:
    """Release leftover scans and uploads; the operator resumes them explicitly."""
    raw = app.state.raw_db
    merchant_ids = set(raw.material_filter_runs.distinct(
        "merchant_id", {"status": {"$in": ["queued", "running"]}}
    )) | set(raw.material_filter_syncs.distinct(
        "merchant_id", {"status": {"$in": ["queued", "running"]}}
    ))
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        db = ScopedDatabase(raw, merchant_id)
        material_filter_store.mark_scheme_interrupted(db)
        material_filter_uat_sync.mark_interrupted(db)


def _mark_media_migration_jobs_interrupted(app: FastAPI) -> None:
    """A restarted process cannot finish its uploads; the operator resumes them."""
    interrupted = media_migration_service.mark_interrupted(app.state.raw_db)
    if interrupted:
        logger.warning("media migration jobs were interrupted by the restart: %s", interrupted)


def _interrupt_asset_index_runs(app: FastAPI) -> None:
    """A restarted process cannot finish its claims; the operator starts a new pass."""
    raw = app.state.raw_db
    merchant_ids = raw[asset_index_store.RUN_COLLECTION].distinct(
        "merchant_id", {"status": {"$in": list(asset_index_store.ACTIVE_RUN_STATUSES)}}
    )
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        asset_index_store.interrupt_active_runs(ScopedDatabase(raw, merchant_id))


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
        _initialize_indexes(db, settings)
        maintenance.initialize(db)
        whole_image_service.mark_interrupted(db)
        _resume_original_replacements(app)
        _resume_poster_edits(app)
        _resume_poster_series(app)
        _resume_agent_tests(app)
        _mark_material_filter_runs_interrupted(app)
        _mark_media_migration_jobs_interrupted(app)
        _interrupt_asset_index_runs(app)
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

# uvicorn's access log has no duration column, so a slow page could not be told apart from a
# slow query on UAT. Anything above this threshold gets its own line.
SLOW_REQUEST_MS = 1000


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if elapsed_ms >= SLOW_REQUEST_MS:
        logger.warning(
            "slow request %s %s -> %s %.0fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.0f}"
    return response

app.include_router(merchant_context.merchant_list_router)

merchant_router = APIRouter(prefix="/api/merchants/{merchant_id}")
merchant_router.include_router(merchant_context.router)
merchant_router.include_router(assets.router)
merchant_router.include_router(brands.router)
merchant_router.include_router(brand_logos.router)
merchant_router.include_router(brand_rules.router)
merchant_router.include_router(prompts.router)
merchant_router.include_router(agent_tests.router)
merchant_router.include_router(task_center.router)
merchant_router.include_router(whole_image.router)
merchant_router.include_router(poster_series.router)
merchant_router.include_router(poster_edit.router)
merchant_router.include_router(material_filters.router)
merchant_router.include_router(asset_index.router)
app.include_router(merchant_router)
app.include_router(platforms.router)
# One-off local operator tool: every route inside re-checks the local bypass gate.
app.include_router(media_migration.router)


@app.exception_handler(material_filter_store.MaterialFilterError)
async def material_filter_error_handler(_: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(asset_index_store.AssetIndexError)
async def asset_index_error_handler(_: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(material_filter_uat_sync.SyncError)
async def material_filter_sync_error_handler(_: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(media_migration_service.MediaMigrationError)
async def media_migration_error_handler(_: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(material_filter_files.MaterialFilterFileError)
async def material_filter_file_error_handler(_: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


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
                "local_merchant_id": local_merchant_id,
                "local_bypass": bypass,
            },
        )
    try:
        request.app.state.raw_db.command("ping")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "initialized": True,
                "mongo": "unavailable",
                "message": f"MongoDB 不可用：{exc}",
                "local_merchant_id": local_merchant_id,
                "local_bypass": bypass,
            },
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
                "local_bypass": bypass,
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
            # The bypass flag itself lets the browser open the local-only tools even
            # before any merchant is imported into this workspace.
            "local_bypass": bypass,
        },
    )
