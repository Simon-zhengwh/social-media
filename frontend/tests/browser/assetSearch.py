"""Hold real search API requests to verify feedback through both search stages."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def run(base_url, out_dir):
    checks, errors, unexpected, submissions = [], [], [], []
    pending = {}
    deletions = []
    assets = [dict(id=name, original_name=name + '.png', width=100, height=100,
                   mime_type='image/png', url='data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22/%3E')
              for name in ('first', 'middle', 'last')]

    def check(name, passed=True):
        assert passed, name
        checks.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        page = browser.new_page(viewport=dict(width=1280, height=900))
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route_api(route):
            request = route.request
            url = urlsplit(request.url)
            path = re.sub(r'^/api/merchants/[^/]+', '/api', url.path)
            if path == '/api/context':
                data = dict(merchant=dict(user_id='test', merchant_id='m1', merchant_name='测试商家'), profile=None, features={})
            elif path in ['/api/brands', '/api/platforms']:
                data = []
            elif path == '/api/brand-logos':
                data = dict(logo_asset_ids=[], logo_images=[])
            elif path == '/api/merchants':
                data = dict(total=1, merchants=[dict(id='m1', name='测试商家')])
            elif path == '/api/asset-index':
                data = dict(total=0, failed=0, active_run=None)
            elif path == '/api/health':
                data = dict(status='ok')
            elif path == '/api/asset-searches':
                submissions.append(request.post_data_json)
                pending['search'] = route
                return
            elif path == '/api/am-assets':
                if parse_qs(url.query).get('search_id'):
                    pending['results'] = route
                    return
                data = dict(items=assets, total=len(assets), skip=0, limit=36)
            elif path == '/api/assets/delete':
                deletion = request.post_data_json
                deletions.append(deletion)
                assets[:] = [asset for asset in assets if asset['id'] not in deletion['ids']]
                data = dict(deleted=deletion['ids'], am_deleted=[], local_deleted=deletion['ids'])
            else:
                unexpected.append(path)
                return route.fulfill(status=404, json=dict(detail='Unexpected fixture request'))
            route.fulfill(json=data)

        page.route('**/api/**', route_api)

        def wait_pending(stage):
            for _ in range(100):
                if stage in pending:
                    return pending.pop(stage)
                page.wait_for_timeout(20)
            raise AssertionError('Missing request: ' + stage)

        try:
            for surface, path in [
                ('library', '/?merchant_id=m1&view=assets'),
                ('picker', '/tests/browser/assetSearch.html?merchant_id=m1'),
            ]:
                page.goto(base_url + path)
                form = page.locator('.asset-toolbar' if surface == 'library' else '.picker-search')
                field = form.get_by_role('searchbox')
                button = form.get_by_role('button', name='搜索', exact=True)
                expect(button).to_be_enabled()
                style = field.evaluate('el => { const s = getComputedStyle(el); return { background: s.backgroundColor, border: s.borderTopWidth, color: s.borderTopColor }; }')
                check(surface + ': search field has an opaque background and visible border',
                      style['background'] == 'rgb(255, 255, 255)' and style['border'] == '1px' and style['color'] != 'rgba(0, 0, 0, 0)')
                field.fill('炸鸡')
                form.get_by_role('checkbox', name='语义搜索').check()
                button.click()
                expect(button.locator('.spinner')).to_be_visible()
                expect(button).to_be_disabled()
                expect(button).to_have_attribute('aria-busy', 'true')
                check(surface + ': spinner animates while semantic vocabulary is pending',
                      button.locator('.spinner').evaluate('el => getComputedStyle(el).animationName') == 'spin')
                search_route = wait_pending('search')
                before = len(submissions)
                field.press('Enter')
                page.wait_for_timeout(60)
                check(surface + ': repeated Enter does not submit while searching', len(submissions) == before)
                search_route.fulfill(json=dict(id=surface, query='炸鸡', mode='semantic', groups=[], degraded=False, total=0))
                results_route = wait_pending('results')
                expect(button.locator('.spinner')).to_be_visible()
                check(surface + ': spinner remains until matching assets arrive')
                results_route.fulfill(json=dict(items=[], total=0, skip=0, limit=36))
                expect(button).to_be_enabled()
                expect(button.locator('.spinner')).to_have_count(0)
                check(surface + ': empty results restore the idle search button')
                form.get_by_role('checkbox', name='语义搜索').uncheck()
                field.press('Enter')
                expect(button.locator('.spinner')).to_be_visible()
                wait_pending('search').fulfill(status=503, json=dict(detail='模拟搜索失败'))
                expect(button).to_be_enabled()
                expect(button.locator('.spinner')).to_have_count(0)
                expect(page.get_by_text('模拟搜索失败', exact=True)).to_be_visible()
                check(surface + ': failed literal search restores the button and shows the error')
                page.set_viewport_size(dict(width=390, height=844))
                check(surface + ': mobile search field stays visible without horizontal overflow',
                      field.bounding_box()['width'] >= 100 and page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                page.set_viewport_size(dict(width=1280, height=900))
            page.goto(base_url + '/?merchant_id=m1&view=assets')
            expect(page.locator('.asset-select')).to_have_count(0)
            page.get_by_role('button', name='多选', exact=True).click(timeout=2000)
            expect(page.locator('.asset-select')).to_have_count(3)
            expect(page.get_by_role('button', name='删除选中 0', exact=True)).to_be_disabled()
            page.get_by_role('button', name='选择 first', exact=True).click()
            page.get_by_role('button', name='选择 last', exact=True).click()
            page.get_by_role('button', name='删除选中 2', exact=True).click()
            dialog = page.get_by_role('dialog', name='删除选中的 2 张素材？')
            dialog.get_by_role('button', name='取消', exact=True).click()
            check('asset selection: cancelling confirmation sends no delete request', not deletions)
            page.get_by_role('button', name='退出多选', exact=True).click()
            expect(page.locator('.asset-select')).to_have_count(0)
            page.get_by_role('button', name='多选', exact=True).click()
            expect(page.get_by_role('button', name='删除选中 0', exact=True)).to_be_disabled()
            page.get_by_role('button', name='选择 first', exact=True).click()
            page.get_by_role('button', name='选择 last', exact=True).click()
            page.get_by_role('button', name='删除选中 2', exact=True).click()
            dialog.get_by_role('button', name='确认删除', exact=True).click()
            expect(page.locator('.asset-card')).to_have_count(1)
            expect(page.get_by_role('button', name='选择 middle', exact=True)).to_be_visible()
            check('asset selection: delete sends only the explicitly selected IDs', deletions == [dict(ids=['first', 'last'], force=False)])
            check('no browser errors or unexpected API calls', not errors and not unexpected)
        finally:
            browser.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = dict(status='PASS', checks=checks, submissions=submissions)
    (out_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vite-url', default='http://127.0.0.1:5173')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.vite_url, args.out), ensure_ascii=False, indent=2))
