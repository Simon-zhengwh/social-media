"""Read-only invariant audit for a multi-turn poster task.

Usage:
    .\\backend\\.venv\\Scripts\\python.exe backend\\scripts\\verify_multiturn.py --task-id task_xxx
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import editor as editor_module


def _layer_map(design: dict) -> dict[str, dict]:
    return {layer["id"]: layer for layer in design.get("layers", [])}


def _changed_ids(first: dict, second: dict) -> list[str]:
    before = _layer_map(first)
    after = _layer_map(second)
    return sorted(
        layer_id
        for layer_id in set(before) | set(after)
        if before.get(layer_id) != after.get(layer_id)
    )


def _is_restore(instruction: str) -> bool:
    return instruction.startswith("恢复到") or instruction.startswith("恢复")


def _is_background(instruction: str) -> bool:
    return any(word in instruction for word in ("背景", "底图", "场景"))


def _is_logo(instruction: str) -> bool:
    return "logo" in instruction.lower() or "标志" in instruction


def _is_copy(instruction: str) -> bool:
    has_role = any(
        word in instruction
        for word in ("标题", "价格", "副标题", "促销", "活动", "地址", "品牌")
    )
    has_content_verb = any(
        word in instruction for word in ("改成", "改为", "换成", "变成", "写成", "设为")
    )
    return has_role and has_content_verb


def _is_style(instruction: str) -> bool:
    has_role = any(
        word in instruction
        for word in ("标题", "价格", "副标题", "促销", "活动", "地址", "品牌")
    )
    has_style_word = any(
        word in instruction
        for word in (
            "大一点",
            "小一点",
            "字号",
            "颜色",
            "加粗",
            "对齐",
            "放大",
            "缩小",
            "深色",
            "浅色",
            "亮色",
            "奶油色",
        )
    )
    return has_role and has_style_word


def audit(task_id: str, mongo_uri: str, base_url: str) -> dict:
    db = MongoClient(mongo_uri)["social_media"]
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise SystemExit(f"task not found: {task_id}")
    versions = list(db.versions.find({"task_id": task_id}).sort("version", 1))
    checks: list[dict] = []
    violations: list[dict] = []

    for previous, current in zip(versions, versions[1:]):
        instruction = current.get("instruction") or ""
        changed = _changed_ids(previous["design"], current["design"])
        before_layers = _layer_map(previous["design"])
        after_layers = _layer_map(current["design"])
        before_bg = previous["design"].get("background") or {}
        after_bg = current["design"].get("background") or {}
        background_changed = before_bg != after_bg
        changed_text = [
            layer_id
            for layer_id in changed
            if (before_layers.get(layer_id) or {}).get("type") == "text"
            or (after_layers.get(layer_id) or {}).get("type") == "text"
        ]
        changed_logo = [
            layer_id
            for layer_id in changed
            if (before_layers.get(layer_id) or {}).get("type") == "logo"
            or (after_layers.get(layer_id) or {}).get("type") == "logo"
        ]
        target_roles = set(editor_module._roles_in_instruction(instruction))
        changed_text_roles = {
            (after_layers.get(layer_id) or before_layers.get(layer_id) or {}).get(
                "role"
            )
            for layer_id in changed_text
        }
        expected = "unknown"
        problems: list[str] = []
        if _is_restore(instruction):
            expected = "restore"
        elif _is_background(instruction):
            expected = "background"
            if changed_text:
                problems.append(f"背景修改意外改动了文字层: {changed_text}")
            if changed_logo:
                problems.append(f"背景修改意外改动了 Logo 层: {changed_logo}")
            if not background_changed:
                problems.append("背景修改没有产生新的底图资产")
        elif _is_logo(instruction):
            expected = "logo"
            if changed_text:
                problems.append(f"Logo 修改意外改动了文字层: {changed_text}")
        elif _is_style(instruction):
            expected = "style"
            if len(changed_text) < 1:
                problems.append(f"样式修改没有改动文字层: {changed_text}")
            extra_roles = changed_text_roles - target_roles
            if extra_roles:
                problems.append(f"样式修改改到了未指定的图层角色: {sorted(extra_roles)}")
            if changed_logo:
                problems.append(f"样式修改意外改动了 Logo 层: {changed_logo}")
            if background_changed:
                problems.append("样式修改意外改动了底图")
        elif _is_copy(instruction):
            expected = "copy"
            if len(changed_text) == 0:
                pass
            else:
                extra_roles = changed_text_roles - target_roles
                if extra_roles:
                    problems.append(
                        f"文案修改改到了未指定的图层角色: {sorted(extra_roles)}"
                    )
                for target_id in changed_text:
                    before_layer = dict(before_layers.get(target_id) or {})
                    after_layer = dict(after_layers.get(target_id) or {})
                    before_layer.pop("content", None)
                    after_layer.pop("content", None)
                    if before_layer != after_layer:
                        problems.append(
                            f"文案修改只应改 content，但图层 {target_id} 的其他属性也变了"
                        )
            if changed_logo:
                problems.append(f"文案修改意外改动了 Logo 层: {changed_logo}")
            if background_changed:
                problems.append("文案修改意外改动了底图")
        elif instruction.startswith("直接编辑") or instruction.startswith("直接调整"):
            expected = "direct"
            if len(changed) != 1:
                problems.append(f"直接图层编辑应只改一个图层，实际: {changed}")
            if background_changed:
                problems.append("直接文字编辑意外改动了底图")

        entry = {
            "from_version": previous["version"],
            "to_version": current["version"],
            "instruction": instruction,
            "expected": expected,
            "changed_layers": changed,
            "changed_text_layers": changed_text,
            "changed_logo_layers": changed_logo,
            "background_changed": background_changed,
            "problems": problems,
            "noop": len(changed) == 0 and not background_changed,
        }
        checks.append(entry)
        if problems:
            violations.append(entry)

    asset_checks = []
    for version in versions:
        asset_id = version.get("output_asset_id")
        if not asset_id:
            asset_checks.append(
                {"version": version["version"], "ok": False, "error": "missing output"}
            )
            continue
        try:
            with urllib.request.urlopen(
                f"{base_url}/api/assets/{asset_id}/file", timeout=60
            ) as response:
                data = response.read()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                asset_checks.append(
                    {
                        "version": version["version"],
                        "ok": True,
                        "size": list(image.size),
                        "bytes": len(data),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            asset_checks.append(
                {"version": version["version"], "ok": False, "error": str(exc)}
            )

    return {
        "task_id": task_id,
        "current_version": task.get("current_version"),
        "versions": len(versions),
        "checks": checks,
        "violations": violations,
        "assets_ok": all(item["ok"] for item in asset_checks),
        "assets": asset_checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27018")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[2] / "storage" / "verification" / "multiturn-invariants.json"),
    )
    args = parser.parse_args()
    report = audit(args.task_id, args.mongo_uri, args.base_url)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "task_id": report["task_id"],
                "versions": report["versions"],
                "violations": len(report["violations"]),
                "assets_ok": report["assets_ok"],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
