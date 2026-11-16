"""Exercise the real React/iframe save bridge against temporary API storage."""
from pathlib import Path
import argparse
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'backend'))

import mongomock
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.config import Settings
from app.middleware import merchant_context_middleware
from app.routes import editable_projects as routes
from app.services import editable_projects as service, maintenance
from app.services.object_store import LocalObjectStore


def run(editor_dir, vite_url):
    project = json.loads((editor_dir/'project.json').read_text(encoding='utf-8'))
    db = mongomock.MongoClient().db
    db.whole_tasks.insert_one({'_id':'task1','merchant_id':'m1','input':{}})
    db.whole_versions.insert_one({'_id':'v1','task_id':'task1','merchant_id':'m1','outputs':[{'id':'o1','asset_id':'source'}]})
    doc = service.create(db, task_id='task1', merchant_id='m1', source_version_id='v1', source_output_id='o1', request_id='fixture')
    checks = []
    def check(name, ok):
        checks.append({'name':name,'pass':bool(ok)})
        if not ok: raise AssertionError(name)
    with tempfile.TemporaryDirectory(prefix='editable-browser-api-') as temp:
        settings = Settings(storage_dir=temp, am_bypass_local=True, social_media_origin=vite_url)
        store = LocalObjectStore(Path(temp)/'objects')
        service.install(db, settings, store, doc, project, editor_html=(editor_dir/'editor.html').read_bytes(), report={'status':'PASS','scope':'temporary_functional_fixture_only'})
        maintenance.initialize(db)
        web = FastAPI(); web.state.raw_db=db; web.state.db=db; web.state.settings=settings; web.state.local_store=store; web.state.store=store
        web.middleware('http')(merchant_context_middleware)
        router=APIRouter(prefix='/api/merchants/{merchant_id}');router.include_router(routes.router);web.include_router(router)
        client=TestClient(web)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            try:
                context=browser.new_context()
                errors=[]
                def route_api(route):
                    request=route.request
                    path=request.url.split('/api/',1)[1]
                    response=client.request(request.method,'/api/'+path,headers={k:v for k,v in request.headers.items() if k not in ('host','content-length')},content=request.post_data_buffer)
                    route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')},body=response.content)
                context.route('**/api/**',route_api)
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'{vite_url}/tests/browser/editableProject.html?merchant_id=m1&project_id={doc["_id"]}')
                page.wait_for_selector('iframe')
                frame=next(f for f in page.frames if f.parent_frame)
                frame.wait_for_function('()=>window.posterEditor?.ready===true && !document.getElementById("save-parent").disabled')
                check('sandbox keeps scripts and downloads without same-origin',page.locator('iframe').get_attribute('sandbox')=='allow-scripts allow-downloads')
                check('API CSP loads embedded fonts',frame.evaluate('posterEditor.getProject().fonts.every(f=>document.fonts.check(\'20px "ImageEditor_\'+f.sha256+\'"\'))'))
                target=next(l for l in project['layers'] if l['type']=='text')
                frame.evaluate('(id)=>posterEditor.select(id)',target['id']);frame.fill('#text-value','周末火锅 $69.99')
                frame.click('#save-parent');page.wait_for_function('document.body.textContent.includes("已保存到当前任务")')
                saved=service.get(db,store,doc['_id'],'m1')
                check('real parent API save retains user text',next(l for l in saved['project']['layers'] if l['id']==target['id'])['text']['value']=='周末火锅 $69.99')
                first_revision=saved['revision']
                frame.goto(frame.url);frame.wait_for_function('()=>window.posterEditor?.ready===true && !document.getElementById("save-parent").disabled')
                check('iframe reload restores latest saved state',next(l for l in frame.evaluate('posterEditor.getProject()')['layers'] if l['id']==target['id'])['text']['value']=='周末火锅 $69.99')
                frame.click('#save-parent');page.wait_for_timeout(500)
                check('iframe reload saves current revision without rollback',service.get(db,store,doc['_id'],'m1')['revision']==first_revision+1)
                page.get_by_role('button',name='关闭',exact=True).click();page.get_by_role('button',name='重新打开工程',exact=True).click()
                page.wait_for_selector('iframe');frame=next(f for f in page.frames if f.parent_frame)
                frame.wait_for_function('()=>window.posterEditor?.ready===true && !document.getElementById("save-parent").disabled')
                check('closing and reopening restores saved project',next(l for l in frame.evaluate('posterEditor.getProject()')['layers'] if l['id']==target['id'])['text']['value']=='周末火锅 $69.99')
                revision=service.get(db,store,doc['_id'],'m1')['revision']
                page.evaluate("window.dispatchEvent(new MessageEvent('message',{source:window,data:{type:'image-editor-v2:save',projectId:'foreign',sessionId:'foreign',revision:1,project:{}}}))")
                page.wait_for_timeout(200)
                check('unrelated window message cannot save',service.get(db,store,doc['_id'],'m1')['revision']==revision)
                check('no React or iframe script errors',not errors)
            finally:
                browser.close()
    return {'status':'PASS','scope':'real_react_iframe_and_temporary_api_functional_only','checks':checks,'count':len(checks),'visual_review':'not_performed'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--editor-dir',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--vite-url',default='http://127.0.0.1:5324');args=parser.parse_args()
    try: report=run(args.editor_dir,args.vite_url)
    except Exception as exc: report={'status':'FAIL','error':str(exc)}
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit(0 if report['status']=='PASS' else 1)
