from __future__ import annotations

import argparse
import io
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from app.config import PROJECT_DIR, get_settings
from app.db import create_client, get_database
from app.services import brand_style, designer, pipeline, storage
from app.services import design as design_utils
from app.services.canvas import canvas_size
from app.services.core_ai import CoreAIClient
from app.services.object_store import create_object_store
from app.services.renderer import render_design

CASES = [
    {
        "id": "restaurant_promo",
        "ratio": "9:16",
        "product": "seejoy火锅.png",
        "logo": "logo.png",
        "prompt": "做一张 9:16 秋季火锅促销海报。文案必须包含：“畅吃自助”和“$56.99/位”，促销“儿童5折”，日期“10月25日”。价格和优惠要有强层级，菜品有真实食欲。",
    },
    {
        "id": "brand_activity",
        "ratio": "1:1",
        "product": None,
        "logo": "seejoy_logo.png",
        "prompt": "做一张 1:1 品牌周年活动海报，不使用商品图，不出现任何价格。标题原文：“相聚周年”，副标题“好食材 才有好相聚”，构图自由。",
    },
    {
        "id": "info_dense",
        "ratio": "4:5",
        "product": "seejoy门店.png",
        "logo": "seejoy_logo.png",
        "prompt": "做一张 4:5 信息密集海报。主标题“夜宵时间到”，活动信息必须完整保留：“营业时间 12PM - 12AM”，“地址 119 W 31st St”，“儿童5折｜5岁以下免费”。",
    },
    {
        "id": "art_layout",
        "ratio": "16:9",
        "product": "seejoy火锅.png",
        "logo": None,
        "prompt": "做一张 16:9 强艺术排版海报。主标题“热气开场”使用侧向、斜向分色组合，允许标题与菜品有意穿插；不要套用上标题下价格的固定模板。",
    },
    {
        "id": "logo_product",
        "ratio": "1:1",
        "product": "seejoy火锅.png",
        "logo": "logo.png",
        "prompt": "做一张 1:1 品牌海报。主标题“今晚见”，商品图和 Logo 需要成为独立可移动对象；Logo 使用原始透明素材，不要重绘。",
        "independent_product": True,
    },
]


class CountingCoreAI:
    def __init__(self, client):
        self.client = client
        self.image_calls = 0
        self.chat_calls = 0

    def generate_image(self, *args, **kwargs):
        self.image_calls += 1
        return self.client.generate_image(*args, **kwargs)

    def edit_image(self, *args, **kwargs):
        self.image_calls += 1
        return self.client.edit_image(*args, **kwargs)

    def chat_json(self, *args, **kwargs):
        self.chat_calls += 1
        return self.client.chat_json(*args, **kwargs)

    def download_image(self, *args, **kwargs):
        return self.client.download_image(*args, **kwargs)


def _find_asset(db, kind: str, name: str):
    asset = db.assets.find_one({"kind": kind, "original_name": name})
    if asset:
        return asset
    path = PROJECT_DIR / "素材" / name
    if not path.is_file():
        return None
    return None


def _seed_asset(settings, store, db, kind: str, name: str | None):
    if not name:
        return None
    asset = _find_asset(db, kind, name)
    if asset:
        return asset
    path = PROJECT_DIR / "素材" / name
    if not path.is_file():
        raise FileNotFoundError(path)
    prepared = storage.prepare_upload(settings, store, path.read_bytes(), name, kind)
    doc = {
        "_id": prepared.id,
        "kind": prepared.kind,
        "original_name": prepared.original_name,
        "path": prepared.path,
        "prepared_path": prepared.prepared_path,
        "mime_type": prepared.mime_type,
        "width": prepared.width,
        "height": prepared.height,
        "bytes": prepared.bytes,
        "sha256": prepared.sha256,
        "created_at": datetime.now(timezone.utc),
    }
    db.assets.insert_one(doc)
    return doc


