"""Confirm ambiguous menu names from a JSON mapping and regenerate the copies.

The mapping is ``{"原文件名": "菜单中文名或菜单ID"}``. Each photo keeps its model
tags, only the dish name is settled by a human, then the local copy is written
again with the final ``中文_英文`` name and the record becomes 通过.

The menu table still owns the wording: the script looks the name up and never
invents one. No model call is made.

Example
-------
    .venv\\Scripts\\python.exe scripts\\confirm_menu_names.py \\
        --scheme mscheme_f0d78fb40102433e --mapping scripts/douyi-pick-names.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_DIR, get_settings  # noqa: E402
from app.db import create_client, get_database  # noqa: E402
from app.services import material_filter_files as files  # noqa: E402
from app.services import material_filters as store  # noqa: E402
from app.services import material_groups as groups  # noqa: E402
from app.services.scoped_db import ScopedDatabase  # noqa: E402


def _menu_item(menu: dict[str, Any], reference: str) -> dict[str, Any]:
    items = menu.get("items") or []
    by_id = [item for item in items if item.get("id") == reference]
    if by_id:
        return by_id[0]
    by_name = [item for item in items if reference in {item.get("zh"), item.get("en")}]
    if not by_name:
        raise SystemExit(f"菜单里找不到：{reference}")
    if len(by_name) > 1:
        raise SystemExit(f"菜单名「{reference}」不唯一，请改用菜单 ID")
    return by_name[0]


def _settled_naming(evaluation: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    naming = dict(evaluation.get("naming") or {})
    note = "人工按原文件名确认菜品"
    reason = str(naming.get("reason") or "").strip()
    naming.update(
        naming_status="matched",
        selected_menu_id=item["id"],
        candidate_menu_ids=[item["id"]],
        name_zh=item.get("zh"),
        name_en=item.get("en"),
        uncertain_attributes=[],
        reason=f"{reason}；{note}" if reason else note,
    )
    return naming


def main() -> int:
    parser = argparse.ArgumentParser(description="Confirm ambiguous menu names and refresh the copies.")
    parser.add_argument("--scheme", required=True, help="方案 ID")
    parser.add_argument("--mapping", required=True, type=Path, help="JSON：原文件名 -> 菜单中文名或菜单ID")
    parser.add_argument("--merchant-id", default=os.environ.get("AM_BYPASS_MERCHANT_ID", ""))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.merchant_id:
        raise SystemExit("缺少商户 ID：请传 --merchant-id 或设置 AM_BYPASS_MERCHANT_ID")
    if args.env_file is not None:
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(args.env_file.resolve())
    elif not os.environ.get("SOCIAL_MEDIA_ENV_FILE") and (BACKEND_DIR / ".env.local").is_file():
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(BACKEND_DIR / ".env.local")

    settings = get_settings()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    mongo = create_client(settings.mongodb_uri)
    db = ScopedDatabase(get_database(mongo, settings.mongodb_database), args.merchant_id)
    failed = 0
    try:
        scheme = store.get_scheme(db, args.scheme)
        menu = scheme.get("menu") or {}
        directory = store.scheme_directory(settings, scheme)
        for original_name, reference in mapping.items():
            evaluation = db[store.EVALUATION_COLLECTION].find_one(
                {"scheme_id": scheme["_id"], "original_name": original_name}
            )
            if not evaluation:
                print(f"跳过（没有记录）：{original_name}")
                failed += 1
                continue
            item = _menu_item(menu, reference)
            naming = _settled_naming(evaluation, item)
            display_name = "_".join(part for part in (naming.get("name_zh"), naming.get("name_en")) if part)
            print(f"{original_name}\n  -> {display_name}")
            if args.dry_run:
                continue
            db[store.EVALUATION_COLLECTION].update_one(
                {"_id": evaluation["_id"]},
                {"$set": {"naming": naming, "display_name": display_name}},
            )
            settled = store.get_evaluation(db, evaluation["_id"])
            reviewed = store.review_evaluation(
                db,
                settings,
                scheme,
                settled,
                decision="pass",
                source_sha256=settled["source_sha256"],
            )
            if reviewed.get("copy_status") != "copied":
                print(f"  复制失败：{reviewed.get('copy_error')}")
                failed += 1
                continue
            # The placeholder copy from the ambiguous verdict is stale now.
            keep = directory / str(reviewed["output_path"])
            folder_dir = keep.parent
            for stale in folder_dir.iterdir():
                if stale != keep and stale.is_file():
                    os.remove(files.long_path(stale))
            # Group cards show the representative name, so refresh it as a run would.
            file_doc = db[store.FILE_COLLECTION].find_one({"_id": reviewed["file_id"]})
            if file_doc and file_doc.get("group_id"):
                try:
                    group = groups.get_group(db, file_doc["group_id"])
                    groups._refresh_reference(db, group, count=1)
                except Exception as exc:  # noqa: BLE001 - naming still landed
                    print(f"  分组标题未刷新：{exc}")
            print(f"  已写入 {reviewed['output_name']}")
    finally:
        mongo.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
