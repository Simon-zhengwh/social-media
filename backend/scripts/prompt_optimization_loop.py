from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import BackgroundTasks
from PIL import Image, ImageOps

from app.config import PROJECT_DIR, Settings, get_settings
from app.db import create_client, get_database
from app.routes import tasks as task_routes
from app.routes import whole_image as whole_routes
from app.schemas import PromptOptimizeRequest, TaskCreate, WholeImageTaskCreate
from app.services import brand_style, pipeline, prompt_optimizer, storage
from app.services import whole_image as whole_service
from app.services.core_ai import CoreAIClient
from app.services.object_store import LocalObjectStore

DEFAULT_SEED = "根据素材图和参考图生成小红书海报"
DEFAULT_ASSETS = (
    PROJECT_DIR / "素材" / "杜师傅.png",
    PROJECT_DIR / "素材" / "粉丝邀请函.jpg",
)
DEFAULT_BASELINE = PROJECT_DIR / "素材" / "基准图.png"
DEFAULT_BRAND_SKILL = PROJECT_DIR / ".agents" / "skills" / "seejoy-brand-poster"
TARGETS = {"layered": 30, "whole_image": 50}
WORKFLOW_LABELS = {"layered": "海报生成（分层）", "whole_image": "自由设计（整图）"}
DIMENSIONS = (
    ("information_accuracy", "任务与信息准确性", 25),
    ("composition_hierarchy", "构图与信息层级", 20),
    ("brand_style", "品牌风格一致性", 15),
    ("asset_fusion", "素材使用与融合", 15),
    ("typography_decoration", "字体与装饰系统", 15),
    ("readability_polish", "可读性与完成度", 10),
)


class LoopError(Exception):
    pass


class CountingCoreAI:
    def __init__(self, client: Any) -> None:
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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def load_state(run_dir: Path) -> dict[str, Any]:
    path = state_path(run_dir)
    if not path.is_file():
        raise LoopError(f"运行状态不存在：{path}")
    return read_json(path)


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utcnow()
    write_json(state_path(run_dir), state)


def _settings_for_run(base: Settings, run_dir: Path, database: str) -> Settings:
    return base.model_copy(
        update={
            "mongodb_database": database,
            "storage_dir": str((run_dir / "object_store").resolve()),
            "cos_secret_id": "",
            "cos_secret_key": "",
            "cos_bucket": "",
            "cos_region": "",
            "llm_timeout_seconds": max(int(base.llm_timeout_seconds), 240),
            "component_generation_enabled": False,
        }
    )


def _seed_asset(db: Any, settings: Settings, store: Any, source: Path, *, kind: str) -> str:
    digest = sha256_file(source)
    existing = db.assets.find_one({"sha256": digest, "kind": kind})
    if existing:
        return str(existing["_id"])
    prepared = storage.prepare_upload(settings, store, source.read_bytes(), source.name, kind)
    db.assets.insert_one(
        {
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
    )
    return prepared.id


def _thumbnail(source: Path, target: Path, max_side: int = 720) -> None:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        image.save(tmp, format="JPEG", quality=84, optimize=True)
        os.replace(tmp, target)


def prepare_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    if state_path(run_dir).exists() and not args.force:
        raise LoopError(f"运行目录已存在，使用 --force 才会重建：{run_dir}")
    if run_dir.exists() and args.force:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    baseline = Path(args.baseline).resolve()
    assets = [Path(value).resolve() for value in args.asset]
    if not baseline.is_file():
        raise LoopError(f"基准图不存在：{baseline}")
    if len(assets) != 2 or any(not path.is_file() for path in assets):
        raise LoopError("必须提供两张有效素材图")

    copied_baseline = run_dir / "baseline.png"
    shutil.copy2(baseline, copied_baseline)
    _thumbnail(copied_baseline, run_dir / "baseline_thumb.jpg")
    copied_assets: list[Path] = []
    for index, source in enumerate(assets, start=1):
        target = inputs_dir / f"material-{index}{source.suffix.lower()}"
        shutil.copy2(source, target)
        copied_assets.append(target)

    run_id = args.run_id or datetime.now().strftime("prompt-opt-%Y%m%d-%H%M%S")
    database = args.database or f"social_media_prompt_opt_{run_id.replace('-', '_')}"
    settings = _settings_for_run(get_settings(), run_dir, database)
    storage.ensure_storage_dirs(settings)
    store = LocalObjectStore(settings.storage_path / "objects")
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, database)
    try:
        brand_result = brand_style.import_brand_skill(
            db, store, settings, DEFAULT_BRAND_SKILL, force=True
        )
        product_ids = [
            _seed_asset(db, settings, store, path, kind="product") for path in copied_assets
        ]
    finally:
        mongo.close()

    state = {
        "run_id": run_id,
        "created_at": utcnow(),
        "database": database,
        "baseline": "baseline.png",
        "seed_prompt": args.seed,
        "optimizer_strategy_version": 1,
        "brand_id": brand_result["brand_id"],
        "brand_mode": "preset",
        "platform_id": "xiaohongshu",
        "placement_id": "note",
        "merchant_info": None,
        "product_asset_ids": product_ids,
        "material_manifest": [
            {
                "path": f"inputs/material-{index}{path.suffix.lower()}",
                "sha256": sha256_file(path),
                "original_name": source.name,
            }
            for index, (path, source) in enumerate(zip(copied_assets, assets), start=1)
        ],
        "baseline_sha256": sha256_file(copied_baseline),
        "workflows": {
            workflow: {
                "target": TARGETS[workflow],
                "next_round": 1,
                "best_round": None,
                "best_score": None,
                "best_prompt": None,
                "best_hard_fail": None,
                "complete": False,
            }
            for workflow in TARGETS
        },
        "overall_winner": None,
    }
    save_state(run_dir, state)
    render_report(run_dir, state)
    return state

