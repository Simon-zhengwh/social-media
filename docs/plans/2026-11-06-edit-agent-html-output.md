#改图 Agent切换（image-editor）+ HTML/图片混合产物 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**把整图工作台「对话改图」分支的 Agent切换到 benchmark验证过的 `5a162c34-f333-453f-9835-de6a4f96e3fb`（image-editor），并把该分支的产物从「透明层 +最终图」扩展为「最终海报 PNG +透明层 + HTML编辑器 +结果 ZIP」，工作台内可内嵌预览 HTML。

**Architecture:**只改 Agent改图分支（首轮生成仍走 Core AI `/images/generations`，接口改图仍走 `/images/edits`），双路并排对比保持不变。Agent分支新增产物解析层（按最终回复的 Markdown图片链接 + artifact标题启发式挑选最终海报）、通用文件下载（HTML/ZIP）、对象存储 `artifacts/`前缀与 `whole_artifacts`集合，再由新路由以 sandbox CSP内联预览 HTML。前端在结果卡片与版本预览里增加 HTML弹层和下载入口。

**Tech Stack:** FastAPI + pymongo/mongomock + Pillow、React19 + TypeScript7 + Vite8、pytest、oxlint。

**Spec:**本文件（需求来源：Agent测试页2026-11-06的实测运行记录 `local_media/agent-tests/*/run-detail.json`；已确认：直接改 `CORE_AI_IMAGE_AGENT_ID`、只改改图分支、保留双路对比、工作台内嵌 iframe预览）。

## Global Constraints

-不改首轮生成链路（`run_generation`、`/images/generations`）。
-不改接口改图链路（`_gateway_edit_branch`、`/images/edits`、提示词内容）。
-不改原照替换（`original_replacement_runner`、`CORE_AI_BACKGROUND_REPLACE_AGENT_*`）。
-不改提示词优化 Agent（`CORE_AI_PROMPT_AGENT_ID`）。
-现有 `edit.branches.gateway` / `edit.branches.agent`结构、`outputs[].channel|role|selectable`语义、`requires_output_selection`流程保持不变。
-历史版本/历史任务（没有 `artifacts`字段）必须继续正常返回和渲染。
-所有新增中文文案使用简体中文。
-后端测试命令固定为（在 `backend/`下执行）：`.venv/Scripts/python.exe -m pytest <路径> -q`
-前端验证命令固定为（在 `frontend/`下执行）：`npm run build`、`npm run lint`。
-计划中代码块统一使用4空格缩进，与仓库现有 Python/TS/CSS风格一致，不要混入 Tab。

---

## File Structure

**Backend**

- Modify `backend/app/config.py` —改 Agent ID默认值、超时默认值。
- Modify `backend/app/services/storage.py` —允许 `artifacts/`子目录且保留扩展名。
- Modify `backend/app/services/core_ai.py` —新增 `download_agent_file()`（HTML/ZIP/JSON/MD）。
- Modify `backend/app/services/agent_image.py` —产物挑选启发式、通用产物落盘。
- Modify `backend/app/services/whole_image.py` — Agent分支改写：挑最终海报、保存透明层与 HTML/ZIP。
- Modify `backend/app/routes/whole_image.py` — `artifacts`序列化 +预览/下载路由。
- Modify `backend/.env.example`、`README.md`、`AGENTS.md` —配置与行为说明。
- Create `backend/tests/test_agent_image.py` —挑选启发式单测。
- Modify `backend/tests/test_core_ai.py` — `download_agent_file`单测。
- Modify `backend/tests/test_whole_image.py` — Agent分支/路由/产物测试。
- Modify `backend/tests/test_agent_tests.py` —默认值断言。

**Frontend**

- Modify `frontend/src/types.ts` — `WholeImageArtifact`类型。
- Create `frontend/src/components/WholeArtifactPreview.tsx` — HTML内嵌预览弹层。
- Modify `frontend/src/components/WholeImageResultPanel.tsx` —结果卡片产物入口、等待文案。
- Modify `frontend/src/components/WholeVersionPanel.tsx` —版本预览里的产物入口。
- Modify `frontend/src/styles.css` —预览弹层样式。

---

### Task 1:切换 Agent ID与超时

**Files:**
- Modify: `backend/app/config.py:23-24`
- Modify: `backend/.env.example:17-18`
- Modify: `README.md:138-139`
- Modify: `backend/tests/test_whole_image.py:1557-1558`
- Test: `backend/tests/test_agent_tests.py`（新增一条默认值断言）

**Interfaces:**
- Consumes:无。
- Produces: `Settings.core_ai_image_agent_id`默认 `5a162c34-f333-453f-9835-de6a4f96e3fb`；`Settings.core_ai_image_agent_timeout_seconds`默认 `1800`。后续任务统一引用这两个设置项，不再写死字符串。

- [ ] **Step1:写失败测试**

在 `backend/tests/test_agent_tests.py`文件末尾追加：

```python
def test_edit_agent_defaults_match_the_benchmark_agent():
    assert (
        Settings.model_fields["core_ai_image_agent_id"].default
        == "5a162c34-f333-453f-9835-de6a4f96e3fb"
    )
    assert (
        Settings.model_fields["core_ai_image_agent_timeout_seconds"].default == 1800
    )
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_tests.py::test_edit_agent_defaults_match_the_benchmark_agent -q`

Expected: FAIL（实际默认值仍是 `86c3a160-...` / `900`）。

- [ ] **Step3:修改配置默认值**

`backend/app/config.py`：

```python
    core_ai_image_agent_id: str = "5a162c34-f333-453f-9835-de6a4f96e3fb"
    core_ai_image_agent_timeout_seconds: int = 1800
```

`backend/.env.example`：

```
CORE_AI_IMAGE_AGENT_ID=5a162c34-f333-453f-9835-de6a4f96e3fb
CORE_AI_IMAGE_AGENT_TIMEOUT_SECONDS=1800
```

`README.md`环境变量段同样替换这两行（保持其余行不变）。

- [ ] **Step4:更新既有断言**

`backend/tests/test_whole_image.py:1557-1558`改为：

```python
    assert fake.agent_calls[-1]["agent_id"] == settings.core_ai_image_agent_id
    assert fake.agent_calls[-1]["timeout_seconds"] == 1800
```

