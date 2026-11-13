"""Incremental scan runner: fingerprint, classify, copy, recover.

One manual run at a time per scheme. The model call is bounded process-wide, the
scan itself keeps writing per-item results so a restart only loses the item whose
request was in flight.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services import material_filter_classifier as classifier
from app.services import material_filter_files as files
from app.services import material_filters as store
from app.services import material_menu_naming as menu_naming
from app.services.material_filter_files import MaterialFilterFileError

logger = logging.getLogger(__name__)

_gate_lock = threading.Lock()
_gate: tuple[int, threading.BoundedSemaphore] | None = None
_active_runs: dict[str, threading.Event] = {}
_active_lock = threading.Lock()


def _classify(client, settings, scheme, data_url, original_name):
    if scheme.get("naming_mode") == "menu_match":
        try:
            return menu_naming.as_classification(menu_naming.classify_image(
                client, menu=scheme["menu"], image_data_url=data_url, original_name=original_name,
                model=scheme["model"], timeout_seconds=settings.material_filter_timeout_seconds,
                system_prompt=scheme.get("prompt_template"),
            ))
        except Exception as exc:
            raise classifier.ClassificationError("menu_naming_failed", str(exc)) from exc
    return classifier.classify_image(
        client, model=str(scheme.get("model")), rules=scheme.get("rules") or [],
        image_data_url=data_url, prompt_template=scheme.get("prompt_template") or classifier.PROMPT_TEMPLATE,
        timeout_seconds=settings.material_filter_timeout_seconds,
    )


def _copy_result(directory, evaluation_id, result, entry_name, snapshot, short_id, *, display_name=None):
    if result.naming:
        try:
            target = menu_naming.write_named_copy(
                directory / "passed", evaluation_id, menu_naming.MenuNamingResult(**result.naming),
                suffix=Path(entry_name).suffix, data=snapshot.data, sha256=snapshot.sha256,
            )
        except menu_naming.MenuNamingError as exc:
            raise MaterialFilterFileError("menu_copy_error", str(exc)) from exc
        return target.name, str(target.relative_to(directory)).replace("\\", "/")
    name, _ = files.copy_approved_bytes(directory, display_name=display_name or result.name, short_id=short_id,
                                      suffix=Path(entry_name).suffix, data=snapshot.data, sha256=snapshot.sha256)
    return name, f"passed/{name}"


def _model_semaphore(limit: int) -> threading.BoundedSemaphore:
    global _gate
    size = max(1, int(limit or 1))
    with _gate_lock:
        if _gate is None or _gate[0] != size:
            _gate = (size, threading.BoundedSemaphore(size))
        return _gate[1]


def start_run(
    db,
    settings: Settings,
    client,
    scheme: dict[str, Any],
    *,
    request_id: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    """Persist the run before answering; the scan keeps going after the page closes."""
    run, created = store.create_run(db, scheme, request_id=request_id, limit=limit)
    if created:
        _spawn(db, settings, client, str(run["_id"]))
    return store.get_run(db, str(run["_id"]))


def resume_run(db, settings: Settings, client, run_id: str) -> dict[str, Any]:
    run = store.get_run(db, run_id)
    if run["status"] not in {"interrupted", "queued"}:
        raise store.MaterialFilterError("run_not_resumable", "只有中断或排队的任务可以继续", 409)
    scheme = store.get_scheme(db, run["scheme_id"])
    claimed = db[store.SCHEME_COLLECTION].find_one_and_update(
        {"_id": scheme["_id"], "active_run_id": None},
        {"$set": {"active_run_id": run_id}},
    )
    if not claimed:
        raise store.MaterialFilterError("scheme_busy", "该方案已有扫描任务在运行", 409)
    db[store.RUN_COLLECTION].update_one(
        {"_id": run_id}, {"$set": {"status": "queued", "error": None}}
    )
    _spawn(db, settings, client, run_id, resume=True)
    return store.get_run(db, run_id)


def retry_failed(db, settings: Settings, client, scheme: dict[str, Any]) -> dict[str, Any]:
    """A new run retries technical failures: only settled decisions are skipped."""
    return start_run(db, settings, client, scheme, request_id=store.new_id("retry"))


def stop_run(run_id: str) -> None:
    with _active_lock:
        event = _active_runs.get(run_id)
    if event:
        event.set()


def _spawn(db, settings: Settings, client, run_id: str, *, resume: bool = False) -> None:
    event = threading.Event()
    with _active_lock:
        _active_runs[run_id] = event
    thread = threading.Thread(
        target=_guarded_run,
        args=(db, settings, client, run_id, event, resume),
        name=f"material-filter-{run_id}",
        daemon=True,
    )
    thread.start()


def _guarded_run(db, settings, client, run_id: str, event: threading.Event, resume: bool) -> None:
    try:
        run_scan(db, settings, client, run_id, resume=resume, stop_event=event)
    except Exception:  # noqa: BLE001 - a crashed run must release the scheme
        logger.exception("material filter run %s crashed", run_id)
        try:
            run = db[store.RUN_COLLECTION].find_one({"_id": run_id}) or {}
            db[store.RUN_COLLECTION].update_one(
                {"_id": run_id},
                {"$set": {"status": "failed", "error": "扫描进程异常结束", "finished_at": store.now()}},
            )
            if run.get("scheme_id"):
                store.release_scheme_run(db, run["scheme_id"], run_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not finalize crashed material filter run %s", run_id)
    finally:
        with _active_lock:
            _active_runs.pop(run_id, None)


def run_scan(
    db,
    settings: Settings,
    client,
    run_id: str,
    *,
    resume: bool = False,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    run = store.get_run(db, run_id)
    if run["status"] == "interrupted" and not resume:
        return run
    if run["status"] not in {"queued", "running", "interrupted"}:
        return run
    scheme = store.get_scheme(db, run["scheme_id"])
    folder = store.get_folder(db, scheme["folder_id"])
    source_root = folder["source_root"]
    directory = store.scheme_directory(settings, scheme)
    counters = dict(run.get("counters") or {})
    for key, _ in store.empty_counters().items():
        counters.setdefault(key, 0)

    db[store.RUN_COLLECTION].update_one(
        {"_id": run_id},
        {"$set": {"status": "running", "started_at": run.get("started_at") or store.now()}},
    )

    evaluator = _Evaluator(db, settings, client, scheme, counters, run_id)
    finished_status = "succeeded"
    error: str | None = None
    try:
        entries = files.scan_source(
            source_root,
            recursive=bool(scheme.get("recursive")),
            max_files=settings.material_filter_max_files_per_run,
        )
    except MaterialFilterFileError as exc:
        db[store.RUN_COLLECTION].update_one(
            {"_id": run_id},
            {"$set": {"status": "failed", "error": exc.message, "finished_at": store.now()}},
        )
        store.release_scheme_run(db, scheme["_id"], run_id)
        return store.get_run(db, run_id)

    counters["scanned"] = len(entries)
    store.set_run_counters(db, run_id, counters)

    try:
        for entry in entries:
            if stop_event is not None and stop_event.is_set():
                finished_status = "interrupted"
                error = "任务被中断，可稍后继续"
                break
            if run.get("limit") and evaluator.evaluated >= int(run["limit"]):
                break
            try:
                evaluator.process(entry, directory)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                logger.exception("material filter item failed: %s", entry.relative_path)
                store.record_run_error(db, run_id, entry.relative_path, str(exc))
                counters["errors"] += 1
            finally:
                # Persist after every file so the page can show real progress.
                store.set_run_counters(db, run_id, counters)
    finally:
        db[store.RUN_COLLECTION].update_one(
            {"_id": run_id},
            {
                "$set": {
                    "status": finished_status,
                    "error": error,
                    "counters": counters,
                    "finished_at": store.now(),
                }
            },
        )
        store.release_scheme_run(db, scheme["_id"], run_id)
    return store.get_run(db, run_id)


class _Evaluator:
    def __init__(
        self,
        db,
        settings: Settings,
        client,
        scheme: dict[str, Any],
        counters: dict[str, int],
        run_id: str,
    ) -> None:
        self.db = db
        self.settings = settings
        self.client = client
        self.scheme = scheme
        self.counters = counters
        self.run_id = run_id
        self.evaluated = 0
        self.semaphore = _model_semaphore(settings.material_filter_concurrency)
        self.rules = [rule for rule in scheme.get("rules") or [] if isinstance(rule, dict)]

    @property
    def scheme_id(self) -> str:
        return str(self.scheme["_id"])

    def _bump(self, **updates: int) -> None:
        for name, value in updates.items():
            self.counters[name] = self.counters.get(name, 0) + value

    def process(self, entry: files.SourceEntry, directory: Path) -> None:
        key = store.path_key(entry.relative_path)
        file_doc = store.get_file(self.db, self.scheme_id, key)
        # A settled verdict (pass/fail/review) is final for this fingerprint; a
        # technical failure is not, so the next run retries it.
        completed = False
        if file_doc and file_doc.get("latest_evaluation_id"):
            latest = self.db[store.EVALUATION_COLLECTION].find_one(
                {"_id": file_doc["latest_evaluation_id"]}
            )
            completed = bool(latest and latest.get("decision") in {"pass", "fail", "uncertain"})
        if completed and file_doc["size"] == entry.size and file_doc["mtime_ns"] == entry.mtime_ns:
            # Size and mtime unchanged: reuse the settled decision without a model call.
            store.mark_file_seen(self.db, file_doc["_id"], size=entry.size, mtime_ns=entry.mtime_ns)
            self._bump(skipped=1)
            return

        try:
            snapshot = files.read_source_snapshot(
                self.scheme.get("source_root_snapshot"),
                entry,
                max_bytes=self.settings.material_filter_max_source_bytes,
            )
        except MaterialFilterFileError as exc:
            store.record_run_error(self.db, self.run_id, entry.relative_path, exc.message)
            self._bump(errors=1)
            return

        if completed and file_doc.get("sha256") == snapshot.sha256:
            # Touched but byte-identical: refresh the fingerprint, keep the conclusion.
            store.upsert_file(
                self.db,
                self.scheme,
                entry,
                size=snapshot.size,
                mtime_ns=snapshot.mtime_ns,
                sha256=snapshot.sha256,
                latest_evaluation_id=file_doc.get("latest_evaluation_id"),
            )
            self._bump(skipped=1)
            return

        if file_doc is None:
            file_doc = store.upsert_file(
                self.db,
                self.scheme,
                entry,
                size=snapshot.size,
                mtime_ns=snapshot.mtime_ns,
                sha256=snapshot.sha256,
                latest_evaluation_id=None,
            )
            self._bump(added=1)
        else:
            self._bump(changed=1)

        existing = store.find_evaluation(self.db, self.scheme_id, file_doc["_id"], snapshot.sha256)
        if existing and existing.get("decision") == "error":
            # One row per content version: the failed attempt is replaced on retry.
            self.db[store.EVALUATION_COLLECTION].delete_one({"_id": existing["_id"]})
            existing = None
        if existing:
            store.upsert_file(
                self.db,
                self.scheme,
                entry,
                size=snapshot.size,
                mtime_ns=snapshot.mtime_ns,
                sha256=snapshot.sha256,
                latest_evaluation_id=existing["_id"],
            )
            self._bump(skipped=1)
            return

        self.evaluate(entry, snapshot, file_doc, directory)

    def evaluate(
        self,
        entry: files.SourceEntry,
        snapshot: files.SourceSnapshot,
        file_doc: dict[str, Any],
        directory: Path,
    ) -> None:
        evaluation_id = store.new_id("meval")
        short_id = evaluation_id.split("_", 1)[-1][:8]
        self.evaluated += 1

        try:
            data_url = files.prepare_model_image_data_url(
                snapshot.data,
                max_side=self.settings.material_filter_model_image_max_side,
            )
            with self.semaphore:
                result = _classify(self.client, self.settings, self.scheme, data_url, entry.name)
        except classifier.ClassificationError as exc:
            store.save_evaluation(
                self.db,
                self.scheme,
                file_doc,
                snapshot,
                evaluation_id=evaluation_id,
                result=None,
                decision="error",
                error=f"{exc.code}: {exc.message}",
                preview_path=None,
                output_path=None,
                output_name=None,
                copy_status="none",
                copy_error=None,
                short_id=short_id,
            )
            self._bump(errors=1)
            return

        preview_path: str | None = None
        try:
            preview_path = files.write_local_preview(directory, evaluation_id, snapshot.data)
        except MaterialFilterFileError:
            preview_path = None

        output_path: str | None = None
        output_name: str | None = None
        copy_status = "none"
        copy_error: str | None = None
        if result.decision == "pass":
            try:
                output_name, output_path = _copy_result(directory, evaluation_id, result, entry.name, snapshot, short_id)
                copy_status = "copied"
                self._bump(copied=1)
            except MaterialFilterFileError as exc:
                copy_status = "error"
                copy_error = exc.message
                self._bump(copy_errors=1)

        evaluation = store.save_evaluation(
            self.db,
            self.scheme,
            file_doc,
            snapshot,
            evaluation_id=evaluation_id,
            result=result,
            decision=result.decision,
            error=None,
            preview_path=preview_path,
            output_path=output_path,
            output_name=output_name,
            copy_status=copy_status,
            copy_error=copy_error,
            short_id=short_id,
        )
        if result.decision == "pass":
            self._bump(passed=1)
        elif result.decision == "fail":
            self._bump(failed=1)
        else:
            self._bump(uncertain=1)
        store.upsert_file(
            self.db,
            self.scheme,
            entry,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
            sha256=snapshot.sha256,
            latest_evaluation_id=evaluation["_id"],
        )

def recheck_evaluation(db, settings: Settings, client, scheme: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """Force a fresh read and model judgement for one image, reusing its content version."""
    folder = store.get_folder(db, scheme["folder_id"])
    try:
        snapshot = files.read_source_file(
            folder["source_root"],
            evaluation["relative_path"],
            max_bytes=settings.material_filter_max_source_bytes,
        )
    except MaterialFilterFileError as exc:
        raise store.MaterialFilterError(exc.code, exc.message, exc.status_code) from exc
    if snapshot.sha256 != evaluation["source_sha256"]:
        raise store.MaterialFilterError(
            "source_changed", "源文件内容已变化，请重新扫描，而不是复用旧结论", 409
        )
    rules = [rule for rule in scheme.get("rules") or [] if isinstance(rule, dict)]
    data_url = files.prepare_model_image_data_url(
        snapshot.data, max_side=settings.material_filter_model_image_max_side
    )
    try:
        with _model_semaphore(settings.material_filter_concurrency):
            result = _classify(client, settings, scheme, data_url, evaluation["original_name"])
    except classifier.ClassificationError as exc:
        raise store.MaterialFilterError(exc.code, exc.message, 502) from exc

    updates: dict[str, Any] = {
        "model": scheme.get("model"),
        "prompt_version": scheme.get("prompt_version"),
        "model_result": {
            "name": result.name,
            "category": result.category,
            "tags": result.tags,
            "description": result.description,
            "rules": [
                {"id": verdict.id, "verdict": verdict.verdict, "reason": verdict.reason}
                for verdict in result.verdicts
            ],
            "decision": result.decision,
            "naming": result.naming,
        },
        "naming": result.naming,
        "model_decision": result.decision,
        "decision": result.decision,
        "category": result.category,
        "error": None,
    }
    if not evaluation.get("human_decision"):
        updates["effective_decision"] = result.decision
    if not evaluation.get("display_name") or evaluation.get("display_name") == evaluation.get("model_result", {}).get("name"):
        updates["display_name"] = result.name
    if evaluation.get("tags") == evaluation.get("model_result", {}).get("tags"):
        updates["tags"] = result.tags
        updates["tag_groups"] = (result.naming or {}).get("tag_groups", {})
    if result.naming and result.naming.get("naming_status") == "ambiguous":
        # Keep historical local bytes, but revoke their active/exportable name.
        # A previous pass or shortlist cannot settle the newly uncertain flavour.
        updates.update({
            "effective_decision": "uncertain", "human_decision": None, "human_decided_at": None,
            "display_name": result.name, "tags": result.tags,
            "tag_groups": (result.naming or {}).get("tag_groups", {}),
            "output_name": None, "output_path": None, "output_sha256": None,
            "copy_status": "none", "copy_error": None,
            "manual_pick": False, "picked_at": None,
        })
    if result.decision == "pass" and (result.naming or evaluation.get("copy_status") != "copied"):
        try:
            output_name, output_path = _copy_result(
                store.scheme_directory(settings, scheme), evaluation["_id"], result,
                evaluation["original_name"], snapshot, evaluation.get("short_id") or evaluation["_id"],
                display_name=evaluation.get("display_name") or result.name,
            )
            updates.update(
                {"output_name": output_name, "output_path": output_path,
                 "output_sha256": snapshot.sha256, "copy_status": "copied", "copy_error": None}
            )
        except MaterialFilterFileError as exc:
            updates.update({"copy_status": "error", "copy_error": exc.message})
    return store.update_evaluation(db, evaluation["_id"], updates)
