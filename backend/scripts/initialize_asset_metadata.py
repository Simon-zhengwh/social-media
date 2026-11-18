"""One-off local initialization of asset names, tags and bilingual keywords.

Writes a restorable backup first, then runs the same background index pass the page
uses, so every image gets exactly one model call and the browser reflects the same
records. A restore only reverts an asset that still holds the value this script wrote,
so later operator edits are never overwritten.

Examples (PowerShell, from ``backend``)::

    .\\.venv\\Scripts\\python.exe -m scripts.initialize_asset_metadata --check
    .\\.venv\\Scripts\\python.exe -m scripts.initialize_asset_metadata --limit 5
    .\\.venv\\Scripts\\python.exe -m scripts.initialize_asset_metadata
    .\\.venv\\Scripts\\python.exe -m scripts.initialize_asset_metadata --restore storage/diagnostics/asset-init-.../before.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import BACKEND_DIR, PROJECT_DIR, Settings, get_settings, local_bypass_enabled
from app.db import create_client
from app.services import asset_index, asset_index_runner
from app.services.core_ai import CoreAIClient
from app.services.object_store import create_object_store
from app.services.scoped_db import ScopedDatabase

DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "storage" / "diagnostics"


class InitError(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=_jsonable) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_settings(env_file: Path | None) -> Settings:
    if env_file is not None:
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(env_file.resolve())
        get_settings.cache_clear()
    return get_settings()


def backup(db, store: Any, asset_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Everything needed to put the library's metadata back the way it is now."""
    rows: list[dict[str, Any]] = []
    for asset in db.assets.find({}):
        asset_id = str(asset["_id"])
        if asset_ids is not None and asset_id not in asset_ids:
            continue
        record = asset_index.record_for_asset(db, asset_id) or {}
        relative = asset.get("prepared_path") or asset.get("path")
        local = store.local_path(relative) if relative and store is not None else None
        rows.append(
            {
                "asset_id": asset_id,
                "original_name": asset.get("original_name"),
                "tags": list(asset.get("tags") or []),
                "has_tags_field": "tags" in asset,
                "description_zh": record.get("description_zh") or "",
                "description_en": record.get("description_en") or "",
                "keywords": record.get("keywords") or {"zh": [], "en": []},
                "description_status": record.get("description_status"),
                "sha256": asset.get("sha256"),
                "path": relative,
                "file_exists": bool(local and Path(local).is_file()),
            }
        )
    return rows