- [ ] **Step5:运行相关测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_tests.py tests/test_whole_image.py -q`

Expected: PASS。

- [ ] **Step6:提交**

```bash
git add backend/app/config.py backend/.env.example README.md backend/tests/test_agent_tests.py backend/tests/test_whole_image.py
git commit -m "feat: switch edit agent to image-editor benchmark agent"
```

---

### Task 2:对象存储支持 `artifacts/`且保留扩展名

**Files:**
- Modify: `backend/app/services/storage.py:43,80-86`
- Test: `backend/tests/test_whole_image.py`（新增存储键单测）

**Interfaces:**
- Consumes:无。
- Produces: `storage.object_key(settings, "artifacts", "<name>.<ext>")`原样返回 `"<cos_prefix>/artifacts/<name>.<ext>"`；`"outputs"`等旧子目录行为不变。

- [ ] **Step1:写失败测试**

在 `backend/tests/test_whole_image.py`文件末尾追加：

```python
def test_artifact_object_keys_keep_their_extension(settings):
    assert (
        storage.object_key(settings, "artifacts", "wtask_v2_wart_abc.html")
        == "social-media/artifacts/wtask_v2_wart_abc.html"
    )
    assert (
        storage.object_key(settings, "artifacts", "wtask_v2_wart_abc.zip")
        == "social-media/artifacts/wtask_v2_wart_abc.zip"
    )
    assert (
        storage.object_key(settings, "outputs", "wtask_v2")
        == "social-media/outputs/wtask_v2.png"
    )
    with pytest.raises(storage.StorageError):
        storage.object_key(settings, "elsewhere", "x.png")
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py::test_artifact_object_keys_keep_their_extension -q`

Expected: FAIL（`StorageError:非法的对象目录`）。

- [ ] **Step3:实现**

`backend/app/services/storage.py`：

```python
STORAGE_SUBDIRS = ("originals", "prepared", "backgrounds", "outputs", "artifacts", "logs")
```

```python
def object_key(settings: Settings, subdir: str, name: str) -> str:
    if subdir not in {"originals", "prepared", "backgrounds", "outputs", "artifacts"}:
        raise StorageError("invalid_subdir", "非法的对象目录")
    if subdir not in {"originals", "artifacts"} and not name.endswith(".png"):
        name = f"{name}.png"
    prefix = settings.cos_prefix.strip("/") or "social-media"
    return f"{prefix}/{subdir}/{name}"
```

- [ ] **Step4:运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py::test_artifact_object_keys_keep_their_extension tests/test_object_store.py -q`

Expected: PASS。

- [ ] **Step5:提交**

```bash
git add backend/app/services/storage.py backend/tests/test_whole_image.py
git commit -m "feat: allow artifact objects with their own extension"
```

---

### Task 3: CoreAI通用产物下载

**Files:**
- Modify: `backend/app/services/core_ai.py`（在 `read_agent_html`之后新增方法）
- Test: `backend/tests/test_core_ai.py`（文件末尾追加）

**Interfaces:**
- Consumes: `CoreAIClient._validate_public_url`、`_download_client`、`settings.max_download_bytes`。
- Produces: `CoreAIClient.download_agent_file(url: str, *, content_type: str) -> bytes`。只接受 `text/html`、`application/zip`、`application/json`、`text/markdown`、`text/plain`；类型不支持、类型不一致、超限、空内容分别抛 `CoreAIError`（`invalid_artifact` / `artifact_too_large` / `empty_artifact` / `artifact_download_failed`）。

- [ ] **Step1:写失败测试**

在 `backend/tests/test_core_ai.py`末尾追加：

```python
def test_download_agent_file_reads_html_and_zip(settings, monkeypatch):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/editor.html":
            return httpx.Response(
                200,
                content=b"<!doctype html><h1>Editor</h1>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return httpx.Response(
            200,
            content=b"PK\x03\x04bundle",
            headers={"Content-Type": "application/zip"},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_validate_public_url", lambda url: None)
    try:
        html = client.download_agent_file(
            "https://files.example.com/editor.html", content_type="text/html"
        )
        bundle = client.download_agent_file(
            "https://files.example.com/result.zip", content_type="application/zip"
        )
    finally:
        client.close()

    assert html.startswith(b"<!doctype html>")
    assert bundle.startswith(b"PK")
    assert requested == ["/editor.html", "/result.zip"]


def test_download_agent_file_rejects_images_and_oversize(settings, monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=b"x" * 64,
            headers={"Content-Type": "image/png"},
        )

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_validate_public_url", lambda url: None)
    original_limit = settings.max_download_bytes
    try:
        with pytest.raises(CoreAIError) as unsupported:
            client.download_agent_file(
                "https://files.example.com/poster.png", content_type="image/png"
            )
        assert unsupported.value.code == "invalid_artifact"

        settings.max_download_bytes = 16
        with pytest.raises(CoreAIError) as too_large:
            client.download_agent_file(
                "https://files.example.com/editor.html", content_type="text/html"
            )
        assert too_large.value.code == "artifact_too_large"
    finally:
        settings.max_download_bytes = original_limit
        client.close()
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_core_ai.py::test_download_agent_file_reads_html_and_zip -q`

Expected: FAIL（`AttributeError: 'CoreAIClient' object has no attribute 'download_agent_file'`）。

- [ ] **Step3:实现**

在 `backend/app/services/core_ai.py`的 `read_agent_html`方法之后插入（保持类缩进）：

