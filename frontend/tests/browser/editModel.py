"""Run with the existing Python Playwright installation against a local Vite server.

All API requests are intercepted. No merchant data or paid models are used.
"""
import asyncio
import copy
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

from posterSession import Fixture

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173"
GPT = "gpt-image-2.5-sunburst"
SEEDREAM = "seedream-5.5-image-to-image"
MINIMAX = "minimax-h3-image-edit"


def output(task_id, model, version=1):
    return {"id": "gateway_result", "channel": "gateway", "role": "gateway_result", "label": "接口结果",
            "asset_id": f"{task_id}-v{version}", "width": 640, "height": 640, "selectable": True,
            "preview_url": "/fixture/poster", "download_url": "/fixture/poster",
            "model_input": {"channel": "gateway", "provider": "core-ai", "endpoint": "/images/edits", "model": model,
                            "prompt": "test", "parameters": {}, "images": []}}


def task(task_id, model):
    result = output(task_id, model)
    return {"id": task_id, "status": "succeeded", "stage": "done", "progress": 100,
            "input": {"image_model": model, "reference_asset_ids": [], "prompt": "测试海报", "brand_mode": "none",
                      "ratio": "1:1", "candidate_count": 1, "candidate_index": 0,
                      "candidate_batch_id": task_id, "generation_workspace_id": "workspace"},
            "provider": {"name": "core-ai", "model": model, "status": "completed"},
            "canvas": {"width": 640, "height": 640}, "outputs": [result], "model_input": result["model_input"],
            "current_version_id": task_id + "-v1", "selected_version_id": task_id + "-v1", "current_version": 1,
            "current_output_id": "gateway_result", "requires_output_selection": False,
            "preview_url": "/fixture/poster", "download_url": "/fixture/poster", "messages": [], "edit": {"status": "idle"},
            "error": None, "created_at": "2026-11-13T00:00:00Z", "updated_at": "2026-11-13T00:00:00Z"}


class EditFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.tasks = {key: task(key, model) for key, model in [("wtask_test_a", GPT), ("wtask_test_b", SEEDREAM)]}
        self.edits = []

    async def route(self, route):
        request = route.request
        path = re.sub(r"^/api/merchants/[^/]+", "/api", urlparse(request.url).path)
        match = re.fullmatch(r"/api/whole-image/tasks/([^/]+)/(edits|versions)", path)
        if not match:
            return await super().route(route)
        task_id, action = match.groups()
        current = self.tasks[task_id]
        if action == "versions":
            return await route.fulfill(json=[{
                "id": current["current_version_id"], "version": current["current_version"], "operation": "generate",
                "width": 640, "height": 640, "outputs": current["outputs"], "instruction": "test",
                "preview_url": "/fixture/poster", "download_url": "/fixture/poster",
            }])
        assert request.method == "POST"
        data = request.post_data_json
        self.edits.append(copy.deepcopy(data))
        await asyncio.sleep(0.2)
        current["edit"] = {**data, "status": "running"}
        current["stage"] = "editing"
        await route.fulfill(status=202, json=current)

    def finish(self, task_id):
        current = self.tasks[task_id]
        current["current_version"] += 1
        current["current_version_id"] = f'{task_id}-v{current["current_version"]}'
        current["selected_version_id"] = current["current_version_id"]
        result = output(task_id, current["edit"]["image_model"], current["current_version"])
        current["outputs"] = [result]
        current["model_input"] = result["model_input"]
        current["edit"]["status"] = "idle"
        current["stage"] = "done"
        current["updated_at"] = "2026-11-13T00:01:00Z"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(10000)
        fixture = EditFixture()
        await context.route("**/api/**", fixture.route)
        await context.route("**/fixture/**", fixture.route)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(BASE + "/?merchant_id=merchant-1&whole=wtask_test_a")
            models = page.get_by_role("combobox", name="改图模型", exact=True)
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(GPT)
            assert await models.locator("option").all_text_contents() == ["GPT Image 2.5", "Seedream 5 Pro", "Seedream 5.5", "MiniMax H3"]
            await models.select_option(SEEDREAM)
            await expect(page.get_by_role("button", name="切换到 GPT Image 2.5", exact=True)).to_have_attribute("aria-pressed", "true")
            await page.get_by_role("textbox", name="修改要求", exact=True).fill("只改标题")
            await page.get_by_role("button", name="发送修改", exact=True).click()
            await expect(models).to_be_disabled()
            await expect(page.get_by_role("button", name="发送修改", exact=True)).to_be_disabled()
            await expect(page.get_by_text("修改中", exact=True)).to_be_visible()
            assert fixture.edits[-1]["image_model"] == SEEDREAM
            assert fixture.edits[-1]["source_version_id"] == "wtask_test_a-v1"
            assert fixture.edits[-1]["source_output_id"] == "gateway_result"
            assert fixture.edits[-1]["reference_mode"] == "unified"
            assert fixture.tasks["wtask_test_a"]["input"]["image_model"] == GPT
            fixture.finish("wtask_test_a")
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(SEEDREAM)
            print("PASS: dropdown selection reaches the edit request independently; in-flight controls are disabled")

            await page.reload()
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(SEEDREAM)
            await models.select_option(MINIMAX)
            await page.get_by_role("textbox", name="修改要求", exact=True).fill("背景改为暖色")
            await page.get_by_role("button", name="发送修改", exact=True).click()
            await expect(page.get_by_text("修改中", exact=True)).to_be_visible()
            assert fixture.edits[-1]["image_model"] == MINIMAX
            assert fixture.edits[-1]["source_version_id"] == "wtask_test_a-v2"
            fixture.finish("wtask_test_a")
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(MINIMAX)
            print("PASS: reload restores the edit model, and the next turn can change models again")

            await page.get_by_role("button", name="切换到 Seedream 5.5", exact=True).click()
            await expect(models).to_have_value(SEEDREAM)
            await page.get_by_role("button", name="切换到 GPT Image 2.5", exact=True).click()
            await expect(models).to_have_value(MINIMAX)
            print("PASS: switching generation candidates preserves independent saved edit models")

            diagnostics = Path(__file__).resolve().parents[3] / "storage/diagnostics/edit-model-dropdown"
            diagnostics.mkdir(parents=True, exist_ok=True)
            for width in [1440, 1024, 768, 390]:
                await page.set_viewport_size({"width": width, "height": 1000})
                composer = page.locator(".chat-composer .image-prompt-composer")
                await expect(models).to_be_visible()
                box = await composer.bounding_box()
                selector = await models.bounding_box()
                send = await page.get_by_role("button", name="发送修改", exact=True).bounding_box()
                assert selector["x"] >= box["x"] and send["x"] + send["width"] <= box["x"] + box["width"] + 1, (width, box, selector, send)
                assert selector["x"] + selector["width"] <= send["x"], (width, selector, send)
                await composer.screenshot(path=str(diagnostics / f"composer-{width}.png"))
            assert not fixture.unhandled, fixture.unhandled
            assert not errors, errors
            print("PASS: dropdown and send button fit at 1440/1024/768/390px with no overlapping controls or browser errors")
        finally:
            await context.close()
            await browser.close()


asyncio.run(main())
