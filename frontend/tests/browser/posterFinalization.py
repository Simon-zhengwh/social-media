"""Exercise the full workbench and asset library against temporary, real API routes."""
import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend'))

from PIL import Image
from playwright.sync_api import sync_playwright
from app.main import merchant_router
from tests.test_poster_finalizations import finalization_app, seed_task, seed_version
from app.config import Settings
from app.services.object_store import LocalObjectStore


def run(vite_url, out_dir):
    checks = []
    def check(name, passed):
        checks.append({'name': name, 'pass': bool(passed)})
        if not passed:
            raise AssertionError(name)
    with tempfile.TemporaryDirectory(prefix='poster-finalization-') as temp:
        settings = Settings(storage_dir=temp, core_ai_api_key='test', am_bypass_local=True,
                            social_media_origin='http://127.0.0.1:5173')
        store = LocalObjectStore(Path(temp) / 'objects')
        client, db = finalization_app.__wrapped__(settings, store)
        client.app.include_router(merchant_router)
        db.whole_tasks.update_one({'_id': 't1'}, {'$set': {'input.reference_asset_ids': ['m1-a', 'm1-b', 'm1-logo']}})
        db.whole_versions.update_one({'_id': 't1-v1'}, {'$push': {'model_input.images': {'asset_id': 'm1-b', 'role': 'reference'}}})
        for doc in db.assets.find({}):
            data = io.BytesIO()
            Image.new('RGB', (320, 240), (90, 140, 80) if doc['_id'].endswith('a') else (160, 90, 80)).save(data, 'PNG')
            key = doc['_id'] + '.png'
            store.put_bytes(key, data.getvalue(), content_type='image/png')
            db.assets.update_one({'_id': doc['_id']}, {'$set': {'path': key, 'prepared_path': key}})
        seed_task(db, 't2', 'workspace', 'm1', ['m1-b'])
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                failures, errors = [], []
                def route_api(route):
                    request = route.request
                    path = '/api/' + request.url.split('/api/', 1)[1]
                    if path == '/api/health':
                        route.fulfill(json={'status': 'ok'})
                        return
                    if path == '/api/platforms':
                        route.fulfill(json=[])
                        return
                    headers = {k: v for k, v in request.headers.items() if k not in ('host', 'content-length')}
                    headers['origin'] = 'http://127.0.0.1:5173'
                    response = client.request(request.method, path, headers=headers, content=request.post_data_buffer)
                    if response.status_code >= 400:
                        failures.append({'path': path, 'status': response.status_code})
                    route.fulfill(status=response.status_code,
                        headers={k: v for k, v in response.headers.items() if k not in ('content-length', 'content-encoding')},
                        body=response.content)
                context.route('**/api/**', route_api)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(vite_url + '/?merchant_id=m1&view=whole&whole=t1&whole_workspace=workspace')
                page.locator('.studio-preview').wait_for()
                finalize = page.get_by_role('button', name='定稿', exact=True)
                finalize.wait_for(timeout=8000)
                download = page.get_by_role('link', name='下载 PNG', exact=True)
                button_style = '''element => {
                    const style = getComputedStyle(element);
                    return [style.border, style.borderRadius, style.backgroundColor, style.color,
                        style.font, style.padding, element.getBoundingClientRect().height];
                }'''
                check('download and finalize use the same outlined button style',
                      download.evaluate(button_style) == finalize.evaluate(button_style))
                finalize.click()
                check('finalize does not open a photo selection page', page.get_by_role('dialog').count() == 0)
                page.get_by_role('button', name='撤销定稿', exact=True).wait_for()
                check('finalization has exactly one toggle button', page.locator('.poster-finalization-actions button').count() == 1)
                check('both recorded source photos stored automatically without logo', db.whole_finalizations.find_one({})['asset_ids'] == ['m1-a', 'm1-b'])
                geometry = []
                for width in (1440, 1280, 1024, 390):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.wait_for_timeout(150)
                    measured = page.locator('.chat-composer-actions').evaluate('''element => {
                        const rect = element.getBoundingClientRect();
                        const model = element.querySelector('select').getBoundingClientRect();
                        const buttons = [...element.querySelectorAll('button, a, select')].filter(item => item.checkVisibility()).map(item => {
                            const r=item.getBoundingClientRect(); return {label:item.textContent.trim(),x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width};
                        });
                        const overlap = buttons.some((a,i) => buttons.slice(i+1).some(b => Math.min(a.right,b.right)-Math.max(a.x,b.x)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1));
                        return {width:innerWidth,modelWidth:model.width,withinViewport:rect.left>=0 && rect.right<=innerWidth+1,overlap,buttons};
                    }''')
                    geometry.append(measured)
                    passed = (measured['withinViewport'] and not measured['overlap'] and measured['modelWidth'] >= 60
                              and all(button['x'] >= 0 and button['right'] <= width + 1 for button in measured['buttons']))
                    if not passed:
                        raise AssertionError('finalization layout: ' + json.dumps(measured, ensure_ascii=False))
                    check(f'finalization actions fit at {width}px', passed)
                page.set_viewport_size({'width': 1440, 'height': 1000})
                revision = db.whole_finalizations.find_one({})['revision']
                page.reload()
                page.get_by_role('button', name='撤销定稿', exact=True).wait_for()
                check('refresh restores one enabled revoke toggle', page.locator('.poster-finalization-actions button').count() == 1 and page.get_by_role('button', name='撤销定稿', exact=True).is_enabled())
                check('identical UI save keeps revision and timestamp', db.whole_finalizations.find_one({})['revision'] == revision)
                page.get_by_role('button', name='资产管理', exact=False).click()
                page.get_by_role('button', name='引用 1 次', exact=True).first.click()
                usage = page.get_by_role('dialog', name='引用明细', exact=True)
                usage.get_by_role('link', name='打开设计任务', exact=True).wait_for()
                check('usage detail shows finalized preview and timestamp', usage.locator('img').count() == 1 and usage.locator('time').count() == 1)
                out_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out_dir / 'usage-dialog.png'))
                usage.get_by_role('button', name='关闭', exact=True).click()
                page.goto(vite_url + '/?merchant_id=m1&view=whole&whole=t2&whole_workspace=workspace')
                page.get_by_role('button', name='定稿', exact=True).click()
                replacement = page.get_by_role('dialog', name='替换已有定稿？', exact=True)
                replacement.wait_for()
                check('other candidate warns that prior finalization will be revoked', '撤销原定稿' in replacement.inner_text())
                check('replacement confirmation keeps prior record until approved', db.whole_finalizations.find_one({})['revision'] == revision)
                replacement.get_by_role('button', name='取消', exact=True).click()
                check('cancelling replacement leaves original finalization', db.whole_finalizations.find_one({})['revision'] == revision)
                page.get_by_role('button', name='定稿', exact=True).click()
                page.get_by_role('dialog').get_by_role('button', name='确认替换', exact=True).click()
                page.get_by_role('button', name='撤销定稿', exact=True).wait_for()
                check('replacing candidate keeps one record with new photo', db.whole_finalizations.count_documents({}) == 1 and db.whole_finalizations.find_one({})['asset_ids'] == ['m1-b'])
                check('replaced candidate still has only one action', page.locator('.poster-finalization-actions button').count() == 1)
                seed_version(db, 't2', 't2-v2', 'm1', ['m1-b'], parent='t2-v1')
                db.whole_tasks.update_one({'_id': 't2'}, {'$set': {'current_version_id': 't2-v2', 'selected_version_id': 't2-v2', 'current_version': 2}})
                page.reload()
                page.get_by_role('button', name='定稿', exact=True).click()
                replacement = page.get_by_role('dialog', name='替换已有定稿？', exact=True)
                replacement.wait_for()
                check('another version of the same candidate also requires confirmation', db.whole_finalizations.find_one({})['version_id'] == 't2-v1')
                page.keyboard.press('Escape')
                check('escape dismisses without replacing old version', db.whole_finalizations.find_one({})['version_id'] == 't2-v1')
                page.get_by_role('button', name='定稿', exact=True).click()
                page.get_by_role('dialog').get_by_role('button', name='确认替换', exact=True).click()
                page.get_by_role('button', name='撤销定稿', exact=True).wait_for()
                check('confirmed new version replaces rather than duplicates finalization', db.whole_finalizations.count_documents({}) == 1 and db.whole_finalizations.find_one({})['version_id'] == 't2-v2')
                page.get_by_role('button', name='撤销定稿', exact=True).click()
                page.get_by_role('button', name='定稿', exact=True).wait_for()
                check('revoke is a direct single-button toggle', page.locator('.poster-finalization-actions button').count() == 1 and page.get_by_role('dialog').count() == 0)
                check('revoke removes effective record', db.whole_finalizations.count_documents({}) == 0)
                page.get_by_role('button', name='资产管理', exact=False).click()
                page.get_by_role('button', name='引用 0 次', exact=True).first.wait_for()
                check('all library counts return to zero', page.get_by_role('button', name='引用 1 次', exact=True).count() == 0)
                check('no script errors', not errors)
                check('no failed API requests', not failures)
            finally:
                browser.close()
    return {'status': 'PASS', 'scope': 'full_react_workbench_and_temporary_api', 'checks': checks, 'geometry': geometry,
            'count': len(checks), 'visual_review': 'not_performed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vite-url', default='http://127.0.0.1:5346')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.vite_url, args.out)
    except Exception as exc:
        result = {'status': 'FAIL', 'error': str(exc)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
