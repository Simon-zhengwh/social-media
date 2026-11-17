"""Background asset index runner: one model call per asset, results saved item by item.

The run is persisted before the request answers, so a closed page or a restarted
process does not lose the work: interrupted items are re-queued and a failed item
keeps the previous name, tags and description.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services import am_assets, asset_index, asset_recognition
from app.services.asset_index import AssetIndexError
from app.services.asset_recognition import RecognitionError
from app.services.material_filter_files import MaterialFilterFileError
from app.services.material_filter_files import prepare_model_image_data_url

logger = logging.getLogger(__name__)

_active_runs: dict[str, threading.Event] = {}
_active_lock = threading.Lock()
_gate_lock = threading.Lock()
_gate: tuple[int, threading.BoundedSemaphore] | None = None


def _model_semaphore(limit: int) -> threading.BoundedSemaphore:
    global _gate
    size = max(1, int(limit or 1))
    with _gate_lock:
        if _gate is None or _gate[0] != size:
            _gate = (size, threading.BoundedSemaphore(size))
        return _gate[1]


def scope_for_mode(db: Any, mode: str) -> list[str]:
    """Every library asset for a full pass; only unfinished rows for a retry."""
    if mode == asset_index.RUN_REINDEX:
        return [entry["asset_id"] for entry in asset_index.all_catalog(db)]
    # `retry` and `describe` only cover rows that are still missing a description.
    return asset_index.unfinished_asset_ids(db)


def sync_catalog(db: Any) -> dict[str, int]:
    """Mirror the library into the search records without calling the model."""
    return asset_index.sync_catalog(db, asset_index.all_catalog(db))


def start_run(
    db: Any,
    settings: Settings,
    client,
    store: Any,
    *,
    mode: str,
    asset_ids: list[str] | None = None,
    request_id: str = "",
    am: Any = None,
    cookie: str = "",
    merchant_id: str = "",
) -> dict[str, Any]:
    """Persist the run before answering; the pass keeps going after the page closes."""
    scope = asset_ids if asset_ids is not None else scope_for_mode(db, mode)
    run, created = asset_index.create_run(
        db, mode=mode, asset_ids=scope, request_id=request_id
    )
    if created:
        _spawn(db, settings, client, store, str(run["_id"]), am=am, cookie=cookie, merchant_id=merchant_id)
    return asset_index.get_run(db, str(run["_id"]))


def describe_asset(
    db: Any,
    settings: Settings,
    client,
    store: Any,
    *,
    merchant_id: str,
    asset_id: str,
    am: Any = None,
    cookie: str = "",
) -> dict[str, Any] | None:
    """Queue one description refresh after an upload or a metadata edit."""
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        return None
    asset_index.request_description(db, asset)
    return start_run(
        db,
        settings,
        client,
        store,
        mode=asset_index.RUN_DESCRIBE,
        asset_ids=[asset_id],
        am=am,
        cookie=cookie,
        merchant_id=merchant_id,
    )


def _spawn(
    db: Any,
    settings: Settings,
    client,
    store: Any,
    run_id: str,
    *,
    am: Any = None,
    cookie: str = "",
    merchant_id: str = "",
) -> None:
    event = threading.Event()
    with _active_lock:
        _active_runs[run_id] = event
    thread = threading.Thread(
        target=_guarded_run,
        args=(db, settings, client, store, run_id, am, cookie, merchant_id, event),
        name=f"asset-index-{run_id}",
        daemon=True,
    )
    thread.start()


def _guarded_run(
    db,
    settings: Settings,
    client,
    store,
    run_id: str,
    am,
    cookie: str,
    merchant_id: str,
    event: threading.Event,
) -> None:
    try:
        run_index(
            db,
            settings,
            client,
            store,
            run_id,
            am=am,
            cookie=cookie,
            merchant_id=merchant_id,
            stop_event=event,
        )
    except Exception:  # noqa: BLE001 - a crashed run must not stay "running" forever
        logger.exception("asset index run %s crashed", run_id)
        try:
            run = db[asset_index.RUN_COLLECTION].find_one({"_id": run_id}) or {}
            asset_index.finish_run(
                db,
                run_id,
                status="failed",
                counters=run.get("counters") or asset_index.empty_counters(),
                error="索引进程异常结束",
            )
            db[asset_index.RECORD_COLLECTION].update_many(
                {"claim_run_id": run_id},
                {"$set": {"claim_run_id": None, "description_status": asset_index.PENDING}},
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not finalize crashed asset index run %s", run_id)
    finally:
        with _active_lock:
            _active_runs.pop(run_id, None)


def run_index(
    db,
    settings: Settings,
    client,
    store,
    run_id: str,
    *,
    am: Any = None,
    cookie: str = "",
    merchant_id: str = "",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    run = asset_index.get_run(db, run_id)
    if run["status"] in asset_index.FINISHED_RUN_STATUSES:
        return run
    mode = str(run.get("mode") or asset_index.RUN_REINDEX)
    scope = [str(item) for item in run.get("scope") or []]
    counters = dict(run.get("counters") or {})
    for key, value in asset_index.empty_counters(len(scope)).items():
        counters.setdefault(key, value)
    counters["remaining"] = len(scope)
    asset_index.mark_started(db, run_id)
    asset_index.set_counters(db, run_id, counters)
    semaphore = _model_semaphore(settings.asset_index_concurrency)
    interrupted = False
    counter_lock = threading.Lock()

    def work(asset_id: str) -> None:
        if stop_event is not None and stop_event.is_set():
            return
        outcome, message = _process(
            db,
            settings,
            client,
            store,
            run_id,
            mode,
            asset_id,
            semaphore,
            am=am,
            cookie=cookie,
            merchant_id=merchant_id,
        )
        with counter_lock:
            if outcome == "succeeded":
                counters["succeeded"] += 1
            elif outcome == "skipped":
                counters["skipped"] += 1
            else:
                counters["failed"] += 1
                asset_index.record_run_error(db, run_id, asset_id, message or "处理失败")
            counters["remaining"] = max(0, counters["remaining"] - 1)
        # Persist after every asset so the page shows real progress.
        asset_index.set_counters(db, run_id, counters)

    workers = max(1, min(int(settings.asset_index_concurrency or 1), len(scope) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asset-index") as pool:
        futures = [pool.submit(work, asset_id) for asset_id in scope]
        for future in as_completed(futures):
            future.result()
    if stop_event is not None and stop_event.is_set():
        interrupted = True

    status = "interrupted" if interrupted else ("failed" if counters["failed"] and not counters["succeeded"] else "succeeded")
    asset_index.finish_run(
        db,
        run_id,
        status=status,
        counters=counters,
        error="任务被中断，可重新发起" if interrupted else None,
    )
    if not interrupted:
        # A description queued while this run was busy joined no scope; queue it now instead of
        # leaving an asset pending until the operator notices.
        _follow_up(db, settings, client, store, am=am, cookie=cookie, merchant_id=merchant_id)
    return asset_index.get_run(db, run_id)


def _follow_up(
    db, settings: Settings, client, store, *, am: Any, cookie: str, merchant_id: str
) -> None:
    waiting = [
        str(record["_id"])
        for record in db[asset_index.RECORD_COLLECTION].find(
            {"description_status": asset_index.PENDING, "claim_run_id": None}, {"_id": 1}
        )
    ]
    if not waiting:
        return
    run, created = asset_index.create_run(
        db, mode=asset_index.RUN_DESCRIBE, asset_ids=waiting, reason="queued-description"
    )
    if created:
        _spawn(
            db, settings, client, store, str(run["_id"]),
            am=am, cookie=cookie, merchant_id=merchant_id,
        )


def _process(
    db,
    settings: Settings,
    client,
    store,
    run_id: str,
    mode: str,
    asset_id: str,
    semaphore: threading.BoundedSemaphore,
    *,
    am: Any,
    cookie: str,
    merchant_id: str,
) -> tuple[str, str | None]:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        # A deleted asset must never come back as an empty record.
        asset_index.delete_record(db, asset_id)
        return "skipped", None
    if asset_index.record_for_asset(db, asset_id) is None:
        # An asset outside the last catalog sync still gets a searchable record.
        asset_index.request_description(db, asset)
    record = asset_index.claim_record(db, asset_id, run_id)
    if record is None:
        return "skipped", None
    version = int(record.get("description_version") or 0)
    claimed_marker = asset_index.asset_marker(asset)
    try:
        image_url = _image_reference(db, settings, store, asset, record)
        name = str(asset.get("original_name") or "")
        tags = [str(tag) for tag in asset.get("tags") or [] if str(tag).strip()]
        with semaphore:
            if mode == asset_index.RUN_INITIALIZE:
                result = asset_recognition.recognize(
                    client,
                    model=settings.asset_index_model_name,
                    image_url=image_url,
                    original_name=name,
                    timeout_seconds=settings.asset_index_timeout_seconds,
                )
            else:
                result = asset_recognition.describe(
                    client,
                    model=settings.asset_index_model_name,
                    image_url=image_url,
                    name=name,
                    tags=tags,
                    timeout_seconds=settings.asset_index_timeout_seconds,
                )
    except (RecognitionError, MaterialFilterFileError) as exc:
        message = getattr(exc, "message", str(exc))
        asset_index.fail_record(db, asset_id, version=version, error=message)
        return "failed", message
    except AssetIndexError as exc:
        asset_index.fail_record(db, asset_id, version=version, error=exc.message)
        return "failed", exc.message

    try:
        if mode == asset_index.RUN_INITIALIZE:
            _write_name_and_tags(db, asset, result, am=am, cookie=cookie, merchant_id=merchant_id)
            name = asset_recognition.name_with_extension(result.name, asset.get("original_name") or "")
            tags = list(result.tags)
        applied = asset_index.save_description(
            db,
            asset_id,
            version=version,
            description_zh=result.description_zh,
            description_en=result.description_en,
        )
    except (ValueError, OSError) as exc:
        asset_index.fail_record(db, asset_id, version=version, error=str(exc))
        return "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - AM writes and provider values both land here
        asset_index.fail_record(db, asset_id, version=version, error=str(exc))
        return "failed", str(exc)
    finally:
        asset_index.release_record(db, asset_id, run_id)

    if not applied:
        # A newer request arrived while this one was running; its result wins.
        return "skipped", None
    if mode == asset_index.RUN_INITIALIZE:
        asset_index.mark_name_tags(
            db, asset_id, name=name, tags=tags, asset_marker=claimed_marker
        )
    return "succeeded", None


def _write_name_and_tags(
    db,
    asset: dict[str, Any],
    result: asset_recognition.RecognitionResult,
    *,
    am: Any,
    cookie: str,
    merchant_id: str,
) -> None:
    asset_id = str(asset["_id"])
    marker = asset_index.asset_marker(asset)
    current = db.assets.find_one({"_id": asset_id})
    if current is None:
        raise AssetIndexError("asset_missing", "素材已删除")
    if asset_index.asset_marker(current) != marker:
        # The operator renamed or re-tagged while the model was reading the image.
        raise AssetIndexError("asset_changed", "素材在识别期间被修改，已跳过")
    name = asset_recognition.name_with_extension(result.name, asset.get("original_name") or "")
    am_assets.normalize_name(name)
    am_assets.normalize_tags(result.tags)
    am_assets.rename_asset(db, am, cookie, merchant_id, asset_id, name)
    am_assets.update_tags(db, am, cookie, merchant_id, asset_id, result.tags)


def _image_reference(
    db: Any,
    settings: Settings,
    store: Any,
    asset: dict[str, Any],
    record: dict[str, Any],
) -> str:
    """A data URL for a local file, or the stored URL for an AM-only record."""
    relative = asset.get("prepared_path") or asset.get("path")
    if relative and store is not None:
        local = store.local_path(relative)
        if local is not None and Path(local).is_file():
            data = Path(local).read_bytes()
            return prepare_model_image_data_url(
                data, max_side=settings.asset_index_image_max_side
            )
    url = str(asset.get("prepared_url") or asset.get("url") or record.get("url") or "")
    if url.startswith(("http://", "https://")) or url.startswith("data:"):
        return url
    raise AssetIndexError("asset_unreadable", "找不到可读取的图片文件")
