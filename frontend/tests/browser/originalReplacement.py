"""Exercise the real studio with isolated API fixtures; never calls a remote model."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def run(vite_url, output_dir):
    now = '2026-11-17T00:00:00Z'
    assets = [dict(id=name, original_name=name + '.png', url='/fixture/' + name + '.svg',
                   width=320, height=400, mime_type='image/png', created_at=now)
              for name in ['photo1', 'photo2', 'photo3', 'photo4', 'photo5', 'logo1', 'logo2']]
    logos = [dict(asset_id=a['id'], **{k: a[k] for k in ['original_name', 'url', 'width', 'height']})
             for a in assets if a['id'].startswith('logo')]
    output = dict(id='o1', channel='gateway', role='gateway_result', label='海报', status='completed',
                  asset_id='poster', selectable=True, width=320, height=400,
                  preview_url='/fixture/poster.svg', download_url='/fixture/poster.svg', model_input=None)
    task = dict(id='task1', status='succeeded', stage='done', progress=100,
                input=dict(reference_asset_ids=[a['id'] for a in assets[:4]], logo_asset_id='logo1',
                           image_model='gpt-image-2.5-sunburst', brand_mode='none', ratio='4:5'),
                canvas=dict(width=320, height=400), provider='gateway', outputs=[output],
                current_output_id='o1', current_version_id='v1', selected_version_id='v1', current_version=1,
                requires_output_selection=False, preview_url=output['preview_url'], download_url=output['download_url'],
                messages=[], edit=dict(status='idle'), error=None, created_at=now, updated_at=now,
                model_input=dict(endpoint='/images/generations', model='gpt-image-2.5-sunburst', prompt='海报', parameters={},
                                 images=[dict(asset_id=a['id'], name=a['original_name'], role='reference',
                                              url=a['url'], preview_url=a['url']) for a in assets]))
    version = dict(id='v1', version=1, parent_version_id=None, operation='generate', instruction='',
                   width=320, height=400, output_asset_id='poster', preview_url=output['preview_url'],
                   download_url=output['download_url'], outputs=[output], created_at=now)
    checks, submissions, records, errors, unexpected = [], [], [], [], []
    html_file = dict(id='editor', kind='html', file_name='editor.html', content_type='text/html', size=120,
                     title='原照编辑器', preview_url='/fixture/editor.html', download_url='/fixture/editor-download.html')
    bundle_file = dict(id='bundle', kind='bundle', file_name='result.zip', content_type='application/zip', size=20,
                       title='完整结果包', preview_url=None, download_url='/fixture/result.zip')
    restores = []

    def check(name, ok=True):
        assert ok, name
        checks.append(name)

    def set_photos(ids):
        task['input']['reference_asset_ids'] = ids
        task['model_input']['images'] = [dict(asset_id=a['id'], name=a['original_name'], role='reference',
                                             url=a['url'], preview_url=a['url']) for a in assets if a['id'] in ids]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        try:
            page = browser.new_page(viewport=dict(width=1600, height=1000))
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route_request(route):
                request = route.request
                path = urlsplit(request.url).path
                if path.startswith('/fixture/') and path.endswith('.svg'):
                    return route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="320" height="400"><rect width="320" height="400" fill="#e4dcff"/></svg>')
                if path == '/fixture/editor.html':
                    return route.fulfill(content_type='text/html', body='<button onclick="this.textContent=\'Edited\'">Drag photo</button>')
                if path == '/fixture/data':
                    data = dict(assets=assets, logos=logos)
                elif path.endswith('/original-replacements'):
                    if request.method == 'POST':
                        payload = request.post_data_json
                        submissions.append(payload)
                        items = [dict(target=item['target'], asset_id=item['asset_id'], name=item['asset_id'],
                                      preview_url='/fixture/' + item['asset_id'] + '.svg', width=320, height=400)
                                 for item in payload['replacements']]
                        data = dict(id='r' + str(len(submissions)), status='succeeded', stage='done', progress=100,
                                    source_version_id='v1', source_output_id='o1', source_version=1,
                                    split_all_elements=payload.get('split_all_elements', False),
                                    artifacts=[html_file, bundle_file], run_id='existing-run',
                                    source=dict(asset_id='poster', preview_url='/fixture/poster.svg', name='海报', width=320, height=400),
                                    replacements=items, output=dict(asset_id='poster', width=320, height=400, preview_url='/fixture/poster.svg', download_url='/fixture/poster.svg'),
                                    error=None, agent_output=None, created_at=now, updated_at=now)
                        records.insert(0, data)
                    else:
                        data = records
                elif path.endswith('/artifacts/restore'):
                    restores.append(path)
                    records[0]['artifacts'] = [html_file, bundle_file]
                    data = records[0]
                elif path.endswith('/finalization'):
                    data = dict(finalization=None)
                elif path.endswith('/versions'):
                    data = [version]
                elif '/workspaces/' in path or '/batches/' in path:
                    data = [task]
                elif path.endswith('/tasks/task1'):
                    data = task
                else:
                    unexpected.append(path)
                    return route.fulfill(status=404, json=dict(detail='Unexpected fixture request'))
                route.fulfill(json=data)

            page.context.route('**/api/**', route_request)
            page.context.route('**/fixture/**', route_request)
            page.goto(vite_url + '/tests/browser/originalReplacement.html?merchant_id=m1&whole=task1&tab=original_replace')
            panel = page.locator('.original-replacement-panel')
            try:
                expect(panel.locator('.replacement-target')).to_have_count(4)
            except AssertionError:
                print(json.dumps(dict(errors=errors, unexpected=unexpected, text=page.locator('body').inner_text()), ensure_ascii=False))
                raise
            expect(panel.locator('.original-replacement-logo')).to_have_count(1)
            split_all = panel.get_by_role('checkbox', name='全部拆分元素（含艺术字）', exact=True)
            top_gap = split_all.evaluate('(input) => input.closest("label").getBoundingClientRect().top - input.closest(".studio-inspector").getBoundingClientRect().top')
            check('whole-element option starts at the top of the inspector', 0 <= top_gap <= 24)
            check('whole-element option precedes all image cards', split_all.evaluate('(input) => input.getBoundingClientRect().bottom < input.closest(".original-replacement-panel").querySelector(".replacement-row").getBoundingClientRect().top'))
            check('photo cards have no filenames or field labels', panel.locator('.original-replacement-row:not(.original-replacement-logo) .original-replacement-name, .original-replacement-row label').count() == 0)
            expect(panel.get_by_role('button', name='移除原照：', exact=False)).to_have_count(4)
            check('every draft photo has a remove control')
            check('photo inputs sit beside their thumbnails with compact padding', panel.locator('.original-replacement-row:not(.original-replacement-logo)').evaluate_all('''rows => rows.every(row => {
              const card = row.getBoundingClientRect(), image = row.querySelector('img').getBoundingClientRect(), input = row.querySelector('input').getBoundingClientRect();
              return input.left >= image.right && Math.abs((input.top + input.bottom - image.top - image.bottom) / 2) <= 2 && card.height <= 90 && card.bottom - Math.max(input.bottom, image.bottom) <= 12;
            })'''))
            check('selected merchant logo appears without a target input', panel.locator('.original-replacement-logo input').count() == 0)
            check('redundant heading and add controls are hidden', panel.locator('.original-replacement-heading, .original-replacement-add, input[type=file]').count() == 0)
            check('duration hint is hidden', '通常约 30 分钟' not in panel.inner_text())
            expect(page.get_by_role('button', name='制作可编辑海报', exact=True)).to_have_count(0)
            expect(split_all).not_to_be_checked()
            check('whole-element split checkbox defaults to off')
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_enabled()
            check('four automatic photo targets and the logo can submit')
            targets = panel.locator('.replacement-target')
            targets.nth(0).fill('右下角主菜')
            page.get_by_role('button', name='logo2', exact=True).click()
            expect(panel.locator('.original-replacement-logo img')).to_have_attribute('src', '/fixture/logo2.svg')
            expect(targets.nth(0)).to_have_value('右下角主菜')
            check('changing the logo preserves photo targets and updates its preview')
            expect(targets.nth(3)).to_be_enabled()
            check('optional position inputs remain editable at the limit')
            panel.get_by_role('button', name='移除原照：photo4', exact=False).click()
            expect(targets).to_have_count(3)
            expect(targets.nth(0)).to_have_value('右下角主菜')
            expect(page.locator('.form-card .prompt-field img[src="/fixture/photo4.svg"]')).to_have_count(1)
            check('removing a reference preserves other targets and the design reference')
            split_all.check()
            panel.get_by_role('button', name='开始原照替换').click()
            expect(panel.locator('.original-replacement-history')).to_be_visible()
            check('request excludes the removed reference and retains explicit and automatic targets', submissions[-1]['replacements'] == [
                dict(target='Logo', asset_id='logo2'), dict(target='右下角主菜', asset_id='photo1'),
                *[dict(target='', asset_id='photo' + str(i)) for i in range(2, 4)]])
            check('removing a replacement leaves the task references unchanged', task['input']['reference_asset_ids'] == ['photo1', 'photo2', 'photo3', 'photo4'])
            check('checked whole-element option is submitted', submissions[-1]['split_all_elements'] is True)
            expect(panel.locator('iframe, .replacement-comparison')).to_have_count(0)
            expect(panel.get_by_role('button', name='应用为新版本', exact=True)).to_have_count(0)
            expect(panel.get_by_role('link', name='下载 HTML', exact=True)).to_have_count(0)
            expect(panel.get_by_role('link', name='下载结果包', exact=True)).to_have_count(0)
            check('replacement history contains no embedded editor, comparisons or extra result actions')
            open_html = panel.get_by_role('link', name='打开 HTML', exact=True)
            expect(open_html).to_have_attribute('href', html_file['preview_url'])
            expect(open_html).to_have_attribute('target', '_blank')
            with page.expect_popup() as opened:
                open_html.click()
            editor_tab = opened.value
            expect(editor_tab.get_by_role('button', name='Drag photo')).to_be_visible()
            editor_tab.get_by_role('button', name='Drag photo').click()
            expect(editor_tab.get_by_role('button', name='Edited')).to_be_visible()
            check('open HTML launches an interactive editor in a new tab without opener access', editor_tab.evaluate('window.opener === null'))
            editor_tab.close()
            records[0]['artifacts'] = []
            page.reload()
            expect(open_html).to_have_count(0)
            panel.get_by_role('button', name='加载 HTML 编辑器', exact=True).click()
            expect(open_html).to_be_visible()
            check('historical files restore without submitting a replacement', len(submissions) == 1 and len(restores) == 1)
            page.reload()
            expect(panel.locator('.original-replacement-targets')).to_contain_text('Logo')
            expect(panel.locator('.original-replacement-targets')).to_contain_text('自动识别')
            check('history restores submitted logo after reload')
            check('history labels empty positions as automatic recognition')
            expect(panel.locator('.original-replacement-mode')).to_contain_text('全部拆分元素（含艺术字）')
            check('history restores the submitted whole-element mode')
            set_photos([])
            page.reload()
            panel.locator('select').select_option('')
            expect(split_all).not_to_be_checked()
            check('a new replacement defaults the whole-element option back to off')
            expect(panel.locator('.original-replacement-logo img')).to_have_attribute('src', '/fixture/logo1.svg')
            panel.get_by_role('button', name='开始原照替换').click()
            expect(panel.locator('select')).to_have_value('r2')
            check('logo-only submission needs no position', submissions[-1]['replacements'] == [dict(target='Logo', asset_id='logo1')])
            check('unchecked whole-element option is submitted as false', submissions[-1]['split_all_elements'] is False)
            set_photos([a['id'] for a in assets[:5]])
            page.reload()
            panel.locator('select').select_option('')
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_disabled()
            expect(panel.get_by_role('alert')).to_contain_text('最多提交 5 项')
            check('all photos count toward the limit even when their positions are empty')
            page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_enabled()
            panel.get_by_role('button', name='开始原照替换').click()
            expect(panel.locator('select')).to_have_value('r3')
            check('five photos submit without any positions or logo', submissions[-1]['replacements'] == [
                dict(target='', asset_id='photo' + str(i)) for i in range(1, 6)])
            set_photos(['photo1'])
            page.reload()
            panel.locator('select').select_option('')
            page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
            panel.locator('.replacement-target').nth(0).fill('背景')
            panel.get_by_role('button', name='开始原照替换').click()
            expect(panel.locator('select')).to_have_value('r4')
            check('disabled logo is excluded from request', submissions[-1]['replacements'] == [dict(target='背景', asset_id='photo1')])
            set_photos([])
            page.reload()
            panel.locator('select').select_option('')
            page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_disabled()
            check('an empty replacement selection cannot submit')
            set_photos([a['id'] for a in assets[:4]])
            records.clear()
            page.reload()
            output_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_dir / 'desktop.png'), full_page=True)
            check('desktop has no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.set_viewport_size(dict(width=390, height=844))
            records.clear()
            page.reload()
            expect(panel.locator('.original-replacement-logo')).to_have_count(1)
            top_gap = split_all.evaluate('(input) => input.closest("label").getBoundingClientRect().top - input.closest(".studio-inspector").getBoundingClientRect().top')
            check('mobile whole-element option starts at the top', 0 <= top_gap <= 24)
            check('mobile photo inputs stay beside thumbnails within compact cards', panel.locator('.original-replacement-row:not(.original-replacement-logo)').evaluate_all('''rows => rows.every(row => {
              const card = row.getBoundingClientRect(), image = row.querySelector('img').getBoundingClientRect(), input = row.querySelector('input').getBoundingClientRect();
              return input.left >= image.right && Math.abs((input.top + input.bottom - image.top - image.bottom) / 2) <= 2 && card.height <= 90;
            })'''))
            page.screenshot(path=str(output_dir / 'mobile.png'), full_page=True)
            check('mobile has no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
            panel.evaluate('(element) => { element.scrollTop = 0 }')
            check('photo-only panel keeps the whole-element option above the cards', split_all.evaluate('(input) => input.getBoundingClientRect().bottom < input.closest(".original-replacement-panel").querySelector(".replacement-row").getBoundingClientRect().top'))
            page.get_by_role('switch', name='在海报中加入 Logo').check()
            panel.get_by_role('button', name='移除原照：photo4', exact=False).click()
            check('removing a photo updates the total replacement count', panel.locator('.replacement-target').count() == 3)
            page.get_by_role('switch', name='在海报中加入 Logo').uncheck()
            for asset_id in ['photo1', 'photo2', 'photo3']:
                panel.get_by_role('button', name='移除原照：' + asset_id, exact=False).click()
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_disabled()
            check('removing all photos with logo off prevents an empty submission')
            page.get_by_role('switch', name='在海报中加入 Logo').check()
            expect(panel.get_by_role('button', name='开始原照替换')).to_be_enabled()
            check('logo remains usable after removing all photos')
            page.get_by_role('button', name='收起辅助工具', exact=True).click()
            expect(panel).to_have_count(0)
            page.get_by_role('button', name='原照替换', exact=True).click()
            expect(panel.locator('.original-replacement-logo')).to_have_count(1)
            check('floating close button remains clickable and the panel reopens')
            check('no browser errors or unexpected API calls', not errors and not unexpected)
        finally:
            browser.close()
    return dict(status='PASS', checks=checks, submissions=submissions, visual_review='not_performed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vite-url', default='http://127.0.0.1:5325')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = run(args.vite_url, args.out)
    (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
