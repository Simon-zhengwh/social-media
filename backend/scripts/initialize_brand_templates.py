"""Preview curated brand migration; pass --apply to back up and persist it."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import json_util

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services.brand_rules import DEFAULT_RULES
from app.services.platform_presets import resolve_placement


CURATED_FIELDS = {
    "seejoy": {
        "palette_notes": "以配置的六个 HEX 色值为基础，可随参考图和 Logo 做协调的同色系变化。日常版以奶油白／米白、深绿／橄榄绿为主；活动版以暖红突出主题，少量姜黄强调数字。",
        "typography": "醒目的复古标题字，配清晰易读的正文与少量手写批注；主题与关键数字突出。",
        "subject_style": "真实美食摄影拼贴，保持原始食物数量、形状、颜色、纹理、摆盘、餐具与光影；人物与门店保持原貌。可裁切、等比缩放，不随意增删或重排照片内部菜品。",
        "background": "奶油白／米白的暖调复古背景，清楚留白与轻手作纸感；纸纹仅用于背景和图形，不覆盖食物。",
        "layout": "主题／核心优惠 → 主图与辅助说明 → 日期、时段、条件。一个视觉重点，标题与关键数字突出，正文精简。日常版清楚留白，活动版可使用白边照片拼贴。",
        "decoration": "少量细线、手写批注；活动版适量使用笔刷、撕纸与促销色块。",
        "identity": "沿用最新品牌 Logo 与暖调复古、真实美食、醒目标题、轻手作感的识别语言。",
        "restrictions": "不默认添加未定稿 IP；食物不做塑料感或 CG 感。用户明确要求删换的元素除外。",
    },
    "duoyi": {
        "palette_notes": "黑色背景、白色布料、深色墨镜、白色主文字，配淡奶油黄或浅杏黄色手写文字；菜品原有色彩作为最鲜明的视觉焦点，其余区域保持黑白与少量暖色。",
        "typography": "大号白色圆润粗体无衬线字，搭配浅奶油黄色连笔手写字。英文菜名可自然拆成两组；短名可用单组白色标题，不虚构副标题。手写字可微斜、贴近或少量叠合粗体，但必须可辨认。",
        "subject_style": "真实商业餐饮摄影，菜品清晰有质感、适度景深。保留用户菜品的形态、数量、配料、纹理、颜色、摆放关系、餐具与独有细节；仅依原始图像支持的视角做裁切、等比缩放、平移和合理透视，不生成看不到的另一侧。优先调整人物动作适配菜品。",
        "background": "纯黑或近黑极简摄影棚，局部灰白、低饱和轻薄烟雾，保留暗部层次。柔和主光照亮白布、手部、菜品，可用极轻暖金轮廓光分离人物。幽默、神秘、松弛、略搞怪但不恐怖的专业餐饮广告。",
        "layout": "9:16；前景真实菜品—中景中央或近中央幽灵—背景烟雾。为英文菜名与箭头留干净空间；文字可少量叠合白布边缘，不影响菜名、墨镜和菜品识别。姿势、镜头远近、食物与标题位置可变化，保持系列感。菜品不被手部大面积遮挡。",
        "decoration": "每张一条白色手绘弯曲箭头，从菜品附近向对应英文菜名延伸，尖端指向菜名。线条顺畅、适度粗细、略不规则，用弧线或柔和转折；避开关键菜品、墨镜及标题主体，不额外添加无意义箭头。",
        "identity": "一整块自然垂坠白色床单／白布覆盖头部和躯干，搭配深色大框墨镜，镜片黑棕或深茶色，贴合布覆头部；不露真实脸、眼、鼻、嘴，不改成骷髅、南瓜人或僵尸。白布有真实织纹、自然褶皱、重力垂坠及袖口遮挡，无多余装饰。同系列体型、布套语言与墨镜风格一致，姿势和手势可自由变化。",
        "restrictions": "手、手腕、前臂统一自然浅至中等暖肤色，暖米／米杏／浅蜜色、东亚人手部视觉特征，中性至柔和暖黄；避免过黑、红棕、发灰、偏绿、死白和不自然橙色。保留掌纹、指节细纹、肤色差异、皮肤纹理和少量光泽，体毛稀少细软；不用浓密粗硬黑毛，不夸张青筋或粗糙质感，不磨皮、蜡像或塑料化，阴影中仍有自然肤色细节，不发光或像抠贴。每手五指，掌指比例、关节位置与弯曲自然，指甲短净、甲床和弧度自然，无夸张美甲。手腕前臂连续，手臂从合理袖口伸出，无悬浮手。端盘从底部支撑，握杯包覆杯壁，指向动作关节与手臂协调；接触受力、透视、接触阴影与遮挡合理，不穿透食物餐具。双手尺寸、肤色、光照一致，无多指、少指、融合指、反关节、扭腕或畸形手掌。英文菜名为唯一必需文案，拼写、大小写、词序完整准确；不擅自添加价格、促销、日期、口号、水印或无关标识。不厚描边、霓虹发光、金属字、复杂渐变，不复杂背景、恐怖血腥、夸张特效或无关道具。不重绘菜品、改食材摆盘或纹理、不增加重复食物或悬浮食材，不凭空生成新视角；不过度锐化或 HDR。箭头必须指向菜名，不能反向或省略。",
    },
}
COUNTS = ("brands", "assets", "whole_tasks")
PRESERVED = ("_id", "colors", "logo_asset_ids", "reference_asset_ids", "cover_asset_id", "created_at")


def prepare_changes(document: dict[str, Any]) -> dict[str, Any]:
    brand_id = document["_id"]
    platform_id, placement_id = ("instagram", "story") if brand_id == "duoyi" else ("xiaohongshu", "note")
    platform, placement = resolve_placement(platform_id, placement_id)
    return {
        "schema_version": 2,
        "style_fields": CURATED_FIELDS[brand_id],
        "legacy_guides": {key: document.get(key, "") for key in ("layout_guide", "background_guide")},
        "platform_id": platform.id,
        "placement_id": placement.id,
        "platform": platform.name,
        "purpose": placement.name,
        "summary": document.get("summary") or ("暖调复古、真实美食、醒目标题与轻手作感。" if brand_id == "seejoy" else "白布幽灵、深色墨镜与黑色烟雾棚拍餐饮广告。"),
    }


def write_backup(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json_util.dumps(payload, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    # Verify complete, parseable Extended JSON before the first database write.
    if json_util.loads(path.read_text(encoding="utf-8")) != json_util.loads(json_util.dumps(payload)):
        raise RuntimeError("Backup verification failed; database was not changed")


def migrate(db: Any, backup_root: Path, *, apply: bool = False) -> dict[str, Any]:
    documents = list(db.brands.find())
    rules = list(db.brand_rules.find())
    before = {name: db[name].count_documents({}) for name in COUNTS}
    candidates = [doc for doc in documents if doc["_id"] in CURATED_FIELDS and doc.get("schema_version", 1) < 2]
    result: dict[str, Any] = {
        "mode": "apply" if apply else "preview", "before": before,
        "changes": [{"brand_id": doc["_id"], "set": prepare_changes(doc)} for doc in candidates],
        "skipped": [doc["_id"] for doc in documents if doc not in candidates],
        "initialize_common_rules": not any(item["_id"] == "default" for item in rules),
        "migrated": [], "conflicts": [],
    }
    if not apply:
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / stamp / "brand-templates.ejson"
    write_backup(backup_path, {"brands": documents, "brand_rules": rules, "counts": before})
    result["backup_path"] = str(backup_path.resolve())
    for document in candidates:
        # Match the source guides, timestamp and structure, including absent fields.
        guarded_keys = ("updated_at", "schema_version", "layout_guide", "background_guide", "summary", "platform_id", "placement_id", "style_fields", "legacy_guides")
        query = {"_id": document["_id"], **{key: document[key] if key in document else {"$exists": False} for key in guarded_keys}}
        changes = prepare_changes(document)
        changes["updated_at"] = datetime.now(timezone.utc)
        update = db.brands.update_one(query, {"$set": changes})
        if update.matched_count:
            result["migrated"].append(document["_id"])
        else:
            result["conflicts"].append(document["_id"])
    db.brand_rules.update_one({"_id": "default"}, {"$setOnInsert": {**DEFAULT_RULES, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    result["after"] = {name: db[name].count_documents({}) for name in COUNTS}
    result["preserved"] = before == result["after"]
    for original in documents:
        current = db.brands.find_one({"_id": original["_id"]})
        if current is None or any(current.get(key) != original.get(key) for key in PRESERVED):
            result["preserved"] = False
    if not result["preserved"]:
        raise RuntimeError(f"Post-migration preservation check failed; restore source from {backup_path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up and apply; default is read-only preview")
    args = parser.parse_args()
    settings = get_settings()
    client = create_client(settings.mongodb_uri)
    try:
        result = migrate(get_database(client, settings.mongodb_database), settings.storage_path / "backups" / "brand-templates", apply=args.apply)
        print(json_util.dumps(result, ensure_ascii=False, indent=2))
        if result["conflicts"]:
            raise SystemExit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
