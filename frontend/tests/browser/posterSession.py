"""Run against a local Vite server: python tests/browser/posterSession.py [base_url].

Requires the existing Python Playwright installation and Edge. All API calls are
intercepted; the test never calls a real model or changes merchant data.
"""
import asyncio
import json
import re
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5176"
MODEL = "gpt-image-2.5-sunburst"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="purple"/></svg>'


class Fixture:
    def __init__(self):
        self.user = "alice"
        self.mode = "fail"
        self.delay = 0
        self.list_delay = 0
        self.tasks = {}
        self.posts = 0
        self.unhandled = []

    async def route(self, route):
        request = route.request
        path = urlparse(request.url).path
        if path.startswith('/fixture/'):
            return await route.fulfill(content_type="image/svg+xml", body=SVG)
        path = re.sub(r"^/api/merchants/[^/]+", "/api", path)
        if re.fullmatch(r'/api/assets/[^/]+/file', path):
            return await route.fulfill(content_type="image/svg+xml", body=SVG)
        if path == "/api/context":
            merchant = re.search(r"/merchants/([^/]+)", urlparse(request.url).path)[1]
            payload = {"merchant": {"user_id": self.user, "user_name": self.user, "merchant_id": merchant, "merchant_name": merchant},
                       "profile": {"brand_info": {"stores": [{"place_id": "store-1", "name": "一店", "address": "测试地址", "phone": "123"}]}}, "features": {}}
        elif path == "/api/brands":
            await asyncio.sleep(self.list_delay)
            payload = []
        elif path == "/api/brand-logos":
            payload = {"logo_asset_ids": ["logo"], "logo_images": [{"asset_id": "logo", "original_name": "logo.png", "url": "/fixture/logo", "width": 100, "height": 100}]}
        elif path == "/api/platforms":
            await asyncio.sleep(self.list_delay)
            payload = [{"id": "instagram", "name": "Instagram", "default_placement_id": "feed", "placements": [
                {"id": "feed", "name": "竖版帖子", "ratio": "4:5", "canvas": {"width": 1080, "height": 1350}},
                {"id": "story", "name": "Story", "ratio": "9:16", "canvas": {"width": 1080, "height": 1920}},
            ]}]
        elif path == "/api/merchants":
            payload = {"total": 2, "merchants": [{"id": key, "name": key} for key in ("merchant-1", "merchant-2")]}
        elif path in ("/api/assets", "/api/am-assets"):
            payload = {"items": [{"id": name, "original_name": name + ".png", "width": 100, "height": 100,
                                  "mime_type": "image/png", "url": "/fixture/" + name, "created_at": "2026-11-13T00:00:00Z"}
                                 for name in ("first", "second")], "total": 2, "skip": 0, "limit": 36}
        elif path == "/api/task-center":
            payload = {"items": [], "total": 0, "has_more": False, "next_cursor": None}
        elif path == "/api/whole-image/tasks" and request.method == "POST":
            self.posts += 1
            number = self.posts
            await asyncio.sleep(self.delay)
            if self.mode == "fail" or (self.mode == "partial" and number % 2 == 0):
                return await route.fulfill(status=503, json={"detail": "模拟提交失败"})
            inputs = request.post_data_json
            task_id = "wtask_test_" + str(number)
            payload = {"id": task_id, "status": "queued", "stage": "pending", "progress": 0, "input": inputs,
                       "canvas": {"width": 1080, "height": 1350}, "provider": {"name": "core-ai", "model": inputs["image_model"], "status": "queued"},
                       "outputs": [], "current_output_id": None, "current_version_id": None, "current_version": 0,
                       "preview_url": None, "download_url": None, "messages": [], "edit": {"status": "idle"}, "error": None,
                       "created_at": "2026-11-13T00:00:00Z", "updated_at": "2026-11-13T00:00:00Z"}
            self.tasks[task_id] = payload
            if self.mode == 'lost':
                return await route.fulfill(status=503, json={"detail": "模拟响应丢失，任务已创建"})
        elif re.fullmatch(r"/api/whole-image/workspaces/[^/]+/tasks", path):
            workspace = path.split('/')[-2]
            payload = [task for task in self.tasks.values() if task["input"]["generation_workspace_id"] == workspace]
        elif path.startswith("/api/whole-image/tasks/wtask_test_"):
            task_id = path.split('/')[4]
            payload = self.tasks[task_id] if path.endswith(task_id) else []
        else:
            self.unhandled.append(path)
            return await route.fulfill(status=500, json={"detail": "Unhandled fixture: " + path})
        await route.fulfill(json=payload)


