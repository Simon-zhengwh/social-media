from __future__ import annotations

import io

import mongomock
from PIL import Image

from app.services import agent_image
from app.services.core_ai import AgentArtifact, AgentRunResult


def _result(output: str, artifacts: list[AgentArtifact]) -> AgentRunResult:
    return AgentRunResult(
        run_id="run-1", status="COMPLETED", output=output, artifacts=artifacts
    )


def test_text_edit_picks_the_labelled_final_poster():
    result = _result(
        "![预览](https://f.example.com/g1.png)\n"
        "![预览](https://f.example.com/g2.png)\n"
        "![成图](https://f.example.com/final.png)\n"
        "![贴纸](https://f.example.com/sticker.png)\n"
        "![区域](https://f.example.com/region.png)",
        [
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/g1.png", "Generated image"),
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/g2.png", "Generated image"),
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/final.png", "改字后海报（热闹和仪式感我们都有）"),
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/sticker.png", "新字透明贴纸（第一阶段素材）"),
            AgentArtifact("edit-region.png", "image/png", "https://f.example.com/region.png", "改动区域标注"),
            AgentArtifact("text-result-v1.zip", "application/zip", "https://f.example.com/result.zip", "完整结果包"),
        ],
    )

    assert agent_image.pick_final_image_urls(result) == ["https://f.example.com/final.png"]
    assert agent_image.pick_bundle_artifact(result).file_name == "text-result-v1.zip"
    assert agent_image.pick_html_artifact(result) is None


def test_background_edit_prefers_markdown_image_and_keeps_html():
    result = _result(
        "![预览](https://f.example.com/preview.png)",
        [
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/g1.png", "Generated image"),
            AgentArtifact("editor.html", "text/html", "https://f.example.com/editor.html", "背景替换编辑器"),
            AgentArtifact("preview.png", "image/png", "https://f.example.com/preview.png", "当前效果预览（1086x1448）"),
            AgentArtifact("bg-replace-v1.zip", "application/zip", "https://f.example.com/result.zip", "完整结果目录"),
        ],
    )

    assert agent_image.pick_final_image_urls(result) == ["https://f.example.com/preview.png"]
    assert agent_image.pick_html_artifact(result).file_name == "editor.html"
    assert agent_image.pick_bundle_artifact(result).file_name == "bg-replace-v1.zip"


def test_final_is_found_by_title_when_reply_only_links_generic_images():
    result = _result(
        "![生成](https://f.example.com/g1.png)",
        [
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/g1.png", "Generated image"),
            AgentArtifact("poster-new-background-1086x1448.png", "image/png", "https://f.example.com/f1.png", "炸物区夜场海报 -已换成图2背景 (1086x1448)"),
            AgentArtifact("poster-new-background-1536x2048.png", "image/png", "https://f.example.com/f2.png", "同款成图 -高分辨率 (1536x2048)"),
        ],
    )

    assert agent_image.pick_final_image_urls(result) == [
        "https://f.example.com/f1.png",
        "https://f.example.com/f2.png",
    ]


def test_transparent_layer_is_never_picked_as_final():
    layer = io.BytesIO()
    Image.new("RGBA", (40,40), (0,0,0,0)).save(layer, format="PNG")
    poster = io.BytesIO()
    Image.new("RGB", (40,40), (10,20,30)).save(poster, format="PNG")

    assert agent_image.has_transparency(layer.getvalue()) is True
    assert agent_image.has_transparency(poster.getvalue()) is False

    result = _result(
        "![透明](https://f.example.com/layer.png)\n![成图](https://f.example.com/final.png)",
        [
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/layer.png", "Generated image"),
            AgentArtifact("generated-image.png", "image/png", "https://f.example.com/final.png", "最终成图"),
        ],
    )
    assert (
        agent_image.pick_transparent_image_url(
            result, excluded=["https://f.example.com/final.png"]
        )
        == "https://f.example.com/layer.png"
    )


def test_save_agent_artifact_stores_html_with_extension(settings, store):
    db = mongomock.MongoClient().db
    artifact = AgentArtifact(
        "editor.html", "text/html; charset=utf-8", "https://f.example.com/editor.html", "编辑器"
    )

    saved = agent_image.save_agent_artifact(
        db, settings, store, task_id="wtask", version=2, artifact=artifact, data=b"<html></html>"
    )

    assert saved["kind"] == "html"
    assert saved["file_name"] == "editor.html"
    assert saved["content_type"] == "text/html"
    assert saved["size"] == len(b"<html></html>")
    doc = db.whole_artifacts.find_one({"_id": saved["id"]})
    assert doc["path"].endswith(".html")
    assert store.get_bytes(doc["path"]) == b"<html></html>"


def test_save_agent_artifact_keeps_zip_suffix(settings, store):
    db = mongomock.MongoClient().db
    artifact = AgentArtifact(
        "bundle", "application/zip", "https://f.example.com/bundle.zip", "结果包"
    )

    saved = agent_image.save_agent_artifact(
        db, settings, store, task_id="wtask", version=2, artifact=artifact, data=b"PK\x03\x04"
    )

    assert saved["kind"] == "bundle"
    assert saved["file_name"] == "bundle.zip"
    assert db.whole_artifacts.find_one({"_id": saved["id"]})["path"].endswith(".zip")