```python
    ARTIFACT_CONTENT_TYPES = (
        "text/html",
        "application/zip",
        "application/json",
        "text/markdown",
        "text/plain",
    )

    def download_agent_file(self, url: str, *, content_type: str) -> bytes:
        """Download a non-image Agent deliverable (HTML editor, result bundle)."""
        expected = content_type.split(";")[0].strip().lower()
        if expected not in self.ARTIFACT_CONTENT_TYPES:
            raise CoreAIError("invalid_artifact", "不支持的 Agent产物类型", 422)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self._validate_public_url(url)
                with self._download_client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise CoreAIError(
                            "artifact_download_failed",
                            f"Agent产物下载失败（HTTP {response.status_code}）",
                        )
                    self._validate_public_url(str(response.url))
                    actual = (
                        response.headers.get("Content-Type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if actual and actual != expected:
                        raise CoreAIError(
                            "invalid_artifact", "Agent产物类型与声明不一致", 422
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_download_bytes:
                            raise CoreAIError(
                                "artifact_too_large", "Agent产物超过体积限制", 413
                            )
                        chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    raise CoreAIError("empty_artifact", "Agent产物为空")
                return data
            except CoreAIError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 + attempt)
        raise CoreAIError(
            "artifact_download_failed", f"Agent产物下载失败：{last_error}"
        )
```

- [ ] **Step4:运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_core_ai.py -q`

Expected:全部 PASS。

- [ ] **Step5:提交**

```bash
git add backend/app/services/core_ai.py backend/tests/test_core_ai.py
git commit -m "feat: download agent html and zip artifacts"
```

---

### Task 4: Agent产物挑选启发式

**Files:**
- Modify: `backend/app/services/agent_image.py`（替换 `run_agent_images` / `classify_agent_images`，新增挑选与落盘函数）
- Create: `backend/tests/test_agent_image.py`

**Interfaces:**
- Consumes: `AgentArtifact`、`AgentRunResult`（`backend/app/services/core_ai.py`）。
- Produces:
 - `agent_image.pick_final_image_urls(result: AgentRunResult) -> list[str]`
 - `agent_image.pick_transparent_image_url(result: AgentRunResult, *, excluded: list[str]) -> str | None`
 - `agent_image.pick_html_artifact(result: AgentRunResult) -> AgentArtifact | None`
 - `agent_image.pick_bundle_artifact(result: AgentRunResult) -> AgentArtifact | None`
 - `agent_image.has_transparency(data: bytes) -> bool`
 - `agent_image.artifact_suffix(content_type: str) -> str`
 - `agent_image.save_agent_artifact(db, settings, store, *, task_id, version, artifact, data) -> dict[str, Any]`，返回 `{"id","kind","file_name","content_type","size","title"}`。

- [ ] **Step1:写失败测试**

新建 `backend/tests/test_agent_image.py`：

```python
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
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(layer, format="PNG")
    poster = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(poster, format="PNG")

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
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_image.py -q`

Expected: FAIL（`AttributeError` / `ImportError`）。

- [ ] **Step3:实现**

`backend/app/services/agent_image.py`顶部导入补充（保留原有 `hashlib`、`io`、`uuid`、`datetime`等）：

```python
import re
from pathlib import Path

from app.services.core_ai import (
    AgentArtifact,
    AgentRunResult,
    CoreAIClient,
    CoreAIError,
)
```

本任务**保留** `run_agent_images`、`classify_agent_images`、`_has_transparency`原样不动，只新增下面的公开函数；`whole_image._agent_edit_branch`仍调用旧函数，因此本任务结束时全仓库测试依然全绿。旧函数在 Task5替换调用点后再删除。`build_agent_edit_input`、`asset_public_url`、`save_png_asset`同样不动。新实现：

```python
AUXILIARY_IMAGE = re.compile(
    r"透明|前景|蒙版|遮罩|mask|cutout|抠图|对比|comparison|before[_ -]?after|"
    r"原图|original|模板|template|贴纸|sticker|改动区域|edit-region",
    re.I,
)
GENERIC_IMAGE_NAME = re.compile(r"^generated-image\.(png|jpe?g|webp)$", re.I)
FINAL_HINT = re.compile(
    r"最终|成图|成品|替换后|修改后|改字后|海报|poster|final|result|预览|preview",
    re.I,
)
TRANSPARENT_HINT = re.compile(r"透明|前景|foreground|layer|alpha", re.I)
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")