async def session(page):
    return await page.evaluate("JSON.parse(sessionStorage.getItem('social-media:poster-session:v1:alice:merchant-1'))")


async def prepare(page):
    await page.goto(BASE + "/?merchant_id=merchant-1")
    await expect(page.get_by_role("textbox", name="设计需求", exact=True)).to_be_visible()
    await page.get_by_role("switch", name="使用品牌风格").uncheck()
    await page.locator('.form-card').get_by_role("button", name="添加参考图", exact=True).click()
    await page.get_by_text("从资产库选择", exact=True).click()
    await page.locator(".picker-card").nth(1).click()
    await page.locator(".picker-card").nth(0).click()
    await page.locator(".picker-footer button").last.click()
    await page.get_by_role("textbox", name="设计需求", exact=True).fill("本周海报 @图片2，保留原照")
    await page.get_by_role("radio", name="GPT Image 2.5 2 张", exact=True).click()
    await page.get_by_label('选择发布平台').select_option('instagram')
    await page.get_by_label('发布用途').select_option('story')
    await page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
    await page.get_by_role('switch', name='在海报中显示门店信息').check()
    await page.get_by_label('电话', exact=False).uncheck()
    await page.get_by_role('button', name='切换到 Seedream 5.5', exact=True).click()
    await page.get_by_role('radio', name='Seedream 5.5 3 张', exact=True).click()
    await page.get_by_role('button', name='切换到 GPT Image 2.5', exact=True).click()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        fixture = Fixture()
        await context.route("**/api/**", fixture.route)
        await context.route("**/fixture/**", fixture.route)
        page = await context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await prepare(page)
            editor = page.get_by_role("textbox", name="设计需求", exact=True)
            expected = await editor.inner_text()
            await page.get_by_role("button", name="资产管理", exact=True).click()
            await page.get_by_role("button", name="海报设计", exact=True).click()
            await expect(editor).to_have_text(expected)
            assert (await session(page))["kind"] == "draft"
            before = await session(page)
            fixture.list_delay = 0.3
            await page.reload()
            await expect(editor).to_have_text(expected)
            await expect(page.get_by_label('选择发布平台')).to_be_visible()
            await expect(page.get_by_role("radio", name="GPT Image 2.5 2 张", exact=True)).to_have_attribute("aria-checked", "true")
            assert (await session(page))["form"] == before["form"]
            fixture.list_delay = 0
            print("PASS: menu navigation and reload preserve the form and bound image references")

            # Same menu is navigation, rather than an implicit new task.
            await page.get_by_role('button', name='海报设计', exact=True).click()
            await expect(editor).to_have_text(expected)
            assert (await session(page))['form'] == before['form']

            # Other operators and merchants sharing this browser never borrow this draft.
            fixture.user = 'bob'
            await page.reload()
            await expect(editor).to_be_empty()
            fixture.user = 'alice'
            await page.reload()
            await expect(editor).to_have_text(expected)
            await page.goto(BASE + '/?merchant_id=merchant-2')
            await expect(editor).to_be_empty()
            await page.goto(BASE + '/?merchant_id=merchant-1')
            await expect(editor).to_have_text(expected)
            other_tab = await context.new_page()
            await other_tab.goto(BASE + '/?merchant_id=merchant-1')
            await expect(other_tab.get_by_role('textbox', name='设计需求', exact=True)).to_be_empty()
            await other_tab.wait_for_load_state('networkidle')
            await other_tab.close()
            print('PASS: operators, merchants and separate tabs are isolated')

            # Requests rejected by the backend retain the complete form.
            await page.get_by_role('button', name='GPT Image 2.5 生成', exact=True).click()
            await expect(page.locator('.model-generation-error')).to_contain_text('模拟提交失败')
            assert (await session(page))['kind'] == 'draft'
            await expect(editor).to_have_text(expected)
            await page.reload()
            await expect(editor).to_have_text(expected)
            print('PASS: failed submissions retain the draft')

            # An accepted task discovered by reconciliation also ends the draft, even
            # after switching to a model that has no candidates in this workspace.
            fixture.mode = 'lost'
            fixture.delay = 0.4
            await page.get_by_role('button', name='GPT Image 2.5 生成', exact=True).click()
            await page.get_by_role('button', name='切换到 Seedream 5.5', exact=True).click()
            await page.wait_for_function("JSON.parse(sessionStorage.getItem('social-media:poster-session:v1:alice:merchant-1'))?.kind === 'task'")
            assert (await session(page))['taskId'] is None
            await page.reload()
            await expect(editor).to_have_text(expected)
            await expect(page.get_by_role('button', name='切换到 Seedream 5.5', exact=True)).to_have_attribute('aria-pressed', 'true')
            await page.get_by_role('button', name='任务中心', exact=True).click()
            await page.get_by_role('button', name='＋ 新建海报设计', exact=True).click()
            await prepare(page)
            print('PASS: reconciled tasks end the draft without borrowing another model selection')

            # A partial success already creates a formal task; navigate while requests finish.
            fixture.mode = 'partial'
            fixture.delay = 0.4
            await page.get_by_role('button', name='GPT Image 2.5 生成', exact=True).click()
            await page.get_by_role('button', name='资产管理', exact=True).click()
            await page.wait_for_function("JSON.parse(sessionStorage.getItem('social-media:poster-session:v1:alice:merchant-1'))?.kind === 'task'")
            await expect(page).to_have_url(re.compile(r'view=assets'))
            assert 'form' not in await session(page)
            await page.get_by_role('button', name='海报设计', exact=True).click()
            await expect(page).to_have_url(re.compile(r'whole=wtask_test_'))
            await expect(editor).to_have_text(expected)
            await page.reload()
            await expect(editor).to_have_text(expected)
            assert (await session(page))['kind'] == 'task'
            print('PASS: partial acceptance ends the draft and resumes the task after navigation/reload')

            # Merchant switching drops the previous merchant's explicit task link.
            await page.get_by_role('button', name='merchant-1', exact=True).click()
            await page.get_by_role('option', name='merchant-2', exact=False).click()
            await expect(editor).to_be_empty()
            assert 'whole=' not in page.url
            await page.get_by_role('button', name='merchant-2', exact=True).click()
            await page.get_by_role('option', name='merchant-1', exact=False).click()
            await expect(editor).to_have_text(expected)

            # Explicit new clears this tab session and never repopulates it with defaults.
            await page.get_by_role('button', name='任务中心', exact=True).click()
            await page.get_by_role('button', name='＋ 新建海报设计', exact=True).click()
            await expect(editor).to_be_empty()
            assert await session(page) is None
            await page.reload()
            await expect(editor).to_be_empty()
            assert await session(page) is None
            print('PASS: explicit new clears the old session without creating a blank draft')

            # A late response from the discarded editor cannot reopen the old task.
            await prepare(page)
            fixture.mode = 'success'
            fixture.delay = 0.6
            await page.get_by_role('button', name='GPT Image 2.5 生成', exact=True).click()
            await page.get_by_role('button', name='任务中心', exact=True).click()
            await page.get_by_role('button', name='＋ 新建海报设计', exact=True).click()
            await page.wait_for_timeout(900)
            await expect(editor).to_be_empty()
            assert await session(page) is None
            assert 'whole=' not in page.url
            print('PASS: delayed responses cannot resurrect a discarded draft or change navigation')

            # Closing a normal tab and opening a fresh one drops its editing state.
            await prepare(page)
            await page.wait_for_load_state('networkidle')
            await page.close()
            page = await context.new_page()
            await page.goto(BASE + '/?merchant_id=merchant-1')
            await expect(page.get_by_role('textbox', name='设计需求', exact=True)).to_be_empty()
            assert await session(page) is None
            await page.wait_for_load_state('networkidle')
            print('PASS: a fresh tab after closing has no unfinished draft')

            blocked = await context.new_page()
            await blocked.add_init_script("Object.defineProperty(window, 'sessionStorage', { get() { throw new Error('blocked') } })")
            await blocked.goto(BASE + '/?merchant_id=merchant-1')
            await blocked.get_by_role('textbox', name='设计需求', exact=True).fill('测试暂存不可用')
            await expect(blocked.get_by_role('alert')).to_contain_text('无法暂存编辑内容')
            await blocked.wait_for_load_state('networkidle')
            await blocked.close()
            print('PASS: blocked session storage leaves the editor usable and shows an explicit warning')
            assert not errors, errors
            assert not fixture.unhandled, fixture.unhandled
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