def restore(db, backup_path: Path) -> dict[str, int]:
    """Revert only what this script wrote; anything edited afterwards is kept."""
    rows = _read_jsonl(backup_path)
    applied_path = backup_path.with_name("results.jsonl")
    applied = {
        row["asset_id"]: row
        for row in (_read_jsonl(applied_path) if applied_path.is_file() else [])
    }
    restored = skipped = missing = 0
    for row in rows:
        asset = db.assets.find_one({"_id": row["asset_id"]})
        if asset is None:
            missing += 1
            continue
        after = applied.get(row["asset_id"], {}).get("applied") or {}
        current_name = str(asset.get("original_name") or "")
        expected_name = str(after.get("name") or row.get("original_name") or "")
        if current_name != expected_name:
            # Renamed after the initialization: leave the operator's name alone.
            skipped += 1
            continue
        updates: dict[str, Any] = {"original_name": row.get("original_name")}
        update: dict[str, Any] = {"$set": updates}
        if row.get("has_tags_field"):
            updates["tags"] = list(row.get("tags") or [])
        else:
            update["$unset"] = {"tags": ""}
        db.assets.update_one({"_id": row["asset_id"]}, update)
        restored += 1
    return {"restored": restored, "skipped": skipped, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=BACKEND_DIR / ".env.local")
    parser.add_argument("--merchant-id", default="")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 张（首批验证用）")
    parser.add_argument("--resume", action="store_true", help="只处理尚未成功的素材，成功的不会重复调用模型")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="只读盘点并备份，不调用模型")
    parser.add_argument("--restore", type=Path, default=None, help="按备份文件恢复名称和 Tag")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file if args.env_file.is_file() else None)
        if not settings.core_ai_api_key and not args.check and args.restore is None:
            raise InitError("未配置 CORE_AI_API_KEY，无法生成名称与描述")
        client_db = create_client(settings.mongodb_uri)
    except Exception as exc:  # noqa: BLE001 - startup failure is reported as text
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    try:
        raw = client_db[settings.mongodb_database]
        merchant_id = args.merchant_id.strip() or settings.am_bypass_merchant_id.strip()
        if not merchant_id:
            raise InitError("请用 --merchant-id 指定商户，或在环境文件中配置 AM_BYPASS_MERCHANT_ID")
        if raw.merchants.find_one({"_id": merchant_id}, {"_id": 1}) is None:
            raise InitError(f"商户 {merchant_id} 尚未导入 MongoDB")
        db = ScopedDatabase(raw, merchant_id)

        if args.restore is not None:
            report = restore(db, args.restore)
            print(f"恢复完成：{report}")
            return 0

        entries = asset_index.all_catalog(db)
        if args.resume:
            unfinished = set(asset_index.unfinished_asset_ids(db))
            entries = [entry for entry in entries if entry["asset_id"] in unfinished]
        if args.limit:
            entries = entries[: args.limit]
        scope = [entry["asset_id"] for entry in entries]
        if not scope:
            raise InitError("该商户资产库里没有素材")
        output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / f"asset-init-{_stamp()}")
        rows = backup(db, create_object_store(settings), scope)
        _write_jsonl(output_dir / "before.jsonl", rows)
        missing_files = [row["asset_id"] for row in rows if not row["file_exists"]]
        print(f"素材 {len(scope)} 张，原图缺失 {len(missing_files)} 张，备份：{output_dir / 'before.jsonl'}")
        if args.check:
            print("只读检查完成；未调用模型，未修改素材。")
            return 0

        # This script is the single writer for its merchant: release anything a crashed
        # process left behind so its claims cannot block the batch.
        asset_index.interrupt_active_runs(db)
        asset_index_runner.sync_catalog(db)
        run, _created = asset_index.create_run(
            db, mode=asset_index.RUN_INITIALIZE, asset_ids=scope, reason="local-initialize"
        )
        settings.asset_index_concurrency = max(1, args.concurrency)
        active = CoreAIClient(settings)
        try:
            finished = asset_index_runner.run_index(
                db,
                settings,
                active,
                create_object_store(settings),
                str(run["_id"]),
                am=None,
                cookie="",
                merchant_id=merchant_id,
            )
        finally:
            active.close()

        results: list[dict[str, Any]] = []
        for asset_id in scope:
            record = asset_index.record_for_asset(db, asset_id) or {}
            asset = db.assets.find_one({"_id": asset_id}) or {}
            results.append(
                {
                    "asset_id": asset_id,
                    "original_name": asset.get("original_name"),
                    "applied": {"name": asset.get("original_name"), "tags": list(asset.get("tags") or [])},
                    "keywords": record.get("keywords") or {"zh": [], "en": []},
                    "description_status": record.get("description_status"),
                    "description_error": record.get("description_error") or "",
                }
            )
        _write_jsonl(output_dir / "results.jsonl", results)
        report = {
            "merchant_id": merchant_id,
            "run_id": str(run["_id"]),
            "mode": finished.get("mode"),
            "status": finished.get("status"),
            "counters": finished.get("counters"),
            "errors": finished.get("errors"),
            "assets": len(scope),
            "missing_files": missing_files,
            "model": settings.asset_index_model_name,
            "concurrency": settings.asset_index_concurrency,
            "finished_at": _jsonable(datetime.now(timezone.utc)),
            "local_bypass": local_bypass_enabled(settings),
        }
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_jsonable), encoding="utf-8"
        )
        print(f"任务 {run['_id']}：{finished.get('status')} {finished.get('counters')}")
        print(f"结果：{output_dir / 'results.jsonl'}")
        return 0 if finished.get("status") == "succeeded" else 2
    except InitError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