def _counting_client(settings: Settings) -> CountingCoreAI:
    return CountingCoreAI(CoreAIClient(settings))


def _request_state(db: Any, settings: Settings, store: Any, client: Any) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(db=db, settings=settings, store=store, core_ai=client)
        )
    )


def _store_asset_bytes(db: Any, store: Any, asset_id: str) -> bytes:
    asset = db.assets.find_one({"_id": asset_id})
    if asset is None:
        raise LoopError(f"找不到输出素材：{asset_id}")
    key = asset.get("prepared_path") or asset.get("path")
    if not key:
        raise LoopError(f"输出素材缺少对象路径：{asset_id}")
    return store.get_bytes(str(key))


def _guidance_from_best(run_dir: Path, state: dict[str, Any], workflow: str) -> str | None:
    info = state["workflows"][workflow]
    best_round = info.get("best_round")
    if not best_round:
        return None
    score_path = run_dir / "rounds" / workflow / f"round-{best_round:03d}" / "score.json"
    if not score_path.is_file():
        return None
    score = read_json(score_path)
    issues = score.get("issues") or []
    fixes = score.get("priority_fixes") or []
    strengths = score.get("strengths") or []
    parts = []
    if strengths:
        parts.append("保留这些优点：" + "；".join(str(item) for item in strengths[:5]))
    if issues:
        parts.append("修复这些问题：" + "；".join(str(item) for item in issues[:8]))
    if fixes:
        parts.append("优先执行：" + "；".join(str(item) for item in fixes[:8]))
    return "\n".join(parts) or None