def _task_doc(settings, brand, product, logo, case):
    task_id = f"task_bench_{case['id']}_{int(time.time())}"
    width, height = canvas_size(case["ratio"])
    now = datetime.now(timezone.utc)
    return {
        "_id": task_id,
        "status": "queued",
        "stage": "pending",
        "progress": 0,
        "input": {
            "product_asset_ids": [product["_id"]] if product else [],
            "logo_asset_id": logo["_id"] if logo else None,
            "brand_id": brand.id,
            "prompt": case["prompt"],
            "ratio": case["ratio"],
        },
        "brand": brand.snapshot(),
        "canvas": {"width": width, "height": height},
        "provider": {"name": "core-ai", "model": settings.core_ai_image_model, "task_id": None, "status": None, "progress": 0},
        "creative_plan": None,
        "design": None,
        "current_version": 0,
        "messages": [{"id": f"msg_{task_id}", "role": "user", "content": case["prompt"], "created_at": now, "version": None, "actions": [], "status": "done"}],
        "edit": {"status": "idle", "instruction": None, "error": None},
        "background_asset_id": None,
        "background_clean_asset_id": None,
        "output_asset_id": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def _thumbnail(image: Image.Image, path: Path, max_side: int = 320) -> None:
    copy = image.copy().convert("RGB")
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    copy.save(path, format="JPEG", quality=82)


def _save_image_from_asset(db, store, asset_id: str, path: Path) -> Image.Image:
    asset = db.assets.find_one({"_id": asset_id})
    image = storage.open_image(store, asset.get("prepared_path") or asset["path"])
    image.save(path)
    return image


def _render_baseline(db, settings, store, task, plan, product, logo, out_dir: Path):
    design = task.get("design") or {}
    canvas = task["canvas"]
    copy = {item["role"]: item["text"] for item in plan.get("copy_items") or []}
    layout = designer.promo_layout(canvas, copy, has_logo=logo is not None)
    text_layers, logo_layer = designer.layout_to_layers(layout, canvas=canvas, logo_asset_id=logo["_id"] if logo else None)
    if not text_layers:
        return None
    baseline = design_utils.make_scene(
        canvas=canvas,
        clean_asset_id=design["background"]["clean_asset_id"],
        render_asset_id=design["background"]["render_asset_id"],
        background_prompt=design["background"].get("prompt") or "",
        logo_integrated=False,
        layers=text_layers + ([logo_layer] if logo_layer else []),
        design_intent=design.get("design_intent"),
        scene_revision=0,
    )
    candidate = pipeline.render_candidate(db, settings, store, task_id=task["_id"], design=baseline, candidate_id="legacy_baseline")
    image = _save_image_from_asset(db, store, candidate["output_asset_id"], out_dir / "baseline.png")
    _thumbnail(image, out_dir / "baseline_thumb.jpg")
    return candidate


def run_case(settings, store, db, client, case, output_dir: Path):
    brand = brand_style.load_brand_skill(PROJECT_DIR / ".agents" / "skills" / "seejoy-brand-poster")
    product = _seed_asset(settings, store, db, "product", case.get("product"))
    logo = _seed_asset(settings, store, db, "logo", case.get("logo"))
    task = _task_doc(settings, brand, product, logo, case)
    task["brand"] = brand_style.snapshot_brand(brand, db, store, settings)
    db.tasks.insert_one(task)
    t0 = time.perf_counter()
    pipeline.run_task(db, settings, store, client, task["_id"])
    elapsed = round(time.perf_counter() - t0, 2)
    stored = db.tasks.find_one({"_id": task["_id"]})
    out = output_dir / case["id"]
    out.mkdir(parents=True, exist_ok=True)
    plan = stored.get("creative_plan") or {}
    report = {
        "case": case["id"],
        "task_id": task["_id"],
        "status": stored.get("status"),
        "stage": stored.get("stage"),
        "error": stored.get("error"),
        "elapsed_seconds": elapsed,
        "copy_items": plan.get("copy_items"),
        "design_intent": plan.get("design_intent"),
        "review": stored.get("review"),
        "quality": stored.get("quality"),
        "model_calls": {"image": getattr(client, "image_calls", None), "chat": getattr(client, "chat_calls", None)},
        "candidates": list(db.candidates.find({"task_id": task["_id"]})),
    }
    if stored.get("output_asset_id"):
        final = _save_image_from_asset(db, store, stored["output_asset_id"], out / "final.png")
        _thumbnail(final, out / "final_thumb.jpg")
        baseline = _render_baseline(db, settings, store, stored, plan, product, logo, out)
        if baseline:
            report["baseline_candidate"] = baseline["candidate_id"]
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(
        f"# {case['id']}\n\n- status: {report['status']}\n- elapsed: {elapsed}s\n- copy: {report['copy_items']}\n- review: {report['review']}\n- quality: {report['quality']}\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="actually call paid core-ai models")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Refusing paid calls without --live")
    settings = get_settings()
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, settings.mongodb_database)
    store = create_object_store(settings)
    client = CountingCoreAI(CoreAIClient(settings))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.out) if args.out else PROJECT_DIR / "storage" / "benchmarks" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = [case for case in CASES if not args.case or case["id"] in args.case]
    reports = []
    try:
        for case in selected:
            print(f"[benchmark] {case['id']}")
            reports.append(run_case(settings, store, db, client, case, output_dir))
    finally:
        client.client.close()
        mongo.close()
    (output_dir / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(output_dir)


if __name__ == "__main__":
    main()
