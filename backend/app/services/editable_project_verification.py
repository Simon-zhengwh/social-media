"""Independent browser verification of imported project HTML and exported pixels."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
import tempfile

from PIL import Image

from app.services.editable_projects import ProjectError


def verify(project, html, final_png):
    try:
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory(prefix='editable-project-verify-') as temp:
            path = Path(temp) / 'editor.html'
            path.write_bytes(html)
            with sync_playwright() as p:
                installed = next((candidate for candidate in [Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
                    Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')] if candidate.is_file()), None)
                browser = p.chromium.launch(headless=True, executable_path=str(installed) if installed else None)
                try:
                    page = browser.new_page()
                    requests = []
                    errors = []
                    page.on('request', lambda request: requests.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route(re.compile(r'^https?://'), lambda route: route.abort())
                    page.goto(path.as_uri())
                    page.wait_for_function('()=>window.posterEditor?.ready===true', timeout=30000)
                    expected_project = dict(project)
                    expected_project.setdefault('selectedId', project['layers'][-1]['id'])
                    if page.evaluate('posterEditor.getProject()') != expected_project:
                        raise ProjectError('HTML 与保存的工程数据不一致')
                    rendered = page.evaluate("()=>posterEditor.renderTo(document.createElement('canvas')).toDataURL().split(',')[1]")
                    with Image.open(io.BytesIO(base64.b64decode(rendered))) as actual, Image.open(io.BytesIO(final_png)) as expected:
                        if actual.convert('RGBA').tobytes() != expected.convert('RGBA').tobytes():
                            raise ProjectError('交付 PNG 与工程实际渲染不一致')
                    target = next((layer for layer in project['layers'] if layer['type'] == 'text'), None)
                    if target:
                        page.evaluate('(id)=>posterEditor.select(id)', target['id'])
                        page.fill('#text-value', target['text']['value'] + ' ')
                        updated = page.evaluate('posterEditor.getProject()')
                        if next(layer for layer in updated['layers'] if layer['id'] == target['id'])['text']['value'] != target['text']['value'] + ' ':
                            raise ProjectError('工程未提供真实文字编辑能力')
                    page.evaluate('(value)=>posterEditor.loadProject(value)', project)
                    if page.evaluate('posterEditor.getProject()') != expected_project or requests or errors:
                        raise ProjectError('工程恢复或离线运行校验失败')
                    from app.routes.whole_image import ARTIFACT_PREVIEW_HEADERS
                    parent_html = '<script>window.messages=[];addEventListener("message",e=>{if(e.source===document.querySelector("iframe")?.contentWindow)messages.push(e.data)})</script><iframe src="/editor.html" sandbox="allow-scripts allow-downloads"></iframe>'
                    host = browser.new_page()
                    host.route(re.compile(r'^https?://'), lambda route: route.abort())
                    # Synthetic responses exercise the deployed CSP without any network service.
                    host.route('https://editor-verification.invalid/**', lambda route:
                        route.fulfill(status=200, content_type='text/html', body=html, headers=ARTIFACT_PREVIEW_HEADERS)
                        if route.request.url.endswith('/editor.html') else
                        route.fulfill(status=200, content_type='text/html', body=parent_html))
                    host.goto('https://editor-verification.invalid/parent.html')
                    child = next(frame for frame in host.frames if frame.parent_frame)
                    child.wait_for_function('()=>window.posterEditor?.ready===true', timeout=30000)
                    host.evaluate('(project)=>document.querySelector("iframe").contentWindow.postMessage({type:"image-editor-v2:init",sessionId:"system-check",projectId:"system-project",revision:1,project},"*")', project)
                    child.wait_for_function('()=>!document.getElementById("save-parent").disabled', timeout=30000)
                    if target:
                        child.evaluate('(id)=>posterEditor.select(id)', target['id'])
                        child.fill('#text-value', target['text']['value'] + ' ')
                    child.click('#save-parent')
                    host.wait_for_function('()=>messages.some(v=>v.type==="image-editor-v2:save")', timeout=10000)
                    saved = host.evaluate('messages.find(v=>v.type==="image-editor-v2:save")')
                    if (saved.get('sessionId'), saved.get('projectId'), saved.get('revision')) != ('system-check', 'system-project', 1):
                        raise ProjectError('编辑器保存协议未绑定正确会话')
                    if target and next(layer for layer in saved['project']['layers'] if layer['id'] == target['id'])['text']['value'] != target['text']['value'] + ' ':
                        raise ProjectError('沙箱编辑后保存的文字不一致')
                    host.close()
                finally:
                    browser.close()
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError('独立浏览器验收未完成：' + str(exc), 503) from exc
    return {'status': 'PASS', 'scope': 'independent_browser_project_and_export_binding', 'text_edit_and_restore': True,
            'opaque_origin_parent_save_handshake': True, 'production_preview_csp': True}
