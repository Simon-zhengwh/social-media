"""Upload manually picked local material-filter results to the deployed asset library.

The script reads the local MongoDB records (same merchant as UAT), takes the
passed photos whose ``manual_pick`` flag is set, and uploads the untouched local
copies to ``/api/merchants/<id>/assets`` using an AM Console session cookie.

Uploads are idempotent: the deployed service returns the existing asset when the
bytes were uploaded before, and the script skips rows that already carry a
``uat_asset_id`` unless ``--force`` is given.

Examples
--------
    # Find the Orca tab on the UAT site and reuse its session cookie
    .venv\\Scripts\\python.exe scripts\\sync_picked_to_uat.py --scheme 排除空座位和蟹腿 --orca-auto --dry-run

    # Upload everything picked, tag it with the AI tags, write a CSV report
    .venv\\Scripts\\python.exe scripts\\sync_picked_to_uat.py --scheme 排除空座位和蟹腿 \\
        --orca-auto --report ../storage/diagnostics/uat-sync.csv

    # Or pass the cookie yourself (DevTools -> Application -> Cookies)
    $env:AM_SESSION_COOKIE = "AMSessionId=..."
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import create_client  # noqa: E402
from app.services import material_filter_files as files  # noqa: E402
from app.services import material_filters as store  # noqa: E402
from app.services.scoped_db import ScopedDatabase  # noqa: E402

DEFAULT_BASE_URL = "https://social-media.connexup-uat.net"
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".gif": "image/gif",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _run_json(command: list[str]) -> Any:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(command)}\n{result.stderr.strip()}")
    return json.loads(result.stdout)


def cookie_from_orca(base_url: str) -> str:
    """Reuse the AM session of an open Orca tab on the deployed site."""
    payload = _run_json(["orca", "tab", "list", "--json"])
    tabs = payload.get("result", {}).get("tabs") or []
    host = httpx.URL(base_url).host
    page_id = ""
    for tab in tabs:
        url = str(tab.get("url") or "")
        if host and host in url:
            page_id = str(tab.get("browserPageId") or "")
            break
    if not page_id:
        raise SystemExit(f"没有找到打开 {base_url} 的 Orca 标签页，请先打开资产库页面。")
    cookies = _run_json(["orca", "cookie", "get", "--page", page_id, "--json"])
    for cookie in cookies.get("result", {}).get("cookies") or []:
        if cookie.get("name") == "AMSessionId":
            return f"AMSessionId={cookie['value']}"
    raise SystemExit("该标签页没有 AM Session，请在浏览器里登录 AM Console 后重试。")


def resolve_scheme(db, value: str) -> dict[str, Any]:
    scheme = db.material_filter_schemes.find_one({"_id": value})
    if scheme is None:
        scheme = db.material_filter_schemes.find_one({"name": value})
    if scheme is None:
        raise SystemExit(f"找不到方案：{value}（可用 --list 查看）")
    return scheme


def pending_rows(db, scheme: dict[str, Any], *, all_passed: bool, force: bool, limit: int) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"scheme_id": scheme["_id"], "copy_status": "copied"}
    if not all_passed:
        query["manual_pick"] = True
    if not force:
        query["uat_asset_id"] = None
    rows = list(db.material_filter_evaluations.find(query).sort("picked_at", -1))
    if limit:
        rows = rows[:limit]
    return rows


def local_copy_path(settings, scheme: dict[str, Any], evaluation: dict[str, Any]) -> Path:
    directory = store.scheme_directory(settings, scheme)
    target = directory / str(evaluation["output_path"])
    if not target.is_file():
        raise FileNotFoundError(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != evaluation["source_sha256"]:
        raise ValueError(f"本机副本与评估内容不一致：{target.name}")
    return target


def _collect_names(client: httpx.Client, base_url: str, merchant_id: str, path: str) -> set[str]:
    names: set[str] = set()
    skip = 0
    while True:
        try:
            response = client.get(
                f"{base_url}/api/merchants/{merchant_id}{path}",
                params={"skip": skip, "limit": 100},
            )
        except Exception:  # noqa: BLE001 - numbering falls back to what we already have
            break
        if response.status_code != 200:
            break
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("items") or []
        for item in items:
            name = str(item.get("original_name") or "").strip()
            if name:
                names.add(name.casefold())
        total = payload.get("total") if isinstance(payload, dict) else None
        skip += len(items)
        if not items or len(items) < 100 or (total is not None and skip >= int(total)):
            break
    return names


def existing_asset_names(client: httpx.Client, base_url: str, merchant_id: str) -> set[str]:
    """Names already visible in the deployed library: uploads plus AM media records."""
    names = _collect_names(client, base_url, merchant_id, "/assets")
    names |= _collect_names(client, base_url, merchant_id, "/am-assets")
    return names


def upload_name(row: dict[str, Any], suffix: str, used: set[str]) -> str:
    """Chinese name only; append -2, -3 ... when the library already has it."""
    stem = files.sanitize_output_stem(
        row.get("display_name") or row.get("original_name") or "图片", max_length=60
    )
    candidate = stem
    index = 2
    while f"{candidate}{suffix}".casefold() in used:
        candidate = f"{stem}-{index}"
        index += 1
    used.add(f"{candidate}{suffix}".casefold())
    return f"{candidate}{suffix}"


def upload(
    client: httpx.Client,
    base_url: str,
    merchant_id: str,
    path: Path,
    *,
    file_name: str,
) -> dict[str, Any]:
    content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    with path.open("rb") as handle:
        response = client.post(
            f"{base_url}/api/merchants/{merchant_id}/assets",
            files={"file": (file_name, handle, content_type)},
        )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def desired_tags(row: dict[str, Any], *, include_model: bool) -> list[str]:
    """Deployed assets allow at most five tags; keep four model tags plus the marker."""
    tags = [] if not include_model else [str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()]
    tags = list(dict.fromkeys(tags))[:4]
    tags.append("素材筛选")
    return list(dict.fromkeys(tag for tag in tags if tag))[:5]


def find_existing_am_media(
    client: httpx.Client,
    base_url: str,
    merchant_id: str,
    original_name: str,
) -> dict[str, Any] | None:
    """The AM library may already hold the same photo from an earlier import."""
    stem = Path(original_name).stem.casefold()
    if not stem:
        return None
    try:
        response = client.get(
            f"{base_url}/api/merchants/{merchant_id}/am-assets",
            params={"keyword": stem, "limit": 50},
        )
        if response.status_code != 200:
            return None
        items = (response.json() or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return None
    for item in items:
        name = str(item.get("original_name") or "").casefold()
        if stem and stem in name:
            return item
    return None


def apply_tags(client: httpx.Client, base_url: str, merchant_id: str, asset_id: str, tags: list[str]) -> None:
    response = client.put(
        f"{base_url}/api/merchants/{merchant_id}/assets/{asset_id}/tags",
        json={"tags": tags},
    )
    if response.status_code >= 300:
        raise RuntimeError(f"打标签失败 HTTP {response.status_code}: {response.text[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把本机「已入选」素材同步到 UAT 资产库")
    parser.add_argument("--scheme", default="", help="方案 ID 或方案名称")
    parser.add_argument("--list", action="store_true", help="只列出本机方案")
    parser.add_argument("--all-passed", action="store_true", help="不限于入选，上传全部已复制副本")
    parser.add_argument("--limit", type=int, default=0, help="最多上传多少张")
    parser.add_argument("--force", action="store_true", help="忽略已同步标记重新上传")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要上传的文件")
    parser.add_argument(
        "--check-names",
        action="store_true",
        help="只检查目标文件名：列出与 UAT 资产库或本批次重名后会追加 -2/-3 的条目",
    )
    parser.add_argument("--no-model-tags", action="store_true", help="不写入 AI 标签，只写「素材筛选」")
    parser.add_argument("--base-url", default=os.environ.get("UAT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--merchant", default="", help="默认取本地配置的商户")
    parser.add_argument("--cookie", default=os.environ.get("AM_SESSION_COOKIE", ""))
    parser.add_argument("--orca-auto", action="store_true", help="从 Orca 已打开的 UAT 标签页读取会话")
    parser.add_argument("--retag", action="store_true", help="只为已同步的素材补写标签（最多 5 个）")
    parser.add_argument("--report", default="", help="CSV 报告输出路径")
    args = parser.parse_args()

    settings = get_settings()
    merchant_id = args.merchant.strip() or settings.am_bypass_merchant_id.strip()
    if not merchant_id:
        raise SystemExit("缺少商户 ID：请设置 AM_BYPASS_MERCHANT_ID 或传 --merchant。")
    db = ScopedDatabase(create_client(settings.mongodb_uri)[settings.mongodb_database], merchant_id)

    if args.list:
        for scheme in db.material_filter_schemes.find():
            folder = db.material_filter_folders.find_one({"_id": scheme["folder_id"]}) or {}
            picked = db.material_filter_evaluations.count_documents(
                {"scheme_id": scheme["_id"], "manual_pick": True}
            )
            print(f"{scheme['_id']}  {scheme['name']}  [{folder.get('display_name', '?')}]  已入选 {picked}")
        return 0

    if not args.scheme:
        raise SystemExit("请用 --scheme 指定方案（可先 --list 查看）。")

    scheme = resolve_scheme(db, args.scheme)

    if args.retag:
        cookie = args.cookie.strip()
        if not cookie and args.orca_auto:
            cookie = cookie_from_orca(args.base_url)
        if not cookie:
            raise SystemExit("缺少 AM 会话：用 --orca-auto 读取浏览器会话，或设置 AM_SESSION_COOKIE。")
        headers = {"Cookie": cookie, "Origin": args.base_url, "X-Media-Request": "1", "Accept": "application/json"}
        rows = list(
            db.material_filter_evaluations.find(
                {"scheme_id": scheme["_id"], "uat_asset_id": {"$ne": None}}
            )
        )
        updated = failed_tags = 0
        with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
            for index, row in enumerate(rows, start=1):
                asset_id = str(row.get("uat_asset_id") or "")
                if not asset_id.startswith("asset_"):
                    # Linked AM records carry the AM id; the tag route needs our local asset id.
                    failed_tags += 1
                    continue
                tags = desired_tags(row, include_model=not args.no_model_tags)
                try:
                    apply_tags(client, args.base_url, merchant_id, asset_id, tags)
                    db.material_filter_evaluations.update_one(
                        {"_id": row["_id"]}, {"$set": {"uat_tag_error": None}}
                    )
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    failed_tags += 1
                    db.material_filter_evaluations.update_one(
                        {"_id": row["_id"]}, {"$set": {"uat_tag_error": str(exc)[:300]}}
                    )
                if index % 10 == 0:
                    print(f"  已处理 {index}/{len(rows)}")
        print(f"标签更新完成：成功 {updated}，失败 {failed_tags}")
        return 0 if failed_tags == 0 else 1
    rows = pending_rows(db, scheme, all_passed=args.all_passed, force=args.force, limit=args.limit)
    if not rows:
        print("没有待上传的图片。提示：先在页面里点「☆ 入选」，或用 --all-passed。")
        return 0

    if args.dry_run or args.check_names:
        cookie = args.cookie.strip()
        if not cookie and args.orca_auto:
            cookie = cookie_from_orca(args.base_url)
        library: set[str] = set()
        if cookie:
            with httpx.Client(
                headers={"Cookie": cookie, "Accept": "application/json"}, timeout=60
            ) as client:
                library = existing_asset_names(client, args.base_url, merchant_id)
        else:
            print("提示：没有会话，只检查本批次内部重名，不比对 UAT 资产库。")

        used = set(library)
        plan: list[tuple[dict[str, Any], str, str]] = []
        for row in rows:
            suffix = Path(str(row.get("output_name") or "x.jpg")).suffix.lower() or ".jpg"
            base = files.sanitize_output_stem(
                row.get("display_name") or row.get("original_name") or "图片", max_length=60
            ) + suffix
            planned = upload_name(row, suffix, used)
            plan.append((row, base, planned))

        collisions = [item for item in plan if item[2] != item[1]]
        print(
            f"方案：{scheme['name']} | 待上传 {len(rows)} 张 | UAT 已有文件名 {len(library)} 个"
            f" | 需要 -N 区分 {len(collisions)} 张"
        )
        if collisions:
            print("以下文件名会与已有素材（或本批次前一张）重名，上传时会自动加序号：")
            for row, base, planned in collisions:
                print(f"  {row['original_name']}: {base} -> {planned}")
        else:
            print("没有重名，全部按「中文名.扩展名」上传。")
        if args.report:
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["原文件名", "计划上传名", "是否重名", "本地副本名"])
                for row, base, planned in plan:
                    writer.writerow(
                        [row["original_name"], planned, "是" if planned != base else "否", row.get("output_name")]
                    )
            print(f"清单：{path}")
        return 0

    cookie = args.cookie.strip()
    if not cookie and args.orca_auto:
        cookie = cookie_from_orca(args.base_url)
    if not cookie:
        raise SystemExit("缺少 AM 会话：用 --orca-auto 读取浏览器会话，或设置 AM_SESSION_COOKIE。")

    headers = {
        "Cookie": cookie,
        "Origin": args.base_url,
        "X-Media-Request": "1",
        "Accept": "application/json",
    }
    report: list[dict[str, Any]] = []
    failed = 0
    with httpx.Client(headers=headers, timeout=300, follow_redirects=True) as client:
        used_names = existing_asset_names(client, args.base_url, merchant_id)
        for index, row in enumerate(rows, start=1):
            label = f"[{index}/{len(rows)}] {row['original_name']}"
            try:
                path = local_copy_path(settings, scheme, row)
                suffix = path.suffix.lower() or ".jpg"
                file_name = upload_name(row, suffix, used_names)
                asset = upload(client, args.base_url, merchant_id, path, file_name=file_name)
                asset_id = str(asset.get("id") or "")
                tags = desired_tags(row, include_model=not args.no_model_tags)
                tag_error = ""
                if tags:
                    try:
                        apply_tags(client, args.base_url, merchant_id, asset_id, tags)
                    except Exception as exc:  # noqa: BLE001 - tagging is optional
                        tag_error = str(exc)[:200]
                        print(f"{label} 已上传，标签未写入：{tag_error}")
                db.material_filter_evaluations.update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "uat_asset_id": asset_id,
                            "uat_asset_name": asset.get("original_name") or file_name,
                            "uat_asset_url": asset.get("url"),
                            "uat_synced_at": now(),
                            "uat_sync_error": None,
                            "uat_tag_error": tag_error or None,
                        }
                    },
                )
                report.append(
                    {"original_name": row["original_name"], "output_name": row.get("output_name"),
                     "uat_name": asset.get("original_name") or file_name,
                     "asset_id": asset_id, "status": "uploaded" if not tag_error else "uploaded-tag-skipped",
                     "error": tag_error}
                )
                print(f"{label} -> {file_name} ({asset_id})")
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                text = str(exc)
                # AM returns 400 when the same bytes already exist in the media library
                # (usually imported earlier as seejoy-gbp-*). Link that record instead.
                if "AM Console" in text or "HTTP 502" in text:
                    existing = find_existing_am_media(client, args.base_url, merchant_id, row["original_name"])
                    if existing:
                        db.material_filter_evaluations.update_one(
                            {"_id": row["_id"]},
                            {
                                "$set": {
                                    "uat_asset_id": str(existing.get("id") or ""),
                                    "uat_asset_name": existing.get("original_name"),
                                    "uat_asset_url": existing.get("url"),
                                    "uat_synced_at": now(),
                                    "uat_sync_error": None,
                                    "uat_sync_note": "AM 资源库已存在同内容素材，已关联",
                                }
                            },
                        )
                        report.append(
                            {"original_name": row["original_name"], "output_name": row.get("output_name"),
                             "uat_name": existing.get("original_name") or "",
                             "asset_id": str(existing.get("id") or ""), "status": "linked-existing",
                             "error": ""}
                        )
                        print(f"{label} 已存在于 AM 资源库，已关联：{existing.get('original_name')}")
                        continue
                failed += 1
                db.material_filter_evaluations.update_one(
                    {"_id": row["_id"]}, {"$set": {"uat_sync_error": str(exc)[:500]}}
                )
                report.append(
                    {"original_name": row["original_name"], "output_name": row.get("output_name"),
                     "uat_name": "", "asset_id": "", "status": "failed", "error": str(exc)[:300]}
                )
                print(f"{label} 失败：{exc}")

    print(f"完成：成功 {len(report) - failed}，失败 {failed}")
    print(f"到资产库查看：{args.base_url}/?merchant_id={merchant_id}&view=assets")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["original_name", "output_name", "uat_name", "asset_id", "status", "error"]
            )
            writer.writeheader()
            writer.writerows(report)
        print(f"报告：{path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