ARTIFACT_KINDS = {"text/html": "html", "application/zip": "bundle"}
ARTIFACT_SUFFIXES = {
    "text/html": ".html",
    "application/zip": ".zip",
    "application/json": ".json",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


def artifact_suffix(content_type: str) -> str:
    return ARTIFACT_SUFFIXES.get(content_type.split(";")[0].strip().lower(), ".bin")


def has_transparency(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if "A" not in image.getbands():
                return False
            alpha = image.convert("RGBA").getchannel("A")
            low, high = alpha.getextrema()
            return low < 250 and high > 0
    except Exception:
        return False


def _image_artifacts(result: AgentRunResult) -> list[AgentArtifact]:
    return [
        artifact
        for artifact in result.artifacts
        if artifact.content_type.split(";")[0].strip().lower().startswith("image/")
    ]


def _describe(artifact: AgentArtifact) -> str:
    return f"{artifact.title} {artifact.file_name}".strip()


def _is_auxiliary(artifact: AgentArtifact) -> bool:
    if AUXILIARY_IMAGE.search(_describe(artifact)):
        return True
    title = artifact.title.strip().lower()
    return bool(GENERIC_IMAGE_NAME.match(artifact.file_name.strip())) and title in {
        "",
        "generated image",
    }


def pick_final_image_urls(result: AgentRunResult) -> list[str]:
    images = _image_artifacts(result)
    by_url = {artifact.download_url: artifact for artifact in images}
    ordered: list[str] = []
    for url in MARKDOWN_IMAGE.findall(result.output or ""):
        artifact = by_url.get(url)
        if artifact is None or _is_auxiliary(artifact):
            continue
        if url not in ordered:
            ordered.append(url)
    if ordered:
        return ordered
    for artifact in images:
        if not _is_auxiliary(artifact) and FINAL_HINT.search(_describe(artifact)):
            if artifact.download_url not in ordered:
                ordered.append(artifact.download_url)
    if ordered:
        return ordered
    return [artifact.download_url for artifact in images]


def pick_transparent_image_url(
    result: AgentRunResult, *, excluded: list[str]
) -> str | None:
    for artifact in _image_artifacts(result):
        if artifact.download_url in excluded:
            continue
        if TRANSPARENT_HINT.search(_describe(artifact)):
            return artifact.download_url
    return None


def pick_html_artifact(result: AgentRunResult) -> AgentArtifact | None:
    for artifact in result.artifacts:
        if artifact.content_type.split(";")[0].strip().lower() == "text/html":
            return artifact
        if artifact.file_name.lower().endswith((".html", ".htm")):
            return artifact
    return None


def pick_bundle_artifact(result: AgentRunResult) -> AgentArtifact | None:
    for artifact in result.artifacts:
        if artifact.content_type.split(";")[0].strip().lower() == "application/zip":
            return artifact
    return None


def save_agent_artifact(
    db: Any,
    settings: Settings,
    store: ObjectStore,
    *,
    task_id: str,
    version: int,
    artifact: AgentArtifact,
    data: bytes,
) -> dict[str, Any]:
    content_type = (
        artifact.content_type.split(";")[0].strip().lower()
        or "application/octet-stream"
    )
    suffix = artifact_suffix(content_type)
    artifact_id = f"wart_{uuid.uuid4().hex}"
    relative = storage.object_key(
        settings, "artifacts", f"{task_id}_v{version}_{artifact_id}{suffix}"
    )
    store.put_bytes(relative, data, content_type)
    file_name = Path(artifact.file_name or f"agent-artifact{suffix}").name
    if not file_name.lower().endswith(suffix):
        file_name = f"{Path(file_name).stem or 'agent-artifact'}{suffix}"
    db.whole_artifacts.insert_one(
        {
            "_id": artifact_id,
            "task_id": task_id,
            "version": version,
            "kind": ARTIFACT_KINDS.get(content_type, "file"),
            "file_name": file_name,
            "content_type": content_type,
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "title": artifact.title,
            "source_url": artifact.download_url,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {
        "id": artifact_id,
        "kind": ARTIFACT_KINDS.get(content_type, "file"),
        "file_name": file_name,
        "content_type": content_type,
        "size": len(data),
        "title": artifact.title,
    }
```

注意：本任务不要删除 `AgentOutputImage` dataclass和 `DownloadedAgentImage`导入，它们仍被保留下来的旧函数使用。

- [ ] **Step4:运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_image.py -q`

Expected:全部 PASS。

- [ ] **Step5:提交**

```bash
git add backend/app/services/agent_image.py backend/tests/test_agent_image.py
git commit -m "feat: pick image-editor deliverables and store artifacts"
```

---

### Task 5: Agent改图分支改为「最终海报 +透明层 + HTML/ZIP」

**Files:**
- Modify: `backend/app/services/whole_image.py:768-935`（`_agent_edit_branch`）及 `run_edit`文案段
- Modify: `backend/tests/test_whole_image.py`（`FakeCoreAI`与相关断言）

**Interfaces:**
- Consumes: Task3 `client.download_agent_file`、Task4 `agent_image.pick_*` / `save_agent_artifact` / `has_transparency`。
- Produces: Agent分支 outputs中——最终海报 `role="agent_result"`、`selectable=True`、带 `artifacts`元数据列表；透明层 `role="agent_transparent"`、`selectable=False`、`artifacts=[]`。输出顺序固定为「最终海报 →透明层」。`edit.branches.agent.model_input.model`由 `"image-gen-skill"`改为 `"image-editor"`。

- [ ] **Step1:更新测试替身并写失败测试**

将 `backend/tests/test_whole_image.py`的导入行改为：

```python
from app.services.core_ai import AgentArtifact, AgentRunResult, CoreAIError
```

（删除不再使用的 `DownloadedAgentImage`。）

`FakeCoreAI.__init__`末尾补充（保留原有字段）：

```python
    self.agent_artifacts = [
        AgentArtifact("generated-image.png", "image/png", "https://example.com/agent-layer.png", "模型原生透明前景"),
        AgentArtifact("generated-image.png", "image/png", "https://example.com/agent-result.png", "最终成图"),
        AgentArtifact("editor.html", "text/html", "https://example.com/editor.html", "离线编辑器"),
        AgentArtifact("result-bundle.zip", "application/zip", "https://example.com/result.zip", "完整结果包"),
    ]
    self.agent_downloads: list[str] = []
    self.agent_result_size: tuple[int, int] | None = None
```

删除 `run_agent_detail`与 `download_agent_images`的旧实现，替换为：

```python
    def _transparent_image(self) -> bytes:
        image = Image.new("RGBA", (640, 640), (0, 0, 0, 0))
        for x in range(120, 520):
            for y in range(120, 220):
                image.putpixel((x, y), (180, 80, 40, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _sized_image(self, size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def run_agent_detail(self, agent_id: str, input_text: str, *, timeout_seconds: int, poll_interval: float = 2.0) -> AgentRunResult:
        self.agent_calls.append(
            {"agent_id": agent_id, "input": input_text, "timeout_seconds": timeout_seconds}
        )
        return AgentRunResult(
            run_id="run-agent-1",
            status="COMPLETED",
            output=(
                "![透明层](https://example.com/agent-layer.png)\n"
                "![成图](https://example.com/agent-result.png)"
            ),
            artifacts=self.agent_artifacts,
        )

    def download_image(self, url: str) -> bytes:
        self.agent_downloads.append(url)
        if url.endswith("agent-layer.png"):
            return self._transparent_image()
        if url.endswith("agent-result.png"):
            if self.agent_result_size is not None:
                return self._sized_image(self.agent_result_size, (120, 40, 180))
            return self._sized_image((640, 640), (120, 40, 180))
        raise CoreAIError("download_failed", "未知图片")

    def download_agent_file(self, url: str, *, content_type: str) -> bytes:
        self.agent_downloads.append(url)
        if content_type == "text/html":
            return b"<!doctype html><h1>Editor</h1>"
        return b"PK\x03\x04bundle"
```

在 `test_dual_edit_stores_three_outputs_and_requires_manual_selection`中，把原有的 role顺序断言：

```python
    assert [output["role"] for output in v2["outputs"]] == [
        "gateway_result",
        "agent_transparent",
        "agent_result",
    ]
```

改为：

```python
    assert [output["role"] for output in v2["outputs"]] == [
        "gateway_result",
        "agent_result",
        "agent_transparent",
    ]
```

紧随其后追加：

```python
    agent_output = next(output for output in v2["outputs"] if output["role"] == "agent_result")
    assert [item["kind"] for item in agent_output["artifacts"]] == ["html", "bundle"]
    assert [item["file_name"] for item in agent_output["artifacts"]] == [
        "editor.html",
        "result-bundle.zip",
    ]
    assert v2["outputs"][0]["artifacts"] == []
```

在文件末尾追加新测试：

```python
def test_agent_branch_rejects_mismatched_aspect_after_trying_candidates(settings, store):
 db = mongomock.MongoClient().db
 fake = FakeCoreAI()
 fake.fail_agent = False
 task_id, _ = _generate(db, settings, store, fake)
 v1 = db.whole_versions.find_one({"task_id": task_id, "version":1})
 # The default fake returns a 640x320 poster while the source canvas is 640x640.
 fake.agent_result_size = (640,320)

 request = _request(db, settings, store, fake)
 whole_routes.create_edit(
 task_id,
 WholeImageEditRequest(instruction="改背景", source_version_id=v1["_id"]),
 request,
 BackgroundTasks(),
 )
 whole_service.run_edit(db, settings, store, fake, task_id, "改背景", v1["_id"])

 task = db.whole_tasks.find_one({"_id": task_id})
 assert task["edit"]["branches"]["agent"]["status"] == "failed"
 assert "画幅" in task["edit"]["branches"]["agent"]["error"]
 assert task["edit"]["outcome"] == "partial"
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py::test_dual_edit_stores_three_outputs_and_requires_manual_selection -q`

Expected: FAIL（`artifacts`为空或缺少 `download_image`）。

- [ ] **Step3:实现**

在 `_agent_edit_branch`定义之前插入辅助函数：

```python
def _save_agent_artifacts(
    db: Database,
    settings: Settings,
    store: ObjectStore,
    client: CoreAIClient,
    *,
    task_id: str,
    version: int,
    result: Any,
) -> list[dict[str, Any]]:
    picks: list[Any] = []
    html = agent_image.pick_html_artifact(result)
    if html is not None:
        picks.append(html)
    bundle = agent_image.pick_bundle_artifact(result)
    if bundle is not None and bundle.download_url not in {
        item.download_url for item in picks
    }:
        picks.append(bundle)
    saved: list[dict[str, Any]] = []
    for artifact in picks:
        try:
            data = client.download_agent_file(
                artifact.download_url, content_type=artifact.content_type
            )
            saved.append(
                agent_image.save_agent_artifact(
                    db,
                    settings,
                    store,
                    task_id=task_id,
                    version=version,
                    artifact=artifact,
                    data=data,
                )
            )
        except Exception: # noqa: BLE001 - poster delivery must survive artifact failures
            logger.exception("failed to store agent artifact for %s", task_id)
    return saved
```

把 `_agent_edit_branch`里 `"model": "image-gen-skill",`改为 `"model": "image-editor",`。

同时删除 `backend/app/services/agent_image.py`中已无调用者的 `AgentOutputImage` dataclass、`run_agent_images`、`classify_agent_images`、`_has_transparency`，以及不再使用的 `DownloadedAgentImage`导入。

把该函数中 `result, outputs = agent_image.run_agent_images(...)`到 `_set_edit_branch(..., "agent", {"status": "completed", ...})`之前的整段代码替换为：

```python
        result = client.run_agent_detail(
            settings.core_ai_image_agent_id,
            agent_input,
            timeout_seconds=settings.core_ai_image_agent_timeout_seconds,
        )
        final_urls = agent_image.pick_final_image_urls(result)
        if not final_urls:
            raise CoreAIError("agent_image_missing", "Agent已完成，但没有返回海报图片", 502)
        artifact_meta = _save_agent_artifacts(
            db, settings, store, client, task_id=task_id, version=version, result=result
        )
        transparent_url = agent_image.pick_transparent_image_url(
            result, excluded=final_urls
        )
        transparent_data: bytes | None = None
        if transparent_url is not None:
            try:
                candidate = client.download_image(transparent_url)
                if agent_image.has_transparency(candidate):
                    transparent_data = candidate
            except CoreAIError:
                logger.warning("could not download agent layer for %s", task_id)
        final_saved: dict[str, Any] | None = None
        final_source: str | None = None
        last_error: CoreAIError | None = None
        for url in final_urls[:4]:
            data = client.download_image(url)
            if agent_image.has_transparency(data):
                if transparent_data is None:
                    transparent_data = data
                continue
            try:
                saved = agent_image.save_png_asset(
                    db,
                    settings,
                    store,
                    key_prefix=f"{task_id}_v{version}_agent_1",
                    original_stem=f"{task_id}-v{version}-agent-1",
                    data=data,
                    role="agent_result",
                    target_size=target_size,
                    require_aspect_match=True,
                )
            except CoreAIError as exc:
                last_error = exc
                continue
            final_saved, final_source = saved, url
            break
        if final_saved is None:
            raise last_error or CoreAIError(
                "agent_image_missing", "Agent没有返回可用的最终海报", 502
            )
        saved_outputs: list[dict[str, Any]] = [
            {
                "id": f"agent_result_{uuid.uuid4().hex}",
                "channel": "agent",
                "role": "agent_result",
                "label": "Agent最终图",
                "asset_id": final_saved["asset_id"],
                "width": final_saved["width"],
                "height": final_saved["height"],
                "selectable": True,
                "model_input": model_input,
                "source_url": final_source,
                "artifacts": artifact_meta,
                "created_at": utcnow(),
            }
        ]
        if transparent_data is not None:
            layer = agent_image.save_png_asset(
                db,
                settings,
                store,
                key_prefix=f"{task_id}_v{version}_agent_2",
                original_stem=f"{task_id}-v{version}-agent-2",
                data=transparent_data,
                role="agent_transparent",
                target_size=target_size,
            )
            saved_outputs.append(
                {
                    "id": f"agent_transparent_{uuid.uuid4().hex}",
                    "channel": "agent",
                    "role": "agent_transparent",
                    "label": "Agent透明 PNG",
                    "asset_id": layer["asset_id"],
                    "width": layer["width"],
                    "height": layer["height"],
                    "selectable": False,
                    "model_input": model_input,
                    "source_url": transparent_url,
                    "artifacts": [],
                    "created_at": utcnow(),
                }
            )
```

`run_edit`中统计文案段改为：

```python
            message = f"已完成 v{version}：接口 {gateway_count}张，Agent {agent_count}张。"
            artifact_count = sum(
                len(item.get("artifacts") or [])
                for item in all_outputs
                if item.get("channel") == "agent"
            )
            if artifact_count:
                message += f"附加 HTML/结果包 {artifact_count}个。"
```

再把 `run_edit`里并发收集结果的循环改为稳定顺序（原来用 `as_completed`，输出顺序不确定，会让 `output_asset_id`和断言随机）：

```python
    all_outputs: list[dict[str, Any]] = []
    for future in as_completed(futures):
        all_outputs.extend(future.result())
    all_outputs.sort(key=_output_sort_key)
```

并在 `run_edit`之前定义排序键：

```python
OUTPUT_ORDER = { "gateway_result": 0, "agent_result": 1, "agent_transparent": 2}


def _output_sort_key(item: dict[str, Any]) -> int:
    if str(item.get("channel") or "gateway") == "gateway":
        return 0
    return OUTPUT_ORDER.get(str(item.get("role") or "gateway_result"), 3)
```

这样 `all_outputs[0]`始终是接口结果，`output_asset_id`与版本默认主图保持原有语义。

- [ ] **Step4:运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py -q`

Expected:全部 PASS。

- [ ] **Step5:提交**

```bash
git add backend/app/services/whole_image.py backend/tests/test_whole_image.py
git commit -m "feat: agent edit branch returns poster plus html artifacts"
```

---

### Task 6:产物序列化与预览/下载路由

**Files:**
- Modify: `backend/app/routes/whole_image.py:284-334`（`_output_out`）与文件末尾新增路由
- Modify: `backend/tests/test_whole_image.py`（追加路由测试）

**Interfaces:**
- Consumes: Task5写入的 `whole_artifacts`集合与 outputs里的 `artifacts`元数据。
- Produces: `output.artifacts: [{id, kind, file_name, content_type, size, title, preview_url, download_url}]`；`GET /api/whole-image/artifacts/{id}/preview`（仅 html，sandbox CSP）；`GET /api/whole-image/artifacts/{id}/download`。

- [ ] **Step1:写失败测试**

在 `backend/tests/test_whole_image.py`末尾追加：

```python
def test_agent_artifacts_are_exposed_with_preview_and_download(settings, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = mongomock.MongoClient().db
    fake = FakeCoreAI()
    fake.fail_agent = False
    task_id, _ = _generate(db, settings, store, fake)
    v1 = db.whole_versions.find_one({"task_id": task_id, "version": 1})
    request = _request(db, settings, store, fake)
    whole_routes.create_edit(
        task_id,
        WholeImageEditRequest(instruction="换背景", source_version_id=v1["_id"]),
        request,
        BackgroundTasks(),
    )
    whole_service.run_edit(db, settings, store, fake, task_id, "换背景", v1["_id"])

    task = whole_routes.get_task(task_id, request)
    agent_output = next(
        output for output in task["outputs"] if output["role"] == "agent_result"
    )
    assert [item["kind"] for item in agent_output["artifacts"]] == ["html", "bundle"]
    html = agent_output["artifacts"][0]
    assert html["preview_url"] == f"/api/whole-image/artifacts/{html['id']}/preview"
    assert html["download_url"] == f"/api/whole-image/artifacts/{html['id']}/download"
    assert agent_output["artifacts"][1]["preview_url"] is None

    web = FastAPI()
    web.include_router(whole_routes.router)
    web.state.db, web.state.settings, web.state.store = db, settings, store
    with TestClient(web) as client:
        preview = client.get(html["preview_url"])
        assert preview.status_code == 200
        csp = preview.headers["content-security-policy"]
        assert "sandbox allow-scripts allow-downloads;" in csp
        assert "allow-same-origin" not in csp
        assert "connect-src 'none'" in csp

        download = client.get(html["download_url"])
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]

        bundle = client.get(agent_output["artifacts"][1]["download_url"])
        assert bundle.status_code == 200

        assert (
            client.get(
                f"/api/whole-image/artifacts/{agent_output['artifacts'][1]['id']}/preview"
            ).status_code
            == 404
        )
```

- [ ] **Step2:运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py::test_agent_artifacts_are_exposed_with_preview_and_download -q`

Expected: FAIL（`KeyError: 'artifacts'`或404）。

- [ ] **Step3:实现**

在 `backend/app/routes/whole_image.py`的 `_output_out`之前插入：

```python
def _artifacts_out(output: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in output.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("id") or "")
        if not artifact_id:
            continue
        kind = str(item.get("kind") or "file")
        items.append(
            {
                "id": artifact_id,
                "kind": kind,
                "file_name": str(item.get("file_name") or ""),
                "content_type": str(item.get("content_type") or ""),
                "size": int(item.get("size") or 0),
                "title": str(item.get("title") or ""),
                "preview_url": (
                    f"/api/whole-image/artifacts/{artifact_id}/preview"
                    if kind == "html"
                    else None
                ),
                "download_url": f"/api/whole-image/artifacts/{artifact_id}/download",
            }
        )
    return items
```

在 `_output_out`返回字典中追加一行（放在 `"model_input"`之后）：

```python
    return {
        "id": output_id,
        "channel": str(output.get("channel") or "gateway"),
        "role": str(output.get("role") or "gateway_result"),
        "label": str(output.get("label") or "接口结果"),
        "asset_id": asset_id,
        "width": int(output.get("width") or (asset or {}).get("width") or 0),
        "height": int(output.get("height") or (asset or {}).get("height") or 0),
        "selectable": bool(output.get("selectable")),
        "preview_url": (
            f"/api/assets/{asset_id}/file?v={version_number}" if asset_id and asset else None
        ),
        "download_url": (
            f"/api/whole-image/tasks/{task_id}/download?version_id={version_id}&output_id={output_id}"
            if asset_id and asset and version_id and output_id
            else None
        ),
        "model_input": _model_input_out(db, output.get("model_input")),
        "artifacts": _artifacts_out(output),
    }
```

在文件末尾追加：

```python
ARTIFACT_PREVIEW_HEADERS = {
    "Content-Disposition": "inline",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-downloads; default-src 'none'; "
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; connect-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}


def _artifact_doc(db: Any, artifact_id: str) -> dict[str, Any]:
    doc = db.whole_artifacts.find_one({"_id": artifact_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    return doc


@router.get("/artifacts/{artifact_id}/preview")
def preview_artifact(artifact_id: str, request: Request) -> Response:
    doc = _artifact_doc(request.app.state.db, artifact_id)
    if doc.get("kind") != "html":
        raise HTTPException(status_code=404, detail="该产物不是 HTML")
    data = request.app.state.store.get_bytes(doc["path"])
    return Response(
        content=data, media_type="text/html", headers=ARTIFACT_PREVIEW_HEADERS
    )


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, request: Request) -> Response:
    doc = _artifact_doc(request.app.state.db, artifact_id)
    settings = request.app.state.settings
    store = request.app.state.store
    filename = str(doc.get("file_name") or f"{artifact_id}.bin")
    local = store.local_path(doc["path"])
    if local is not None:
        return FileResponse(
            local,
            media_type=str(doc.get("content_type") or "application/octet-stream"),
            filename=filename,
            content_disposition_type="attachment",
        )
    return RedirectResponse(
        store.presign(
            doc["path"],
            expires=settings.cos_presign_expiry_seconds,
            download_name=filename,
        ),
        status_code=302,
    )
```

- [ ] **Step4:运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_whole_image.py tests/test_main.py -q`

Expected:全部 PASS。

- [ ] **Step5:提交**

```bash
git add backend/app/routes/whole_image.py backend/tests/test_whole_image.py
git commit -m "feat: serve agent html previews and artifact downloads"
```

---

### Task 7:前端类型、HTML预览弹层与卡片入口

**Files:**
- Modify: `frontend/src/types.ts:170-192`
- Create: `frontend/src/components/WholeArtifactPreview.tsx`
- Modify: `frontend/src/components/WholeImageResultPanel.tsx`
- Modify: `frontend/src/components/WholeVersionPanel.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes:后端 `output.artifacts`。
- Produces: `WholeArtifactPreview`组件，props `{ artifact: WholeImageArtifact | null; onClose: () => void }`。

- [ ] **Step1:更新类型**

`frontend/src/types.ts`在 `WholeImageOutput`之前插入：

```ts
export type WholeImageArtifact = {
    id: string
    kind: 'html' | 'bundle' | 'file'
    file_name: string
    content_type: string
    size: number
    title: string
    preview_url: string | null
    download_url: string | null
}
```

并在 `WholeImageOutput`中追加：

```ts
    artifacts?: WholeImageArtifact[]
```

- [ ] **Step2:新建预览组件**

创建 `frontend/src/components/WholeArtifactPreview.tsx`：

```tsx
import type { WholeImageArtifact } from '../types'

type Props = {
    artifact: WholeImageArtifact | null
    onClose: () => void
}

export default function WholeArtifactPreview({ artifact, onClose }: Props) {
    if (!artifact?.preview_url) {
        return null
    }
    const title = artifact.title || artifact.file_name
    return (
        <div
            className="version-preview-backdrop"
            role="dialog"
            aria-modal="true"
            aria-label={`${title}预览`}
            onClick={onClose}
        >
            <div className="artifact-preview-card" onClick={(event) => event.stopPropagation()}>
                <div className="version-preview-head">
                    <strong>{title}</strong>
                    <div className="artifact-preview-actions">
                        {artifact.download_url ? (
                            <a className="mp-btn-ghost mp-btn-small" href={artifact.download_url} download>
                                下载 HTML
                            </a>
                        ) : null}
                        <button type="button" onClick={onClose}>
                            关闭
                        </button>
                    </div>
                </div>
                <iframe
                    title={`${title}编辑器`}
                    src={artifact.preview_url}
                    sandbox="allow-scripts allow-downloads"
                />
                <p className="model-input-hint">
                    在编辑器里可拖动、缩放原照并导出 PNG；完全离线运行，不发起外部请求。
                </p>
            </div>
        </div>
    )
}
```

- [ ] **Step3:结果卡片接入**

`frontend/src/components/WholeImageResultPanel.tsx`顶部改为：

```tsx
import { useState } from 'react'
import type { WholeImageArtifact, WholeImageBranch, WholeImageOutput, WholeImageTask } from '../types'
import WholeArtifactPreview from './WholeArtifactPreview'
```

`OutputCard`的 props增加 `onPreviewArtifact`，函数体开头改为：

```tsx
function OutputCard({
    output,
    task,
    onSelectOutput,
    onPreviewArtifact,
    selectingOutput,
}: {
    output: WholeImageOutput
    task: WholeImageTask
    onSelectOutput?: (versionId: string, outputId: string) => void
    onPreviewArtifact: (artifact: WholeImageArtifact) => void
    selectingOutput: boolean
}) {
    const selected = task.current_output_id === output.id
    const transparent = output.role === 'agent_transparent'
    const artifacts = output.artifacts ?? []
    const htmlArtifact = artifacts.find((artifact) => artifact.kind === 'html')
    const bundleArtifact = artifacts.find((artifact) => artifact.kind === 'bundle')
    return (
```

在 `<div className="comparison-actions">`内、「下载 PNG」链接之前插入：

```tsx
                {htmlArtifact ? (
                    <button
                        type="button"
                        className="mp-btn-secondary mp-btn-small"
                        onClick={() => onPreviewArtifact(htmlArtifact)}
                    >
                        打开 HTML编辑器
                    </button>
                ) : null}
                {bundleArtifact?.download_url ? (
                    <a className="mp-btn-ghost mp-btn-small" href={bundleArtifact.download_url} download>
                        下载结果包
                    </a>
                ) : null}
```

`WholeImageResultPanel`函数体开头（`useState`区域）增加：

```tsx
    const [artifactPreview, setArtifactPreview] = useState<WholeImageArtifact | null>(null)
```

`OutputCard`调用处追加 prop：

```tsx
                    onPreviewArtifact={setArtifactPreview}
```

在组件 `return`的最外层 `</section>`之前追加：

```tsx
            <WholeArtifactPreview
                artifact={artifactPreview}
                onClose={() => setArtifactPreview(null)}
            />
```

同时把等待文案改为：

```tsx
                <span>接口与 Agent正在并行生成，Agent通常需要10–30分钟</span>
```

- [ ] **Step4:版本预览接入**

`frontend/src/components/WholeVersionPanel.tsx`顶部改为：

```tsx
import { useState } from 'react'
import type { WholeImageArtifact, WholeImageVersion } from '../types'
import { parseServerDate } from '../time'
import WholeArtifactPreview from './WholeArtifactPreview'
```

组件内增加状态：

```tsx
    const [artifactPreview, setArtifactPreview] = useState<WholeImageArtifact | null>(null)
```

在版本预览弹层的关闭按钮之前插入产物入口：

```tsx
                        {preview.outputs
                            .flatMap((output) => output.artifacts ?? [])
                            .map((artifact) =>
                                artifact.preview_url ? (
                                    <button
                                        key={artifact.id}
                                        type="button"
                                        className="mp-btn-ghost mp-btn-small"
                                        onClick={() => setArtifactPreview(artifact)}
                                    >
                                        打开 {artifact.title || artifact.file_name}
                                    </button>
                                ) : artifact.download_url ? (
                                    <a
                                        key={artifact.id}
                                        className="mp-btn-ghost mp-btn-small"
                                        href={artifact.download_url}
                                        download
                                    >
                                        下载 {artifact.title || artifact.file_name}
                                    </a>
                                ) : null,
                            )}
```

在组件最外层 `</div>`之前追加：

```tsx
            <WholeArtifactPreview
                artifact={artifactPreview}
                onClose={() => setArtifactPreview(null)}
            />
```

- [ ] **Step5:样式**

`frontend/src/styles.css`在 `.version-preview-card`规则之后追加：

```css
.artifact-preview-card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    width: min(1180px, 94vw);
    height: min(86vh, 900px);
    padding: 14px;
    border-radius: 18px;
    background: #fff;
    box-shadow: 0 24px 72px rgba(17, 24, 39, 0.28);
}

.artifact-preview-card iframe {
    flex: 1;
    width: 100%;
    min-height: 0;
    border: 0;
    border-radius: 10px;
    background: #fff;
}

.artifact-preview-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}
```

- [ ] **Step6:构建与 lint**

Run: `cd frontend && npm run build`

Expected:构建成功，无 TS报错。

Run: `cd frontend && npm run lint`

Expected:无错误。

- [ ] **Step7:提交**

```bash
git add frontend/src/types.ts frontend/src/components/WholeArtifactPreview.tsx frontend/src/components/WholeImageResultPanel.tsx frontend/src/components/WholeVersionPanel.tsx frontend/src/styles.css
git commit -m "feat: preview agent html editor in poster workbench"
```

---

### Task 8:文档与全量验证

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes:前面所有任务。
- Produces:无代码接口；文档与最终验证。

- [ ] **Step1:更新 README**

在「主要能力」中把 Agent改图相关描述替换为：

```markdown
-生成调用 core-ai `/images/generations`；自然语言改图并行调用 `/images/edits`和图片 Agent（`image-editor`），两个结果并排对比。
-图片 Agent使用 `CORE_AI_IMAGE_AGENT_ID`指定的 image-editor Agent，返回最终海报 PNG、可选透明层、离线 HTML编辑器和完整结果 ZIP；工作台内可内嵌预览 HTML并导出 PNG。
- Agent产物保存在对象存储 `artifacts/`前缀，元数据写入 MongoDB `whole_artifacts`；历史版本没有产物时继续只显示图片。
-改图 Agent单次通常耗时10–30分钟，默认超时1800秒。
```

在「工作流接口」表追加：

```markdown
| `GET /api/whole-image/artifacts/{artifact_id}/preview` |内嵌预览 Agent返回的 HTML编辑器（sandbox隔离） |
| `GET /api/whole-image/artifacts/{artifact_id}/download` |下载 Agent返回的 HTML或结果 ZIP |
```

- [ ] **Step2:更新 AGENTS.md**

在「主要目录」列表末尾追加：

```markdown
- Agent改图产物位于对象存储 `artifacts/`前缀，元数据保存在 MongoDB `whole_artifacts`；HTML通过 `/api/whole-image/artifacts/{id}/preview`以 sandbox CSP内联预览。
```

- [ ] **Step3:全量后端测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`

Expected:全部 PASS；如出现失败，先修复再继续，不得跳过。

- [ ] **Step4:前端构建与 lint**

Run: `cd frontend && npm run build`

Run: `cd frontend && npm run lint`

Expected:均无错误。

- [ ] **Step5:提交**

```bash
git add README.md AGENTS.md
git commit -m "docs: document image-editor agent artifacts"
```

---

##可选人工验收（会消耗付费额度，默认不执行）

在本地启动后端与前端后：

1.选品牌/素材生成一张海报。
2.在「对话」输入「把图1中的背景替换为图2」并附一张背景图。
3.确认结果区出现两张并排结果（接口 / Agent），Agent卡片有「打开 HTML编辑器」「下载结果包」。
4.打开 HTML弹层，确认编辑器可拖动/缩放并导出 PNG。
5.选择 Agent结果后继续发起第二轮改图，确认版本与产物仍然可查。

执行前必须显式征得同意；AGENTS.md要求不在未执行验证时声称通过。

---

##自检记录

- **Spec覆盖**：Agent ID切换 → Task1；HTML +图片混合产物 → Task2/3/4/5；工作台内嵌预览 → Task6/7；文档与全量验证 → Task8。
- **占位符扫描**：未使用 TBD/TODO/「适当处理」等占位表述；每个代码步骤均给出完整代码。
- **类型一致性**：`save_agent_artifact`返回键 `id/kind/file_name/content_type/size/title`与 Task5写入 outputs的元数据、Task6 `_artifacts_out`读取的键、Task7前端 `WholeImageArtifact`字段一致；gateway输出没有 `artifacts`时序列化为空列表。
- **顺序变更已覆盖**：Agent分支输出顺序由「透明层→最终图」改为「最终图→透明层」，Task5 Step1已同步修改既有断言。
