from __future__ import annotations

import argparse
import json
from pathlib import Path

import mongomock
from PIL import Image

from scripts import prompt_optimization_loop as loop


def _png(path: Path, color: tuple[int, int, int], size=(320, 426)) -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _score(workflow: str, round_number: int, total: float = 81) -> dict:
    return {
        "workflow": workflow,
        "round": round_number,
        "total": total,
        "hard_fail": False,
        "hard_fail_items": [],
        "dimensions": [
            {
                "key": key,
                "score": max_value * 0.8,
                "reason": f"{label}的高分原因是层级清楚，低分原因是细节仍需加强。",
            }
            for key, label, max_value in loop.DIMENSIONS
        ],
        "strengths": ["整体构图接近基准，品牌色使用正确。"],
        "issues": ["主标题体量仍偏小，部分小字不够清晰。"],
        "priority_fixes": ["放大毛笔主标题并减少底部信息拥挤。"],
        "verdict": "通过",
    }


def test_dry_run_records_reasons_and_completes(tmp_path, monkeypatch, settings):
    run_dir = tmp_path / "run"
    baseline = _png(tmp_path / "baseline.png", (120, 50, 30), (600, 800))
    asset1 = _png(tmp_path / "person.png", (80, 120, 60))
    asset2 = _png(tmp_path / "invite.png", (220, 210, 170))
    monkeypatch.setattr(loop, "get_settings", lambda: settings)
    monkeypatch.setattr(loop, "create_client", lambda uri: mongomock.MongoClient())

    prepared = loop.prepare_run(
        argparse.Namespace(
            run_dir=str(run_dir),
            run_id="dry-run",
            database="prompt_opt_test",
            baseline=str(baseline),
            asset=[str(asset1), str(asset2)],
            seed=loop.DEFAULT_SEED,
            force=False,
        )
    )
    assert len(prepared["product_asset_ids"]) == 2
    for workflow in loop.TARGETS:
        prepared["workflows"][workflow]["target"] = 2
    loop.save_state(run_dir, prepared)

    for workflow in loop.TARGETS:
        for round_number in (1, 2):
            loop.generate_round(
                argparse.Namespace(
                    run_dir=str(run_dir),
                    workflow=workflow,
                    guidance_file=None,
                    dry_run=True,
                )
            )
            score_path = tmp_path / f"{workflow}-{round_number}.json"
            score_path.write_text(
                json.dumps(_score(workflow, round_number, 81 + round_number), ensure_ascii=False),
                encoding="utf-8",
            )
            loop.score_round(
                argparse.Namespace(
                    run_dir=str(run_dir),
                    workflow=workflow,
                    score_file=str(score_path),
                )
            )

    state = loop.load_state(run_dir)
    assert all(item["complete"] for item in state["workflows"].values())
    assert state["workflows"][loop.WORKFLOW]["best_round"] == 2
    report = (run_dir / "index.html").read_text(encoding="utf-8")
    assert "高分原因" in report
    assert "低分原因" in report
    assert "主标题体量仍偏小" in report
    assert "按分数" in report
    curated = loop.curate_run(
        argparse.Namespace(run_dir=str(run_dir), whole_limit=1)
    )
    assert len(curated["curation"]["items"][loop.WORKFLOW]) == 1
    assert (run_dir / "curated" / loop.WORKFLOW).is_dir()
    assert "CURATED FINAL SET" in (run_dir / "index.html").read_text(encoding="utf-8")


def test_score_validation_requires_each_dimension_reason():
    invalid = _score(loop.WORKFLOW, 1)
    invalid["dimensions"][0]["reason"] = ""

    try:
        loop._validate_score(invalid, loop.WORKFLOW, 1)
    except loop.LoopError as exc:
        assert "必须提供具体评分原因" in str(exc)
    else:
        raise AssertionError("expected validation error")
