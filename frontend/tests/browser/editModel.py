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

from posterSession import Fixture, SVG

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173"
GPT = "gpt-image-2.5-sunburst"
SEEDREAM = "seedream-5.5-image-to-image"
MINIMAX = "minimax-h3-image-edit"
ASSET_NAMES = ["first", "second", "third", "fourth", *[f"reference-{index}" for index in range(5, 17)]]


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
        self.tasks["wtask_test_a"]["input"]["reference_asset_ids"] = ["original"]
        self.versions = {key: [self.version(current, "generate", "测试海报")] for key, current in self.tasks.items()}
        self.edits = []

    @staticmethod
    def version(current, operation, instruction):
        return {
            "id": current["current_version_id"], "version": current["current_version"], "operation": operation,
            "width": 640, "height": 640, "outputs": current["outputs"], "instruction": instruction,
            "model_input": current["model_input"],
            "preview_url": "/fixture/poster", "download_url": "/fixture/poster",
        }

    async def route(self, route):
        request = route.request
        path = re.sub(r"^/api/merchants/[^/]+", "/api", urlparse(request.url).path)
        if path == "/api/whole-image/series-config":
            return await route.fulfill(json={"enabled": False})
        if path == "/api/assets" and request.method == "POST":
            return await route.fulfill(status=201, json={
                "id": "uploaded", "original_name": "uploaded.svg", "url": "/fixture/uploaded",
                "mime_type": "image/svg+xml", "width": 100, "height": 100,
                "created_at": "2026-11-13T00:00:00Z",
            })
        if path in ("/api/assets", "/api/am-assets") and request.method == "GET":
            items = [{
                "id": name, "original_name": f"{name}.svg", "url": f"/fixture/{name}",
                "mime_type": "image/svg+xml", "width": 100, "height": 100,
                "created_at": "2026-11-13T00:00:00Z",
            } for name in ASSET_NAMES]
            return await route.fulfill(json={"items": items, "total": len(items), "skip": 0, "limit": 36})
        match = re.fullmatch(r"/api/whole-image/tasks/([^/]+)/(edits|versions)", path)
        if not match:
            return await super().route(route)
        task_id, action = match.groups()
        current = self.tasks[task_id]
        if action == "versions":
            return await route.fulfill(json=self.versions[task_id])
        assert request.method == "POST"
        data = request.post_data_json
        self.edits.append(copy.deepcopy(data))
        await asyncio.sleep(0.2)
        current["edit"] = {**data, "status": "running"}
        current["stage"] = "editing"
        current["messages"].append({
            "id": f'request-{len(self.edits)}', "role": "user", "content": data["instruction"],
            "image_model": data["image_model"], "created_at": current["updated_at"],
            "attachments": [{"asset_id": ref, "name": ref, "url": f"/fixture/{ref}"} for ref in data["reference_asset_ids"]],
        })
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
        self.versions[task_id].append(self.version(current, "edit", current["edit"]["instruction"]))
        current["messages"].append({
            "id": f'result-{len(self.edits)}', "role": "assistant", "content": '已完成',
            "version_id": current["current_version_id"], "version": current["current_version"],
            "created_at": current["updated_at"],
        })


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
            chat = page.get_by_role("region", name="对话改图", exact=True)
            editor = chat.get_by_role("textbox", name="修改要求", exact=True)
            tiles = chat.locator(".image-reference-tile:visible")
            add = chat.get_by_role("button", name="添加参考图", exact=True)
            mentions = page.get_by_role("listbox", name="选择引用图片")
            await expect(page.locator(".form-card .image-reference-tile")).to_have_count(1)
            await expect(tiles).to_have_count(0)
            await expect(add).to_contain_text("0/15")
            await editor.press_sequentially("@")
            await expect(mentions.get_by_role("option")).to_have_count(1)
            await expect(mentions.get_by_role("option", name="海报 当前海报")).to_be_visible()
            await editor.press("Escape")
            await add.click()
            await chat.get_by_role("button", name="从资产库选择").click()
            picker = page.get_by_role("dialog", name="选择改图参考图")
            await picker.get_by_text("first", exact=True).click()
            await picker.get_by_role("button", name="确定").click()
            await add.click()
            await chat.get_by_label("上传参考图").set_input_files({
                "name": "uploaded.svg", "mimeType": "image/svg+xml", "buffer": SVG.encode(),
            })
            await expect(tiles).to_have_count(2)
            await expect(add).to_contain_text("2/15")

            assert await models.locator("option").all_text_contents() == [
                "GPT Image 2.5", "Gemini 3 Pro Image", "Seedream 5 Pro", "Seedream 5.5", "MiniMax H3",
            ]
            await expect(page.get_by_role("button", name="切换到 Gemini 3 Pro Image", exact=True)).to_be_visible()
            await models.select_option(SEEDREAM)
            await expect(page.get_by_role("button", name="切换到 GPT Image 2.5", exact=True)).to_have_attribute("aria-pressed", "true")
            await editor.fill("只改标题，参考")
            await editor.press("End")
            await editor.press_sequentially("@")
            await mentions.get_by_role("option", name="图1 first").click()
            await editor.press_sequentially("和 @")
            await mentions.get_by_role("option", name="图2 uploaded").click()
            await page.get_by_role("button", name="发送修改", exact=True).click()
            await expect(models).to_be_disabled()
            await expect(page.get_by_role("button", name="发送修改", exact=True)).to_be_disabled()
            await expect(page.get_by_text("修改中", exact=True)).to_be_visible()
            assert fixture.edits[-1]["image_model"] == SEEDREAM
            assert fixture.edits[-1]["source_version_id"] == "wtask_test_a-v1"
            assert fixture.edits[-1]["source_output_id"] == "gateway_result"
            assert fixture.edits[-1]["reference_mode"] == "unified"
            assert fixture.edits[-1]["reference_asset_ids"] == ["first", "uploaded"]
            assert fixture.edits[-1]["instruction"] == "只改标题，参考@图1 和 @图2"
            assert fixture.tasks["wtask_test_a"]["input"]["image_model"] == GPT
            history = page.locator(".chat-history-panel")
            if not await history.is_visible():
                await page.get_by_role("button", name=re.compile("修改记录")).click()
            first_request = history.locator(".chat-user").filter(has_text="只改标题")
            first_model = first_request.get_by_text("Seedream 5.5", exact=True)
            await expect(first_model).to_be_visible()
            label = await first_model.bounding_box()
            thumbnail = await first_request.get_by_role("img", name="uploaded", exact=True).bounding_box()
            assert label["x"] >= thumbnail["x"] + thumbnail["width"], (label, thumbnail)
            assert abs(label["y"] + label["height"] - thumbnail["y"] - thumbnail["height"]) <= 1, (label, thumbnail)
            fixture.finish("wtask_test_a")
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(SEEDREAM)
            await expect(tiles).to_have_count(2)
            print("PASS: dropdown selection reaches the edit request independently; in-flight controls are disabled")

            # Older requests have no stored model, but their result versions retain the actual model.
            del fixture.tasks["wtask_test_a"]["messages"][0]["image_model"]
            await page.reload()
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(SEEDREAM)
            await expect(tiles).to_have_count(0)
            if await history.is_visible():
                await page.get_by_role("button", name="收起辅助工具").click()
            await models.select_option(MINIMAX)
            await editor.fill("背景改为暖色")
            await page.get_by_role("button", name="发送修改", exact=True).click()
            await expect(page.get_by_text("修改中", exact=True)).to_be_visible()
            assert fixture.edits[-1]["image_model"] == MINIMAX
            assert fixture.edits[-1]["source_version_id"] == "wtask_test_a-v2"
            assert fixture.edits[-1]["reference_asset_ids"] == []
            print("PASS: only uploaded and picked images can be referenced as 图1/图2; reload starts with zero references")
            if not await history.is_visible():
                await page.get_by_role("button", name=re.compile("修改记录")).click()
            second_request = history.locator(".chat-user").filter(has_text="背景改为暖色")
            await expect(first_request.get_by_text("Seedream 5.5", exact=True)).to_be_visible()
            await expect(second_request.get_by_text("MiniMax H3", exact=True)).to_be_visible()
            print("PASS: request bubbles show their own models beside attachments, including legacy records after reload")
            fixture.finish("wtask_test_a")
            await expect(models).to_be_enabled()
            await expect(models).to_have_value(MINIMAX)
            print("PASS: reload restores the edit model, and the next turn can change models again")

            await page.get_by_role("button", name="切换到 Seedream 5.5", exact=True).click()
            await expect(models).to_have_value(SEEDREAM)
            await page.get_by_role("button", name="切换到 GPT Image 2.5", exact=True).click()
            await expect(models).to_have_value(MINIMAX)
            print("PASS: switching generation candidates preserves independent saved edit models")

            if await history.is_visible():
                await page.get_by_role("button", name="收起辅助工具").click()
            await add.click()
            await chat.get_by_role("button", name="从资产库选择").click()
            for name in ASSET_NAMES[:4]:
                await picker.get_by_text(name, exact=True).click()
            await picker.get_by_role("button", name="确定").click()
            await expect(tiles).to_have_count(4)
            await expect(add).to_contain_text("4/15")
            diagnostics = Path(__file__).resolve().parents[3] / "storage/diagnostics/edit-model-dropdown"
            diagnostics.mkdir(parents=True, exist_ok=True)
            for width in (1440, 1280, 1024, 768, 390, 320):
                if width == 1280:
                    await page.get_by_role("button", name=re.compile("修改记录")).click()
                    await expect(history).to_be_visible()
                await page.set_viewport_size({"width": width, "height": 1000})
                composer = page.locator(".chat-composer .image-prompt-composer")
                await expect(models).to_be_visible()
                layout = await composer.evaluate('''element => {
                    const bounds = item => item.getBoundingClientRect().toJSON();
                    return {
                        composer: bounds(element),
                        tiles: Array.from(element.querySelectorAll('.image-reference-tile'), bounds),
                        strip: bounds(element.querySelector('.image-reference-scroll')),
                        add: bounds(element.querySelector('.image-reference-add')),
                        actions: Array.from(element.querySelectorAll('.chat-composer-actions a, .chat-composer-actions button, .chat-composer-actions select'), bounds),
                    };
                }''')
                row = layout["tiles"]
                actions = layout["actions"]
                assert len(row) == 4
                assert all(left["right"] <= right["left"] + 1 for left, right in zip(row, row[1:])), (width, layout)
                assert all(tile["top"] == row[0]["top"] for tile in row), (width, layout)
                assert layout["strip"]["right"] <= layout["add"]["left"], (width, layout)
                assert all(
                    layout["composer"]["left"] <= control["left"] < control["right"] <= layout["composer"]["right"] + 1
                    for control in [layout["strip"], layout["add"], *actions]
                ), (width, layout)
                assert all(
                    min(action["right"], other["right"]) - max(action["left"], other["left"]) <= 1
                    or min(action["bottom"], other["bottom"]) - max(action["top"], other["top"]) <= 1
                    for index, action in enumerate(actions) for other in actions[index + 1:]
                ), (width, layout)
                if width == 1440:
                    assert layout["add"]["right"] < actions[0]["left"], (width, layout)
                else:
                    assert layout["strip"]["bottom"] <= min(action["top"] for action in actions), (width, layout)
                await composer.screenshot(path=str(diagnostics / f"composer-{width}.png"))
            await add.click()
            await chat.get_by_role("button", name="从资产库选择").click()
            for name in ASSET_NAMES[4:]:
                await picker.get_by_text(name, exact=True).click()
            await picker.get_by_role("button", name="确定").click()
            await expect(tiles).to_have_count(15)
            await expect(add).to_contain_text("15/15")
            await expect(add).to_be_disabled()
            await expect(chat.get_by_role("alert")).to_have_count(0)
            strip = chat.locator(".image-reference-scroll")
            assert await strip.evaluate("element => element.scrollWidth > element.clientWidth")
            add_position = (await add.bounding_box())["x"]
            await strip.evaluate("element => element.scrollLeft = element.scrollWidth")
            await tiles.last.scroll_into_view_if_needed()
            await expect(tiles.last).to_be_in_viewport()
            assert (await add.bounding_box())["x"] == add_position
            await editor.fill("")
            await editor.press_sequentially("@")
            await mentions.get_by_role("option", name="图15 reference-15", exact=True).click()
            await expect(editor.locator(".image-mention-chip")).to_have_text("图15")
            await page.get_by_role("button", name="发送修改", exact=True).click()
            await expect(page.get_by_text("修改中", exact=True)).to_be_visible()
            assert fixture.edits[-1]["reference_asset_ids"] == ASSET_NAMES[:15]
            assert fixture.edits[-1]["instruction"] == "@图15"
            assert not fixture.unhandled, fixture.unhandled
            assert not errors, errors
            print("PASS: attachments stay in one row from 1440px to 320px and 15 references submit with @图15")
        finally:
            await context.close()
            await browser.close()


asyncio.run(main())