def generate_round(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    workflow = args.workflow
    if workflow not in TARGETS:
        raise LoopError("workflow 必须是 layered 或 whole_image")
    info = state["workflows"][workflow]
    if info["complete"]:
        return {"complete": True, "workflow": workflow}
    round_number = int(info["next_round"])
    round_dir = run_dir / "rounds" / workflow / f"round-{round_number:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)

    settings = _settings_for_run(get_settings(), run_dir, state["database"])
    store = LocalObjectStore(settings.storage_path / "objects")
    started = time.perf_counter()
    client = None if args.dry_run else _counting_client(settings)
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, state["database"])
    try:
        if args.dry_run:
            prompt = f"[DRY RUN {workflow} round {round_number}] {state['seed_prompt']}"
            image_path = round_dir / "generated.png"
            shutil.copy2(run_dir / "baseline.png", image_path)
            model_calls = {"image": 0, "chat": 0}
            task_id = f"dry_{workflow}_{round_number}"
            simulated = True
        else:
            base_prompt = str(info.get("best_prompt") or state["seed_prompt"])
            guidance_file = Path(args.guidance_file).resolve() if args.guidance_file else None
            guidance = guidance_file.read_text(encoding="utf-8") if guidance_file else None
            guidance = guidance or _guidance_from_best(run_dir, state, workflow)
            request = PromptOptimizeRequest(
                workflow=workflow,
                prompt=base_prompt,
                brand_mode=state["brand_mode"],
                brand_id=state["brand_id"],
                product_asset_ids=state["product_asset_ids"],
                platform_id=state["platform_id"],
                placement_id=state["placement_id"],
                merchant_info=state["merchant_info"],
            )
            assert client is not None
            prompt = prompt_optimizer.optimize_prompt(
                db, settings, store, client, request=request, revision_guidance=guidance
            )
            request_state = _request_state(db, settings, store, client)
            if workflow == "whole_image":
                created = whole_routes.create_task(
                    WholeImageTaskCreate(
                        prompt=prompt,
                        ratio="3:4",
                        product_asset_ids=state["product_asset_ids"],
                        brand_id=state["brand_id"],
                        brand_mode="preset",
                        candidate_count=1,
                        platform_id=state["platform_id"],
                        placement_id=state["placement_id"],
                    ),
                    request_state,
                    BackgroundTasks(),
                )
                task_id = str(created["id"])
                whole_service.run_generation(db, settings, store, client, task_id)
                task = db.whole_tasks.find_one({"_id": task_id})
                if not task or task.get("status") != "succeeded":
                    raise LoopError(
                        "整图生成失败：" + str((task or {}).get("error") or "任务未成功完成")
                    )
                version = db.whole_versions.find_one({"task_id": task_id, "version": 1})
                output_id = str((version or {}).get("output_asset_id") or "")
            else:
                created = task_routes.create_task(
                    TaskCreate(
                        prompt=prompt,
                        product_asset_ids=state["product_asset_ids"],
                        brand_id=state["brand_id"],
                        brand_mode="preset",
                        candidate_count=1,
                        generation_mode="reference_guided",
                        platform_id=state["platform_id"],
                        placement_id=state["placement_id"],
                    ),
                    request_state,
                    BackgroundTasks(),
                )
                task_id = str(created["id"])
                pipeline.run_task(db, settings, store, client, task_id)
                task = db.tasks.find_one({"_id": task_id})
                if not task or task.get("status") != "succeeded":
                    raise LoopError(
                        "分层海报生成失败：" + str((task or {}).get("error") or "任务未成功完成")
                    )
                output_id = str(task.get("output_asset_id") or "")
            if not output_id:
                raise LoopError("生成任务没有输出图片")
            data = _store_asset_bytes(db, store, output_id)
            image_path = round_dir / "generated.png"
            image_path.write_bytes(data)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            model_calls = {"image": client.image_calls, "chat": client.chat_calls}
            simulated = False

        atomic_write(round_dir / "prompt.txt", prompt + "\n")
        _thumbnail(image_path, round_dir / "generated_thumb.jpg")
        result = {
            "workflow": workflow,
            "round": round_number,
            "task_id": task_id,
            "prompt": prompt,
            "image": "generated.png",
            "thumbnail": "generated_thumb.jpg",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "model_calls": model_calls,
            "simulated": simulated,
            "optimizer_strategy_version": int(state.get("optimizer_strategy_version", 1)),
            "created_at": utcnow(),
        }
        write_json(round_dir / "result.json", result)
        render_report(run_dir, state)
        return result
    except Exception as exc:
        append_jsonl(
            run_dir / "attempts.jsonl",
            {
                "workflow": workflow,
                "round": round_number,
                "status": "failed",
                "error": str(exc),
                "created_at": utcnow(),
            },
        )
        raise
    finally:
        if client is not None and hasattr(client, "client"):
            client.client.close()
        mongo.close()

