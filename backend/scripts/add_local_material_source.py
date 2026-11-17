"""Register a local fixed-disk folder as a material-filter source and run it.

The folder is added exactly like an SMB TAB, a scheme is created from an existing
menu-naming scheme (same menu, model and prompt), then the group scan and the
representative recognition run in this process. Nothing is written into the
source folder; copies, previews and records stay in the scheme output root,
local MongoDB and the Core AI model calls.

Examples
--------
    # Only register the folder and its scheme; no grouping, no model calls
    .venv\\Scripts\\python.exe scripts\\add_local_material_source.py \\
        --source "D:\\素材\\菜品选图" --template-scheme "朵颐菜单命名·全量菜单" --dry-run

    # Register, group every photo and recognize one representative per group
    .venv\\Scripts\\python.exe scripts\\add_local_material_source.py \\
        --source "D:\\素材\\菜品选图" --template-scheme "朵颐菜单命名·全量菜单" --name "朵颐菜品选图"
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_DIR, get_settings  # noqa: E402
from app.db import create_client, get_database  # noqa: E402
from app.services import material_filter_files as files  # noqa: E402
from app.services import material_filters as store  # noqa: E402
from app.services import material_group_runner as group_runner  # noqa: E402
from app.services.core_ai import CoreAIClient  # noqa: E402
from app.services.scoped_db import ScopedDatabase  # noqa: E402


def _scheme_by_reference(db, reference: str) -> dict[str, Any]:
    scheme = db[store.SCHEME_COLLECTION].find_one({"_id": reference})
    if scheme:
        return scheme
    matches = list(db[store.SCHEME_COLLECTION].find({"name": reference}))
    if not matches:
        raise SystemExit(f"找不到方案：{reference}")
    if len(matches) > 1:
        raise SystemExit(
            f"方案名称「{reference}」有 {len(matches)} 个，请改用方案 ID：" + ", ".join(s["_id"] for s in matches)
        )
    return matches[0]


def _run_label(run: dict[str, Any]) -> str:
    counters = run.get("counters") or {}
    parts = [f"{key}={value}" for key, value in counters.items() if value]
    return f"{run['_id']} {run['status']}" + (f" ({', '.join(parts)})" if parts else "")


def _ensure_scheme(db, settings, folder: dict[str, Any], template: dict[str, Any], name: str) -> dict[str, Any]:
    for existing in store.list_schemes(db, folder["_id"]):
        if existing.get("name") == name:
            return existing
    return store.create_scheme(
        db,
        settings,
        folder,
        name=name,
        rules=None,
        recursive=bool(template.get("recursive")),
        output_root=template.get("output_root") or "",
        model=template.get("model") or "",
        naming_mode=template.get("naming_mode") or "descriptive",
        menu=template.get("menu"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a local material-filter source and run it.")
    parser.add_argument("--source", required=True, help="本机固定磁盘文件夹的绝对路径")
    parser.add_argument("--template-scheme", required=True, help="沿用其菜单、模型和提示词的方案名称或 ID")
    parser.add_argument("--name", default="", help="新方案名称，默认沿用来源目录名")
    parser.add_argument("--merchant-id", default=os.environ.get("AM_BYPASS_MERCHANT_ID", ""))
    parser.add_argument("--dry-run", action="store_true", help="只登记来源和方案，不分组、不请求模型")
    parser.add_argument("--skip-groups", action="store_true", help="已有分组时跳过分组扫描")
    parser.add_argument("--group-limit", type=int, default=None, help="只识别分组数最少的前 N 组，试跑用")
    parser.add_argument("--request-prefix", default="", help="操作 ID 前缀，重复执行时复用作幂等键")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="配置文件的路径，默认与 scripts/local_dev.py 一致：优先 backend/.env.local",
    )
    args = parser.parse_args()

    # Match the local dev entry point: the pipeline reads local data, so the
    # local env file wins over the sample backend/.env.
    if args.env_file is not None:
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(args.env_file.resolve())
    elif not os.environ.get("SOCIAL_MEDIA_ENV_FILE") and (BACKEND_DIR / ".env.local").is_file():
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(BACKEND_DIR / ".env.local")

    if not args.merchant_id:
        raise SystemExit("缺少商户 ID：请传 --merchant-id 或设置 AM_BYPASS_MERCHANT_ID")

    settings = get_settings()
    if not args.dry_run and not settings.core_ai_api_key:
        raise SystemExit("缺少 CORE_AI_API_KEY，无法请求模型")

    mongo = create_client(settings.mongodb_uri)
    db = ScopedDatabase(get_database(mongo, settings.mongodb_database), args.merchant_id)
    try:
        template = _scheme_by_reference(db, args.template_scheme)
        folder, created = store.add_folder(db, args.source)
        print(f"来源：{folder['display_name']} [{folder.get('source_kind') or 'smb'}] {folder['source_root']}"
              f" ({'新增' if created else '已存在'})")

        scheme_name = args.name.strip() or folder["display_name"]
        scheme = _ensure_scheme(db, settings, folder, template, scheme_name)
        print(f"方案：{scheme['_id']} {scheme['name']} · 模型 {scheme.get('model')} · 菜单 "
              f"{(scheme.get('menu') or {}).get('menu_version')} · 输出 {scheme.get('output_root')}")

        if args.dry_run:
            entries = files.scan_source(
                scheme["source_root_snapshot"],
                recursive=bool(scheme.get("recursive")),
                max_files=settings.material_filter_max_files_per_run,
            )
            print(f"来源图片：{len(entries)} 张（未分组、未请求模型）")
            return 0

        prefix = args.request_prefix or uuid.uuid4().hex[:12]
        if not args.skip_groups:
            scan, created = group_runner.create_scan_run(db, scheme, request_id=prefix + "-scan")
            if created:
                print("开始分组扫描…")
                scan = group_runner.run_job(db, settings, None, scan["_id"])
            print("分组：", _run_label(scan))

        scheme = store.get_scheme(db, scheme["_id"])
        client = CoreAIClient(settings)
        try:
            recognition, created = group_runner.create_recognition_run(
                db,
                settings,
                scheme,
                request_id=prefix + "-recognize",
                mode="representatives",
                representative_count=1,
                group_limit=args.group_limit,
            )
            if created:
                print("开始识别代表图…")
                recognition = group_runner.run_job(db, settings, client, recognition["_id"])
        finally:
            client.close()
        print("识别：", _run_label(recognition))
        errors = recognition.get("error") or ""
        auto_pick_errors = recognition.get("auto_pick_errors") or []
        if errors:
            print("识别失败：", errors)
        if auto_pick_errors:
            print("自动入选问题：", "; ".join(auto_pick_errors))
        return 1 if errors else 0
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