def _validate_score(score: dict[str, Any], workflow: str, round_number: int) -> dict[str, Any]:
    if score.get("workflow") != workflow or int(score.get("round") or 0) != round_number:
        raise LoopError("评分文件的 workflow/round 与当前轮次不一致")
    dimensions = score.get("dimensions")
    if not isinstance(dimensions, list):
        raise LoopError("评分必须包含 dimensions 数组")
    normalized: list[dict[str, Any]] = []
    by_key = {str(item.get("key")): item for item in dimensions if isinstance(item, dict)}
    for key, label, maximum in DIMENSIONS:
        item = by_key.get(key)
        if not isinstance(item, dict):
            raise LoopError(f"评分缺少维度：{label}")
        value = item.get("score")
        reason = str(item.get("reason") or "").strip()
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= maximum:
            raise LoopError(f"{label} 分数无效")
        if len(reason) < 4:
            raise LoopError(f"{label} 必须提供具体评分原因")
        normalized.append(
            {
                "key": key,
                "label": label,
                "score": round(float(value), 2),
                "max": maximum,
                "reason": reason,
            }
        )
    total = score.get("total")
    if not isinstance(total, (int, float)) or not 0 <= float(total) <= 100:
        raise LoopError("总分必须是 0–100")
    reason_fields: dict[str, list[str]] = {}
    for field, label in (
        ("strengths", "高分原因"),
        ("issues", "低分原因"),
        ("priority_fixes", "优先修改"),
    ):
        values = score.get(field)
        if not isinstance(values, list) or not any(str(item).strip() for item in values):
            raise LoopError(f"评分必须输出{label}")
        reason_fields[field] = [str(item).strip() for item in values if str(item).strip()]
    return {
        "workflow": workflow,
        "round": round_number,
        "total": round(float(total), 2),
        "hard_fail": bool(score.get("hard_fail")),
        "hard_fail_items": [str(item) for item in score.get("hard_fail_items") or []],
        "dimensions": normalized,
        "strengths": reason_fields["strengths"],
        "issues": reason_fields["issues"],
        "priority_fixes": reason_fields["priority_fixes"],
        "verdict": str(score.get("verdict") or ("不通过" if score.get("hard_fail") else "通过")),
        "scored_at": utcnow(),
    }


def score_round(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    workflow = args.workflow
    info = state["workflows"][workflow]
    if info["complete"]:
        raise LoopError("该工作流已经完成")
    round_number = int(info["next_round"])
    round_dir = run_dir / "rounds" / workflow / f"round-{round_number:03d}"
    if not (round_dir / "result.json").is_file():
        raise LoopError("当前轮次尚未生成结果")
    score = _validate_score(read_json(Path(args.score_file).resolve()), workflow, round_number)
    write_json(round_dir / "score.json", score)
    append_jsonl(run_dir / "results.jsonl", score)

    best_score = info.get("best_score")
    improves = best_score is None or score["total"] > float(best_score)
    if improves:
        info["best_round"] = round_number
        info["best_score"] = score["total"]
        info["best_prompt"] = (round_dir / "prompt.txt").read_text(encoding="utf-8").strip()
        info["best_hard_fail"] = score["hard_fail"]
    info["last_round"] = round_number
    info["next_round"] = round_number + 1
    info["complete"] = info["next_round"] > int(info["target"])
    save_state(run_dir, state)
    render_report(run_dir, state)
    return score


def finalize_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    workflow, raw_round = args.winner.split(":", 1)
    if workflow not in TARGETS:
        raise LoopError("winner 必须是 workflow:round")
    round_number = int(raw_round)
    round_dir = run_dir / "rounds" / workflow / f"round-{round_number:03d}"
    if not (round_dir / "score.json").is_file():
        raise LoopError("冠军轮次缺少评分")
    score = read_json(round_dir / "score.json")
    state["overall_winner"] = {
        "workflow": workflow,
        "round": round_number,
        "score": score,
        "finalized_at": utcnow(),
    }
    save_state(run_dir, state)
    _copy_best_files(run_dir, state)
    render_report(run_dir, state)
    return state["overall_winner"]


def _copy_best_files(run_dir: Path, state: dict[str, Any]) -> None:
    for workflow, filename in (
        ("layered", "best-poster.png"),
        ("whole_image", "best-whole-image.png"),
    ):
        info = state["workflows"][workflow]
        if not info.get("best_round"):
            continue
        source = run_dir / "rounds" / workflow / f"round-{int(info['best_round']):03d}" / "generated.png"
        if source.is_file():
            shutil.copy2(source, run_dir / filename)
    winner = state.get("overall_winner")
    if winner:
        source = run_dir / "rounds" / winner["workflow"] / f"round-{int(winner['round']):03d}" / "generated.png"
        shutil.copy2(source, run_dir / "winner.png")
        prompt_path = source.with_name("prompt.txt")
        shutil.copy2(prompt_path, run_dir / "winner-prompt.txt")


def _candidate_cards(run_dir: Path, state: dict[str, Any], workflow: str, allowed_rounds: set[int] | None = None) -> str:
    del state
    cards: list[str] = []
    rounds_root = run_dir / "rounds" / workflow
    if not rounds_root.exists():
        return '<p class="empty">还没有已评分结果。</p>'
    for round_dir in sorted(rounds_root.glob("round-*")):
        result_path = round_dir / "result.json"
        score_path = round_dir / "score.json"
        if not result_path.is_file() or not score_path.is_file():
            continue
        result = read_json(result_path)
        if allowed_rounds is not None and int(result.get("round") or 0) not in allowed_rounds:
            continue
        score = read_json(score_path)
        dimensions = score.get("dimensions")
        if not isinstance(dimensions, list) or not all(
            isinstance(item, dict) and {"label", "score", "max", "reason"} <= set(item)
            for item in dimensions
        ):
            continue
        label_by_key = {key: label for key, label, _ in DIMENSIONS}
        dims = "".join(
            f'<div class="dim"><span>{html.escape(item.get("label") or label_by_key.get(item["key"], item["key"]))}</span>'
            f'<div class="bar"><i style="width:{float(item["score"]) / float(item["max"]) * 100:.1f}%"></i></div>'
            f'<b>{item["score"]}/{item["max"]}</b><p>{html.escape(str(item.get("reason") or ""))}</p></div>'
            for item in score["dimensions"]
        )
        lists = "".join(
            f"<section><h4>{label}</h4><ul>{''.join(f'<li>{html.escape(str(item))}</li>' for item in score[field])}</ul></section>"
            for field, label in (
                ("strengths", "高分原因"),
                ("issues", "低分原因"),
                ("priority_fixes", "优先修改"),
            )
        )
        hard = "硬失败" if score["hard_fail"] else "通过"
        cards.append(
            f'''<article class="candidate{' hard' if score['hard_fail'] else ''}" data-score="{score['total']}" data-round="{result['round']}" data-hard="{str(score['hard_fail']).lower()}">
  <header><div><span class="round">第 {result['round']} 轮</span><span class="badge">{hard}</span></div><strong class="total">{score['total']:.1f}<small>/100</small></strong></header>
  <div class="compare"><figure><img src="baseline_thumb.jpg" alt="基准图"><figcaption>基准图</figcaption></figure><figure><a href="rounds/{workflow}/round-{result['round']:03d}/generated.png"><img src="rounds/{workflow}/round-{result['round']:03d}/generated_thumb.jpg" alt="第 {result['round']} 轮成图"></a><figcaption>第 {result['round']} 轮</figcaption></figure></div>
  <div class="meta">耗时 {result['elapsed_seconds']}s · 生图调用 {result['model_calls']['image']} · 文本调用 {result['model_calls']['chat']} · {html.escape(score['verdict'])}</div>
  <div class="dims">{dims}</div>
  {lists}
  <details><summary>本轮完整提示词</summary><pre>{html.escape(result['prompt'])}</pre></details>
</article>'''
        )
    return "".join(cards) or '<p class="empty">还没有已评分结果。</p>'

def _ranked_rounds(run_dir: Path, workflow: str, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rounds_root = run_dir / "rounds" / workflow
    if not rounds_root.exists():
        return []
    for round_dir in rounds_root.glob("round-*"):
        result_path = round_dir / "result.json"
        score_path = round_dir / "score.json"
        if not result_path.is_file() or not score_path.is_file():
            continue
        result = read_json(result_path)
        score = read_json(score_path)
        dimensions = score.get("dimensions")
        if not isinstance(dimensions, list) or not all(
            isinstance(item, dict) and {"label", "score", "max", "reason"} <= set(item)
            for item in dimensions
        ):
            continue
        by_key = {item["key"]: float(item["score"]) for item in dimensions}
        candidates.append(
            {
                "round": int(result["round"]),
                "total": float(score["total"]),
                "hard_fail": bool(score.get("hard_fail")),
                "information": by_key.get("information_accuracy", 0),
                "composition": by_key.get("composition_hierarchy", 0),
                "result": result,
                "score": score,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["hard_fail"],
            -item["total"],
            -item["information"],
            -item["composition"],
            item["round"],
        )
    )
    return candidates[: max(0, limit)]


def curate_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    curated_root = run_dir / "curated"
    if curated_root.exists():
        shutil.rmtree(curated_root)
    limits = {"layered": int(args.poster_limit), "whole_image": int(args.whole_limit)}
    state["curation"] = {"created_at": utcnow(), "limits": dict(limits), "items": {}}
    for workflow, limit in limits.items():
        ranked = _ranked_rounds(run_dir, workflow, limit)
        workflow_dir = curated_root / workflow
        workflow_dir.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for rank, item in enumerate(ranked, start=1):
            round_number = int(item["round"])
            round_dir = run_dir / "rounds" / workflow / f"round-{round_number:03d}"
            target_dir = workflow_dir / f"{rank:02d}-round-{round_number:03d}"
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in ("generated.png", "generated_thumb.jpg", "prompt.txt", "result.json", "score.json"):
                source = round_dir / name
                if source.is_file():
                    shutil.copy2(source, target_dir / name)
            entries.append(
                {
                    "rank": rank,
                    "round": round_number,
                    "total": item["total"],
                    "hard_fail": item["hard_fail"],
                    "directory": str(target_dir.relative_to(run_dir)).replace("\\", "/"),
                }
            )
        state["curation"]["items"][workflow] = entries
    save_state(run_dir, state)
    report = render_report(run_dir, state)
    return {"report": str(report), "curation": state["curation"]}

def render_report(run_dir: Path, state: dict[str, Any]) -> Path:
    winners = []
    for workflow in TARGETS:
        info = state["workflows"][workflow]
        winners.append(
            f'<div class="winner"><span>{WORKFLOW_LABELS[workflow]}</span>'
            f'<strong>{info.get("best_score") if info.get("best_score") is not None else "—"}</strong>'
            f'<small>第 {info.get("best_round") or "—"} 轮</small></div>'
        )
    overall = state.get("overall_winner")
    if overall:
        overall_html = (
            f'<div class="champion"><span>最终冠军</span><strong>{overall["score"]["total"]:.1f}</strong>'
            f'<p>{WORKFLOW_LABELS[overall["workflow"]]} · 第 {overall["round"]} 轮</p>'
            f'<img src="winner.png" alt="最终冠军"><br><a href="winner-prompt.txt">查看冠军提示词</a></div>'
        )
    else:
        overall_html = '<div class="champion pending"><span>最终冠军</span><strong>待 meta-judge 复核</strong></div>'
    meta_html = ""
    meta_path = run_dir / "meta-judge.json"
    if meta_path.is_file():
        meta = read_json(meta_path)
        finalist_rows = "".join(
            f'<li><b>{html.escape(str(item.get("candidate") or ""))}</b> · {item.get("total", "—")} 分<p>{html.escape(str(item.get("reason") or ""))}</p></li>'
            for item in meta.get("scores") or []
        )
        meta_html = f'''<section class="meta-review"><span class="eyebrow">FINAL META-JUDGE</span><h2>最终复核与选择理由</h2><p>{html.escape(str(meta.get("decision_reason") or ""))}</p><ol>{finalist_rows}</ol></section>'''

    sections = []
    for workflow in TARGETS:
        info = state["workflows"][workflow]
        completed = info["target"] if info["next_round"] > info["target"] else info["next_round"] - 1
        sections.append(
            f'''<section class="workflow"><div class="section-head"><div><span class="eyebrow">WORKFLOW</span><h2>{WORKFLOW_LABELS[workflow]}</h2></div><div class="progress"><strong>{completed}</strong> / {info["target"]} 个有效结果</div></div>
<div class="controls"><button type="button" data-sort="round">按轮次</button><button type="button" data-sort="score">按分数</button><label><input type="checkbox" data-hard-filter> 只看硬失败</label></div>
<div class="cards">{_candidate_cards(run_dir, state, workflow)}</div></section>'''
        )
    curated_sections = []
    curation = state.get("curation")
    if isinstance(curation, dict):
        for workflow in TARGETS:
            entries = (curation.get("items") or {}).get(workflow) or []
            allowed = {int(item["round"]) for item in entries if item.get("round") is not None}
            available = _ranked_rounds(run_dir, workflow, 10000)
            if len(entries) >= len(available):
                continue
            curated_sections.append(
                f'''<section class="workflow curated"><div class="section-head"><div><span class="eyebrow">CURATED FINAL SET</span><h2>精选 {len(entries)} 张 · {WORKFLOW_LABELS[workflow]}</h2></div><div class="progress">按分数与硬性要求排序</div></div><div class="cards">{_candidate_cards(run_dir, state, workflow, allowed)}</div></section>'''
            )
    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>提示词优化基准报告 · {html.escape(state['run_id'])}</title>
<style>
:root{{--ink:#17201d;--muted:#68726d;--paper:#f5f1e8;--card:#fffdf8;--green:#173f2b;--red:#b62d24;--gold:#d9a62e;--line:#ded8cb}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",sans-serif}}header.hero{{padding:48px 6vw 30px;background:linear-gradient(135deg,#102f22,#1f5636 62%,#8d2a22);color:#fff}}header.hero h1{{font-size:clamp(30px,5vw,64px);margin:8px 0;letter-spacing:-.04em}}header.hero p{{max-width:900px;color:#eee8da}}.summary{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;padding:24px 6vw;max-width:1700px;margin:auto}}.winner,.champion{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px}}.winner span,.champion span{{display:block;color:var(--muted);font-size:12px}}.winner strong,.champion strong{{font-size:34px;color:var(--green)}}.winner small{{display:block;color:var(--muted)}}.champion img{{width:100%;max-height:420px;object-fit:contain;background:#eee;border-radius:12px;margin-top:12px}}.pending strong{{font-size:20px}}.workflow{{max-width:1700px;margin:30px auto;padding:0 6vw}}.section-head{{display:flex;justify-content:space-between;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:12px}}.section-head h2{{margin:2px 0;font-size:34px}}.eyebrow{{font-size:11px;letter-spacing:.16em;color:var(--red)}}.progress{{font-size:13px;color:var(--muted)}}.progress strong{{font-size:28px;color:var(--ink)}}.controls{{display:flex;gap:8px;align-items:center;padding:14px 0}}.controls button{{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 13px;cursor:pointer}}.controls label{{font-size:12px;margin-left:auto}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px}}.candidate{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:0 9px 30px rgba(30,40,35,.06)}}.candidate.hard{{border-color:#dba7a1}}.candidate>header{{display:flex;justify-content:space-between;align-items:center}}.round{{font-weight:700;margin-right:7px}}.badge{{font-size:10px;padding:4px 8px;border-radius:999px;background:#e5f0e8;color:var(--green)}}.hard .badge{{background:#f6dedb;color:var(--red)}}.total{{font-size:30px;color:var(--green)}}.total small{{font-size:11px;color:var(--muted)}}.compare{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}figure{{margin:0}}figure img{{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:12px;background:#eee}}figcaption{{font-size:11px;color:var(--muted);text-align:center;margin-top:5px}}.meta{{font-size:11px;color:var(--muted)}}.dims{{margin:14px 0}}.dim{{display:grid;grid-template-columns:150px 1fr 55px;gap:8px;align-items:center;margin:9px 0;font-size:11px}}.dim p{{grid-column:1/-1;margin:-2px 0 5px;color:var(--muted);line-height:1.45}}.bar{{height:7px;background:#e8e3d8;border-radius:99px;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--gold)}}details section h4{{margin:12px 0 4px;font-size:12px}}details ul,.candidate section ul{{padding-left:18px;font-size:12px;line-height:1.5}}summary{{cursor:pointer;color:var(--red);font-size:12px;font-weight:700}}pre{{white-space:pre-wrap;background:#f1eee5;padding:12px;border-radius:10px;font:11px/1.55 Consolas,monospace}}.meta-review{{max-width:1700px;margin:24px auto;padding:22px 6vw;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}.meta-review h2{{margin:4px 0 8px}}.meta-review li{{margin:12px 0;line-height:1.5}}.meta-review li p{{margin:4px 0;color:var(--muted)}}.empty{{color:var(--muted)}}footer{{padding:40px 6vw;color:var(--muted);font-size:11px}}@media(max-width:700px){{.summary{{grid-template-columns:1fr}}.cards{{grid-template-columns:1fr}}.dim{{grid-template-columns:110px 1fr 48px}}.compare{{grid-template-columns:1fr}}}}
</style></head><body>
<header class="hero"><span class="eyebrow">PROMPT OPTIMIZATION LAB</span><h1>提示词优化基准报告</h1><p>种子：{html.escape(state['seed_prompt'])} · 小红书 3:4 · SeeJoy 预设风格 · 独立评审。每轮只保留一张最终成图，失败不计入有效轮次。</p></header>
<main><section class="summary">{''.join(winners)}{overall_html}</section>{meta_html}{''.join(curated_sections)}{''.join(sections)}</main>
<footer>生成时间 {html.escape(utcnow())} · 基准 SHA256 {html.escape(state['baseline_sha256'])}</footer>
<script>
document.querySelectorAll('.workflow').forEach((section)=>{{const cards=[...section.querySelectorAll('.candidate')];const sortRound=()=>cards.sort((a,b)=>+a.dataset.round-+b.dataset.round).forEach(c=>section.querySelector('.cards').append(c));const sortScore=()=>cards.sort((a,b)=>+b.dataset.score-+a.dataset.score).forEach(c=>section.querySelector('.cards').append(c));section.querySelector('[data-sort="round"]').onclick=sortRound;section.querySelector('[data-sort="score"]').onclick=sortScore;section.querySelector('[data-hard-filter]').onchange=(e)=>cards.forEach(c=>c.style.display=e.target.checked&&c.dataset.hard!=='true'?'none':'');}});
</script></body></html>'''
    target = run_dir / "index.html"
    atomic_write(target, doc)
    _copy_best_files(run_dir, state)
    return target

def status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    return {
        "run_id": state["run_id"],
        "report": str(run_dir / "index.html"),
        "workflows": {
            key: {
                "next_round": value["next_round"],
                "target": value["target"],
                "complete": value["complete"],
                "best_round": value["best_round"],
                "best_score": value["best_score"],
            }
            for key, value in state["workflows"].items()
        },
        "overall_winner": state.get("overall_winner"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prompt optimizer benchmark loop")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--run-dir", required=True)
    prepare.add_argument("--run-id")
    prepare.add_argument("--database")
    prepare.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    prepare.add_argument(
        "--asset", action="append", default=[str(path) for path in DEFAULT_ASSETS]
    )
    prepare.add_argument("--seed", default=DEFAULT_SEED)
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=prepare_run)

    generate = sub.add_parser("optimize-and-generate")
    generate.add_argument("--run-dir", required=True)
    generate.add_argument("--workflow", required=True, choices=sorted(TARGETS))
    generate.add_argument("--guidance-file")
    generate.add_argument("--dry-run", action="store_true")
    generate.set_defaults(func=generate_round)

    score = sub.add_parser("score")
    score.add_argument("--run-dir", required=True)
    score.add_argument("--workflow", required=True, choices=sorted(TARGETS))
    score.add_argument("--score-file", required=True)
    score.set_defaults(func=score_round)

    report = sub.add_parser("report")
    report.add_argument("--run-dir", required=True)

    def report_command(args: argparse.Namespace) -> dict[str, Any]:
        run_dir = Path(args.run_dir).resolve()
        state = load_state(run_dir)
        return {"report": str(render_report(run_dir, state))}

    report.set_defaults(func=report_command)

    curate = sub.add_parser("curate")
    curate.add_argument("--run-dir", required=True)
    curate.add_argument("--poster-limit", type=int, default=10)
    curate.add_argument("--whole-limit", type=int, default=15)
    curate.set_defaults(func=curate_run)

    resume = sub.add_parser("resume")
    resume.add_argument("--run-dir", required=True)
    resume.set_defaults(func=status)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--run-dir", required=True)
    finalize.add_argument("--winner", required=True, help="workflow:round")
    finalize.set_defaults(func=finalize_run)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
